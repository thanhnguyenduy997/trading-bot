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
    lines: list[str] = [
        "//@version=6",
        'indicator("MT5 History Setup Position Tool", overlay=true, max_boxes_count=500, max_lines_count=500, max_labels_count=500)',
        "",
        f'focusSetupId = input.string("", "Focus setup id")',
        f'showLastN = input.int({max(1, min(resolved.show_last_n, 150))}, "Show last N setups", minval=1, maxval=150)',
        f'timeShiftHours = input.int({resolved.time_shift_hours}, "Time shift hours: MT5 server -> TradingView")',
        f'uiMode = input.string("{_pine_escape(resolved.ui_mode)}", "UI mode", options=["compact", "position_tool_style", "detailed_focus"])',
        'showFullWin = input.bool(true, "Show FULL WIN")',
        'showManagedWin = input.bool(true, "Show TP1 + BE")',
        'showFullLoss = input.bool(true, "Show FULL LOSS")',
        'showManual = input.bool(true, "Show MANUAL")',
        'showReview = input.bool(true, "Show REVIEW")',
        "",
        _array_declaration("string", "setupIds", [_pine_str(setup.setup_id) for setup in setups]),
        _array_declaration("string", "sides", [_pine_str(setup.side) for setup in setups]),
        _array_declaration("int", "entryTimes", [_pine_time(setup.entry_time) for setup in setups]),
        _array_declaration("float", "entryPrices", [_pine_float(setup.entry_price) for setup in setups]),
        _array_declaration("float", "slPrices", [_pine_float(setup.initial_sl) for setup in setups]),
        _array_declaration("float", "tp1Prices", [_pine_float(setup.tp1) for setup in setups]),
        _array_declaration("float", "tp2Prices", [_pine_float(setup.tp2) for setup in setups]),
        _array_declaration("int", "closeTimes", [_pine_time(setup.close_time) for setup in setups]),
        _array_declaration("float", "closePrices", [_pine_float(setup.close_price) for setup in setups]),
        _array_declaration("string", "order1Results", [_pine_str(setup.order1_result) for setup in setups]),
        _array_declaration("string", "order2Results", [_pine_str(setup.order2_result) for setup in setups]),
        _array_declaration("string", "setupResults", [_pine_str(setup.setup_result) for setup in setups]),
        _array_declaration("float", "pnlTotals", [_pine_float(setup.pnl_total) for setup in setups]),
        _array_declaration("string", "order1Tickets", [_pine_str(setup.order1_ticket or "") for setup in setups]),
        _array_declaration("string", "order2Tickets", [_pine_str(setup.order2_ticket or "") for setup in setups]),
        "",
        "shiftTime(int rawTime) =>",
        "    na(rawTime) ? na : rawTime + timeShiftHours * 60 * 60 * 1000",
        "",
        "resultLabel(string result) =>",
        "    switch result",
        '        "full_win" => "FULL WIN"',
        '        "managed_win" => "TP1 + BE"',
        '        "full_loss" => "FULL LOSS"',
        '        "partial_win" => "PARTIAL WIN"',
        '        "manual_close" => "MANUAL"',
        '        => "REVIEW"',
        "",
        "resultShort(string result) =>",
        "    switch result",
        '        "full_win" => "FW"',
        '        "managed_win" => "BE"',
        '        "full_loss" => "FL"',
        '        "partial_win" => "PW"',
        '        "manual_close" => "MAN"',
        '        => "REV"',
        "",
        "resultColor(string result) =>",
        "    switch result",
        '        "full_win" => color.new(color.lime, 0)',
        '        "managed_win" => color.new(color.teal, 0)',
        '        "full_loss" => color.new(color.red, 0)',
        '        "partial_win" => color.new(color.green, 15)',
        '        "manual_close" => color.new(color.blue, 15)',
        '        => color.new(color.orange, 0)',
        "",
        "resultAllowed(string result) =>",
        '    result == "full_win" ? showFullWin : result == "managed_win" ? showManagedWin : result == "full_loss" ? showFullLoss : result == "manual_close" or result == "partial_win" ? showManual : showReview',
        "",
        "var rendered = false",
        "if barstate.islast and not rendered",
        "    tradeCount = array.size(setupIds)",
        "    visibleCount = math.min(showLastN, tradeCount)",
        "    startIndex = math.max(0, tradeCount - visibleCount)",
        '    hasFocus = str.length(str.trim(focusSetupId)) > 0',
        '    compactLabels = not hasFocus and showLastN > 10',
        "    for i = startIndex to tradeCount - 1",
        "        setupId = array.get(setupIds, i)",
        "        shouldRender = not hasFocus or setupId == focusSetupId",
        "        if shouldRender",
        "            side = str.upper(array.get(sides, i))",
        "            entryTime = shiftTime(array.get(entryTimes, i))",
        "            closeTimeRaw = shiftTime(array.get(closeTimes, i))",
        "            entryPrice = array.get(entryPrices, i)",
        "            sl = array.get(slPrices, i)",
        "            tp1 = array.get(tp1Prices, i)",
        "            tp2 = array.get(tp2Prices, i)",
        "            closePrice = array.get(closePrices, i)",
        "            result = array.get(setupResults, i)",
        "            if resultAllowed(result) and not na(entryTime) and not na(entryPrice)",
        "                closeTime = na(closeTimeRaw) ? entryTime + 4 * 60 * 60 * 1000 : closeTimeRaw",
        "                target = not na(tp2) ? tp2 : tp1",
        '                isBuy = side == "BUY"',
        "                rewardTop = isBuy ? target : entryPrice",
        "                rewardBottom = isBuy ? entryPrice : target",
        "                riskTop = isBuy ? entryPrice : sl",
        "                riskBottom = isBuy ? sl : entryPrice",
        "                if uiMode != \"compact\" and not na(target)",
        "                    box.new(entryTime, rewardTop, closeTime, rewardBottom, xloc=xloc.bar_time, bgcolor=color.new(color.green, 87), border_color=color.new(color.green, 70))",
        "                if uiMode != \"compact\" and not na(sl)",
        "                    box.new(entryTime, riskTop, closeTime, riskBottom, xloc=xloc.bar_time, bgcolor=color.new(color.red, 86), border_color=color.new(color.red, 70))",
        "                line.new(entryTime, entryPrice, closeTime, entryPrice, xloc=xloc.bar_time, color=color.new(color.yellow, 0), width=1)",
        "                if not na(tp1)",
        "                    line.new(entryTime, tp1, closeTime, tp1, xloc=xloc.bar_time, color=color.new(color.green, 25), width=1, style=line.style_dotted)",
        "                if not na(tp2)",
        "                    line.new(entryTime, tp2, closeTime, tp2, xloc=xloc.bar_time, color=color.new(color.teal, 20), width=1, style=line.style_dotted)",
        "                badgePrice = na(closePrice) ? entryPrice : closePrice",
        "                badgeText = compactLabels ? resultShort(result) : resultLabel(result)",
        "                label.new(closeTime, badgePrice, text=badgeText, xloc=xloc.bar_time, style=label.style_label_left, color=resultColor(result), textcolor=color.white, size=size.small)",
        "                if hasFocus or uiMode == \"detailed_focus\"",
        "                    details = setupId + \"\\n\" + side + \"\\nEntry: \" + str.tostring(entryPrice) + \"\\nSL: \" + str.tostring(sl) + \"\\nTP1: \" + str.tostring(tp1) + \"\\nTP2: \" + str.tostring(tp2) + \"\\nClose: \" + str.tostring(closePrice) + \"\\nP/L: \" + str.tostring(array.get(pnlTotals, i)) + \"\\nO1: \" + array.get(order1Results, i) + \"\\nO2: \" + array.get(order2Results, i)",
        "                    label.new(closeTime, badgePrice, text=details, xloc=xloc.bar_time, style=label.style_label_lower_left, color=color.new(color.black, 10), textcolor=color.white, size=size.small)",
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
