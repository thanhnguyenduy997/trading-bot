from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import re

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.execution.base import AdapterError
from app.models.mt5_trade_history import MT5TradeHistory
from app.models.trade_setup import TradeSetup
from app.models.trading_account import TradingAccount
from app.models.user import User
from app.services.execution import default_adapter_factory
from app.services.mt5_session_state import persist_session_failure, persist_session_matched
from app.services.trading_accounts import get_trading_account, list_trading_accounts


SETUP_COMMENT_RE = re.compile(r"setup-(\d+)-", re.IGNORECASE)
MANUAL_BREAKEVEN_PNL_TOLERANCE = 1.0
SYNC_LOOKBACK_DAYS = 90
ALL_TIME_START = datetime(2000, 1, 1, tzinfo=timezone.utc)


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
    ) -> dict[str, object]:
        account = get_trading_account(self.db, account_id, user_id)
        if not account:
            raise LookupError("Trading account not found")

        adapter = self.adapter_factory(account)
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
            return setup, order_index, "system"
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
            return comment_matches[0], None, "system"
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
        return {
            "filters": filters,
            "selected_account": selected_account,
            "range_start": range_start,
            "range_end": range_end,
            "summary": self._build_summary(items),
            "pnl_chart": self._series(items, "close_date", "realized_pnl"),
            "trade_count_chart": self._count_series(items, "close_date"),
            "source_breakdown": self._count_series(items, "trade_source"),
            "table_rows": items,
            "accounts": list_trading_accounts(self.db, actor.id),
            "session_badge": self._session_badge(selected_account),
            "account_warning": self._account_warning(selected_account),
        }

    def resolve_time_range(self, filters: DashboardFilters) -> tuple[datetime, datetime]:
        now = self.now_provider()
        tz = now.tzinfo or timezone.utc
        start_of_today = datetime.combine(now.date(), time.min, tzinfo=tz)
        end_of_today = datetime.combine(now.date(), time.max, tzinfo=tz)

        if filters.range_key == "all_time":
            return ALL_TIME_START.astimezone(tz), end_of_today
        if filters.range_key == "yesterday":
            day = now.date() - timedelta(days=1)
            return datetime.combine(day, time.min, tzinfo=tz), datetime.combine(day, time.max, tzinfo=tz)
        if filters.range_key == "last_7_days":
            return start_of_today - timedelta(days=6), end_of_today
        if filters.range_key == "last_30_days":
            return start_of_today - timedelta(days=29), end_of_today
        if filters.range_key == "this_month":
            first_day = now.date().replace(day=1)
            return datetime.combine(first_day, time.min, tzinfo=tz), end_of_today
        if filters.range_key == "custom":
            if not filters.start_date or not filters.end_date:
                raise ValueError("Custom date range requires both Start Date and End Date.")
            if filters.end_date < filters.start_date:
                raise ValueError("Custom date range end date must be on or after the start date.")
            return (
                datetime.combine(filters.start_date, time.min, tzinfo=tz),
                datetime.combine(filters.end_date, time.max, tzinfo=tz),
            )
        return start_of_today, end_of_today

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
        effective_outcome = setup.setup_outcome if trade.trade_source == "system" and setup else trade.outcome
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

    def _build_summary(self, items: list[dict[str, object]]) -> dict[str, object]:
        total_trades = len(items)
        total_pnl = sum(item["realized_pnl"] for item in items)
        win_trades = sum(1 for item in items if item["realized_pnl"] > MANUAL_BREAKEVEN_PNL_TOLERANCE)
        return {
            "total_realized_pnl": total_pnl,
            "trade_count": total_trades,
            "win_rate": round((win_trades / total_trades * 100), 2) if total_trades else 0.0,
            "manual_trades": sum(1 for item in items if item["trade_source"] == "manual"),
            "system_trades": sum(1 for item in items if item["trade_source"] == "system"),
            "stoploss_count": sum(1 for item in items if item["effective_outcome"] == "stoploss"),
            "breakeven_count": sum(1 for item in items if item["effective_outcome"] == "breakeven"),
            "take_profit_count": sum(1 for item in items if item["effective_outcome"] in {"tp2_hit", "take_profit"}),
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
