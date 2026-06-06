from datetime import date, datetime, timedelta, timezone

from app.models.mt5_trade_history import MT5TradeHistory
from app.models.trade_event import TradeEvent
from app.models.trade_setup import TradeSetup
from app.schemas.trade_setup import TradeSetupCreate
from app.schemas.trading_account import TradingAccountCreate
from app.schemas.user import UserCreate
from app.services.trade_setups import create_trade_setup
from app.services.trading_accounts import create_trading_account
from app.services.tradingview_export import TradingViewExportFilters, TradingViewExportService
from app.services.users import create_user


def _login(client, email: str, password: str = "password123"):
    return client.post("/api/auth/login", data={"email": email, "password": password}, follow_redirects=False)


def _create_admin(db_session):
    return create_user(
        db_session,
        UserCreate(email="admin-export@example.com", password="password123", full_name="Admin Export", role="admin", is_active=True),
    )


def _create_account(db_session, user, account_number: str):
    return create_trading_account(
        db_session,
        user.id,
        TradingAccountCreate(
            broker_name="Demo Broker",
            account_number=account_number,
            server_name="demo-server",
            password="secret-pass",
        ),
    )


def _create_setup(db_session, user, account, *, symbol: str, side: str, executed_at: datetime, outcome: str):
    setup = create_trade_setup(
        db_session,
        user.id,
        TradeSetupCreate(
            trading_account_id=account.id,
            symbol=symbol,
            side=side,
            sl_price=2319.2,
            risk_mode="fixed_money",
            risk_value=100,
            rr_order2=2,
            estimated_entry=2320.2,
            r_value=1.0,
            tp1_price=2321.2,
            tp2_price=2322.2,
            total_risk_money=100.0,
            risk_per_order=50.0,
            order1_volume=0.5,
            order2_volume=0.5,
            status="executed",
        ),
    )
    setup.executed_at = executed_at
    setup.order1_ticket = 90001 + setup.id
    setup.order2_ticket = 91001 + setup.id
    setup.order1_closed_at = executed_at.replace(hour=executed_at.hour + 1)
    setup.order2_closed_at = executed_at.replace(hour=executed_at.hour + 2)
    setup.order1_close_price = 2321.2
    setup.order2_close_price = 2322.2
    setup.order1_realized_pnl = 50.0
    setup.order2_realized_pnl = -0.2 if outcome == "closed_at_be" else 100.0
    setup.setup_outcome = outcome
    setup.setup_outcome_recorded_at = setup.order2_closed_at
    if outcome == "closed_at_be":
        setup.order2_outcome = "closed_at_be"
    db_session.add(setup)
    db_session.commit()
    db_session.refresh(setup)
    return setup


def test_export_defaults_to_last_two_month_range(db_session, created_user):
    account = _create_account(db_session, created_user, "TV-001")
    now = datetime(2026, 5, 28, 12, tzinfo=timezone.utc)
    _create_setup(db_session, created_user, account, symbol="XAUUSD", side="buy", executed_at=now.replace(day=15), outcome="full_win")

    result = TradingViewExportService(db_session, now_provider=lambda: now).build_export(
        TradingViewExportFilters(symbol="XAUUSD")
    )

    assert result.range_start.date() == date(2026, 3, 29)
    assert result.range_end.date() == date(2026, 5, 28)
    assert result.exported_trades == 1


def test_export_includes_unknown_outcome_and_open_trade_without_close(db_session, created_user):
    account_a = _create_account(db_session, created_user, "TV-002")
    setup = _create_setup(
        db_session,
        created_user,
        account_a,
        symbol="XAUUSD",
        side="buy",
        executed_at=datetime(2026, 4, 10, 8, tzinfo=timezone.utc),
        outcome="managed_win",
    )
    setup.setup_outcome = None
    setup.order1_closed_at = None
    setup.order2_closed_at = None
    setup.order1_close_price = None
    setup.order2_close_price = None
    db_session.commit()

    result = TradingViewExportService(db_session).build_export(
        TradingViewExportFilters(
            account_id=account_a.id,
            symbol="XAUUSD",
            start_date=date(2026, 4, 1),
            end_date=date(2026, 4, 30),
        )
    )

    assert result.exported_trades == 1
    assert "var int[] closeTimesRaw = array.from(na, na)" in result.pine_code
    assert "var float[] closePrices = array.from(na, na)" in result.pine_code


