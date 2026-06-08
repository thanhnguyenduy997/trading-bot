from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from threading import Lock
import logging
import math
import re

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.execution.base import AdapterError
from app.models.mt5_trade_history import MT5TradeHistory
from app.models.trade_setup import TradeSetup
from app.models.trading_account import TradingAccount
from app.models.user import User
from app.services.account_runtime_state import get_recent_account_views
from app.services.discipline_score import DisciplineScoreService
from app.services.trade_setup_outcomes import (
    FINAL_SETUP_OUTCOMES,
    TradeSetupOutcomeService,
    compute_setup_realized_pnl,
    get_setup_1r_value,
    setup_outcome_label,
)
from app.services.execution import default_adapter_factory
from app.services.mt5_session_state import persist_session_failure, persist_session_matched
from app.services.trading_accounts import get_trading_account, list_trading_accounts


SETUP_COMMENT_RE = re.compile(r"setup-(\d+)-", re.IGNORECASE)
MANUAL_BREAKEVEN_PNL_TOLERANCE = 1.0
SYNC_LOOKBACK_DAYS = 90
ALL_TIME_START = datetime(2000, 1, 1, tzinfo=timezone.utc)
DEFAULT_AUTO_SYNC_LOOKBACK = timedelta(days=7)
VALID_DASHBOARD_RANGE_KEYS = {
    "all_time",
    "today",
    "yesterday",
    "last_7_days",
    "last_30_days",
    "this_month",
    "custom",
}
_account_sync_locks: dict[int, Lock] = {}
_account_sync_locks_guard = Lock()
logger = logging.getLogger(__name__)


@dataclass
class DashboardFilters:
    range_key: str = "all_time"
    start_date: date | None = None
    end_date: date | None = None
    trading_account_id: int | None = None


class DashboardAuthorizationError(PermissionError):
    pass


