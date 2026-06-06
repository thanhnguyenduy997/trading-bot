from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from io import StringIO
import re


SETUP_COMMENT_RE = re.compile(r"\b(setup-\d+)-(o[12])\b", re.IGNORECASE)


@dataclass
class TradePosition:
    account_name: str | None
    account_number: str | None
    broker_company: str | None
    report_date: str | None
    symbol: str
    position_ticket: str
    setup_id: str | None
    order_leg: str | None
    side: str
    volume: float | None
    entry_time_raw: str
    entry_price: float | None
    initial_sl: float | None
    initial_tp: float | None
    close_time_raw: str
    close_price: float | None
    commission: float
    swap: float
    profit: float
    raw_comment: str | None
    result_order_level: str


@dataclass
class ParsedMt5History:
    account_name: str | None
    account_number: str | None
    broker_company: str | None
    report_date: str | None
    positions: list[TradePosition]


@dataclass
class TradeSetup:
    setup_id: str
    symbol: str
    side: str
    entry_time: str
    entry_price: float | None
    initial_sl: float | None
    tp1: float | None
    tp2: float | None
    close_time: str
    close_price: float | None
    order1_ticket: str | None
    order2_ticket: str | None
    order1_result: str
    order2_result: str
    setup_result: str
    pnl_total: float
    volume_total: float
    review_required_grouping: bool
    order1_entry_price: float | None = None
    order2_entry_price: float | None = None
    order1_close_price: float | None = None
    order2_close_price: float | None = None
    order1_close_time: str | None = None
    order2_close_time: str | None = None
    order1_profit: float | None = None
    order2_profit: float | None = None


@dataclass
class Mt5HistoryExportOptions:
    time_shift_hours: int = 4
    symbol_filter: str | None = None
    show_last_n: int = 20
    ui_mode: str = "position_tool_style"
    tolerance_price: float = 0.0005
    tolerance_profit: float = 1.0


@dataclass
class Mt5HistoryExportResult:
    parsed: ParsedMt5History
    setups: list[TradeSetup]
    pine_code: str
    csv_content: str


class _Mt5TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self.all_text: list[str] = []
        self._table_stack: list[list[list[str]]] = []
        self._current_row: list[str] | None = None
        self._current_cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized = tag.lower()
        if normalized == "table":
            self._table_stack.append([])
        elif normalized == "tr" and self._table_stack:
            self._current_row = []
        elif normalized in {"td", "th"} and self._current_row is not None:
            self._current_cell = []

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        if normalized in {"td", "th"} and self._current_row is not None and self._current_cell is not None:
            self._current_row.append(_clean_text(" ".join(self._current_cell)))
            self._current_cell = None
        elif normalized == "tr" and self._table_stack and self._current_row is not None:
            if any(cell for cell in self._current_row):
                self._table_stack[-1].append(self._current_row)
            self._current_row = None
            self._current_cell = None
        elif normalized == "table" and self._table_stack:
            table = self._table_stack.pop()
            if table:
                self.tables.append(table)

    def handle_data(self, data: str) -> None:
        text = _clean_text(data)
        if text:
            self.all_text.append(text)
        if self._current_cell is not None:
            self._current_cell.append(data)


def parse_mt5_html_report(file_path: str) -> ParsedMt5History:
    with open(file_path, "r", encoding="utf-8", errors="ignore") as handle:
        content = handle.read()
    parser = _Mt5TableParser()
    parser.feed(content)

    account_name = _extract_metadata(content, ("Name", "Account Name"))
    account_number = _extract_metadata(content, ("Account", "Login"))
    broker_company = _extract_metadata(content, ("Company", "Broker"))
    report_date = _extract_metadata(content, ("Date", "Report Date"))

    positions_table, header_index = _find_positions_table(parser.tables)
    positions: list[TradePosition] = []
    if positions_table is not None and header_index is not None:
        headers = _normalize_position_headers(positions_table[header_index])
        for row in positions_table[header_index + 1 :]:
            item = _row_to_position(
                row=row,
                headers=headers,
                account_name=account_name,
                account_number=account_number,
                broker_company=broker_company,
                report_date=report_date,
            )
            if item is not None:
                positions.append(item)

    return ParsedMt5History(
        account_name=account_name,
        account_number=account_number,
        broker_company=broker_company,
        report_date=report_date,
        positions=positions,
    )