def test_export_includes_entry_inside_range_and_close_inside_range(db_session, created_user):
    account = _create_account(db_session, created_user, "TV-004")
    row1 = _create_setup(
        db_session,
        created_user,
        account,
        symbol="XAUUSD",
        side="buy",
        executed_at=datetime(2026, 4, 10, 8, tzinfo=timezone.utc),
        outcome="full_win",
    )
    row2 = _create_setup(
        db_session,
        created_user,
        account,
        symbol="XAUUSD",
        side="sell",
        executed_at=datetime(2026, 3, 25, 8, tzinfo=timezone.utc),
        outcome="managed_win",
    )
    row2.order2_closed_at = datetime(2026, 4, 2, 9, tzinfo=timezone.utc)
    row2.setup_outcome_recorded_at = row2.order2_closed_at
    db_session.add_all([row1, row2])
    db_session.commit()

    result = TradingViewExportService(db_session).build_export(
        TradingViewExportFilters(
            account_id=account.id,
            symbol="XAUUSD",
            start_date=date(2026, 4, 1),
            end_date=date(2026, 4, 30),
        )
    )
    assert result.exported_trades == 2


def test_db_timefix_export_uses_order_level_history_without_double_shift(db_session, created_user):
    account = _create_account(db_session, created_user, "TV-225")
    setup = _create_setup(
        db_session,
        created_user,
        account,
        symbol="XAUUSD",
        side="sell",
        executed_at=datetime(2026, 6, 5, 16, 50, 21, tzinfo=timezone(timedelta(hours=7))),
        outcome="managed_win",
    )
    setup.sl_price = 4469.50
    setup.estimated_entry = 4464.84
    setup.tp1_price = 4460.22
    setup.tp2_price = 4450.94
    setup.order1_close_price = 4459.15
    setup.order2_close_price = 4465.46
    setup.order1_closed_at = datetime(2026, 6, 5, 18, 24, 15, tzinfo=timezone(timedelta(hours=7)))
    setup.order2_closed_at = datetime(2026, 6, 5, 17, 0, 1, tzinfo=timezone(timedelta(hours=7)))
    setup.order1_outcome = "tp_hit"
    setup.order2_outcome = "closed_at_be"
    db_session.add_all(
        [
            setup,
            MT5TradeHistory(
                user_id=created_user.id,
                trading_account_id=account.id,
                linked_setup_id=setup.id,
                position_ticket=setup.order1_ticket,
                symbol="XAUUSD",
                side="sell",
                trade_source="system",
                outcome="tp",
                volume=0.5,
                open_price=4464.84,
                close_price=4459.15,
                realized_pnl=50.0,
                open_time=datetime(2026, 6, 5, 16, 50, 20, tzinfo=timezone(timedelta(hours=7))),
                close_time=datetime(2026, 6, 5, 18, 24, 15, tzinfo=timezone(timedelta(hours=7))),
                comment=f"setup-{setup.id}-o1",
            ),
            MT5TradeHistory(
                user_id=created_user.id,
                trading_account_id=account.id,
                linked_setup_id=setup.id,
                position_ticket=setup.order2_ticket,
                symbol="XAUUSD",
                side="sell",
                trade_source="system",
                outcome="be",
                volume=0.5,
                open_price=4464.84,
                close_price=4465.46,
                realized_pnl=-0.2,
                open_time=datetime(2026, 6, 5, 16, 50, 21, tzinfo=timezone(timedelta(hours=7))),
                close_time=datetime(2026, 6, 5, 17, 0, 1, tzinfo=timezone(timedelta(hours=7))),
                comment=f"setup-{setup.id}-o2",
            ),
        ]
    )
    db_session.commit()

    service = TradingViewExportService(db_session)
    export = service.build_export(
        TradingViewExportFilters(
            account_id=account.id,
            symbol="XAUUSD",
            start_date=date(2026, 6, 5),
            end_date=date(2026, 6, 5),
        )
    )
    histories = {
        trade.position_ticket: trade
        for trade in db_session.query(MT5TradeHistory)
        .filter(MT5TradeHistory.linked_setup_id == setup.id)
        .all()
    }
    o1_entry_ms = service.to_pine_timestamp_ms(histories[setup.order1_ticket].open_time)
    o2_entry_ms = service.to_pine_timestamp_ms(histories[setup.order2_ticket].open_time)
    o1_close_ms = service.to_pine_timestamp_ms(histories[setup.order1_ticket].close_time)
    o2_close_ms = service.to_pine_timestamp_ms(histories[setup.order2_ticket].close_time)
    pine = export.pine_code

    assert pine.startswith("//@version=6\nindicator(\"DB History EXACT CLEAN V6 TimeFix - ")
    assert "// CLEAN V6 TIMEFIX" in pine
    assert 'timeShiftHours = input.int(-3, "Time shift hours: DB time -> TradingView", minval=-12, maxval=12)' in pine
    assert 'debugTimeAudit = input.bool(false, "Debug: show time audit table")' in pine
    assert 'debugTimezone = input.string("GMT+7", "Debug display timezone")' in pine
    assert 'f_time_text(t) =>' in pine
    assert 'na(t) ? "na" : str.format_time(t, "yyyy-MM-dd HH:mm:ss", debugTimezone)' in pine
    assert 'var table timeAuditTable = table.new(position.top_right, 9,' in pine
    assert 'table.cell(timeAuditTable, 2, 0, "Entry raw"' in pine
    assert 'table.cell(timeAuditTable, 3, 0, "Entry shifted"' in pine
    assert 'table.cell(timeAuditTable, 4, 0, "Close raw"' in pine
    assert 'table.cell(timeAuditTable, 5, 0, "Close shifted"' in pine
    assert f'var string[] ids = array.from("setup-{setup.id}-o1", "setup-{setup.id}-o2")' in pine
    assert f'var string[] orderTags = array.from("o1", "o2")' in pine
    assert f"var int[] entryTimesRaw = array.from({o1_entry_ms}, {o2_entry_ms})" in pine
    assert f"var float[] entryPrices = array.from(4464.840000, 4464.840000)" in pine
    assert f"var float[] initialSLs = array.from(4469.500000, 4469.500000)" in pine
    assert f"var float[] initialTPs = array.from(4460.220000, 4450.940000)" in pine
    assert f"var int[] closeTimesRaw = array.from({o1_close_ms}, {o2_close_ms})" in pine
    assert f"var float[] closePrices = array.from(4459.150000, 4465.460000)" in pine
    assert f'var string[] results = array.from("win", "be")' in pine
    assert "int entryRaw = array.get(entryTimesRaw, i)" in pine
    assert "int closeRaw = array.get(closeTimesRaw, i)" in pine
    assert "int entryT = f_shift_time(entryRaw)" in pine
    assert "int closeT = f_shift_time(closeRaw)" in pine
    assert "table.cell(timeAuditTable, 2, auditRow, f_time_text(entryRaw))" in pine
    assert "table.cell(timeAuditTable, 3, auditRow, f_time_text(entryT))" in pine


