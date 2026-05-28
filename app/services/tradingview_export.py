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
        return max(valid)

    def _resolve_close_price(self, setup: TradeSetup) -> float | None:
        if setup.order2_close_price is not None:
            return self._to_float(setup.order2_close_price)
        if setup.order1_close_price is not None:
            return self._to_float(setup.order1_close_price)
        return None

    def _resolve_realized_pnl(self, setup: TradeSetup) -> float | None:
        components = [setup.order1_realized_pnl, setup.order2_realized_pnl]
        if all(value is None for value in components):
            return None
        return float(sum(Decimal(str(value or 0)) for value in components))

    def _to_float(self, value: object | None) -> float | None:
        if value is None:
            return None
        return float(value)

    def to_pine_timestamp_ms(self, dt: datetime | None) -> int | None:
        if dt is None:
            return None
        if dt.tzinfo is None:
            app_timezone = get_settings().timezone
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
        lines: list[str] = []
        lines.append("//@version=6")
        lines.append("// Export audit:")
        for key in (
            "requested_symbol",
            "requested_start_date",
            "requested_end_date",
            "raw_setup_count",
            "normalized_trade_count",
            "exported_trade_count",
            "skipped_trade_count",
            "max_trades",
            "was_limited_by_max_trades",
            "first_exported_entry_time",
            "last_exported_entry_time",
        ):
            lines.append(f"// {key}: {audit_summary.get(key)}")
        if skipped_records:
            lines.append(f"// skipped_records_json: {self._pine_comment_json(skipped_records)}")
        lines.append(
            f'indicator("Trade History - {symbol} - {range_start.date().isoformat()} to {range_end.date().isoformat()}", '
            "overlay=true, max_labels_count=500, max_lines_count=500)"
        )
        lines.append("")
        lines.append("var entryTimes = array.new_int()")
        lines.append("var entryPrices = array.new_float()")
        lines.append("var closeTimes = array.new_int()")
        lines.append("var closePrices = array.new_float()")
        lines.append("var slPrices = array.new_float()")
        lines.append("var tp1Prices = array.new_float()")
        lines.append("var tp2Prices = array.new_float()")
        lines.append("var signalTimes = array.new_int()")
        lines.append("var entryBarTimes = array.new_int()")
        lines.append("var closeBarTimes = array.new_int()")
        lines.append("var tp1HitTimes = array.new_int()")
        lines.append("var tp2HitTimes = array.new_int()")
        lines.append("var slHitTimes = array.new_int()")
        lines.append("var beMovedTimes = array.new_int()")
        lines.append("var beHitTimes = array.new_int()")
        lines.append("var directions = array.new_string()")
        lines.append("var outcomes = array.new_string()")
        lines.append("var labelTexts = array.new_string()")
        lines.append("var reviewReasons = array.new_string()")
        lines.append("")
        lines.append("outcomeColor(string outcome) =>")
        lines.append("    switch outcome")
        lines.append('        "full_win" => color.new(color.green, 0)')
        lines.append('        "managed_win" => color.new(color.green, 0)')
        lines.append('        "closed_at_be" => color.new(color.gray, 0)')
        lines.append('        "sl_hit" => color.new(color.red, 0)')
        lines.append('        "full_loss" => color.new(color.red, 0)')
        lines.append('        "review_required" => color.new(color.orange, 0)')
        lines.append('        "manual_close" => color.new(color.blue, 20)')
        lines.append("        => color.new(color.gray, 0)")
        lines.append("")
        lines.append("if barstate.isfirst")
        for trade in trades:
            entry_ts = self._pine_ms(trade["entry_time_ms"])
            close_ts = self._pine_ms(trade["close_time_ms"])
            signal_ts = self._pine_ms(trade["signal_time_ms"])
            entry_bar_ts = self._pine_ms(trade["entry_bar_time_ms"])
            close_bar_ts = self._pine_ms(trade["close_bar_time_ms"])
            tp1_hit_ts = self._pine_ms(trade["tp1_hit_time_ms"])
            tp2_hit_ts = self._pine_ms(trade["tp2_hit_time_ms"])
            sl_hit_ts = self._pine_ms(trade["sl_hit_time_ms"])
            be_moved_ts = self._pine_ms(trade["be_moved_time_ms"])
            be_hit_ts = self._pine_ms(trade["be_hit_time_ms"])
            entry_price = self._pine_num(trade["entry_price"])
            close_price = self._pine_num(trade["actual_close_price"])
            sl = self._pine_num(trade["initial_sl"])
            tp1 = self._pine_num(trade["tp1_price"])
            tp2 = self._pine_num(trade["tp2_price"])
            direction = self._pine_str(trade["direction"] or "unknown")
            outcome = self._pine_str(trade["outcome"] or "unknown")
            label_text = self._pine_str(self._label_text(trade))
            review_reason = self._pine_str(trade.get("review_required_reason") or "")
            lines.append(f"    array.push(entryTimes, {entry_ts})")
            lines.append(f"    array.push(entryPrices, {entry_price})")
            lines.append(f"    array.push(closeTimes, {close_ts})")
            lines.append(f"    array.push(closePrices, {close_price})")
            lines.append(f"    array.push(slPrices, {sl})")
            lines.append(f"    array.push(tp1Prices, {tp1})")
            lines.append(f"    array.push(tp2Prices, {tp2})")
            lines.append(f"    array.push(signalTimes, {signal_ts})")
            lines.append(f"    array.push(entryBarTimes, {entry_bar_ts})")
            lines.append(f"    array.push(closeBarTimes, {close_bar_ts})")
            lines.append(f"    array.push(tp1HitTimes, {tp1_hit_ts})")
            lines.append(f"    array.push(tp2HitTimes, {tp2_hit_ts})")
            lines.append(f"    array.push(slHitTimes, {sl_hit_ts})")
            lines.append(f"    array.push(beMovedTimes, {be_moved_ts})")
            lines.append(f"    array.push(beHitTimes, {be_hit_ts})")
            lines.append(f"    array.push(directions, {direction})")
            lines.append(f"    array.push(outcomes, {outcome})")
            lines.append(f"    array.push(labelTexts, {label_text})")
            lines.append(f"    array.push(reviewReasons, {review_reason})")
        lines.append("")
        lines.append("focusTradeNo = input.int(1, 'Focus trade number', minval=0)")
        lines.append("positionTarget = input.string('TP1', 'Position target', options=['TP1', 'TP2'])")
        lines.append("positionWidthHours = input.int(12, 'Position width hours', minval=1, maxval=240)")
        lines.append("showSelectedBox = input.bool(true, 'Show selected risk/reward box')")
        lines.append("showSelectedLevels = input.bool(true, 'Show Entry / SL / TP levels')")
        lines.append("showSelectedPriceTags = input.bool(true, 'Show price tags')")
        lines.append("showSelectedEntryLabel = input.bool(true, 'Show entry label')")
        lines.append("showEventMarkers = input.bool(true, 'Show TP/SL/BE event markers')")
        lines.append("showTinyMarkersForAllTrades = input.bool(false, 'Show tiny markers for all trades')")
        lines.append("")
        lines.append("var rendered = false")
        lines.append("if barstate.islast and not rendered")
        lines.append("    tradeCount = array.size(entryTimes)")
        lines.append("    if focusTradeNo > 0 and focusTradeNo <= tradeCount")
        lines.append("        i = focusTradeNo - 1")
        lines.append("        entryTime = array.get(entryTimes, i)")
        lines.append("        entryBarTime = array.get(entryBarTimes, i)")
        lines.append("        entryPrice = array.get(entryPrices, i)")
        lines.append("        closeTime = array.get(closeTimes, i)")
        lines.append("        closePrice = array.get(closePrices, i)")
        lines.append("        sl = array.get(slPrices, i)")
        lines.append("        tp1 = array.get(tp1Prices, i)")
        lines.append("        tp2 = array.get(tp2Prices, i)")
        lines.append("        tp1HitTime = array.get(tp1HitTimes, i)")
        lines.append("        tp2HitTime = array.get(tp2HitTimes, i)")
        lines.append("        slHitTime = array.get(slHitTimes, i)")
        lines.append("        beMovedTime = array.get(beMovedTimes, i)")
        lines.append("        beHitTime = array.get(beHitTimes, i)")
        lines.append("        direction = array.get(directions, i)")
        lines.append("        outcome = array.get(outcomes, i)")
        lines.append("        text = array.get(labelTexts, i)")
        lines.append("        reviewReason = array.get(reviewReasons, i)")
        lines.append("        directionIsBuy = direction == 'buy'")
        lines.append("        targetPrice = positionTarget == 'TP2' and not na(tp2) ? tp2 : tp1")
        lines.append("        widthMs = positionWidthHours * 60 * 60 * 1000")
        lines.append("        rightTime = entryBarTime + widthMs")
        lines.append("        rewardTop = directionIsBuy ? targetPrice : entryPrice")
        lines.append("        rewardBottom = directionIsBuy ? entryPrice : targetPrice")
        lines.append("        riskTop = directionIsBuy ? entryPrice : sl")
        lines.append("        riskBottom = directionIsBuy ? sl : entryPrice")
        lines.append("        if showSelectedBox and not na(targetPrice) and not na(sl)")
        lines.append("            box.new(entryBarTime, rewardTop, rightTime, rewardBottom, xloc=xloc.bar_time, bgcolor=color.new(color.green, 85), border_color=color.new(color.green, 10))")
        lines.append("            box.new(entryBarTime, riskTop, rightTime, riskBottom, xloc=xloc.bar_time, bgcolor=color.new(color.red, 86), border_color=color.new(color.red, 10))")
        lines.append("        if showSelectedLevels")
        lines.append("            line.new(entryBarTime, entryPrice, rightTime, entryPrice, xloc=xloc.bar_time, color=color.new(color.white, 0), width=1)")
        lines.append("            line.new(entryBarTime, sl, rightTime, sl, xloc=xloc.bar_time, color=color.new(color.red, 0), width=1)")
        lines.append("            line.new(entryBarTime, tp1, rightTime, tp1, xloc=xloc.bar_time, color=color.new(color.green, 0), width=1)")
        lines.append("            if not na(tp2)")
        lines.append("                line.new(entryBarTime, tp2, rightTime, tp2, xloc=xloc.bar_time, color=color.new(color.teal, 0), width=1)")
        lines.append("        if showSelectedPriceTags")
        lines.append("            label.new(rightTime, entryPrice, text='Entry', xloc=xloc.bar_time, style=label.style_label_left, color=color.new(color.black, 0), textcolor=color.white)")
        lines.append("            label.new(rightTime, sl, text='SL', xloc=xloc.bar_time, style=label.style_label_left, color=color.new(color.red, 0), textcolor=color.white)")
        lines.append("            label.new(rightTime, tp1, text='TP1', xloc=xloc.bar_time, style=label.style_label_left, color=color.new(color.green, 0), textcolor=color.white)")
        lines.append("            if not na(tp2)")
        lines.append("                label.new(rightTime, tp2, text='TP2', xloc=xloc.bar_time, style=label.style_label_left, color=color.new(color.teal, 0), textcolor=color.white)")
        lines.append("        if showSelectedEntryLabel")
        lines.append("            style = directionIsBuy ? label.style_label_up : label.style_label_down")
        lines.append("            label.new(entryTime, entryPrice, text=text, xloc=xloc.bar_time, style=style, color=outcomeColor(outcome), textcolor=color.white)")
        lines.append("        if showEventMarkers")
        lines.append("            if not na(tp1HitTime)")
        lines.append("                label.new(tp1HitTime, tp1, text='TP1', xloc=xloc.bar_time, style=label.style_label_down, color=color.new(color.green, 0), textcolor=color.white)")
        lines.append("            if not na(tp2HitTime) and not na(tp2)")
        lines.append("                label.new(tp2HitTime, tp2, text='TP2', xloc=xloc.bar_time, style=label.style_label_down, color=color.new(color.teal, 0), textcolor=color.white)")
        lines.append("            if not na(slHitTime)")
        lines.append("                label.new(slHitTime, sl, text='SL', xloc=xloc.bar_time, style=label.style_label_up, color=color.new(color.red, 0), textcolor=color.white)")
        lines.append("            if not na(beMovedTime)")
        lines.append("                label.new(beMovedTime, entryPrice, text='BE Move', xloc=xloc.bar_time, style=label.style_label_left, color=color.new(color.gray, 15), textcolor=color.white)")
        lines.append("            if not na(beHitTime)")
        lines.append("                label.new(beHitTime, entryPrice, text='BE Hit', xloc=xloc.bar_time, style=label.style_label_left, color=color.new(color.gray, 0), textcolor=color.white)")
        lines.append("            if not na(closeTime) and not na(closePrice)")
        lines.append("                label.new(closeTime, closePrice, text='Close\\n' + outcome, xloc=xloc.bar_time, style=label.style_label_left, color=outcomeColor(outcome), textcolor=color.white)")
        lines.append("            if str.length(reviewReason) > 0")
        lines.append("                label.new(entryTime, entryPrice, text='Review: ' + reviewReason, xloc=xloc.bar_time, style=label.style_label_left, color=color.new(color.orange, 0), textcolor=color.white)")
        lines.append("    else if focusTradeNo == 0 and showTinyMarkersForAllTrades")
        lines.append("        for i = 0 to tradeCount - 1")
        lines.append("            entryTime = array.get(entryTimes, i)")
        lines.append("            entryPrice = array.get(entryPrices, i)")
        lines.append("            direction = array.get(directions, i)")
        lines.append("            style = direction == 'buy' ? label.style_circle : label.style_diamond")
        lines.append("            label.new(entryTime, entryPrice, text='', xloc=xloc.bar_time, style=style, color=color.new(color.white, 70), textcolor=color.white, size=size.tiny)")
        lines.append("    rendered := true")
        lines.append("")
        lines.append("plot(na)")
        return "\n".join(lines) + "\n"

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