def classify_position(position: TradePosition, tolerance_price: float, tolerance_profit: float) -> str:
    entry = position.entry_price
    sl = position.initial_sl
    tp = position.initial_tp
    close = position.close_price
    profit = position.profit
    side = position.side.upper()

    if tp is not None and close is not None:
        if _near(close, tp, tolerance_price):
            return "tp"
        if side == "BUY" and close >= tp - tolerance_price:
            return "tp"
        if side == "SELL" and close <= tp + tolerance_price:
            return "tp"

    is_order2 = position.order_leg == "o2"
    near_entry_close = entry is not None and close is not None and _near(close, entry, tolerance_price)
    near_entry_sl = entry is not None and sl is not None and _near(sl, entry, tolerance_price)
    scratch_pnl = profit <= tolerance_profit and profit >= -tolerance_profit
    slightly_negative_at_entry = profit < 0 and profit >= -(tolerance_profit * 2) and near_entry_close
    is_be = is_order2 and (near_entry_sl or near_entry_close or slightly_negative_at_entry or (scratch_pnl and near_entry_close))
    if is_be:
        return "be"

    if sl is not None and close is not None:
        if _near(close, sl, tolerance_price):
            return "sl"
        true_sl_buy = side == "BUY" and close <= sl + tolerance_price and profit < -tolerance_profit
        true_sl_sell = side == "SELL" and close >= sl - tolerance_price and profit < -tolerance_profit
        if true_sl_buy or true_sl_sell:
            return "sl"

    if profit > 0:
        return "manual_win"
    if profit < 0:
        return "manual_loss"
    return "review_required"


def group_positions_to_setups(positions: list[TradePosition], tolerance_price: float = 0.0005) -> list[TradeSetup]:
    grouped: dict[str, list[TradePosition]] = {}
    ungrouped_count = 0
    for position in positions:
        key = position.setup_id
        if key is None:
            ungrouped_count += 1
            key = f"review-{position.position_ticket or ungrouped_count}"
        grouped.setdefault(key, []).append(position)

    setups: list[TradeSetup] = []
    for setup_id, group in grouped.items():
        sorted_group = sorted(group, key=lambda item: (_datetime_sort_key(item.entry_time_raw), item.order_leg or ""))
        order1 = _select_leg(sorted_group, "o1")
        order2 = _select_leg(sorted_group, "o2")
        if order1 is None and sorted_group:
            order1 = sorted_group[0]
        if order2 is None and len(sorted_group) > 1:
            order2 = next((item for item in sorted_group if item is not order1), None)
        review_required_grouping = any(item.setup_id is None for item in sorted_group) or len(sorted_group) != 2

        entry_time = min((item.entry_time_raw for item in sorted_group if item.entry_time_raw), key=_datetime_sort_key, default="")
        close_time = max((item.close_time_raw for item in sorted_group if item.close_time_raw), key=_datetime_sort_key, default="")
        entry_prices = [item.entry_price for item in (order1, order2) if item is not None and item.entry_price is not None]
        entry_price = order1.entry_price if order1 and order1.entry_price is not None else _average(entry_prices)
        close_prices = [item.close_price for item in (order1, order2) if item is not None and item.close_price is not None]
        close_price = order2.close_price if order2 and order2.close_price is not None else (close_prices[-1] if close_prices else None)
        initial_sl = order1.initial_sl if order1 else (sorted_group[0].initial_sl if sorted_group else None)
        if order1 and order2 and order1.initial_sl is not None and order2.initial_sl is not None:
            order2_sl_is_be_move = (
                order2.result_order_level == "be"
                and order2.entry_price is not None
                and _near(order2.initial_sl, order2.entry_price, tolerance_price)
            )
            if not order2_sl_is_be_move and not _near(order1.initial_sl, order2.initial_sl, tolerance_price):
                review_required_grouping = True
        pnl_total = sum(item.profit for item in sorted_group)
        volume_total = sum(item.volume or 0 for item in sorted_group)
        order1_result = order1.result_order_level if order1 else "review_required"
        order2_result = order2.result_order_level if order2 else "review_required"
        setup_result = classify_setup_result(order1_result, order2_result, review_required_grouping)

        setups.append(
            TradeSetup(
                setup_id=setup_id,
                symbol=order1.symbol if order1 else sorted_group[0].symbol,
                side=order1.side if order1 else sorted_group[0].side,
                entry_time=entry_time,
                entry_price=entry_price,
                initial_sl=initial_sl,
                tp1=order1.initial_tp if order1 else None,
                tp2=order2.initial_tp if order2 else None,
                close_time=close_time,
                close_price=close_price,
                order1_ticket=order1.position_ticket if order1 else None,
                order2_ticket=order2.position_ticket if order2 else None,
                order1_result=order1_result,
                order2_result=order2_result,
                setup_result=setup_result,
                pnl_total=pnl_total,
                volume_total=volume_total,
                review_required_grouping=review_required_grouping,
                order1_entry_price=order1.entry_price if order1 else None,
                order2_entry_price=order2.entry_price if order2 else None,
                order1_close_price=order1.close_price if order1 else None,
                order2_close_price=order2.close_price if order2 else None,
                order1_close_time=order1.close_time_raw if order1 else None,
                order2_close_time=order2.close_time_raw if order2 else None,
                order1_profit=order1.profit if order1 else None,
                order2_profit=order2.profit if order2 else None,
            )
        )

    return sorted(setups, key=lambda item: _datetime_sort_key(item.entry_time))