def test_export_includes_setup_time_when_entry_time_missing(db_session, created_user):
    account = _create_account(db_session, created_user, "TV-005")
    setup = _create_setup(db_session, created_user, account, symbol="XAUUSD", side="buy", executed_at=datetime(2026, 4, 5, 8, tzinfo=timezone.utc), outcome="managed_win")
    setup.executed_at = None
    setup.created_at = datetime(2026, 4, 7, 8, tzinfo=timezone.utc)
    db_session.add(setup)
    db_session.commit()

    result = TradingViewExportService(db_session).build_export(
        TradingViewExportFilters(account_id=account.id, symbol="XAUUSD", start_date=date(2026, 4, 1), end_date=date(2026, 4, 30))
    )

    assert result.exported_trades == 1


def test_export_does_not_silently_drop_low_setup_ids(db_session, created_user):
    account = _create_account(db_session, created_user, "TV-006")
    created = []
    for _ in range(5):
        created.append(
            _create_setup(
                db_session,
                created_user,
                account,
                symbol="XAUUSD",
                side="buy",
                executed_at=datetime(2026, 4, 10, 8, tzinfo=timezone.utc),
                outcome="managed_win",
            )
        )
    export = TradingViewExportService(db_session).build_export(
        TradingViewExportFilters(account_id=account.id, symbol="XAUUSD", start_date=date(2026, 4, 1), end_date=date(2026, 4, 30), max_trades=500)
    )
    exported_ids = {row["setup_id"] for row in export.normalized_rows}
    assert {item.id for item in created}.issubset(exported_ids)


