from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.models.trade_setup import TradeSetup

VALID_QUICK_RANGES = {"last_2_months", "last_30_days", "last_7_days"}
DEFAULT_MAX_TRADES = 100
ABSOLUTE_MAX_TRADES = 500
TV_MAX_LABELS = 500
TV_MAX_LINES = 500


@dataclass
class TradingViewExportFilters:
    symbol: str
    account_id: int | None = None
    start_date: date | None = None
    end_date: date | None = None
    quick_range: str = "last_2_months"
    outcome: str | None = None
    max_trades: int = DEFAULT_MAX_TRADES


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

        entry_expr = self._entry_time_expr()
        close_expr = self._close_time_expr()
        query = query.filter(
            or_(
                entry_expr.between(start_at, end_at),
                close_expr.between(start_at, end_at),
            )
        )

        setups = query.order_by(entry_expr.asc(), TradeSetup.id.asc()).all()
        total_matched = len(setups)
        if total_matched > max_trades:
            warnings.append(
                f"Matched {total_matched} trades; exporting latest {max_trades} by entry time due to max_trades."
            )
            setups = setups[-max_trades:]

        setups = self._trim_for_tv_limits(setups, warnings)
        trades = [self._setup_to_export_row(setup) for setup in setups]
        pine_code = self._generate_pine_code(symbol=symbol, range_start=start_at, range_end=end_at, trades=trades)

        return TradingViewExportResult(
            symbol=symbol,
            range_start=start_at,
            range_end=end_at,
            requested_max_trades=max_trades,
            exported_trades=len(trades),
            total_matched_trades=total_matched,
            warnings=warnings,
            pine_code=pine_code,
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

    def _trim_for_tv_limits(self, setups: list[TradeSetup], warnings: list[str]) -> list[TradeSetup]:
        # Conservative estimate per trade to avoid TradingView object cap explosions.
        # labels: entry + close (if closed), lines: sl + tp1 + tp2 + entry-close (if closed).
        labels = 0
        lines = 0
        selected: list[TradeSetup] = []
        for setup in reversed(setups):
            close_exists = self._resolve_close_time(setup) is not None and self._resolve_close_price(setup) is not None
            has_tp2 = setup.tp2_price is not None
            next_labels = labels + 1 + (1 if close_exists else 0)
            next_lines = lines + 2 + (1 if has_tp2 else 0) + (1 if close_exists else 0)
            if next_labels > TV_MAX_LABELS or next_lines > TV_MAX_LINES:
                continue
            labels = next_labels
            lines = next_lines
            selected.append(setup)
        selected.reverse()
        if len(selected) < len(setups):
            warnings.append(
                f"Trimmed to {len(selected)} trades to stay within TradingView object limits "
                f"(labels <= {TV_MAX_LABELS}, lines <= {TV_MAX_LINES})."
            )
        return selected

    def _setup_to_export_row(self, setup: TradeSetup) -> dict[str, Any]:
        entry_time = self._resolve_entry_time(setup)
        close_time = self._resolve_close_time(setup)
        realized_pnl = self._resolve_realized_pnl(setup)
        risk_r = None
        if realized_pnl is not None and setup.r_value not in (None, 0):
            risk_r = float(realized_pnl) / float(setup.r_value)
        return {
            "setup_id": setup.id,
            "order_id": setup.order1_ticket or setup.order2_ticket,
            "symbol": setup.symbol,
            "direction": setup.side,
            "entry_time": entry_time,
            "entry_price": self._to_float(setup.estimated_entry),
            "close_time": close_time,
            "close_price": self._resolve_close_price(setup),
            "initial_sl": self._to_float(setup.sl_price),
            "tp1_price": self._to_float(setup.tp1_price),
            "tp2_price": self._to_float(setup.tp2_price),
            "realized_pnl": realized_pnl,
            "risk_r": risk_r,
            "outcome": setup.setup_outcome,
            "setup_outcome": setup.setup_outcome,
            "be_moved": setup.order2_be_moved_at is not None,
            "be_moved_at": setup.order2_be_moved_at,
            "discipline_score": None,
            "notes": setup.execution_error,
        }

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

    def _generate_pine_code(
        self,
        *,
        symbol: str,
        range_start: datetime,
        range_end: datetime,
        trades: list[dict[str, Any]],
    ) -> str:
        lines: list[str] = []
        lines.append("//@version=6")
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
        lines.append("var directions = array.new_string()")
        lines.append("var outcomes = array.new_string()")
        lines.append("var labelTexts = array.new_string()")
        lines.append("")
        lines.append("color outcomeColor(string outcome) =>")
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
            entry_ts = self._pine_ts(trade["entry_time"])
            close_ts = self._pine_ts(trade["close_time"])
            entry_price = self._pine_num(trade["entry_price"])
            close_price = self._pine_num(trade["close_price"])
            sl = self._pine_num(trade["initial_sl"])
            tp1 = self._pine_num(trade["tp1_price"])
            tp2 = self._pine_num(trade["tp2_price"])
            direction = self._pine_str(trade["direction"] or "unknown")
            outcome = self._pine_str(trade["outcome"] or "unknown")
            label_text = self._pine_str(self._label_text(trade))
            lines.append(f"    array.push(entryTimes, {entry_ts})")
            lines.append(f"    array.push(entryPrices, {entry_price})")
            lines.append(f"    array.push(closeTimes, {close_ts})")
            lines.append(f"    array.push(closePrices, {close_price})")
            lines.append(f"    array.push(slPrices, {sl})")
            lines.append(f"    array.push(tp1Prices, {tp1})")
            lines.append(f"    array.push(tp2Prices, {tp2})")
            lines.append(f"    array.push(directions, {direction})")
            lines.append(f"    array.push(outcomes, {outcome})")
            lines.append(f"    array.push(labelTexts, {label_text})")
        lines.append("")
        lines.append("var rendered = false")
        lines.append("if barstate.islast and not rendered")
        lines.append("    for i = 0 to array.size(entryTimes) - 1")
        lines.append("        entryTime = array.get(entryTimes, i)")
        lines.append("        entryPrice = array.get(entryPrices, i)")
        lines.append("        closeTime = array.get(closeTimes, i)")
        lines.append("        closePrice = array.get(closePrices, i)")
        lines.append("        sl = array.get(slPrices, i)")
        lines.append("        tp1 = array.get(tp1Prices, i)")
        lines.append("        tp2 = array.get(tp2Prices, i)")
        lines.append("        direction = array.get(directions, i)")
        lines.append("        outcome = array.get(outcomes, i)")
        lines.append("        text = array.get(labelTexts, i)")
        lines.append("        directionIsBuy = direction == 'buy'")
        lines.append("        style = directionIsBuy ? label.style_label_up : label.style_label_down")
        lines.append("        entryColor = outcomeColor(outcome)")
        lines.append("        label.new(entryTime, entryPrice, text=text, xloc=xloc.bar_time, style=style, color=entryColor, textcolor=color.white)")
        lines.append("        line.new(entryTime, sl, closeTime == na ? entryTime : closeTime, sl, xloc=xloc.bar_time, color=color.new(color.red, 15), width=1)")
        lines.append("        line.new(entryTime, tp1, closeTime == na ? entryTime : closeTime, tp1, xloc=xloc.bar_time, color=color.new(color.green, 15), width=1)")
        lines.append("        if tp2 != na")
        lines.append("            line.new(entryTime, tp2, closeTime == na ? entryTime : closeTime, tp2, xloc=xloc.bar_time, color=color.new(color.teal, 10), width=1)")
        lines.append("        if closeTime != na and closePrice != na")
        lines.append("            line.new(entryTime, entryPrice, closeTime, closePrice, xloc=xloc.bar_time, color=color.new(entryColor, 10), width=2)")
        lines.append("            label.new(closeTime, closePrice, text='Close\\n' + outcome, xloc=xloc.bar_time, style=label.style_label_left, color=color.new(entryColor, 5), textcolor=color.white)")
        lines.append("    rendered := true")
        lines.append("")
        lines.append("plot(na)")
        return "\n".join(lines) + "\n"

    def _pine_ts(self, value: datetime | None) -> str:
        if value is None:
            return "na"
        dt = value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return f"timestamp({dt.year}, {dt.month}, {dt.day}, {dt.hour}, {dt.minute})"

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
        if trade.get("close_price") is not None:
            lines.append(f"Close: {self._display_num(trade.get('close_price'))}")
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
