from app.services.mt5_history_pine_export import (
    Mt5HistoryExportOptions,
    TradePosition,
    classify_position,
    classify_setup_result,
    generate_tradingview_pine_setups,
    group_positions_to_setups,
    parse_mt5_html_report,
)


def _position(
    *,
    ticket: str,
    comment: str,
    side: str = "BUY",
    entry: float = 100.0,
    sl: float = 99.0,
    tp: float = 101.0,
    close: float = 101.0,
    profit: float = 10.0,
) -> TradePosition:
    return TradePosition(
        account_name="Test",
        account_number="123",
        broker_company="Demo Broker",
        report_date="2026.04.10",
        symbol="XAUUSD",
        position_ticket=ticket,
        setup_id=None,
        order_leg=None,
        side=side,
        volume=0.1,
        entry_time_raw="2026.04.10 08:00:00",
        entry_price=entry,
        initial_sl=sl,
        initial_tp=tp,
        close_time_raw="2026.04.10 09:00:00",
        close_price=close,
        commission=0.0,
        swap=0.0,
        profit=profit,
        raw_comment=comment,
        result_order_level="review_required",
    )


def test_parse_mt5_positions_table_and_extract_setup_reference(tmp_path):
    report = tmp_path / "report.html"
    report.write_text(
        """
        <html><body>
        <table>
            <tr><td>Name:</td><td>Tester</td></tr>
        </table>
        <table>
            <tr>
                <th>Time</th><th>Position</th><th>Symbol</th><th>Type</th><th>Volume</th>
                <th>Price</th><th>S / L</th><th>T / P</th><th>Close Time</th>
                <th>Price</th><th>Commission</th><th>Swap</th><th>Profit</th><th>Comment</th>
            </tr>
            <tr>
                <td>2026.04.10 08:00:00</td><td>90001</td><td>XAUUSD</td><td>buy</td><td>0.10</td>
                <td>2320.20</td><td>2319.20</td><td>2321.20</td><td>2026.04.10 08:30:00</td>
                <td>2321.20</td><td>-0.10</td><td>0.00</td><td>49.90</td><td>setup-225-o1</td>
            </tr>
            <tr>
                <td>2026.04.10 08:00:01</td><td>90002</td><td>XAUUSD</td><td>buy</td><td>0.10</td>
                <td>2320.22</td><td>2320.22</td><td>2322.20</td><td>2026.04.10 09:15:00</td>
                <td>2320.21</td><td>-0.10</td><td>0.00</td><td>-0.30</td><td>setup-225-o2</td>
            </tr>
        </table>
        </body></html>
        """,
        encoding="utf-8",
    )

    parsed = parse_mt5_html_report(str(report))

    assert len(parsed.positions) == 2
    assert parsed.positions[0].setup_id == "setup-225"
    assert parsed.positions[0].order_leg == "o1"
    assert parsed.positions[1].setup_id == "setup-225"
    assert parsed.positions[1].order_leg == "o2"


def test_classify_order1_tp_when_close_is_near_tp():
    position = _position(ticket="1", comment="setup-225-o1", close=100.9998, tp=101.0)
    position.setup_id = "setup-225"
    position.order_leg = "o1"

    assert classify_position(position, tolerance_price=0.001, tolerance_profit=1.0) == "tp"


def test_classify_order2_be_when_sl_equals_entry_and_profit_slightly_negative():
    position = _position(ticket="2", comment="setup-225-o2", sl=100.0, close=99.9998, profit=-0.4)
    position.setup_id = "setup-225"
    position.order_leg = "o2"

    assert classify_position(position, tolerance_price=0.001, tolerance_profit=1.0) == "be"


def test_does_not_classify_be_as_sl_just_because_profit_is_negative():
    position = _position(ticket="2", comment="setup-225-o2", sl=100.0, close=100.0001, profit=-0.8)
    position.setup_id = "setup-225"
    position.order_leg = "o2"

    assert classify_position(position, tolerance_price=0.001, tolerance_profit=1.0) == "be"


def test_classify_true_sl_when_close_is_near_original_sl_and_not_be():
    position = _position(ticket="3", comment="setup-225-o1", sl=99.0, close=99.0002, profit=-10.0)
    position.setup_id = "setup-225"
    position.order_leg = "o1"

    assert classify_position(position, tolerance_price=0.001, tolerance_profit=1.0) == "sl"


def test_group_o1_and_o2_into_managed_win_setup():
    order1 = _position(ticket="1", comment="setup-225-o1", tp=101.0, close=101.0, profit=50.0)
    order2 = _position(ticket="2", comment="setup-225-o2", sl=100.0, tp=102.0, close=100.0, profit=-0.3)
    for position in (order1, order2):
        position.setup_id = "setup-225"
    order1.order_leg = "o1"
    order2.order_leg = "o2"
    order1.result_order_level = classify_position(order1, 0.001, 1.0)
    order2.result_order_level = classify_position(order2, 0.001, 1.0)

    setups = group_positions_to_setups([order1, order2], tolerance_price=0.001)

    assert len(setups) == 1
    assert setups[0].order1_ticket == "1"
    assert setups[0].order2_ticket == "2"
    assert setups[0].setup_result == "managed_win"


def test_setup_result_rules_full_win_full_loss_and_managed_win():
    assert classify_setup_result("tp", "be") == "managed_win"
    assert classify_setup_result("tp", "tp") == "full_win"
    assert classify_setup_result("sl", "sl") == "full_loss"


def test_generated_pine_has_required_inputs_xloc_and_no_broken_multiline_literals():
    order1 = _position(ticket="1", comment="setup-225-o1", tp=101.0, close=101.0, profit=50.0)
    order2 = _position(ticket="2", comment="setup-225-o2", sl=100.0, tp=102.0, close=100.0, profit=-0.3)
    for position in (order1, order2):
        position.setup_id = "setup-225"
    order1.order_leg = "o1"
    order2.order_leg = "o2"
    order1.result_order_level = "tp"
    order2.result_order_level = "be"
    setup = group_positions_to_setups([order1, order2], tolerance_price=0.001)[0]

    pine = generate_tradingview_pine_setups([setup], Mt5HistoryExportOptions())

    assert pine.startswith("//@version=6")
    assert "xloc=xloc.bar_time" in pine
    assert "focusSetupId = input.string" in pine
    assert "timeShiftHours = input.int" in pine
    assert 'var string[] setupIds = array.from("setup-225")' in pine
    assert '"\\nEntry: "' in pine
    assert 'details = setupId + "\\n"' in pine