def test_timestamp_ms_and_bar_alignment_helpers(db_session, created_user):
    service = TradingViewExportService(db_session)
    base = datetime(2026, 4, 9, 9, 27, 13, tzinfo=timezone.utc)

    assert service.floor_to_timeframe_bar(base, "M5") == datetime(2026, 4, 9, 9, 25, 0, tzinfo=timezone.utc)
    assert service.floor_to_timeframe_bar(base, "M15") == datetime(2026, 4, 9, 9, 15, 0, tzinfo=timezone.utc)
    assert service.floor_to_timeframe_bar(base, "H1") == datetime(2026, 4, 9, 9, 0, 0, tzinfo=timezone.utc)
    expected_ms = int(base.timestamp() * 1000)
    assert service.to_pine_timestamp_ms(base) == expected_ms
    assert service.to_pine_timestamp_ms(datetime(2026, 4, 9, 9, 27, 13)) == expected_ms


def test_export_debug_payload_and_audit_summary(db_session, created_user):
    account = _create_account(db_session, created_user, "TV-007")
    setup = _create_setup(
        db_session,
        created_user,
        account,
        symbol="XAUUSD",
        side="buy",
        executed_at=datetime(2026, 4, 10, 8, tzinfo=timezone.utc),
        outcome="managed_win",
    )
    db_session.add(
        TradeEvent(
            user_id=created_user.id,
            setup_id=setup.id,
            event_type="tp1_hit",
            message="TP1 hit",
            created_at=datetime(2026, 4, 10, 8, 14, tzinfo=timezone.utc),
        )
    )
    db_session.add(
        TradeEvent(
            user_id=created_user.id,
            setup_id=setup.id,
            event_type="order2_sl_hit",
            message="SL hit",
            created_at=datetime(2026, 4, 10, 8, 49, tzinfo=timezone.utc),
        )
    )
    db_session.commit()

    result = TradingViewExportService(db_session).build_export(
        TradingViewExportFilters(account_id=account.id, symbol="XAUUSD", start_date=date(2026, 4, 1), end_date=date(2026, 4, 30), debug=True)
    )
    assert result.audit_summary["requested_symbol"] == "XAUUSD"
    assert result.audit_summary["raw_setup_count"] >= 1
    assert isinstance(result.skipped_records, list)
    assert isinstance(result.normalized_rows, list)
    assert "// CLEAN V6 TIMEFIX" in result.pine_code
    assert "timestamp(2026" not in result.pine_code
    assert "array.push(entryTimes" not in result.pine_code
    assert "var int[] entryTimesRaw = array.from(" in result.pine_code