class MT5TradeHistorySyncService:
    def __init__(self, db: Session, adapter_factory=None) -> None:
        self.db = db
        self.adapter_factory = adapter_factory or default_adapter_factory

    def sync_account_history(
        self,
        *,
        account_id: int,
        user_id: int,
        range_start: datetime,
        range_end: datetime,
        allow_skip_if_busy: bool = False,
    ) -> dict[str, object]:
        account = get_trading_account(self.db, account_id, user_id)
        if not account:
            raise LookupError("Trading account not found")

        sync_lock = _get_account_sync_lock(account.id)
        acquired = sync_lock.acquire(blocking=not allow_skip_if_busy)
        if not acquired:
            return {"synced_count": 0, "skipped": "busy"}

        adapter = self.adapter_factory(account)
        try:
            try:
                adapter.connect()
                account_info = adapter.get_account_info()
                persist_session_matched(self.db, account, account_info=account_info)
                deals = adapter.get_trade_history(
                    date_from=range_start - timedelta(days=SYNC_LOOKBACK_DAYS),
                    date_to=range_end + timedelta(days=1),
                )
            except AdapterError as exc:
                persist_session_failure(self.db, account, error=exc)
                raise
            finally:
                close = getattr(adapter, "close", None)
                if callable(close):
                    close()

            setup_index = self._build_setup_index(account)
            grouped = self._group_deals_by_position(deals)
            synced_count = 0

            for position_ticket, trade_deals in grouped.items():
                normalized = self._normalize_trade(
                    position_ticket=position_ticket,
                    deals=trade_deals,
                    setup_index=setup_index,
                )
                if normalized is None or normalized["close_time"] is None:
                    continue
                self._upsert_trade(account=account, normalized=normalized)
                synced_count += 1

            self.db.commit()
            return {"synced_count": synced_count}
        finally:
            sync_lock.release()

    def latest_history_sync_at(self, *, account_id: int, user_id: int) -> datetime | None:
        latest = (
            self.db.query(func.max(MT5TradeHistory.synced_at))
            .filter(
                MT5TradeHistory.user_id == user_id,
                MT5TradeHistory.trading_account_id == account_id,
            )
            .scalar()
        )
        if latest is None:
            return None
        return latest if latest.tzinfo else latest.replace(tzinfo=timezone.utc)

    def select_accounts_for_auto_sync(
        self,
        *,
        max_accounts: int,
        stale_after_seconds: int,
        now: datetime | None = None,
    ) -> list[TradingAccount]:
        effective_now = now or datetime.now(timezone.utc)
        stale_before = effective_now - timedelta(seconds=max(30, stale_after_seconds))
        recent_views = get_recent_account_views(since=effective_now - timedelta(hours=6))
        accounts = list(self.db.query(TradingAccount).all())
        setup_activity = self._setup_activity_map()
        history_sync = self._history_sync_map()
        ranked: list[tuple[tuple[float, ...], TradingAccount]] = []

        for account in accounts:
            is_connected_candidate = account.mt5_session_status == "matched" or account.connection_status == "connected"
            if not is_connected_candidate and account.mt5_session_status != "disconnected":
                continue
            latest_sync = history_sync.get(account.id)
            if latest_sync is not None and latest_sync >= stale_before:
                continue

            ranked.append(
                (
                    (
                        1.0 if account.mt5_session_status == "matched" else 0.0,
                        1.0 if account.connection_status == "connected" else 0.0,
                        self._timestamp(recent_views.get(account.id)),
                        self._timestamp(setup_activity.get(account.id)),
                        self._timestamp(account.last_heartbeat_at),
                        self._timestamp(latest_sync),
                        float(account.id),
                    ),
                    account,
                )
            )

        ranked.sort(key=lambda item: item[0], reverse=True)
        return [account for _score, account in ranked[: max(1, max_accounts)]]

    def auto_sync_active_accounts(
        self,
        *,
        max_accounts: int,
        stale_after_seconds: int,
        now: datetime | None = None,
    ) -> dict[str, object]:
        effective_now = now or datetime.now(timezone.utc)
        selected_accounts = self.select_accounts_for_auto_sync(
            max_accounts=max_accounts,
            stale_after_seconds=stale_after_seconds,
            now=effective_now,
        )
        lookback_start = effective_now - DEFAULT_AUTO_SYNC_LOOKBACK
        synced_accounts = 0
        synced_trades = 0
        skipped_busy = 0

        for account in selected_accounts:
            try:
                result = self.sync_account_history(
                    account_id=account.id,
                    user_id=account.user_id,
                    range_start=lookback_start,
                    range_end=effective_now,
                    allow_skip_if_busy=True,
                )
            except AdapterError:
                continue
            if result.get("skipped") == "busy":
                skipped_busy += 1
                continue
            synced_accounts += 1
            synced_trades += int(result.get("synced_count", 0))

        return {
            "selected_accounts": len(selected_accounts),
            "synced_accounts": synced_accounts,
            "synced_trades": synced_trades,
            "skipped_busy": skipped_busy,
        }

    def _history_sync_map(self) -> dict[int, datetime]:
        rows = (
            self.db.query(
                MT5TradeHistory.trading_account_id,
                func.max(MT5TradeHistory.synced_at),
            )
            .group_by(MT5TradeHistory.trading_account_id)
            .all()
        )
        return {
            int(account_id): self._normalize_datetime(last_synced)
            for account_id, last_synced in rows
            if account_id is not None and last_synced is not None
        }

    def _setup_activity_map(self) -> dict[int, datetime]:
        rows = (
            self.db.query(
                TradeSetup.trading_account_id,
                func.max(func.coalesce(TradeSetup.setup_outcome_recorded_at, TradeSetup.executed_at, TradeSetup.updated_at)),
            )
            .group_by(TradeSetup.trading_account_id)
            .all()
        )
        return {
            int(account_id): self._normalize_datetime(last_seen)
            for account_id, last_seen in rows
            if account_id is not None and last_seen is not None
        }

    def _normalize_datetime(self, value: datetime | None) -> datetime:
        if value is None:
            return datetime.min.replace(tzinfo=timezone.utc)
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    def _timestamp(self, value: datetime | None) -> float:
        if value is None:
            return 0.0
        return self._normalize_datetime(value).timestamp()

    def _build_setup_index(self, account: TradingAccount) -> dict[str, object]:
        setups = (
            self.db.query(TradeSetup)
            .filter(TradeSetup.trading_account_id == account.id)
            .order_by(TradeSetup.id.asc())
            .all()
        )
        ticket_to_setup: dict[int, tuple[TradeSetup, int]] = {}
        comment_setup_ids: dict[int, TradeSetup] = {}
        for setup in setups:
            if setup.order1_ticket:
                ticket_to_setup[int(setup.order1_ticket)] = (setup, 1)
            if setup.order2_ticket:
                ticket_to_setup[int(setup.order2_ticket)] = (setup, 2)
            comment_setup_ids[setup.id] = setup
        return {"ticket_to_setup": ticket_to_setup, "comment_setup_ids": comment_setup_ids}

    def _group_deals_by_position(self, deals: list[dict[str, object]]) -> dict[int, list[dict[str, object]]]:
        grouped: dict[int, list[dict[str, object]]] = defaultdict(list)
        for deal in deals:
            position_ticket = self._coerce_int(
                deal.get("position_id") or deal.get("position") or deal.get("order") or deal.get("ticket")
            )
            if position_ticket is None:
                continue
            grouped[position_ticket].append(deal)
        for position_ticket, entries in grouped.items():
            grouped[position_ticket] = sorted(entries, key=self._deal_sort_key)
        return grouped

    def _normalize_trade(
        self,
        *,
        position_ticket: int,
        deals: list[dict[str, object]],
        setup_index: dict[str, object],
    ) -> dict[str, object] | None:
        if not deals:
            return None

        open_deals = [deal for deal in deals if self._normalize_entry(deal.get("entry")) == "in"]
        close_deals = [deal for deal in deals if self._normalize_entry(deal.get("entry")) in {"out", "out_by"}]
        if not close_deals:
            return None

        first_open = open_deals[0] if open_deals else deals[0]
        last_close = close_deals[-1]
        comments = [str(deal.get("comment")).strip() for deal in deals if deal.get("comment")]
        linked_setup, linked_order_index, trade_source = self._classify_source(
            position_ticket=position_ticket,
            open_deal_ticket=self._coerce_int(first_open.get("ticket")),
            close_deal_ticket=self._coerce_int(last_close.get("ticket")),
            comments=comments,
            setup_index=setup_index,
        )

        realized_pnl = sum(
            self._coerce_float(deal.get("profit"))
            + self._coerce_float(deal.get("swap"))
            + self._coerce_float(deal.get("commission"))
            for deal in close_deals
        )
        outcome = self._derive_trade_outcome(linked_setup, linked_order_index, trade_source, realized_pnl)
        effective_comment = comments[0] if comments else None

        return {
            "linked_setup_id": linked_setup.id if linked_setup else None,
            "position_ticket": position_ticket,
            "open_deal_ticket": self._coerce_int(first_open.get("ticket")),
            "close_deal_ticket": self._coerce_int(last_close.get("ticket")),
            "symbol": str((first_open.get("symbol") or last_close.get("symbol") or "")).upper(),
            "side": self._infer_side(first_open),
            "trade_source": trade_source,
            "outcome": outcome,
            "volume": self._coerce_float(first_open.get("volume")) or self._coerce_float(last_close.get("volume")),
            "open_price": self._coerce_float(first_open.get("price")),
            "close_price": self._coerce_float(last_close.get("price")),
            "realized_pnl": realized_pnl,
            "open_time": self._coerce_datetime(first_open),
            "close_time": self._coerce_datetime(last_close),
            "comment": effective_comment,
        }

    def _classify_source(
        self,
        *,
        position_ticket: int,
        open_deal_ticket: int | None,
        close_deal_ticket: int | None,
        comments: list[str],
        setup_index: dict[str, object],
    ) -> tuple[TradeSetup | None, int | None, str]:
        ticket_to_setup: dict[int, tuple[TradeSetup, int]] = setup_index["ticket_to_setup"]
        comment_setup_ids: dict[int, TradeSetup] = setup_index["comment_setup_ids"]

        direct_matches: list[tuple[TradeSetup, int]] = []
        for candidate_ticket in {position_ticket, open_deal_ticket, close_deal_ticket}:
            if candidate_ticket is None:
                continue
            matched = ticket_to_setup.get(candidate_ticket)
            if matched and matched not in direct_matches:
                direct_matches.append(matched)

        if len({match[0].id for match in direct_matches}) == 1 and direct_matches:
            setup, order_index = direct_matches[0]
            return setup, order_index, self._linked_trade_source(setup)
        if len({match[0].id for match in direct_matches}) > 1:
            return None, None, "unknown"

        comment_matches: list[TradeSetup] = []
        for comment in comments:
            match = SETUP_COMMENT_RE.search(comment)
            if not match:
                continue
            setup = comment_setup_ids.get(int(match.group(1)))
            if setup and setup not in comment_matches:
                comment_matches.append(setup)

        if len(comment_matches) == 1:
            return comment_matches[0], None, self._linked_trade_source(comment_matches[0])
        if len(comment_matches) > 1:
            return None, None, "unknown"
        return None, None, "manual"

    def _derive_trade_outcome(
        self,
        linked_setup: TradeSetup | None,
        linked_order_index: int | None,
        trade_source: str,
        realized_pnl: float,
    ) -> str | None:
        if trade_source == "system" and linked_setup is not None:
            if linked_order_index == 1 and linked_setup.order1_outcome:
                return linked_setup.order1_outcome
            if linked_order_index == 2 and linked_setup.order2_outcome:
                return linked_setup.order2_outcome
            return linked_setup.setup_outcome
        if realized_pnl > MANUAL_BREAKEVEN_PNL_TOLERANCE:
            return "take_profit"
        if realized_pnl < -MANUAL_BREAKEVEN_PNL_TOLERANCE:
            return "stoploss"
        return "breakeven"

    def _linked_trade_source(self, setup: TradeSetup) -> str:
        return "manual_setup" if setup.setup_source == "manual" else "system"

    def _upsert_trade(self, *, account: TradingAccount, normalized: dict[str, object]) -> None:
        trade = (
            self.db.query(MT5TradeHistory)
            .filter(
                MT5TradeHistory.trading_account_id == account.id,
                MT5TradeHistory.position_ticket == normalized["position_ticket"],
            )
            .first()
        )
        if trade is None:
            trade = MT5TradeHistory(user_id=account.user_id, trading_account_id=account.id, **normalized)
            self.db.add(trade)
            self.db.flush()
            return

        for field, value in normalized.items():
            setattr(trade, field, value)
        trade.user_id = account.user_id
        trade.synced_at = datetime.now(timezone.utc)
        self.db.add(trade)
        self.db.flush()

    def _deal_sort_key(self, deal: dict[str, object]) -> tuple[datetime, int]:
        return (
            self._coerce_datetime(deal) or datetime.min.replace(tzinfo=timezone.utc),
            self._coerce_int(deal.get("ticket")) or 0,
        )

    def _normalize_entry(self, value: object) -> str:
        if value in {0, "in"}:
            return "in"
        if value in {1, "out"}:
            return "out"
        if value in {3, "out_by"}:
            return "out_by"
        return str(value).lower()

    def _infer_side(self, deal: dict[str, object]) -> str:
        deal_type = deal.get("type")
        if deal_type in {0, "buy"}:
            return "buy"
        if deal_type in {1, "sell"}:
            return "sell"
        return "unknown"

    def _coerce_int(self, value: object) -> int | None:
        if value in (None, ""):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _coerce_float(self, value: object) -> float:
        if value in (None, ""):
            return 0.0
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def _coerce_datetime(self, deal: dict[str, object]) -> datetime | None:
        raw = deal.get("time")
        if raw in (None, ""):
            return None
        if isinstance(raw, datetime):
            return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
        try:
            return datetime.fromtimestamp(float(raw), tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            return None


class DashboardService:
    def __init__(self, db: Session, now_provider=None) -> None:
        self.db = db
        self.now_provider = now_provider or (lambda: datetime.now(timezone.utc))

    def resolve_selected_account(self, *, actor: User, requested_account_id: int | None) -> TradingAccount | None:
        accounts = list_trading_accounts(self.db, actor.id)
        owned_accounts = {account.id: account for account in accounts}

        if requested_account_id is not None:
            selected = owned_accounts.get(requested_account_id)
            if not selected:
                raise DashboardAuthorizationError("Trading account not found for the current signed-in user.")
            return selected
        if not accounts:
            return None

        matched_accounts = [account for account in accounts if account.mt5_session_status == "matched"]
        if matched_accounts:
            matched_accounts.sort(
                key=lambda account: (
                    self._normalize_datetime(account.last_heartbeat_at),
                    self._normalize_datetime(account.updated_at),
                    account.id,
                ),
                reverse=True,
            )
            return matched_accounts[0]

        activity = self._account_activity_map(actor.id)
        scored_accounts = sorted(
            accounts,
            key=lambda account: (
                self._normalize_datetime(activity.get(account.id)),
                self._normalize_datetime(account.updated_at),
                self._normalize_datetime(account.created_at),
                account.id,
            ),
            reverse=True,
        )
        return scored_accounts[0]

    def build_dashboard(
        self,
        *,
        actor: User,
        filters: DashboardFilters,
        selected_account: TradingAccount,
    ) -> dict[str, object]:
        range_start, range_end = self.resolve_time_range(filters)
        backfill_result = TradeSetupOutcomeService(self.db).backfill_setup_outcomes_from_stored_data(
            user_id=actor.id,
            trading_account_id=selected_account.id,
        )
        query = (
            self.db.query(MT5TradeHistory, TradeSetup)
            .outerjoin(TradeSetup, TradeSetup.id == MT5TradeHistory.linked_setup_id)
            .filter(
                MT5TradeHistory.user_id == actor.id,
                MT5TradeHistory.trading_account_id == selected_account.id,
                MT5TradeHistory.close_time.isnot(None),
                MT5TradeHistory.close_time >= range_start,
                MT5TradeHistory.close_time <= range_end,
            )
        )
        rows = query.order_by(MT5TradeHistory.close_time.desc(), MT5TradeHistory.id.desc()).all()
        items = [self._row_to_item(trade, setup, selected_account) for trade, setup in rows]
        setup_items = self._setup_items(
            actor=actor,
            selected_account=selected_account,
            range_start=range_start,
            range_end=range_end,
        )
        logger.debug(
            "Dashboard setup metrics account_id=%s range_start=%s range_end=%s setup_backfill_examined=%s setup_backfill_updated=%s setup_rows_matched=%s outcome_counts=%s",
            selected_account.id,
            range_start,
            range_end,
            backfill_result["examined"],
            backfill_result["updated"],
            len(setup_items),
            self._debug_setup_outcome_counts(setup_items),
        )
        latest_history_sync_at = self._latest_history_sync_at(selected_account.id, actor.id)
        now = self.now_provider()
        discipline_service = DisciplineScoreService(self.db)
        discipline_score = discipline_service.compute(
            actor=actor,
            selected_account=selected_account,
            range_start=range_start,
            range_end=range_end,
        )
        return {
            "filters": filters,
            "selected_account": selected_account,
            "range_start": range_start,
            "range_end": range_end,
            "summary": self._build_summary(items),
            "setup_summary": self._build_setup_summary(setup_items),
            "discipline_score": discipline_score,
            "daily_trends": self._daily_trends(
                actor=actor,
                selected_account=selected_account,
                items=items,
                setup_items=setup_items,
                range_start=range_start,
                range_end=range_end,
                discipline_service=discipline_service,
            ),
            "pnl_chart": self._series(items, "close_date", "realized_pnl"),
            "trade_count_chart": self._count_series(items, "close_date"),
            "source_breakdown": self._count_series(items, "trade_source"),
            "setup_outcome_breakdown": self._count_series(setup_items, "setup_outcome_label"),
            "table_rows": items,
            "setup_rows": setup_items,
            "accounts": list_trading_accounts(self.db, actor.id),
            "session_badge": self._session_badge(selected_account),
            "account_warning": self._account_warning(selected_account),
            "latest_history_sync_at": latest_history_sync_at,
            "history_freshness": self._freshness_badge(latest_history_sync_at, now=now, stale_after_seconds=300),
            "generated_at": now,
        }

    def resolve_time_range(self, filters: DashboardFilters) -> tuple[datetime, datetime]:
        now = self.now_provider()
        tz = now.tzinfo or timezone.utc
        start_of_today = datetime.combine(now.date(), time.min, tzinfo=tz)
        end_of_today = datetime.combine(now.date(), time.max, tzinfo=tz)

        range_key = filters.range_key if filters.range_key in VALID_DASHBOARD_RANGE_KEYS else "all_time"
        if range_key == "all_time":
            return ALL_TIME_START.astimezone(tz), end_of_today
        if range_key == "today":
            return start_of_today, end_of_today
        if range_key == "yesterday":
            day = now.date() - timedelta(days=1)
            return datetime.combine(day, time.min, tzinfo=tz), datetime.combine(day, time.max, tzinfo=tz)
        if range_key == "last_7_days":
            return start_of_today - timedelta(days=6), end_of_today
        if range_key == "last_30_days":
            return start_of_today - timedelta(days=29), end_of_today
        if range_key == "this_month":
            first_day = now.date().replace(day=1)
            return datetime.combine(first_day, time.min, tzinfo=tz), end_of_today
        if range_key == "custom":
            if not filters.start_date or not filters.end_date:
                raise ValueError("Custom date range requires both Start Date and End Date.")
            if filters.end_date < filters.start_date:
                raise ValueError("Custom date range end date must be on or after the start date.")
            return (
                datetime.combine(filters.start_date, time.min, tzinfo=tz),
                datetime.combine(filters.end_date, time.max, tzinfo=tz),
            )
        return ALL_TIME_START.astimezone(tz), end_of_today

    def _account_activity_map(self, user_id: int) -> dict[int, datetime]:
        activity: dict[int, datetime] = {}
        trade_rows = (
            self.db.query(
                MT5TradeHistory.trading_account_id,
                func.max(func.coalesce(MT5TradeHistory.synced_at, MT5TradeHistory.close_time)),
            )
            .filter(MT5TradeHistory.user_id == user_id)
            .group_by(MT5TradeHistory.trading_account_id)
            .all()
        )
        for account_id, last_seen in trade_rows:
            if account_id is not None and last_seen is not None:
                activity[int(account_id)] = last_seen

        setup_rows = (
            self.db.query(
                TradeSetup.trading_account_id,
                func.max(func.coalesce(TradeSetup.executed_at, TradeSetup.updated_at, TradeSetup.created_at)),
            )
            .filter(TradeSetup.user_id == user_id)
            .group_by(TradeSetup.trading_account_id)
            .all()
        )
        for account_id, last_seen in setup_rows:
            if account_id is None or last_seen is None:
                continue
            previous = activity.get(int(account_id))
            if previous is None or last_seen > previous:
                activity[int(account_id)] = last_seen
        return activity

    def _row_to_item(
        self,
        trade: MT5TradeHistory,
        setup: TradeSetup | None,
        selected_account: TradingAccount,
    ) -> dict[str, object]:
        effective_outcome = trade.outcome or (setup.setup_outcome if trade.trade_source in {"system", "manual_setup"} and setup else None)
        return {
            "position_ticket": trade.position_ticket,
            "open_time": trade.open_time,
            "close_time": trade.close_time,
            "close_date": trade.close_time.date().isoformat() if trade.close_time else "unknown",
            "account_display": selected_account.account_number,
            "symbol": trade.symbol,
            "side": trade.side,
            "volume": float(trade.volume),
            "open_price": float(trade.open_price) if trade.open_price is not None else None,
            "close_price": float(trade.close_price) if trade.close_price is not None else None,
            "realized_pnl": float(trade.realized_pnl) if trade.realized_pnl is not None else 0.0,
            "trade_source": trade.trade_source,
            "linked_setup_id": trade.linked_setup_id,
            "comment": trade.comment,
            "effective_outcome": effective_outcome,
        }

    def _setup_items(
        self,
        *,
        actor: User,
        selected_account: TradingAccount,
        range_start: datetime,
        range_end: datetime,
    ) -> list[dict[str, object]]:
        candidate_setups = (
            self.db.query(TradeSetup)
            .filter(
                TradeSetup.user_id == actor.id,
                TradeSetup.trading_account_id == selected_account.id,
            )
            .count()
        )
        setups = (
            self.db.query(TradeSetup)
            .filter(
                TradeSetup.user_id == actor.id,
                TradeSetup.trading_account_id == selected_account.id,
                TradeSetup.setup_outcome.in_(FINAL_SETUP_OUTCOMES),
            )
            .all()
        )
        items: list[dict[str, object]] = []
        for setup in setups:
            close_time = self._setup_close_time(setup)
            if close_time is None or close_time < range_start or close_time > range_end:
                continue
            if setup.setup_source == "manual" and setup.manual_confirmed_at is None:
                continue
            items.append(
                {
                    "setup_id": setup.id,
                    "close_time": close_time,
                    "setup_outcome": setup.setup_outcome,
                    "setup_outcome_label": setup_outcome_label(setup.setup_outcome),
                    "setup_realized_pnl": compute_setup_realized_pnl(setup) or 0.0,
                    "setup_1r_value": get_setup_1r_value(setup),
                    "setup_source": setup.setup_source,
                }
            )
        items.sort(key=lambda item: (item["close_time"], item["setup_id"]))
        logger.debug(
            "Dashboard setup query account_id=%s range_start=%s range_end=%s candidate_setups=%s final_outcome_setups=%s in_range_setups=%s",
            selected_account.id,
            range_start,
            range_end,
            candidate_setups,
            len(setups),
            len(items),
        )
        return items

    def _setup_close_time(self, setup: TradeSetup) -> datetime | None:
        if setup.setup_outcome_recorded_at is not None:
            return self._normalize_datetime(setup.setup_outcome_recorded_at)
        if setup.result_recorded_at is not None:
            return self._normalize_datetime(setup.result_recorded_at)

        close_times = [value for value in (setup.order1_closed_at, setup.order2_closed_at) if value is not None]
        if close_times:
            return max(self._normalize_datetime(value) for value in close_times)

        if setup.executed_at is not None:
            return self._normalize_datetime(setup.executed_at)
        return None

    def _build_summary(self, items: list[dict[str, object]]) -> dict[str, object]:
        total_trades = len(items)
        total_pnl = sum(item["realized_pnl"] for item in items)
        return {
            "total_realized_pnl": total_pnl,
            "trade_count": total_trades,
            "manual_trades": sum(1 for item in items if item["trade_source"] in {"manual", "manual_setup"}),
            "system_trades": sum(1 for item in items if item["trade_source"] == "system"),
            "stoploss_count": sum(1 for item in items if item["effective_outcome"] == "stoploss"),
            "breakeven_count": sum(1 for item in items if item["effective_outcome"] == "breakeven"),
            "take_profit_count": sum(1 for item in items if item["effective_outcome"] in {"tp2_hit", "take_profit"}),
        }

    def _build_setup_summary(self, items: list[dict[str, object]]) -> dict[str, object]:
        full_win_count = sum(1 for item in items if item["setup_outcome"] == "full_win")
        managed_win_count = sum(1 for item in items if item["setup_outcome"] == "managed_win")
        full_loss_count = sum(1 for item in items if item["setup_outcome"] == "full_loss")
        single_tp_hit_count = sum(1 for item in items if item["setup_outcome"] == "single_tp_hit")
        single_sl_hit_count = sum(1 for item in items if item["setup_outcome"] == "single_sl_hit")
        scratch_manual_count = sum(1 for item in items if item["setup_outcome"] == "scratch_manual")
        review_required_count = sum(1 for item in items if item["setup_outcome"] == "review_required")
        denominator = full_win_count + managed_win_count + full_loss_count + single_tp_hit_count + single_sl_hit_count
        return {
            "closed_setup_count": len(items),
            "setup_win_rate": round(((full_win_count + managed_win_count + single_tp_hit_count) / denominator) * 100, 2) if denominator else 0.0,
            "full_win_count": full_win_count,
            "managed_win_count": managed_win_count,
            "full_loss_count": full_loss_count,
            "single_tp_hit_count": single_tp_hit_count,
            "single_sl_hit_count": single_sl_hit_count,
            "scratch_manual_count": scratch_manual_count,
            "review_required_count": review_required_count,
            "full_win_rate": round((full_win_count / denominator) * 100, 2) if denominator else 0.0,
            "managed_win_rate": round((managed_win_count / denominator) * 100, 2) if denominator else 0.0,
            "scratch_manual_rate": round((scratch_manual_count / len(items)) * 100, 2) if items else 0.0,
        }

    def _series(self, items: list[dict[str, object]], key: str, metric: str) -> list[dict[str, object]]:
        buckets: dict[str, float] = defaultdict(float)
        for item in items:
            buckets[str(item[key])] += float(item[metric])
        values = [{"label": label, "value": value} for label, value in sorted(buckets.items())]
        max_abs = max((abs(item["value"]) for item in values), default=0.0)
        for item in values:
            item["width_percent"] = 0 if max_abs == 0 else round(abs(item["value"]) / max_abs * 100, 2)
            item["direction"] = "positive" if item["value"] >= 0 else "negative"
        return values

    def _count_series(self, items: list[dict[str, object]], key: str) -> list[dict[str, object]]:
        buckets: dict[str, int] = defaultdict(int)
        for item in items:
            buckets[str(item[key])] += 1
        values = [{"label": label, "value": value} for label, value in sorted(buckets.items())]
        max_value = max((item["value"] for item in values), default=0)
        for item in values:
            item["width_percent"] = 0 if max_value == 0 else round(item["value"] / max_value * 100, 2)
        return values

    def _daily_trends(
        self,
        *,
        actor: User,
        selected_account: TradingAccount,
        items: list[dict[str, object]],
        setup_items: list[dict[str, object]],
        range_start: datetime,
        range_end: datetime,
        discipline_service: DisciplineScoreService,
    ) -> dict[str, object]:
        day_starts = self._daily_bucket_starts(range_start=range_start, range_end=range_end, items=items, setup_items=setup_items)
        pnl_by_day: dict[str, float] = defaultdict(float)
        for item in items:
            close_time = item.get("close_time")
            if not isinstance(close_time, datetime):
                continue
            day_key = self._normalize_datetime(close_time).astimezone(range_start.tzinfo or timezone.utc).date().isoformat()
            pnl_by_day[day_key] += float(item["realized_pnl"])

        setup_counts = self._setup_counts_by_day(setup_items, range_start)
        max_abs_pnl = max((abs(pnl_by_day.get(day.date().isoformat(), 0.0)) for day in day_starts), default=0.0)
        max_abs_pnl = max(max_abs_pnl, 1.0)
        pnl_points: list[dict[str, object]] = []
        discipline_points: list[dict[str, object]] = []
        for day_start in day_starts:
            day_end = datetime.combine(day_start.date(), time.max, tzinfo=day_start.tzinfo)
            key = day_start.date().isoformat()
            pnl_value = pnl_by_day.get(key, 0.0)
            discipline = discipline_service.compute(
                actor=actor,
                selected_account=selected_account,
                range_start=day_start,
                range_end=day_end,
            )
            counts = setup_counts.get(key, {})
            pnl_points.append(
                {
                    "date": key,
                    "label": day_start.strftime("%b %d"),
                    "value": round(pnl_value, 2),
                    "height_percent": round(abs(pnl_value) / max_abs_pnl * 100, 2),
                    "direction": "positive" if pnl_value >= 0 else "negative",
                    "setup_count": counts.get("setup_count", 0),
                    "managed_win_count": counts.get("managed_win_count", 0),
                    "full_win_count": counts.get("full_win_count", 0),
                    "full_loss_count": counts.get("full_loss_count", 0),
                    "scratch_manual_count": counts.get("scratch_manual_count", 0),
                    "review_required_count": counts.get("review_required_count", 0),
                }
            )
            discipline_points.append(
                {
                    "date": key,
                    "label": day_start.strftime("%b %d"),
                    "score": discipline.total_score,
                    "height_percent": discipline.total_score,
                    "setup_count": counts.get("setup_count", 0),
                    "scratch_manual_count": counts.get("scratch_manual_count", 0),
                    "review_required_count": counts.get("review_required_count", 0),
                }
            )
        return {
            "pnl": pnl_points,
            "discipline": discipline_points,
            "pnl_chart": self._daily_pnl_svg_chart(pnl_points),
            "discipline_chart": self._daily_discipline_svg_chart(discipline_points),
            "single_day": len(day_starts) == 1,
            "empty_days_included": self._should_include_empty_daily_buckets(range_start, range_end),
        }

    def _daily_pnl_svg_chart(self, points: list[dict[str, object]]) -> dict[str, object]:
        width = 360
        height = 150
        left = 42
        right = 12
        top = 12
        bottom = 28
        plot_width = width - left - right
        plot_height = height - top - bottom
        values = [self._safe_float(point.get("value"), 0.0) for point in points]
        has_data = any(abs(value) > 1e-9 for value in values)
        y_min = min(values + [0.0])
        y_max = max(values + [0.0])
        if y_min == y_max:
            y_min -= 1.0
            y_max += 1.0
        zero_y = self._scale_value(0.0, y_min, y_max, top, plot_height)
        count = max(len(points), 1)
        slot = plot_width / count
        bar_width = max(4.0, min(22.0, slot * 0.58))
        bars = []
        label_step = self._axis_label_step(len(points))
        for index, point in enumerate(points):
            value = self._safe_float(point.get("value"), 0.0)
            value_y = self._scale_value(value, y_min, y_max, top, plot_height)
            bar_height = max(1.0, abs(zero_y - value_y)) if abs(value) > 1e-9 else 1.0
            x = left + index * slot + (slot - bar_width) / 2
            y = min(value_y, zero_y) if abs(value) > 1e-9 else zero_y - 0.5
            bars.append(
                {
                    "x": round(x, 2),
                    "y": round(y, 2),
                    "width": round(bar_width, 2),
                    "height": round(bar_height, 2),
                    "class": "negative" if value < 0 else "positive",
                    "value": round(value, 2),
                    "date": point.get("date"),
                    "label": point.get("label"),
                    "setup_count": int(point.get("setup_count") or 0),
                    "show_label": index == 0 or index == len(points) - 1 or index % label_step == 0,
                    "label_x": round(x + bar_width / 2, 2),
                }
            )
        return {
            "width": width,
            "height": height,
            "has_data": has_data,
            "bars": bars,
            "zero_y": round(zero_y, 2),
            "y_min": round(y_min, 2),
            "y_max": round(y_max, 2),
            "left": left,
            "right": right,
            "top": top,
            "bottom": bottom,
            "plot_right": width - right,
        }

    def _daily_discipline_svg_chart(self, points: list[dict[str, object]]) -> dict[str, object]:
        width = 360
        height = 150
        left = 42
        right = 12
        top = 12
        bottom = 28
        plot_width = width - left - right
        plot_height = height - top - bottom
        count = len(points)
        label_step = self._axis_label_step(count)
        sanitized_points = []
        for index, point in enumerate(points):
            score = self._clamp(self._safe_float(point.get("score"), 100.0), 0.0, 100.0)
            x = left + (plot_width / max(count - 1, 1) * index if count > 1 else plot_width / 2)
            y = top + (100.0 - score) / 100.0 * plot_height
            sanitized_points.append(
                {
                    "x": round(x, 2),
                    "y": round(y, 2),
                    "score": round(score, 2),
                    "date": point.get("date"),
                    "label": point.get("label"),
                    "setup_count": int(point.get("setup_count") or 0),
                    "scratch_manual_count": int(point.get("scratch_manual_count") or 0),
                    "review_required_count": int(point.get("review_required_count") or 0),
                    "show_label": index == 0 or index == count - 1 or index % label_step == 0,
                }
            )
        path = ""
        if sanitized_points:
            commands = [f"M {sanitized_points[0]['x']} {sanitized_points[0]['y']}"]
            commands.extend(f"L {point['x']} {point['y']}" for point in sanitized_points[1:])
            path = " ".join(commands)
        return {
            "width": width,
            "height": height,
            "has_data": bool(sanitized_points),
            "points": sanitized_points,
            "path": path,
            "left": left,
            "right": right,
            "top": top,
            "bottom": bottom,
            "plot_right": width - right,
        }

    def _scale_value(self, value: float, y_min: float, y_max: float, top: float, plot_height: float) -> float:
        if y_max == y_min:
            return top + plot_height / 2
        return top + (y_max - value) / (y_max - y_min) * plot_height

    def _axis_label_step(self, count: int) -> int:
        if count <= 8:
            return 1
        if count <= 16:
            return 2
        if count <= 32:
            return 4
        return 7

    def _safe_float(self, value: object, default: float) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return default
        return number if math.isfinite(number) else default

    def _clamp(self, value: float, minimum: float, maximum: float) -> float:
        return max(minimum, min(maximum, value))

    def _daily_bucket_starts(
        self,
        *,
        range_start: datetime,
        range_end: datetime,
        items: list[dict[str, object]],
        setup_items: list[dict[str, object]],
    ) -> list[datetime]:
        tz = range_start.tzinfo or timezone.utc
        start_day = range_start.astimezone(tz).date()
        end_day = range_end.astimezone(tz).date()
        if self._should_include_empty_daily_buckets(range_start, range_end):
            return [datetime.combine(start_day + timedelta(days=offset), time.min, tzinfo=tz) for offset in range((end_day - start_day).days + 1)]

        active_days = {
            self._normalize_datetime(item["close_time"]).astimezone(tz).date()
            for item in items
            if isinstance(item.get("close_time"), datetime)
        }
        active_days.update(
            self._normalize_datetime(item["close_time"]).astimezone(tz).date()
            for item in setup_items
            if isinstance(item.get("close_time"), datetime)
        )
        if not active_days:
            active_days.add(end_day)
        return [datetime.combine(day, time.min, tzinfo=tz) for day in sorted(active_days)]

    def _should_include_empty_daily_buckets(self, range_start: datetime, range_end: datetime) -> bool:
        return (range_end.date() - range_start.date()).days <= 62

    def _setup_counts_by_day(self, setup_items: list[dict[str, object]], range_start: datetime) -> dict[str, dict[str, int]]:
        tz = range_start.tzinfo or timezone.utc
        counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for item in setup_items:
            close_time = item.get("close_time")
            if not isinstance(close_time, datetime):
                continue
            key = self._normalize_datetime(close_time).astimezone(tz).date().isoformat()
            outcome = str(item.get("setup_outcome") or "")
            counts[key]["setup_count"] += 1
            if outcome in {"managed_win", "full_win", "full_loss", "single_tp_hit", "single_sl_hit", "scratch_manual", "review_required"}:
                counts[key][f"{outcome}_count"] += 1
        return {day: dict(day_counts) for day, day_counts in counts.items()}

    def _debug_setup_outcome_counts(self, items: list[dict[str, object]]) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for item in items:
            counts[str(item["setup_outcome"])] += 1
        return dict(sorted(counts.items()))

    def _session_badge(self, account: TradingAccount) -> dict[str, str]:
        status = account.mt5_session_status or "unknown"
        labels = {
            "matched": "Matched",
            "mismatch": "Mismatch",
            "disconnected": "Disconnected",
            "unknown": "Unknown",
        }
        tones = {
            "matched": "success",
            "mismatch": "warning",
            "disconnected": "danger",
            "unknown": "muted",
        }
        return {"status": status, "label": labels.get(status, status.title()), "tone": tones.get(status, "muted")}

    def _account_warning(self, account: TradingAccount) -> str | None:
        if not account.terminal_path:
            return "Terminal path is not configured for this trading account."
        if account.mt5_session_status == "mismatch":
            return (
                f"MT5 terminal is currently logged into {account.current_mt5_login or 'another account'}. "
                f"Log into {account.account_number} before live actions."
            )
        if account.mt5_session_status == "disconnected":
            return "MT5 terminal is disconnected. Log into the selected account before syncing or running live actions."
        if account.last_error:
            return account.last_error
        return None

    def _normalize_datetime(self, value: datetime | None) -> datetime:
        if value is None:
            return datetime.min.replace(tzinfo=timezone.utc)
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    def _history_sync_map(self) -> dict[int, datetime]:
        rows = (
            self.db.query(
                MT5TradeHistory.trading_account_id,
                func.max(MT5TradeHistory.synced_at),
            )
            .group_by(MT5TradeHistory.trading_account_id)
            .all()
        )
        return {
            int(account_id): self._normalize_datetime(last_synced)
            for account_id, last_synced in rows
            if account_id is not None and last_synced is not None
        }

    def _setup_activity_map(self) -> dict[int, datetime]:
        rows = (
            self.db.query(
                TradeSetup.trading_account_id,
                func.max(func.coalesce(TradeSetup.setup_outcome_recorded_at, TradeSetup.executed_at, TradeSetup.updated_at)),
            )
            .group_by(TradeSetup.trading_account_id)
            .all()
        )
        return {
            int(account_id): self._normalize_datetime(last_seen)
            for account_id, last_seen in rows
            if account_id is not None and last_seen is not None
        }

    def _latest_history_sync_at(self, account_id: int, user_id: int) -> datetime | None:
        latest = (
            self.db.query(func.max(MT5TradeHistory.synced_at))
            .filter(
                MT5TradeHistory.user_id == user_id,
                MT5TradeHistory.trading_account_id == account_id,
            )
            .scalar()
        )
        return self._normalize_datetime(latest) if latest else None

    def _freshness_badge(
        self,
        value: datetime | None,
        *,
        now: datetime,
        stale_after_seconds: int,
    ) -> dict[str, str]:
        if value is None:
            return {"label": "Never synced", "tone": "warning"}
        age_seconds = max(0, int((self._normalize_datetime(now) - self._normalize_datetime(value)).total_seconds()))
        if age_seconds <= stale_after_seconds:
            return {"label": f"Fresh · {self._relative_age(age_seconds)} ago", "tone": "success"}
        return {"label": f"Stale · {self._relative_age(age_seconds)} ago", "tone": "warning"}

    def _relative_age(self, age_seconds: int) -> str:
        if age_seconds < 60:
            return f"{age_seconds}s"
        if age_seconds < 3600:
            return f"{age_seconds // 60}m"
        return f"{age_seconds // 3600}h"

    def _timestamp(self, value: datetime | None) -> float:
        if value is None:
            return 0.0
        normalized = self._normalize_datetime(value)
        return normalized.timestamp()


def _get_account_sync_lock(account_id: int) -> Lock:
    with _account_sync_locks_guard:
        lock = _account_sync_locks.get(account_id)
        if lock is None:
            lock = Lock()
            _account_sync_locks[account_id] = lock
        return lock
