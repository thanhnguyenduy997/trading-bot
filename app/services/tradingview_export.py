from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import json
import math
from typing import Any

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.mt5_trade_history import MT5TradeHistory
from app.models.trade_event import TradeEvent
from app.models.trade_setup import TradeSetup

VALID_QUICK_RANGES = {"last_2_months", "last_30_days", "last_7_days"}
VALID_REVIEW_TIMEFRAMES = {"M5": 5, "M15": 15, "H1": 60}
DEFAULT_MAX_TRADES = 500
ABSOLUTE_MAX_TRADES = 500


@dataclass
class TradingViewExportFilters:
    symbol: str
    account_id: int | None = None
    start_date: date | None = None
    end_date: date | None = None
    quick_range: str = "last_2_months"
    review_timeframe: str = "M5"
    include_event_times: bool = True
    include_candle_fallback: bool = True
    outcome: str | None = None
    max_trades: int = DEFAULT_MAX_TRADES
    sort: str = "entry_time_asc"
    debug: bool = False
    selected_account_label: str | None = None
    available_accounts_count: int = 0


@dataclass
class TradingViewExportResult:
    symbol: str
    range_start: datetime
    range_end: datetime
    requested_max_trades: int
    exported_trades: int
    total_matched_trades: int
    warnings: list[str]
    pine_code: str
    audit_summary: dict[str, Any]
    skipped_records: list[dict[str, Any]]
    normalized_rows: list[dict[str, Any]]