def test_same_candle_ambiguity_sets_review_reason(db_session, created_user):
    account = _create_account(db_session, created_user, "TV-008")
    setup = _create_setup(
        db_session,
        created_user,
        account,
        symbol="XAUUSD",
        side="buy",
        executed_at=datetime(2026, 4, 10, 8, tzinfo=timezone.utc),
        outcome="review_required",
    )
    db_session.add_all(
        [
            TradeEvent(
                user_id=created_user.id,
                setup_id=setup.id,
                event_type="tp1_hit",
                message="TP1",
                created_at=datetime(2026, 4, 10, 8, 21, tzinfo=timezone.utc),
            ),
            TradeEvent(
                user_id=created_user.id,
                setup_id=setup.id,
                event_type="order2_sl_hit",
                message="SL",
                created_at=datetime(2026, 4, 10, 8, 24, tzinfo=timezone.utc),
            ),
        ]
    )
    db_session.commit()

    service = TradingViewExportService(db_session)
    export = service.build_export(
        TradingViewExportFilters(
            account_id=account.id,
            symbol="XAUUSD",
            start_date=date(2026, 4, 1),
            end_date=date(2026, 4, 30),
            review_timeframe="M5",
        )
    )
    assert "same_candle_ambiguity" not in export.pine_code
    assert any("same_candle_ambiguity" in warning for warning in export.warnings)


def test_max_trades_limit_warning_and_audit_flag(db_session, created_user):
    account = _create_account(db_session, created_user, "TV-010")
    for day in range(1, 6):
        _create_setup(
            db_session,
            created_user,
            account,
            symbol="XAUUSD",
            side="buy",
            executed_at=datetime(2026, 4, day, 8, tzinfo=timezone.utc),
            outcome="managed_win",
        )
    export = TradingViewExportService(db_session).build_export(
        TradingViewExportFilters(account_id=account.id, symbol="XAUUSD", start_date=date(2026, 4, 1), end_date=date(2026, 4, 30), max_trades=2)
    )
    assert export.exported_trades == 2
    assert export.audit_summary["was_limited_by_max_trades"] is True
    assert any("Export limited from" in warning for warning in export.warnings)


def test_skipped_records_include_skip_reason(db_session, created_user):
    account = _create_account(db_session, created_user, "TV-011")
    _create_setup(
        db_session,
        created_user,
        account,
        symbol="XAUUSD",
        side="buy",
        executed_at=datetime(2026, 4, 10, 8, tzinfo=timezone.utc),
        outcome="managed_win",
    )
    export = TradingViewExportService(db_session).build_export(
        TradingViewExportFilters(account_id=account.id, symbol="XAUUSD", start_date=date(2026, 5, 1), end_date=date(2026, 5, 2))
    )
    assert any(item["skip_reason"] == "outside_date_range" for item in export.skipped_records)


def test_close_data_not_reused_between_trades(db_session, created_user):
    account = _create_account(db_session, created_user, "TV-009")
    setup1 = _create_setup(
        db_session,
        created_user,
        account,
        symbol="XAUUSD",
        side="buy",
        executed_at=datetime(2026, 4, 10, 8, tzinfo=timezone.utc),
        outcome="full_win",
    )
    setup2 = _create_setup(
        db_session,
        created_user,
        account,
        symbol="XAUUSD",
        side="sell",
        executed_at=datetime(2026, 4, 11, 8, tzinfo=timezone.utc),
        outcome="closed_at_be",
    )
    setup1.order2_closed_at = datetime(2026, 4, 10, 11, tzinfo=timezone.utc)
    setup1.order2_close_price = 2325.5
    setup2.order2_closed_at = datetime(2026, 4, 11, 9, tzinfo=timezone.utc)
    setup2.order2_close_price = None
    db_session.add_all([setup1, setup2])
    db_session.commit()

    export = TradingViewExportService(db_session).build_export(
        TradingViewExportFilters(account_id=account.id, symbol="XAUUSD", start_date=date(2026, 4, 1), end_date=date(2026, 4, 30))
    )
    code = export.pine_code
    assert "var float[] closePrices = array.from(" in code
    assert "na" in code
    assert "array.push(closeTimes" not in code
    assert export.audit_summary["exported_trade_count"] == export.exported_trades