def classify_setup_result(order1_result: str, order2_result: str, review_required_grouping: bool = False) -> str:
    if review_required_grouping:
        return "review_required"
    if order1_result == "tp" and order2_result == "tp":
        return "full_win"
    if order1_result == "tp" and order2_result == "be":
        return "managed_win"
    if order1_result == "sl" and order2_result == "sl":
        return "full_loss"
    if order1_result == "tp" and order2_result == "manual_win":
        return "partial_win"
    if order1_result in {"manual_win", "manual_loss"} or order2_result in {"manual_win", "manual_loss"}:
        return "manual_close"
    return "review_required"


def build_mt5_history_export(file_path: str, options: Mt5HistoryExportOptions | None = None) -> Mt5HistoryExportResult:
    resolved_options = options or Mt5HistoryExportOptions()
    parsed = parse_mt5_html_report(file_path)
    positions = parsed.positions
    if resolved_options.symbol_filter:
        symbol = resolved_options.symbol_filter.strip().upper()
        positions = [position for position in positions if position.symbol.upper() == symbol]
    if not positions:
        raise ValueError("No MT5 Positions rows were found for the selected report and symbol filter.")
    for position in positions:
        position.result_order_level = classify_position(
            position,
            tolerance_price=resolved_options.tolerance_price,
            tolerance_profit=resolved_options.tolerance_profit,
        )
    setups = group_positions_to_setups(positions, tolerance_price=resolved_options.tolerance_price)
    pine_code = generate_tradingview_pine_setups(setups, resolved_options)
    csv_content = generate_normalized_csv(setups)
    return Mt5HistoryExportResult(parsed=parsed, setups=setups, pine_code=pine_code, csv_content=csv_content)


