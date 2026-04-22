from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import re

from sqlalchemy import or_
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


@dataclass
class DashboardFilters:
    range_key: str = "today"
    start_date: date | None = None
    end_date: date | None = None
    trading_account_id: int | None = None
    user_id: int | None = None
    symbol: str | None = None
    trade_source: str | None = None
    outcome: str | None = None


class MT5TradeHistorySyncService:
    def __init__(self, db: Session, adapter_factory=None) -> None:
        self.db = db
        self.adapter_factory = adapter_factory or default_adapter_factory

    def sync_accounts_for_filters(
        self,
        *,
        actor: User,
        filters: DashboardFilters,
        range_start: datetime,
        range_end: datetime,
    ) -> dict[str, object]:
        accounts = self._resolve_accounts(actor=actor, filters=filters)
        synced_accounts = 0
        synced_trades = 0
        errors: list[str] = []

        for account in accounts:
            try:
                result = self.sync_account_history(
                    account_id=account.id,
                    user_id=account.user_id,
                    range_start=range_start,
                    range_end=range_end,
                )
                synced_accounts += 1
                synced_trades += int(result["synced_count"])
            except (LookupError, AdapterError, ValueError) as exc:
                errors.append(f"{account.account_number}: {str(exc)}")

        return {
            "synced_accounts": synced_accounts,
            "synced_trades": synced_trades,
            "errors": errors,
        }

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
                account=account,
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

    def _resolve_accounts(self, *, actor: User, filters: DashboardFilters) -> list[TradingAccount]:
        if filters.trading_account_id:
            if actor.role == "admin" and filters.user_id and filters.user_id != actor.id:
                account = (
                    self.db.query(TradingAccount)
                    .filter(
                        TradingAccount.id == filters.trading_account_id,
                        TradingAccount.user_id == filters.user_id,
                    )
                    .first()
                )
                return [account] if account else []
            account = get_trading_account(self.db, filters.trading_account_id, actor.id)
            return [account] if account else []

        target_user_id = filters.user_id if actor.role == "admin" and filters.user_id else actor.id
        if actor.role == "admin" and filters.user_id is None:
            return self.db.query(TradingAccount).order_by(TradingAccount.id.asc()).all()
        return list_trading_accounts(self.db, target_user_id)

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
        return {
            "ticket_to_setup": ticket_to_setup,
            "comment_setup_ids": comment_setup_ids,
        }

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
        account: TradingAccount,
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
            account=account,
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
        effective_comment = comments[0] if comments else None
        outcome = self._derive_trade_outcome(linked_setup, linked_order_index, trade_source, realized_pnl)

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
        account: TradingAccount,
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
            setup_id = int(match.group(1))
            setup = comment_setup_ids.get(setup_id)
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

    def build_dashboard(self, *, actor: User, filters: DashboardFilters) -> dict[str, object]:
        range_start, range_end = self.resolve_time_range(filters)
        query = (
            self.db.query(MT5TradeHistory, TradingAccount, TradeSetup)
            .join(TradingAccount, TradingAccount.id == MT5TradeHistory.trading_account_id)
            .outerjoin(TradeSetup, TradeSetup.id == MT5TradeHistory.linked_setup_id)
            .filter(
                MT5TradeHistory.close_time.isnot(None),
                MT5TradeHistory.close_time >= range_start,
                MT5TradeHistory.close_time <= range_end,
            )
        )

        if actor.role != "admin":
            query = query.filter(MT5TradeHistory.user_id == actor.id)
        elif filters.user_id:
            query = query.filter(MT5TradeHistory.user_id == filters.user_id)

        if filters.trading_account_id:
            query = query.filter(MT5TradeHistory.trading_account_id == filters.trading_account_id)
        if filters.symbol:
            query = query.filter(MT5TradeHistory.symbol == filters.symbol.upper())
        if filters.trade_source:
            query = query.filter(MT5TradeHistory.trade_source == filters.trade_source)

        rows = query.order_by(MT5TradeHistory.close_time.desc(), MT5TradeHistory.id.desc()).all()
        items = [self._row_to_item(trade, account, setup) for trade, account, setup in rows]
        if filters.outcome:
            items = [item for item in items if item["effective_outcome"] == filters.outcome]

        available_accounts = self._available_accounts(actor=actor, filters=filters)
        available_users = self._available_users(actor=actor)

        summary = self._build_summary(items)
        return {
            "filters": filters,
            "range_start": range_start,
            "range_end": range_end,
            "summary": summary,
            "pnl_chart": self._series(items, "close_date", "realized_pnl"),
            "trade_count_chart": self._count_series(items, "close_date"),
            "source_breakdown": self._count_series(items, "trade_source"),
            "symbol_breakdown": self._count_series(items, "symbol"),
            "table_rows": items,
            "accounts": available_accounts,
            "users": available_users,
            "symbols": sorted({item["symbol"] for item in items if item["symbol"]}),
        }

    def resolve_time_range(self, filters: DashboardFilters) -> tuple[datetime, datetime]:
        now = self.now_provider()
        start_of_today = datetime.combine(now.date(), time.min, tzinfo=now.tzinfo or timezone.utc)
        end_of_today = datetime.combine(now.date(), time.max, tzinfo=now.tzinfo or timezone.utc)

        if filters.range_key == "yesterday":
            day = now.date() - timedelta(days=1)
            return (
                datetime.combine(day, time.min, tzinfo=now.tzinfo or timezone.utc),
                datetime.combine(day, time.max, tzinfo=now.tzinfo or timezone.utc),
            )
        if filters.range_key == "last_7_days":
            return start_of_today - timedelta(days=6), end_of_today
        if filters.range_key == "last_30_days":
            return start_of_today - timedelta(days=29), end_of_today
        if filters.range_key == "this_month":
            first_day = now.date().replace(day=1)
            return (
                datetime.combine(first_day, time.min, tzinfo=now.tzinfo or timezone.utc),
                end_of_today,
            )
        if filters.range_key == "custom" and filters.start_date and filters.end_date:
            if filters.end_date < filters.start_date:
                raise ValueError("Custom date range end date must be on or after the start date.")
            return (
                datetime.combine(filters.start_date, time.min, tzinfo=now.tzinfo or timezone.utc),
                datetime.combine(filters.end_date, time.max, tzinfo=now.tzinfo or timezone.utc),
            )
        return start_of_today, end_of_today

    def _row_to_item(self, trade: MT5TradeHistory, account: TradingAccount, setup: TradeSetup | None) -> dict[str, object]:
        effective_outcome = self._effective_outcome(trade, setup)
        return {
            "id": trade.id,
            "position_ticket": trade.position_ticket,
            "open_time": trade.open_time,
            "close_time": trade.close_time,
            "close_date": trade.close_time.date().isoformat() if trade.close_time else "unknown",
            "account_id": account.id,
            "account_display": account.account_number,
            "user_id": trade.user_id,
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
            "setup_outcome": setup.setup_outcome if setup else None,
        }

    def _effective_outcome(self, trade: MT5TradeHistory, setup: TradeSetup | None) -> str | None:
        if trade.trade_source == "system" and setup is not None:
            return setup.setup_outcome or trade.outcome
        return trade.outcome

    def _build_summary(self, items: list[dict[str, object]]) -> dict[str, object]:
        total_trades = len(items)
        total_setups = len({item["linked_setup_id"] for item in items if item["linked_setup_id"] is not None})
        total_realized_pnl = sum(item["realized_pnl"] for item in items)
        total_volume = sum(item["volume"] for item in items)
        wins = sum(1 for item in items if item["realized_pnl"] > MANUAL_BREAKEVEN_PNL_TOLERANCE)
        system_trades = sum(1 for item in items if item["trade_source"] == "system")
        manual_trades = sum(1 for item in items if item["trade_source"] == "manual")

        system_setup_outcomes = {
            item["linked_setup_id"]: item["setup_outcome"]
            for item in items
            if item["trade_source"] == "system" and item["linked_setup_id"] is not None and item["setup_outcome"]
        }
        manual_like_items = [item for item in items if item["trade_source"] != "system"]
        stoploss_count = sum(1 for outcome in system_setup_outcomes.values() if outcome == "stoploss") + sum(
            1 for item in manual_like_items if item["effective_outcome"] == "stoploss"
        )
        breakeven_count = sum(1 for outcome in system_setup_outcomes.values() if outcome == "breakeven") + sum(
            1 for item in manual_like_items if item["effective_outcome"] == "breakeven"
        )
        take_profit_count = sum(1 for outcome in system_setup_outcomes.values() if outcome == "tp2_hit") + sum(
            1 for item in manual_like_items if item["effective_outcome"] == "take_profit"
        )

        return {
            "total_trades": total_trades,
            "total_setups": total_setups,
            "total_realized_pnl": total_realized_pnl,
            "win_rate": round((wins / total_trades * 100), 2) if total_trades else 0.0,
            "total_volume": total_volume,
            "system_trades": system_trades,
            "manual_trades": manual_trades,
            "stoploss_count": stoploss_count,
            "breakeven_count": breakeven_count,
            "take_profit_count": take_profit_count,
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

    def _available_accounts(self, *, actor: User, filters: DashboardFilters) -> list[TradingAccount]:
        query = self.db.query(TradingAccount)
        if actor.role != "admin":
            query = query.filter(TradingAccount.user_id == actor.id)
        elif filters.user_id:
            query = query.filter(TradingAccount.user_id == filters.user_id)
        return query.order_by(TradingAccount.account_number.asc()).all()

    def _available_users(self, *, actor: User) -> list[User]:
        if actor.role != "admin":
            return []
        return self.db.query(User).order_by(User.email.asc()).all()