def test_admin_export_page_and_download(client, db_session):
    admin = _create_admin(db_session)
    user = create_user(
        db_session,
        UserCreate(email="tv-user@example.com", password="password123", full_name="TV User"),
    )
    account = _create_account(db_session, user, "TV-005")
    _create_setup(
        db_session,
        user,
        account,
        symbol="XAUUSD",
        side="buy",
        executed_at=datetime(2026, 4, 20, 8, tzinfo=timezone.utc),
        outcome="managed_win",
    )
    _login(client, admin.email)

    page = client.get(f"/admin/tradingview-export?symbol=XAUUSD&account_id={account.id}&max_trades=5&debug=true")
    download = client.get(f"/admin/tradingview-export?symbol=XAUUSD&account_id={account.id}&download=true")
    debug_json = client.get(f"/admin/tradingview-export?symbol=XAUUSD&account_id={account.id}&debug=true", headers={"accept": "application/json"})

    assert page.status_code == 200
    assert "TradingView History Export" in page.text
    assert "Audit summary" in page.text
    assert "//@version=6" in page.text
    assert download.status_code == 200
    assert "attachment; filename=" in download.headers.get("content-disposition", "")
    assert "//@version=6" in download.text
    assert debug_json.status_code == 200
    assert "audit_summary" in debug_json.json()


def test_export_page_renders_account_dropdown_with_all_option(client, db_session):
    admin = _create_admin(db_session)
    user = create_user(
        db_session,
        UserCreate(email="dropdown-user@example.com", password="password123", full_name="Dropdown User"),
    )
    _create_account(db_session, user, "TV-012")
    _login(client, admin.email)

    response = client.get("/admin/tradingview-export")

    assert response.status_code == 200
    assert 'name="account_id"' in response.text
    assert "All accounts" in response.text
    assert "Demo Broker" in response.text


def test_specific_account_filter_never_exports_other_accounts(client, db_session):
    admin = _create_admin(db_session)
    user = create_user(
        db_session,
        UserCreate(email="specific-account-user@example.com", password="password123", full_name="Specific User"),
    )
    account_a = _create_account(db_session, user, "TV-013-A")
    account_b = _create_account(db_session, user, "TV-013-B")
    _create_setup(
        db_session,
        user,
        account_a,
        symbol="XAUUSD",
        side="buy",
        executed_at=datetime(2026, 4, 20, 8, tzinfo=timezone.utc),
        outcome="managed_win",
    )
    _create_setup(
        db_session,
        user,
        account_b,
        symbol="XAUUSD",
        side="sell",
        executed_at=datetime(2026, 4, 20, 9, tzinfo=timezone.utc),
        outcome="managed_win",
    )
    _login(client, admin.email)

    debug_payload = client.get(
        f"/admin/tradingview-export?symbol=XAUUSD&account_id={account_a.id}&start_date=2026-04-01&end_date=2026-04-30&debug=true",
        headers={"accept": "application/json"},
    )

    assert debug_payload.status_code == 200
    body = debug_payload.json()
    assert body["audit_summary"]["selected_account_id"] == account_a.id
    assert body["audit_summary"]["exported_account_ids"] == [account_a.id]


def test_all_accounts_can_export_multiple_accounts_and_show_warning(db_session, created_user):
    account_a = _create_account(db_session, created_user, "TV-014-A")
    account_b = _create_account(db_session, created_user, "TV-014-B")
    _create_setup(
        db_session,
        created_user,
        account_a,
        symbol="XAUUSD",
        side="buy",
        executed_at=datetime(2026, 4, 20, 8, tzinfo=timezone.utc),
        outcome="managed_win",
    )
    _create_setup(
        db_session,
        created_user,
        account_b,
        symbol="XAUUSD",
        side="sell",
        executed_at=datetime(2026, 4, 20, 9, tzinfo=timezone.utc),
        outcome="managed_win",
    )

    export = TradingViewExportService(db_session).build_export(
        TradingViewExportFilters(
            symbol="XAUUSD",
            account_id=None,
            start_date=date(2026, 4, 1),
            end_date=date(2026, 4, 30),
        )
    )

    assert export.audit_summary["exported_account_count"] == 2
    assert any("multiple accounts" in warning for warning in export.warnings)