def generate_normalized_csv(setups: list[TradeSetup]) -> str:
    output = StringIO()
    fieldnames = [
        "setup_id",
        "side",
        "entry_time",
        "entry_price",
        "sl",
        "tp1",
        "tp2",
        "close_time",
        "close_price",
        "order1_ticket",
        "order2_ticket",
        "order1_result",
        "order2_result",
        "setup_result",
        "pnl_total",
        "volume_total",
        "review_required_grouping",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for setup in setups:
        writer.writerow(
            {
                "setup_id": setup.setup_id,
                "side": setup.side,
                "entry_time": setup.entry_time,
                "entry_price": setup.entry_price,
                "sl": setup.initial_sl,
                "tp1": setup.tp1,
                "tp2": setup.tp2,
                "close_time": setup.close_time,
                "close_price": setup.close_price,
                "order1_ticket": setup.order1_ticket,
                "order2_ticket": setup.order2_ticket,
                "order1_result": setup.order1_result,
                "order2_result": setup.order2_result,
                "setup_result": setup.setup_result,
                "pnl_total": setup.pnl_total,
                "volume_total": setup.volume_total,
                "review_required_grouping": setup.review_required_grouping,
            }
        )
    return output.getvalue()


def generate_tradingview_pine_setups(setups: list[TradeSetup], options: Mt5HistoryExportOptions | None = None) -> str:
    resolved = options or Mt5HistoryExportOptions()
    order_rows = _setup_order_rows(setups)
    max_count = max(1, min(len(order_rows), 300))
    lines: list[str] = [
        "//@version=6",
        'indicator("MT5/DB History EXACT CLEAN V6 TimeFix - MT5 HTML", overlay=true, max_lines_count=500, max_labels_count=500)',
        "",
        'focusText = input.string("setup-225", "Focus setup/order/ticket. Empty = show last N positions")',
        f'showLastNWhenNoFocus = input.int({max(1, min(resolved.show_last_n, max_count))}, "If focus empty: show last N positions", minval=1, maxval={max_count})',
        'orderFilter = input.string("Both", "Order filter", options=["Both", "o1", "o2", "manual"])',
        'slTpSource = input.string("Initial order plan", "SL/TP source", options=["Initial order plan", "DB/MT5 final/current"])',
        f'timeShiftHours = input.int({resolved.time_shift_hours}, "Time shift hours: MT5 server -> TradingView", minval=-12, maxval=12)',
        'timeShiftMinutes = input.int(0, "Time shift minutes if needed", minval=-59, maxval=59)',
        "",
        'showEntryLine = input.bool(true, "Show exact ENTRY line")',
        'showSLLine = input.bool(true, "Show exact SL line")',
        'showTPLine = input.bool(true, "Show exact TP line")',
        'showCloseLine = input.bool(true, "Show exact CLOSE line")',
        'showMarkers = input.bool(true, "Show E/C markers")',
        'showTextLabels = input.bool(false, "Show full text labels")',
        'lineWidth = input.int(1, "Line width", minval=1, maxval=4)',
        "",
        _array_declaration("string", "ids", [_pine_str(row["id"]) for row in order_rows]),
        _array_declaration("string", "setupIds", [_pine_str(row["setup_id"]) for row in order_rows]),
        _array_declaration("string", "tickets", [_pine_str(row["ticket"]) for row in order_rows]),
        _array_declaration("string", "orderLegs", [_pine_str(row["order_leg"]) for row in order_rows]),
        _array_declaration("string", "sides", [_pine_str(row["side"]) for row in order_rows]),
        _array_declaration("int", "entryTimes", [_pine_time(row["entry_time"]) for row in order_rows]),
        _array_declaration("float", "entryPrices", [_pine_float(row["entry_price"]) for row in order_rows]),
        _array_declaration("float", "initialSLs", [_pine_float(row["initial_sl"]) for row in order_rows]),
        _array_declaration("float", "currentSLs", [_pine_float(row["current_sl"]) for row in order_rows]),
        _array_declaration("float", "initialTPs", [_pine_float(row["initial_tp"]) for row in order_rows]),
        _array_declaration("float", "currentTPs", [_pine_float(row["current_tp"]) for row in order_rows]),
        _array_declaration("int", "closeTimes", [_pine_time(row["close_time"]) for row in order_rows]),
        _array_declaration("float", "closePrices", [_pine_float(row["close_price"]) for row in order_rows]),
        _array_declaration("float", "profits", [_pine_float(row["profit"]) for row in order_rows]),
        _array_declaration("string", "results", [_pine_str(row["result"]) for row in order_rows]),
        "",
        "resultColor(string result) =>",
        "    result == \"TP\" or result == \"FULL WIN\" or result == \"TP1 + BE\" ? color.new(color.lime, 0) :",
        "     result == \"BE\" ? color.new(color.gray, 0) :",
        "     result == \"SL\" or result == \"FULL LOSS\" ? color.new(color.red, 0) :",
        "     result == \"MANUAL\" ? color.new(color.blue, 0) : color.new(color.orange, 0)",
        "",
        "var rendered = false",
        "if barstate.islast and not rendered",
        "    tradeCount = array.size(ids)",
        "    shiftMs = (timeShiftHours * 60 + timeShiftMinutes) * 60 * 1000",
        "    focus = str.lower(str.trim(focusText))",
        "    hasFocus = str.length(focus) > 0",
        "    startIndex = math.max(0, tradeCount - showLastNWhenNoFocus)",
        "    for i = 0 to tradeCount - 1",
        "        id = array.get(ids, i)",
        "        setupId = array.get(setupIds, i)",
        "        ticket = array.get(tickets, i)",
        "        leg = array.get(orderLegs, i)",
        "        side = array.get(sides, i)",
        "        entryTime = array.get(entryTimes, i)",
        "        closeTime = array.get(closeTimes, i)",
        "        entryPrice = array.get(entryPrices, i)",
        "        initialSL = array.get(initialSLs, i)",
        "        currentSL = array.get(currentSLs, i)",
        "        initialTP = array.get(initialTPs, i)",
        "        currentTP = array.get(currentTPs, i)",
        "        closePrice = array.get(closePrices, i)",
        "        profit = array.get(profits, i)",
        "        result = array.get(results, i)",
        "        orderAllowed = orderFilter == \"Both\" or leg == orderFilter",
        "        focusAllowed = not hasFocus or str.contains(str.lower(id), focus) or str.contains(str.lower(setupId), focus) or str.contains(str.lower(ticket), focus)",
        "        rangeAllowed = hasFocus or i >= startIndex",
        "        if orderAllowed and focusAllowed and rangeAllowed and not na(entryTime) and not na(entryPrice)",
        "            entryTimeShifted = entryTime + shiftMs",
        "            closeTimeShifted = na(closeTime) ? na : closeTime + shiftMs",
        "            rightTime = na(closeTimeShifted) ? entryTimeShifted + 60 * 60 * 1000 : closeTimeShifted",
        "            selectedSL = slTpSource == \"Initial order plan\" ? initialSL : currentSL",
        "            selectedTP = slTpSource == \"Initial order plan\" ? initialTP : currentTP",
        "            markerColor = resultColor(result)",
        "            if showEntryLine",
        "                line.new(entryTimeShifted, entryPrice, rightTime, entryPrice, xloc=xloc.bar_time, color=color.new(color.yellow, 0), width=lineWidth)",
        "            if showSLLine and not na(selectedSL)",
        "                line.new(entryTimeShifted, selectedSL, rightTime, selectedSL, xloc=xloc.bar_time, color=color.new(color.red, 0), width=lineWidth)",
        "            if showTPLine and not na(selectedTP)",
        "                line.new(entryTimeShifted, selectedTP, rightTime, selectedTP, xloc=xloc.bar_time, color=color.new(color.lime, 0), width=lineWidth)",
        "            if showCloseLine and not na(closeTimeShifted) and not na(closePrice)",
        "                line.new(closeTimeShifted, closePrice, closeTimeShifted + 30 * 60 * 1000, closePrice, xloc=xloc.bar_time, color=markerColor, width=lineWidth)",
        "            if showMarkers",
        "                label.new(entryTimeShifted, entryPrice, text=\"E\", xloc=xloc.bar_time, style=side == \"BUY\" ? label.style_label_up : label.style_label_down, color=color.new(color.yellow, 0), textcolor=color.black, size=size.tiny)",
        "                if not na(closeTimeShifted) and not na(closePrice)",
        "                    label.new(closeTimeShifted, closePrice, text=result, xloc=xloc.bar_time, style=label.style_label_left, color=markerColor, textcolor=color.white, size=size.tiny)",
        "            if showTextLabels",
        "                detail = id + \" \" + side + \"\\nEntry: \" + str.tostring(entryPrice) + \"\\nSL: \" + str.tostring(selectedSL) + \"\\nTP: \" + str.tostring(selectedTP) + \"\\nClose: \" + str.tostring(closePrice) + \"\\nP/L: \" + str.tostring(profit)",
        "                label.new(rightTime, entryPrice, text=detail, xloc=xloc.bar_time, style=label.style_label_left, color=color.new(color.black, 10), textcolor=color.white, size=size.small)",
        "    rendered := true",
        "",
        "plot(na)",
    ]
    return "\n".join(lines) + "\n"


def _find_positions_table(tables: list[list[list[str]]]) -> tuple[list[list[str]] | None, int | None]:
    for table in tables:
        for index, row in enumerate(table):
            normalized = [_normalize_header(cell) for cell in row]
            if {"time", "position", "symbol", "type", "volume"}.issubset(set(normalized)):
                if "close_time" in normalized or "profit" in normalized:
                    return table, index
    return None, None


def _setup_order_rows(setups: list[TradeSetup]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for setup in setups:
        if setup.order1_ticket:
            rows.append(
                {
                    "id": f"{setup.setup_id}-o1",
                    "setup_id": setup.setup_id,
                    "ticket": setup.order1_ticket,
                    "order_leg": "o1",
                    "side": setup.side,
                    "entry_time": setup.entry_time,
                    "entry_price": setup.order1_entry_price if setup.order1_entry_price is not None else setup.entry_price,
                    "initial_sl": setup.initial_sl,
                    "current_sl": setup.initial_sl,
                    "initial_tp": setup.tp1,
                    "current_tp": setup.tp1,
                    "close_time": setup.order1_close_time or setup.close_time,
                    "close_price": setup.order1_close_price,
                    "profit": setup.order1_profit,
                    "result": _short_result(setup.order1_result, setup.setup_result),
                }
            )
        if setup.order2_ticket:
            current_sl = setup.order2_entry_price if setup.order2_result == "be" and setup.order2_entry_price is not None else setup.initial_sl
            rows.append(
                {
                    "id": f"{setup.setup_id}-o2",
                    "setup_id": setup.setup_id,
                    "ticket": setup.order2_ticket,
                    "order_leg": "o2",
                    "side": setup.side,
                    "entry_time": setup.entry_time,
                    "entry_price": setup.order2_entry_price if setup.order2_entry_price is not None else setup.entry_price,
                    "initial_sl": setup.initial_sl,
                    "current_sl": current_sl,
                    "initial_tp": setup.tp2,
                    "current_tp": setup.tp2,
                    "close_time": setup.order2_close_time or setup.close_time,
                    "close_price": setup.order2_close_price,
                    "profit": setup.order2_profit,
                    "result": _short_result(setup.order2_result, setup.setup_result),
                }
            )
        if not setup.order1_ticket and not setup.order2_ticket:
            rows.append(
                {
                    "id": f"pos-{setup.setup_id}",
                    "setup_id": setup.setup_id,
                    "ticket": setup.setup_id,
                    "order_leg": "manual",
                    "side": setup.side,
                    "entry_time": setup.entry_time,
                    "entry_price": setup.entry_price,
                    "initial_sl": setup.initial_sl,
                    "current_sl": setup.initial_sl,
                    "initial_tp": setup.tp1,
                    "current_tp": setup.tp1,
                    "close_time": setup.close_time,
                    "close_price": setup.close_price,
                    "profit": setup.pnl_total,
                    "result": _short_result("manual", setup.setup_result),
                }
            )
    return rows


def _short_result(order_result: str | None, setup_result: str | None = None) -> str:
    value = (order_result or setup_result or "").lower()
    if value == "tp":
        return "TP"
    if value == "be":
        return "BE"
    if value == "sl":
        return "SL"
    if value == "full_win":
        return "FULL WIN"
    if value == "managed_win":
        return "TP1 + BE"
    if value == "full_loss":
        return "FULL LOSS"
    if value.startswith("manual") or value == "partial_win":
        return "MANUAL"
    return "REVIEW"


def _normalize_position_headers(row: list[str]) -> list[str]:
    headers: list[str] = []
    seen_price = 0
    for cell in row:
        header = _normalize_header(cell)
        if header == "price":
            seen_price += 1
            header = "entry_price" if seen_price == 1 else "close_price"
        headers.append(header)
    return headers


def _row_to_position(
    *,
    row: list[str],
    headers: list[str],
    account_name: str | None,
    account_number: str | None,
    broker_company: str | None,
    report_date: str | None,
) -> TradePosition | None:
    data = {headers[index]: row[index] if index < len(row) else "" for index in range(len(headers))}
    side = (data.get("type") or "").strip().upper()
    if side not in {"BUY", "SELL"}:
        return None
    symbol = (data.get("symbol") or "").strip().upper()
    ticket = (data.get("position") or "").strip()
    if not symbol or not ticket:
        return None
    raw_comment = data.get("comment") or None
    setup_id, order_leg = extract_setup_reference(raw_comment or "")
    position = TradePosition(
        account_name=account_name,
        account_number=account_number,
        broker_company=broker_company,
        report_date=report_date,
        symbol=symbol,
        position_ticket=ticket,
        setup_id=setup_id,
        order_leg=order_leg,
        side=side,
        volume=_parse_number(data.get("volume")),
        entry_time_raw=data.get("time") or "",
        entry_price=_parse_number(data.get("entry_price")),
        initial_sl=_parse_number(data.get("s_l")),
        initial_tp=_parse_number(data.get("t_p")),
        close_time_raw=data.get("close_time") or "",
        close_price=_parse_number(data.get("close_price")),
        commission=_parse_number(data.get("commission")) or 0.0,
        swap=_parse_number(data.get("swap")) or 0.0,
        profit=_parse_number(data.get("profit")) or 0.0,
        raw_comment=raw_comment,
        result_order_level="review_required",
    )
    position.result_order_level = classify_position(position, tolerance_price=0.0005, tolerance_profit=1.0)
    return position


def extract_setup_reference(comment: str) -> tuple[str | None, str | None]:
    match = SETUP_COMMENT_RE.search(comment or "")
    if not match:
        return None, None
    return match.group(1).lower(), match.group(2).lower()


def _normalize_header(value: str) -> str:
    text = _clean_text(value).lower().replace("/", "_")
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    replacements = {
        "s_l": "s_l",
        "sl": "s_l",
        "t_p": "t_p",
        "tp": "t_p",
        "close_time": "close_time",
        "close_price": "close_price",
        "commission": "commission",
        "swap": "swap",
        "profit": "profit",
        "comment": "comment",
    }
    return replacements.get(text, text)


def _extract_metadata(content: str, keys: tuple[str, ...]) -> str | None:
    plain = re.sub(r"<[^>]+>", " ", content)
    plain = _clean_text(plain)
    for key in keys:
        pattern = rf"{re.escape(key)}\s*[:#]?\s*([A-Za-z0-9 ._@/-]+)"
        match = re.search(pattern, plain, re.IGNORECASE)
        if match:
            return match.group(1).strip()[:120]
    return None


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip()


def _parse_number(value: str | None) -> float | None:
    if value is None:
        return None
    cleaned = _clean_text(value).replace(" ", "")
    if not cleaned or cleaned in {"-", "--"}:
        return None
    cleaned = cleaned.replace(",", "")
    match = re.search(r"[-+]?\d+(?:\.\d+)?", cleaned)
    if not match:
        return None
    return float(match.group(0))


def _near(left: float, right: float, tolerance: float) -> bool:
    return abs(left - right) <= tolerance


def _average(values: list[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    if not present:
        return None
    return sum(present) / len(present)


def _select_leg(positions: list[TradePosition], leg: str) -> TradePosition | None:
    return next((position for position in positions if position.order_leg == leg), None)


def _datetime_sort_key(value: str | None) -> datetime:
    parsed = _parse_mt5_datetime(value)
    return parsed or datetime.max.replace(tzinfo=timezone.utc)


def _parse_mt5_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    text = _clean_text(value)
    for fmt in ("%Y.%m.%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y.%m.%d %H:%M", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _pine_time(value: str | None) -> str:
    parsed = _parse_mt5_datetime(value)
    if parsed is None:
        return "na"
    return str(int(parsed.timestamp() * 1000))


def _pine_float(value: float | None) -> str:
    if value is None:
        return "na"
    return f"{float(value):.6f}"


def _pine_str(value: str) -> str:
    return f'"{_pine_escape(value)}"'


def _pine_escape(value: str) -> str:
    return (value or "").replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _array_declaration(pine_type: str, name: str, values: list[str]) -> str:
    if not values:
        return f"var {pine_type}[] {name} = array.new_{pine_type}()"
    return f"var {pine_type}[] {name} = array.from({', '.join(values)})"