class TradingViewExportService:
    def __init__(self, db: Session, now_provider=None) -> None:
        self.db = db
        self.now_provider = now_provider or (lambda: datetime.now(timezone.utc))

    def build_export(self, filters: TradingViewExportFilters) -> TradingViewExportResult:
        symbol = filters.symbol.strip().upper()
        if not symbol:
            raise ValueError("symbol is required.")

        start_at, end_at = self._resolve_range(filters)
        max_trades = min(max(1, filters.max_trades), ABSOLUTE_MAX_TRADES)
        warnings: list[str] = []

        query = self.db.query(TradeSetup).filter(TradeSetup.symbol == symbol)
        if filters.account_id is not None:
            query = query.filter(TradeSetup.trading_account_id == filters.account_id)
        if filters.outcome:
            query = query.filter(TradeSetup.setup_outcome == filters.outcome)
        raw_setups = query.order_by(TradeSetup.id.asc()).all()
        setup_ids = [setup.id for setup in raw_setups]
        event_map_all = self._bulk_load_event_maps(setup_ids)
        history_map_all = self._bulk_load_history_maps(setup_ids)

        normalized_rows: list[dict[str, Any]] = []
        skipped_records: list[dict[str, Any]] = []
        for setup in raw_setups:
            row = self._setup_to_export_row(
                setup,
                filters=filters,
                event_map=event_map_all.get(setup.id, {}),
                history_map=history_map_all.get(setup.id, {}),
            )
            skip_reason = self._resolve_skip_reason(row=row, setup=setup, range_start=start_at, range_end=end_at, filters=filters)
            if skip_reason is not None:
                skipped_records.append(
                    self._build_skipped_record(setup=setup, row=row, skip_reason=skip_reason)
                )
                continue
            normalized_rows.append(row)

        raw_order_count = sum((1 if setup.order1_ticket else 0) + (1 if setup.order2_ticket else 0) for setup in raw_setups)
        raw_deal_count = (
            self.db.query(MT5TradeHistory.id)
            .filter(MT5TradeHistory.linked_setup_id.in_(setup_ids))
            .count()
            if setup_ids
            else 0
        )

        duplicate_ids: set[int] = set()
        deduped_rows: list[dict[str, Any]] = []
        for row in normalized_rows:
            setup_id = int(row["setup_id"])
            if setup_id in duplicate_ids:
                skipped_records.append(
                    {
                        "setup_id": row.get("setup_id"),
                        "order_id": row.get("order_id"),
                        "symbol": row.get("symbol"),
                        "account_id": row.get("account_id"),
                        "entry_time": self._iso_dt(row.get("entry_time")),
                        "close_time": self._iso_dt(row.get("close_time")),
                        "status": row.get("status"),
                        "outcome": row.get("outcome"),
                        "skip_reason": "duplicate_collapsed",
                        "original_ids": [setup_id],
                    }
                )
                continue
            duplicate_ids.add(setup_id)
            deduped_rows.append(row)

        sort_key = filters.sort if filters.sort in {"entry_time_asc", "entry_time_desc"} else "entry_time_asc"
        deduped_rows.sort(
            key=lambda item: (item.get("entry_time_ms") is None, item.get("entry_time_ms") or math.inf, item.get("setup_id")),
            reverse=sort_key == "entry_time_desc",
        )

        normalized_count = len(deduped_rows)
        was_limited_by_max_trades = normalized_count > max_trades
        if was_limited_by_max_trades:
            warnings.append(f"Export limited from {normalized_count} to {max_trades} trades")
            if sort_key == "entry_time_desc":
                exported_rows = deduped_rows[:max_trades]
            else:
                exported_rows = deduped_rows[:max_trades]
        else:
            exported_rows = deduped_rows

        self._append_data_quality_warnings(exported_rows, warnings, filters=filters)
        audit_summary = {
            "selected_account_id": filters.account_id,
            "selected_account_label": filters.selected_account_label or "All accounts",
            "available_accounts_count": filters.available_accounts_count,
            "requested_account_id": filters.account_id,
            "requested_symbol": symbol,
            "requested_start_date": start_at.date().isoformat(),
            "requested_end_date": end_at.date().isoformat(),
            "requested_review_timeframe": filters.review_timeframe,
            "raw_setup_count": len(raw_setups),
            "raw_order_count": raw_order_count,
            "raw_deal_count": raw_deal_count,
            "normalized_trade_count": normalized_count,
            "exported_trade_count": len(exported_rows),
            "skipped_trade_count": len(skipped_records),
            "max_trades": max_trades,
            "was_limited_by_max_trades": was_limited_by_max_trades,
            "first_exported_entry_time": self._iso_dt(exported_rows[0].get("entry_time")) if exported_rows else None,
            "last_exported_entry_time": self._iso_dt(exported_rows[-1].get("entry_time")) if exported_rows else None,
            "sort": sort_key,
            "query_logic_summary": (
                "setup candidates by account/symbol/outcome; include if entry_time OR close_time OR setup_time "
                "OR linked deal open/close time OR trade_event time is inside range."
            ),
        }
        exported_account_ids = sorted({int(row.get("account_id")) for row in exported_rows if row.get("account_id") is not None})
        audit_summary["exported_account_ids"] = exported_account_ids
        audit_summary["exported_account_count"] = len(exported_account_ids)
        if filters.account_id is None and len(exported_account_ids) > 1:
            warnings.append("You are exporting multiple accounts. Select a specific account if you only want one account.")

        pine_code = self._generate_pine_code(
            symbol=symbol,
            range_start=start_at,
            range_end=end_at,
            trades=exported_rows,
            audit_summary=audit_summary,
            skipped_records=skipped_records,
        )

        return TradingViewExportResult(
            symbol=symbol,
            range_start=start_at,
            range_end=end_at,
            requested_max_trades=max_trades,
            exported_trades=len(exported_rows),
            total_matched_trades=normalized_count,
            warnings=warnings,
            pine_code=pine_code,
            audit_summary=audit_summary,
            skipped_records=skipped_records,
            normalized_rows=deduped_rows,
        )

    def _resolve_range(self, filters: TradingViewExportFilters) -> tuple[datetime, datetime]:
        now = self.now_provider()
        tz = now.tzinfo or timezone.utc
        today = now.astimezone(tz).date()
        if filters.start_date or filters.end_date:
            if filters.start_date is None or filters.end_date is None:
                raise ValueError("Both start_date and end_date are required when using a custom range.")
            if filters.end_date < filters.start_date:
                raise ValueError("end_date must be on or after start_date.")
            return (
                datetime.combine(filters.start_date, time.min, tzinfo=tz),
                datetime.combine(filters.end_date, time.max, tzinfo=tz),
            )

        key = filters.quick_range if filters.quick_range in VALID_QUICK_RANGES else "last_2_months"
        if key == "last_7_days":
            start_day = today - timedelta(days=6)
        elif key == "last_30_days":
            start_day = today - timedelta(days=29)
        else:
            start_day = today - timedelta(days=60)
        return datetime.combine(start_day, time.min, tzinfo=tz), datetime.combine(today, time.max, tzinfo=tz)

    def _entry_time_expr(self):
        return func.coalesce(TradeSetup.executed_at, TradeSetup.created_at)

    def _close_time_expr(self):
        return func.coalesce(
            TradeSetup.setup_outcome_recorded_at,
            TradeSetup.result_recorded_at,
            TradeSetup.order2_closed_at,
            TradeSetup.order1_closed_at,
            TradeSetup.manual_confirmed_at,
        )

    def _setup_to_export_row(
        self,
        setup: TradeSetup,
        *,
        filters: TradingViewExportFilters,
        event_map: dict[str, list[TradeEvent]],
        history_map: dict[int, MT5TradeHistory],
    ) -> dict[str, Any]:
        entry_time = self._resolve_entry_time(setup)
        close_time = self._resolve_close_time(setup)
        signal_time = setup.created_at
        tp1_hit_time = self._resolve_event_time(event_map, "tp1_hit", setup.order1_closed_at)
        tp2_hit_time = self._resolve_event_time(event_map, "order2_tp_hit", setup.order2_closed_at if setup.order2_outcome == "tp_hit" else None)
        sl_hit_time = self._resolve_event_time(
            event_map,
            "order2_sl_hit",
            self._max_datetime(
                setup.order1_closed_at if setup.order1_outcome == "sl_hit" else None,
                setup.order2_closed_at if setup.order2_outcome == "sl_hit" else None,
            ),
        )
        be_moved_time = self._resolve_event_time(event_map, "be_move_completed", setup.order2_be_moved_at)
        be_hit_time = self._resolve_event_time(
            event_map,
            "order2_closed_at_be",
            setup.order2_closed_at if setup.order2_outcome == "closed_at_be" else None,
        )

        tp1_hit_price = self._resolve_event_price(event_map, "tp1_hit", self._to_float(setup.tp1_price))
        tp2_hit_price = self._resolve_event_price(event_map, "order2_tp_hit", self._to_float(setup.tp2_price))
        sl_hit_price = self._resolve_event_price(event_map, "order2_sl_hit", self._to_float(setup.sl_price))
        be_price = self._resolve_event_price(event_map, "be_move_completed", self._to_float(setup.estimated_entry))
        order1_history = history_map.get(int(setup.order1_ticket)) if setup.order1_ticket else None
        order2_history = history_map.get(int(setup.order2_ticket)) if setup.order2_ticket else None

        realized_pnl = self._resolve_realized_pnl(setup)
        risk_r = None
        if realized_pnl is not None and setup.r_value not in (None, 0):
            risk_r = float(realized_pnl) / float(setup.r_value)
        timeframe = filters.review_timeframe if filters.review_timeframe in VALID_REVIEW_TIMEFRAMES else "M5"
        review_required_reason = None
        if setup.setup_outcome == "review_required":
            review_required_reason = "backend_review_required"
        if (
            tp1_hit_time is not None
            and sl_hit_time is not None
            and self.floor_to_timeframe_bar(tp1_hit_time, timeframe) == self.floor_to_timeframe_bar(sl_hit_time, timeframe)
        ):
            review_required_reason = "same_candle_ambiguity"
        if filters.include_candle_fallback:
            # No candle storage exists in this app yet, so expose this as unresolved instead of guessing.
            if any(value is None for value in (tp1_hit_time, sl_hit_time, close_time)):
                review_required_reason = review_required_reason or "candle_fallback_unavailable"

        actual_entry_price = self._resolve_actual_entry_price(setup, history_map)
        actual_close_price = self._resolve_close_price(setup)
        events_are_candle_derived = False
        data_quality_flags: list[str] = []
        if entry_time is None:
            data_quality_flags.append("missing_entry_time")
        if actual_close_price is None:
            data_quality_flags.append("missing_close_price")
        if close_time is None:
            data_quality_flags.append("missing_close_time")
        if any(value is None for value in (tp1_hit_time, tp2_hit_time, sl_hit_time, be_moved_time, be_hit_time)):
            data_quality_flags.append("missing_event_times")

        linked_event_times = self._collect_related_event_times(setup=setup, event_map=event_map, history_map=history_map)
        row = {
            "setup_id": setup.id,
            "order_id": setup.order1_ticket or setup.order2_ticket,
            "order1_id": setup.order1_ticket,
            "order2_id": setup.order2_ticket,
            "account_id": setup.trading_account_id,
            "symbol": setup.symbol,
            "direction": setup.side,
            "status": setup.status,
            "entry_time": entry_time,
            "signal_time": signal_time,
            "entry_price": self._to_float(setup.estimated_entry),
            "close_time": close_time,
            "tp1_hit_time": tp1_hit_time,
            "tp2_hit_time": tp2_hit_time,
            "sl_hit_time": sl_hit_time,
            "be_moved_time": be_moved_time,
            "be_hit_time": be_hit_time,
            "signal_bar_time": self.floor_to_timeframe_bar(signal_time, timeframe),
            "entry_bar_time": self.floor_to_timeframe_bar(entry_time, timeframe),
            "close_bar_time": self.floor_to_timeframe_bar(close_time, timeframe),
            "tp1_hit_bar_time": self.floor_to_timeframe_bar(tp1_hit_time, timeframe),
            "tp2_hit_bar_time": self.floor_to_timeframe_bar(tp2_hit_time, timeframe),
            "sl_hit_bar_time": self.floor_to_timeframe_bar(sl_hit_time, timeframe),
            "be_moved_bar_time": self.floor_to_timeframe_bar(be_moved_time, timeframe),
            "be_hit_bar_time": self.floor_to_timeframe_bar(be_hit_time, timeframe),
            "signal_time_ms": self.to_pine_timestamp_ms(signal_time),
            "entry_time_ms": self.to_pine_timestamp_ms(entry_time),
            "close_time_ms": self.to_pine_timestamp_ms(close_time),
            "tp1_hit_time_ms": self.to_pine_timestamp_ms(tp1_hit_time),
            "tp2_hit_time_ms": self.to_pine_timestamp_ms(tp2_hit_time),
            "sl_hit_time_ms": self.to_pine_timestamp_ms(sl_hit_time),
            "be_moved_time_ms": self.to_pine_timestamp_ms(be_moved_time),
            "be_hit_time_ms": self.to_pine_timestamp_ms(be_hit_time),
            "signal_bar_time_ms": self.to_pine_timestamp_ms(self.floor_to_timeframe_bar(signal_time, timeframe)),
            "entry_bar_time_ms": self.to_pine_timestamp_ms(self.floor_to_timeframe_bar(entry_time, timeframe)),
            "close_bar_time_ms": self.to_pine_timestamp_ms(self.floor_to_timeframe_bar(close_time, timeframe)),
            "tp1_hit_bar_time_ms": self.to_pine_timestamp_ms(self.floor_to_timeframe_bar(tp1_hit_time, timeframe)),
            "tp2_hit_bar_time_ms": self.to_pine_timestamp_ms(self.floor_to_timeframe_bar(tp2_hit_time, timeframe)),
            "sl_hit_bar_time_ms": self.to_pine_timestamp_ms(self.floor_to_timeframe_bar(sl_hit_time, timeframe)),
            "be_moved_bar_time_ms": self.to_pine_timestamp_ms(self.floor_to_timeframe_bar(be_moved_time, timeframe)),
            "be_hit_bar_time_ms": self.to_pine_timestamp_ms(self.floor_to_timeframe_bar(be_hit_time, timeframe)),
            "initial_sl": self._to_float(setup.sl_price),
            "tp1_price": self._to_float(setup.tp1_price),
            "tp2_price": self._to_float(setup.tp2_price),
            "actual_entry_price": actual_entry_price,
            "actual_close_price": actual_close_price,
            "tp1_hit_price": tp1_hit_price,
            "tp2_hit_price": tp2_hit_price,
            "sl_hit_price": sl_hit_price,
            "be_price": be_price,
            "realized_pnl": realized_pnl,
            "realized_r": risk_r,
            "risk_r": risk_r,
            "planned_rr_tp1": 1.0,
            "planned_rr_tp2": self._to_float(setup.rr_order2),
            "order1_outcome": setup.order1_outcome,
            "order2_outcome": setup.order2_outcome,
            "order1_entry_time_ms": self.to_pine_timestamp_ms(order1_history.open_time if order1_history else entry_time),
            "order2_entry_time_ms": self.to_pine_timestamp_ms(order2_history.open_time if order2_history else entry_time),
            "order1_entry_price": self._history_float(order1_history, "open_price", actual_entry_price),
            "order2_entry_price": self._history_float(order2_history, "open_price", actual_entry_price),
            "order1_close_time_ms": self.to_pine_timestamp_ms(order1_history.close_time if order1_history and order1_history.close_time else setup.order1_closed_at),
            "order2_close_time_ms": self.to_pine_timestamp_ms(order2_history.close_time if order2_history and order2_history.close_time else setup.order2_closed_at),
            "order1_close_price": self._history_float(order1_history, "close_price", self._to_float(setup.order1_close_price)),
            "order2_close_price": self._history_float(order2_history, "close_price", self._to_float(setup.order2_close_price)),
            "order1_realized_pnl": self._history_float(order1_history, "realized_pnl", self._to_float(setup.order1_realized_pnl)),
            "order2_realized_pnl": self._history_float(order2_history, "realized_pnl", self._to_float(setup.order2_realized_pnl)),
            "order1_history_outcome": order1_history.outcome if order1_history else None,
            "order2_history_outcome": order2_history.outcome if order2_history else None,
            "final_outcome": setup.setup_outcome,
            "setup_outcome": setup.setup_outcome,
            "outcome": setup.setup_outcome,
            "be_moved": setup.order2_be_moved_at is not None,
            "be_moved_at": setup.order2_be_moved_at,
            "review_required_reason": review_required_reason,
            "discipline_score": None,
            "notes": setup.execution_error,
            "events_are_candle_derived": events_are_candle_derived,
            "data_quality_flags": data_quality_flags,
            "linked_event_times": linked_event_times,
        }
        return row

    def _resolve_entry_time(self, setup: TradeSetup) -> datetime | None:
        return setup.executed_at or setup.created_at

    def _resolve_close_time(self, setup: TradeSetup) -> datetime | None:
        times = [
            setup.setup_outcome_recorded_at,
            setup.result_recorded_at,
            setup.order1_closed_at,
            setup.order2_closed_at,
            setup.manual_confirmed_at,
        ]
        valid = [item for item in times if item is not None]
        if not valid:
            return None
        return max(self._normalize_datetime(item) for item in valid)

    def _resolve_close_price(self, setup: TradeSetup) -> float | None:
        if setup.order2_close_price is not None:
            return self._to_float(setup.order2_close_price)
        if setup.order1_close_price is not None:
            return self._to_float(setup.order1_close_price)
        return None

    def _normalize_datetime(self, value: datetime) -> datetime:
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    def _resolve_realized_pnl(self, setup: TradeSetup) -> float | None:
        components = [setup.order1_realized_pnl, setup.order2_realized_pnl]
        if all(value is None for value in components):
            return None
        return float(sum(Decimal(str(value or 0)) for value in components))

    def _to_float(self, value: object | None) -> float | None:
        if value is None:
            return None
        return float(value)

    def _history_float(self, history: MT5TradeHistory | None, field_name: str, fallback: float | None) -> float | None:
        if history is None:
            return fallback
        value = getattr(history, field_name)
        if value is None:
            return fallback
        return float(value)

    def to_pine_timestamp_ms(self, dt: datetime | None) -> int | None:
        if dt is None:
            return None
        if dt.tzinfo is None:
            app_timezone = getattr(get_settings(), "timezone", "UTC")
            try:
                if app_timezone.upper() == "UTC":
                    aware = dt.replace(tzinfo=timezone.utc)
                else:
                    aware = dt.replace(tzinfo=timezone.utc)
            except Exception:
                aware = dt.replace(tzinfo=timezone.utc)
        else:
            aware = dt
        return int(aware.astimezone(timezone.utc).timestamp() * 1000)

    def floor_to_timeframe_bar(self, dt: datetime | None, timeframe: str) -> datetime | None:
        if dt is None:
            return None
        tf = timeframe if timeframe in VALID_REVIEW_TIMEFRAMES else "M5"
        minutes = VALID_REVIEW_TIMEFRAMES[tf]
        normalized = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        floored_minute = (normalized.minute // minutes) * minutes
        return normalized.replace(minute=floored_minute, second=0, microsecond=0)

    def _bulk_load_event_maps(self, setup_ids: list[int]) -> dict[int, dict[str, list[TradeEvent]]]:
        if not setup_ids:
            return {}
        rows = (
            self.db.query(TradeEvent)
            .filter(TradeEvent.setup_id.in_(setup_ids))
            .order_by(TradeEvent.setup_id.asc(), TradeEvent.created_at.asc(), TradeEvent.id.asc())
            .all()
        )
        result: dict[int, dict[str, list[TradeEvent]]] = {}
        for event in rows:
            setup_map = result.setdefault(event.setup_id, {})
            setup_map.setdefault(event.event_type, []).append(event)
        return result

    def _bulk_load_history_maps(self, setup_ids: list[int]) -> dict[int, dict[int, MT5TradeHistory]]:
        if not setup_ids:
            return {}
        rows = (
            self.db.query(MT5TradeHistory)
            .filter(MT5TradeHistory.linked_setup_id.in_(setup_ids))
            .all()
        )
        result: dict[int, dict[int, MT5TradeHistory]] = {}
        for row in rows:
            setup_map = result.setdefault(int(row.linked_setup_id), {})
            setup_map[int(row.position_ticket)] = row
        return result

    def _resolve_event_time(self, event_map: dict[str, list[TradeEvent]], event_type: str, fallback: datetime | None) -> datetime | None:
        if event_type in event_map and event_map[event_type]:
            return event_map[event_type][-1].created_at
        return fallback

    def _resolve_event_price(self, event_map: dict[str, list[TradeEvent]], event_type: str, fallback: float | None) -> float | None:
        if event_type not in event_map or not event_map[event_type]:
            return fallback
        details = event_map[event_type][-1].details
        if not details:
            return fallback
        try:
            payload = json.loads(details)
        except (TypeError, ValueError, json.JSONDecodeError):
            return fallback
        for key in ("close_price", "price", "tp1_price", "tp2_price", "sl_price", "be_price"):
            value = payload.get(key)
            if value is not None:
                try:
                    return float(value)
                except (TypeError, ValueError):
                    continue
        return fallback

    def _resolve_actual_entry_price(self, setup: TradeSetup, history_map: dict[int, MT5TradeHistory]) -> float | None:
        if setup.order1_ticket and int(setup.order1_ticket) in history_map:
            return self._to_float(history_map[int(setup.order1_ticket)].open_price)
        if setup.order2_ticket and int(setup.order2_ticket) in history_map:
            return self._to_float(history_map[int(setup.order2_ticket)].open_price)
        return self._to_float(setup.estimated_entry)

    def _collect_related_event_times(
        self,
        *,
        setup: TradeSetup,
        event_map: dict[str, list[TradeEvent]],
        history_map: dict[int, MT5TradeHistory],
    ) -> list[datetime]:
        times: list[datetime] = []
        setup_times = [
            setup.created_at,
            setup.executed_at,
            setup.setup_outcome_recorded_at,
            setup.result_recorded_at,
            setup.order1_closed_at,
            setup.order2_closed_at,
            setup.manual_confirmed_at,
        ]
        times.extend(item for item in setup_times if item is not None)
        for events in event_map.values():
            times.extend(item.created_at for item in events if item.created_at is not None)
        for trade in history_map.values():
            for maybe in (trade.open_time, trade.close_time):
                if maybe is not None:
                    times.append(maybe)
        return times

    def _resolve_skip_reason(
        self,
        *,
        row: dict[str, Any],
        setup: TradeSetup,
        range_start: datetime,
        range_end: datetime,
        filters: TradingViewExportFilters,
    ) -> str | None:
        if row.get("symbol") != filters.symbol.strip().upper():
            return "symbol_mismatch"
        if filters.account_id is not None and int(row.get("account_id") or -1) != filters.account_id:
            return "account_mismatch"
        if not row.get("direction"):
            return "missing_direction"
        if row.get("entry_price") is None:
            return "missing_entry_price"
        if row.get("initial_sl") is None or row.get("tp1_price") is None:
            return "missing_plan_prices"
        if setup.status not in {"executed", "draft", "failed"}:
            return "unsupported_status"

        in_range = False
        for value in row.get("linked_event_times", []):
            normalized = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
            if range_start <= normalized <= range_end:
                in_range = True
                break
        if not in_range:
            return "outside_date_range"
        if row.get("entry_time") is None and row.get("signal_time") is None:
            return "missing_entry_time"
        return None

    def _build_skipped_record(self, *, setup: TradeSetup, row: dict[str, Any], skip_reason: str) -> dict[str, Any]:
        return {
            "setup_id": setup.id,
            "order_id": row.get("order_id"),
            "symbol": row.get("symbol"),
            "account_id": row.get("account_id"),
            "entry_time": self._iso_dt(row.get("entry_time")),
            "close_time": self._iso_dt(row.get("close_time")),
            "status": setup.status,
            "outcome": row.get("outcome"),
            "skip_reason": skip_reason,
        }

    def _iso_dt(self, value: datetime | None) -> str | None:
        if value is None:
            return None
        return value.isoformat()

    def _max_datetime(self, left: datetime | None, right: datetime | None) -> datetime | None:
        if left is None:
            return right
        if right is None:
            return left
        return max(left, right)

    def _append_data_quality_warnings(
        self,
        trades: list[dict[str, Any]],
        warnings: list[str],
        *,
        filters: TradingViewExportFilters,
    ) -> None:
        missing_entry_bar = sum(1 for trade in trades if trade.get("entry_bar_time_ms") is None)
        missing_close_data = sum(
            1 for trade in trades if trade.get("close_time_ms") is None or trade.get("actual_close_price") is None
        )
        fallback_count = sum(1 for trade in trades if trade.get("events_are_candle_derived"))
        ambiguity_count = sum(1 for trade in trades if trade.get("review_required_reason") == "same_candle_ambiguity")
        fallback_unavailable_count = sum(
            1 for trade in trades if trade.get("review_required_reason") == "candle_fallback_unavailable"
        )
        if missing_entry_bar:
            warnings.append(f"{missing_entry_bar} trade(s) have missing entry_bar_time_ms.")
        if missing_close_data:
            warnings.append(f"{missing_close_data} trade(s) have missing close time/price data.")
        if filters.include_candle_fallback and fallback_unavailable_count:
            warnings.append(
                f"{fallback_unavailable_count} trade(s) requested candle fallback but candle data is unavailable."
            )
        if fallback_count:
            warnings.append(f"{fallback_count} trade(s) include candle-derived fallback event times.")
        if ambiguity_count:
            warnings.append(f"{ambiguity_count} trade(s) flagged with same_candle_ambiguity.")
        close_time_groups: dict[int, int] = {}
        for trade in trades:
            close_ms = trade.get("close_time_ms")
            if close_ms is None:
                continue
            close_time_groups[int(close_ms)] = close_time_groups.get(int(close_ms), 0) + 1
        for trade in trades:
            close_ms = trade.get("close_time_ms")
            if close_ms is not None and close_time_groups.get(int(close_ms), 0) > 1:
                flags = trade.setdefault("data_quality_flags", [])
                if "shared_close_time_suspected" not in flags:
                    flags.append("shared_close_time_suspected")

    def _generate_pine_code(
        self,
        *,
        symbol: str,
        range_start: datetime,
        range_end: datetime,
        trades: list[dict[str, Any]],
        audit_summary: dict[str, Any],
        skipped_records: list[dict[str, Any]],
    ) -> str:
        order_rows = self._build_timefix_order_rows(trades)
        title_suffix = audit_summary.get("selected_account_label") or symbol
        max_count = max(1, min(len(order_rows), ABSOLUTE_MAX_TRADES * 2))
        lines: list[str] = [
            "//@version=6",
            f'indicator("DB History EXACT CLEAN V6 TimeFix - {self._pine_title(title_suffix)}", overlay=true, max_lines_count=500, max_labels_count=500)',
            "// CLEAN V6 TIMEFIX",
            "// Purpose: show exact DB/MT5 entry / SL / TP / close at exact timestamp + exact price.",
            "// Default is focus mode to avoid clutter.",
            "// Data source: DB trade history export.",
            "",
            'focusText = input.string("setup-225", "Focus setup/order/ticket. Empty = show last N positions")',
            f'showLastNWhenNoFocus = input.int(4, "If focus empty: show last N positions", minval=1, maxval={max_count})',
            'orderFilter = input.string("Both", "Order filter", options=["Both", "o1", "o2", "manual"])',
            'slTpSource = input.string("Initial order plan", "SL/TP source", options=["Initial order plan", "DB Positions final/current"])',
            'timeShiftHours = input.int(0, "Time shift hours: DB time -> TradingView", minval=-12, maxval=12)',
            'timeShiftMinutes = input.int(0, "Time shift minutes if needed", minval=-59, maxval=59)',
            "",
            'showEntryLine = input.bool(true, "Show exact ENTRY line")',
            'showSLLine = input.bool(true, "Show exact SL line")',
            'showTPLine = input.bool(true, "Show exact TP line")',
            'showCloseLine = input.bool(true, "Show exact CLOSE line")',
            'showMarkers = input.bool(true, "Show E/C markers")',
            'showTextLabels = input.bool(false, "Show full text labels")',
            'lineWidth = input.int(1, "Line width", minval=1, maxval=4)',
            'debugTimeAudit = input.bool(false, "Debug: show time audit table")',
            'debugTimezone = input.string("GMT+7", "Debug display timezone")',
            "",
            self._pine_array("string", "ids", [self._pine_str(row["id"]) for row in order_rows]),
            self._pine_array("string", "setupIds", [self._pine_str(row["setup_id"]) for row in order_rows]),
            self._pine_array("string", "tickets", [self._pine_str(row["ticket"]) for row in order_rows]),
            self._pine_array("string", "orderTags", [self._pine_str(row["order_tag"]) for row in order_rows]),
            self._pine_array("string", "sides", [self._pine_str(row["side"]) for row in order_rows]),
            self._pine_array("string", "results", [self._pine_str(row["result"]) for row in order_rows]),
            self._pine_array("int", "entryTimesRaw", [self._pine_ms(row["entry_time_ms"]) for row in order_rows]),
            self._pine_array("int", "closeTimesRaw", [self._pine_ms(row["close_time_ms"]) for row in order_rows]),
            self._pine_array("float", "entryPrices", [self._pine_num(row["entry_price"]) for row in order_rows]),
            self._pine_array("float", "positionSLs", [self._pine_num(row["position_sl"]) for row in order_rows]),
            self._pine_array("float", "positionTPs", [self._pine_num(row["position_tp"]) for row in order_rows]),
            self._pine_array("float", "initialSLs", [self._pine_num(row["initial_sl"]) for row in order_rows]),
            self._pine_array("float", "initialTPs", [self._pine_num(row["initial_tp"]) for row in order_rows]),
            self._pine_array("float", "closePrices", [self._pine_num(row["close_price"]) for row in order_rows]),
            self._pine_array("float", "pnls", [self._pine_num(row["profit"]) for row in order_rows]),
            "",
            "f_shift_time(t) =>",
            "    t + (timeShiftHours * 60 * 60 * 1000) + (timeShiftMinutes * 60 * 1000)",
            "",
            "f_time_text(t) =>",
            "    na(t) ? \"na\" : str.format_time(t, \"yyyy-MM-dd HH:mm:ss\", debugTimezone)",
            "",
            "f_norm(s) =>",
            "    str.lower(str.trim(s))",
            "",
            "f_match_focus(id, setupId, ticket) =>",
            "    string f = f_norm(focusText)",
            "    f == \"\" ? false : (str.contains(f_norm(id), f) or str.contains(f_norm(setupId), f) or str.contains(f_norm(ticket), f))",
            "",
            "f_match_order(tag) =>",
            "    orderFilter == \"Both\" ? true : tag == orderFilter",
            "",
            "f_result_text(result) =>",
            "    result == \"win\" ? \"WIN\" : result == \"loss\" ? \"LOSS\" : result == \"be\" ? \"BE\" : \"REVIEW\"",
            "",
            "f_result_color(result, side) =>",
            "    result == \"win\" ? color.lime : result == \"loss\" ? color.red : result == \"be\" ? color.orange : color.gray",
            "",
            "f_side_color(side) =>",
            "    side == \"BUY\" ? color.lime : color.red",
            "",
            "f_line(x1, y1, x2, y2, c, st, w) =>",
            "    line.new(x1=x1, y1=y1, x2=x2, y2=y2, xloc=xloc.bar_time, extend=extend.none, color=c, style=st, width=w)",
            "",
            "f_marker(x, y, txt, c) =>",
            "    label.new(x=x, y=y, text=txt, xloc=xloc.bar_time, yloc=yloc.price, style=label.style_none, textcolor=c, size=size.small)",
            "",
            "f_label(x, y, txt, c, side) =>",
            "    label.new(x=x, y=y, text=txt, xloc=xloc.bar_time, yloc=yloc.price, style=side == \"BUY\" ? label.style_label_up : label.style_label_down, color=color.new(c, 10), textcolor=color.white, size=size.tiny)",
            "",
            f"var table timeAuditTable = table.new(position.top_right, 9, {max_count + 1}, border_width=1)",
            "",
            "if barstate.islast",
            "    int total = array.size(ids)",
            "    bool hasFocus = str.trim(focusText) != \"\"",
            "    int startIndex = hasFocus ? 0 : math.max(0, total - showLastNWhenNoFocus)",
            "    int auditRow = 1",
            "",
            "    if debugTimeAudit and hasFocus",
            f"        table.clear(timeAuditTable, 0, 0, 8, {max_count})",
            "        table.cell(timeAuditTable, 0, 0, \"ID\", text_color=color.white, bgcolor=color.new(color.black, 0))",
            "        table.cell(timeAuditTable, 1, 0, \"Order\", text_color=color.white, bgcolor=color.new(color.black, 0))",
            "        table.cell(timeAuditTable, 2, 0, \"Entry raw\", text_color=color.white, bgcolor=color.new(color.black, 0))",
            "        table.cell(timeAuditTable, 3, 0, \"Entry shifted\", text_color=color.white, bgcolor=color.new(color.black, 0))",
            "        table.cell(timeAuditTable, 4, 0, \"Close raw\", text_color=color.white, bgcolor=color.new(color.black, 0))",
            "        table.cell(timeAuditTable, 5, 0, \"Close shifted\", text_color=color.white, bgcolor=color.new(color.black, 0))",
            "        table.cell(timeAuditTable, 6, 0, \"Entry price\", text_color=color.white, bgcolor=color.new(color.black, 0))",
            "        table.cell(timeAuditTable, 7, 0, \"Close price\", text_color=color.white, bgcolor=color.new(color.black, 0))",
            "        table.cell(timeAuditTable, 8, 0, \"Result\", text_color=color.white, bgcolor=color.new(color.black, 0))",
            "",
            "    for i = startIndex to total - 1",
            "        string id = array.get(ids, i)",
            "        string setupId = array.get(setupIds, i)",
            "        string ticket = array.get(tickets, i)",
            "        string tag = array.get(orderTags, i)",
            "        string side = array.get(sides, i)",
            "        string result = array.get(results, i)",
            "",
            "        bool shouldShow = hasFocus ? f_match_focus(id, setupId, ticket) : true",
            "        shouldShow := shouldShow and f_match_order(tag)",
            "",
            "        if shouldShow",
            "            int entryRaw = array.get(entryTimesRaw, i)",
            "            int closeRaw = array.get(closeTimesRaw, i)",
            "            int entryT = f_shift_time(entryRaw)",
            "            int closeT = f_shift_time(closeRaw)",
            "            float entryP = array.get(entryPrices, i)",
            "            float closeP = array.get(closePrices, i)",
            "            float slP = slTpSource == \"Initial order plan\" ? array.get(initialSLs, i) : array.get(positionSLs, i)",
            "            float tpP = slTpSource == \"Initial order plan\" ? array.get(initialTPs, i) : array.get(positionTPs, i)",
            "            float pnl = array.get(pnls, i)",
            "",
            "            color sideCol = f_side_color(side)",
            "            color resCol = f_result_color(result, side)",
            "",
            "            if showEntryLine",
            "                f_line(entryT, entryP, closeT, entryP, color.new(color.white, 0), line.style_solid, lineWidth)",
            "            if showSLLine and not na(slP)",
            "                f_line(entryT, slP, closeT, slP, color.new(color.red, 0), line.style_dashed, lineWidth)",
            "            if showTPLine and not na(tpP)",
            "                f_line(entryT, tpP, closeT, tpP, color.new(color.lime, 0), line.style_dotted, lineWidth)",
            "            if showCloseLine",
            "                f_line(entryT, closeP, closeT, closeP, color.new(resCol, 0), line.style_solid, lineWidth)",
            "",
            "            if showMarkers",
            "                f_marker(entryT, entryP, \"E\", sideCol)",
            "                f_marker(closeT, closeP, \"C\", resCol)",
            "",
            "            if showTextLabels",
            "                string entryText = id + \"\\nENTRY \" + side + \" @ \" + str.tostring(entryP) + \"\\nSL \" + str.tostring(slP) + \" | TP \" + str.tostring(tpP)",
            "                string closeText = id + \"\\n\" + f_result_text(result) + \" CLOSE @ \" + str.tostring(closeP) + \"\\nP/L \" + str.tostring(pnl)",
            "                f_label(entryT, entryP, entryText, sideCol, side)",
            "                f_label(closeT, closeP, closeText, resCol, side)",
            "",
            "            if debugTimeAudit and hasFocus and auditRow <= total",
            "                table.cell(timeAuditTable, 0, auditRow, id)",
            "                table.cell(timeAuditTable, 1, auditRow, tag)",
            "                table.cell(timeAuditTable, 2, auditRow, f_time_text(entryRaw))",
            "                table.cell(timeAuditTable, 3, auditRow, f_time_text(entryT))",
            "                table.cell(timeAuditTable, 4, auditRow, f_time_text(closeRaw))",
            "                table.cell(timeAuditTable, 5, auditRow, f_time_text(closeT))",
            "                table.cell(timeAuditTable, 6, auditRow, str.tostring(entryP))",
            "                table.cell(timeAuditTable, 7, auditRow, str.tostring(closeP))",
            "                table.cell(timeAuditTable, 8, auditRow, result)",
            "                auditRow += 1",
            "",
            "plot(na)",
        ]
        return "\n".join(lines) + "\n"

    def _build_timefix_order_rows(self, trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for trade in trades:
            setup_ref = f"setup-{trade.get('setup_id')}"
            side = str(trade.get("direction") or "").upper()
            base = {
                "setup_id": setup_ref,
                "side": side,
                "initial_sl": trade.get("initial_sl"),
            }
            order1_ticket = trade.get("order1_id")
            if order1_ticket:
                rows.append(
                    {
                        **base,
                        "id": f"{setup_ref}-o1",
                        "ticket": str(order1_ticket),
                        "order_tag": "o1",
                        "entry_time_ms": trade.get("order1_entry_time_ms") or trade.get("entry_time_ms"),
                        "entry_price": trade.get("order1_entry_price") if trade.get("order1_entry_price") is not None else trade.get("entry_price"),
                        "initial_tp": trade.get("tp1_price"),
                        "position_sl": trade.get("initial_sl"),
                        "position_tp": trade.get("tp1_hit_price") if trade.get("tp1_hit_price") is not None else trade.get("tp1_price"),
                        "close_time_ms": trade.get("order1_close_time_ms") or trade.get("tp1_hit_time_ms"),
                        "close_price": trade.get("order1_close_price"),
                        "profit": trade.get("order1_realized_pnl"),
                        "result": self._timefix_result_label(
                            trade.get("order1_history_outcome") or trade.get("order1_outcome"),
                            trade.get("setup_outcome"),
                            order_tag="o1",
                            profit=trade.get("order1_realized_pnl"),
                        ),
                    }
                )
            order2_ticket = trade.get("order2_id")
            if order2_ticket:
                current_sl = trade.get("be_price") if trade.get("be_price") is not None else trade.get("initial_sl")
                current_tp = trade.get("tp2_hit_price") if trade.get("tp2_hit_price") is not None else trade.get("tp2_price")
                rows.append(
                    {
                        **base,
                        "id": f"{setup_ref}-o2",
                        "ticket": str(order2_ticket),
                        "order_tag": "o2",
                        "entry_time_ms": trade.get("order2_entry_time_ms") or trade.get("entry_time_ms"),
                        "entry_price": trade.get("order2_entry_price") if trade.get("order2_entry_price") is not None else trade.get("entry_price"),
                        "initial_tp": trade.get("tp2_price"),
                        "position_sl": current_sl,
                        "position_tp": current_tp,
                        "close_time_ms": trade.get("order2_close_time_ms") or trade.get("tp2_hit_time_ms") or trade.get("be_hit_time_ms") or trade.get("sl_hit_time_ms"),
                        "close_price": trade.get("order2_close_price"),
                        "profit": trade.get("order2_realized_pnl"),
                        "result": self._timefix_result_label(
                            trade.get("order2_history_outcome") or trade.get("order2_outcome"),
                            trade.get("setup_outcome"),
                            order_tag="o2",
                            profit=trade.get("order2_realized_pnl"),
                        ),
                    }
                )
            if not order1_ticket and not order2_ticket:
                ticket = str(trade.get("order_id") or trade.get("setup_id") or "")
                rows.append(
                    {
                        **base,
                        "id": f"pos-{ticket}" if ticket else setup_ref,
                        "ticket": ticket,
                        "order_tag": "manual",
                        "entry_time_ms": trade.get("entry_time_ms"),
                        "entry_price": trade.get("actual_entry_price") if trade.get("actual_entry_price") is not None else trade.get("entry_price"),
                        "initial_tp": trade.get("tp1_price"),
                        "position_sl": trade.get("initial_sl"),
                        "position_tp": trade.get("tp1_price"),
                        "close_time_ms": trade.get("close_time_ms"),
                        "close_price": trade.get("actual_close_price"),
                        "profit": trade.get("realized_pnl"),
                        "result": self._timefix_result_label(
                            trade.get("setup_outcome"),
                            trade.get("outcome"),
                            order_tag="manual",
                            profit=trade.get("realized_pnl"),
                        ),
                    }
                )
        return rows

    def _timefix_result_label(
        self,
        outcome: object | None,
        fallback: object | None = None,
        *,
        order_tag: str | None = None,
        profit: object | None = None,
    ) -> str:
        value = str(outcome or fallback or "").lower()
        if value in {"tp", "tp_hit", "tp1_hit", "tp2_hit", "order2_tp_hit", "full_win"}:
            return "win"
        if value in {"be", "closed_at_be", "breakeven", "scratch_manual", "order2_be", "order2_closed_at_be"}:
            return "be"
        if value in {"sl", "sl_hit", "order2_sl_hit", "full_loss"}:
            return "loss"
        if value in {"managed_win", "tp1_be", "tp1_plus_be"}:
            return "be" if order_tag == "o2" else "win"
        if value in {"manual_close", "manual_win", "manual_loss"} or "manual" in value:
            try:
                pnl = float(profit) if profit is not None else 0.0
            except (TypeError, ValueError):
                pnl = 0.0
            if pnl > 0:
                return "win"
            if pnl < 0:
                return "loss"
            return "be"
        return "review"

    def _pine_array(self, pine_type: str, name: str, values: list[str]) -> str:
        if not values:
            fallback = '""' if pine_type == "string" else "na"
            return f"var {pine_type}[] {name} = array.from({fallback})"
        return f"var {pine_type}[] {name} = array.from({', '.join(values)})"

    def _pine_title(self, value: object) -> str:
        text = str(value or "history")
        return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")[:80]

    def _pine_comment_json(self, payload: object) -> str:
        text = json.dumps(payload, separators=(",", ":"), default=str)
        return text.replace("\n", " ")

    def _pine_ts(self, value: datetime | None) -> str:
        if value is None:
            return "na"
        dt = value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return f"timestamp({dt.year}, {dt.month}, {dt.day}, {dt.hour}, {dt.minute})"

    def _pine_ms(self, value: int | None) -> str:
        if value is None:
            return "na"
        return str(int(value))

    def _pine_num(self, value: float | None) -> str:
        if value is None:
            return "na"
        return f"{value:.6f}"

    def _pine_str(self, value: str) -> str:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        return f'"{escaped}"'

    def _label_text(self, trade: dict[str, Any]) -> str:
        setup_ref = f"setup:{trade['setup_id']}" if trade.get("setup_id") else f"order:{trade.get('order_id', '-')}"
        lines = [
            f"{(trade.get('direction') or 'unknown').upper()} {setup_ref}",
            f"Entry: {self._display_num(trade.get('entry_price'))}",
            f"SL: {self._display_num(trade.get('initial_sl'))}",
            f"TP1: {self._display_num(trade.get('tp1_price'))}",
        ]
        if trade.get("tp2_price") is not None:
            lines.append(f"TP2: {self._display_num(trade.get('tp2_price'))}")
        if trade.get("actual_close_price") is not None:
            lines.append(f"Close: {self._display_num(trade.get('actual_close_price'))}")
        if trade.get("realized_pnl") is not None:
            lines.append(f"PnL: {self._display_num(trade.get('realized_pnl'))}")
        if trade.get("risk_r") is not None:
            lines.append(f"R: {self._display_num(trade.get('risk_r'))}")
        lines.append(f"Outcome: {trade.get('outcome') or 'unknown'}")
        if trade.get("discipline_score") is not None:
            lines.append(f"Discipline: {trade.get('discipline_score')}")
        return "\n".join(lines)

    def _display_num(self, value: float | None) -> str:
        if value is None:
            return "n/a"
        return f"{value:.4f}"