def test_invalid_account_id_returns_clear_error(client, db_session):
    admin = _create_admin(db_session)
    _login(client, admin.email)

    response = client.get("/admin/tradingview-export?symbol=XAUUSD&account_id=999999&start_date=2026-04-01&end_date=2026-04-30")

    assert response.status_code == 400
    assert "Selected account_id does not exist." in response.text


def test_generated_pine_avoids_reserved_text_variable(db_session, created_user):
    account = _create_account(db_session, created_user, "TV-015")
    _create_setup(
        db_session,
        created_user,
        account,
        symbol="XAUUSD",
        side="buy",
        executed_at=datetime(2026, 4, 20, 8, tzinfo=timezone.utc),
        outcome="managed_win",
    )
    export = TradingViewExportService(db_session).build_export(
        TradingViewExportFilters(account_id=account.id, symbol="XAUUSD", start_date=date(2026, 4, 1), end_date=date(2026, 4, 30))
    )
    pine = export.pine_code
    assert "text = array.get(labelTexts" not in pine
    assert "labelTexts" not in pine
    assert "showTextLabels = input.bool(false" in pine
    assert "string entryText = id +" in pine


def test_generated_pine_chunks_large_if_barstate_block(db_session, created_user):
    account = _create_account(db_session, created_user, "TV-016")
    for day in range(1, 33):
        _create_setup(
            db_session,
            created_user,
            account,
            symbol="XAUUSD",
            side="buy" if day % 2 else "sell",
            executed_at=datetime(2026, 4, 1 + (day % 28), 8, tzinfo=timezone.utc),
            outcome="managed_win",
        )
    export = TradingViewExportService(db_session).build_export(
        TradingViewExportFilters(account_id=account.id, symbol="XAUUSD", start_date=date(2026, 4, 1), end_date=date(2026, 4, 30), max_trades=500)
    )
    pine = export.pine_code
    assert "loadTradesPart" not in pine
    assert "array.push" not in pine
    assert "var string[] ids = array.from(" in pine
    assert "var string[] orderTags = array.from(" in pine
    assert "// Data source: DB trade history export." in pine


def test_focus_trade_zero_all_mode_inputs_and_logic(db_session, created_user):
    account = _create_account(db_session, created_user, "TV-017")
    _create_setup(
        db_session,
        created_user,
        account,
        symbol="XAUUSD",
        side="buy",
        executed_at=datetime(2026, 4, 20, 8, tzinfo=timezone.utc),
        outcome="managed_win",
    )
    export = TradingViewExportService(db_session).build_export(
        TradingViewExportFilters(account_id=account.id, symbol="XAUUSD", start_date=date(2026, 4, 1), end_date=date(2026, 4, 30))
    )
    pine = export.pine_code
    assert 'focusText = input.string("setup-225", "Focus setup/order/ticket. Empty = show last N positions")' in pine
    assert 'showLastNWhenNoFocus = input.int(4, "If focus empty: show last N positions"' in pine
    assert 'orderFilter = input.string("Both", "Order filter", options=["Both", "o1", "o2", "manual"])' in pine
    assert "f_match_focus(id, setupId, ticket) =>" in pine
    assert "f_match_order(tag) =>" in pine
    assert "int entryRaw = array.get(entryTimesRaw, i)" in pine
    assert "int entryT = f_shift_time(entryRaw)" in pine
    assert "xloc=xloc.bar_time" in pine
