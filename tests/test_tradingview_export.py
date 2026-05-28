from datetime import date, datetime, timezone

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


def test_export_filters_account_symbol_outcome_and_range_overlap(db_session, created_user):
    account_a = _create_account(db_session, created_user, "TV-002")
    account_b = _create_account(db_session, created_user, "TV-003")

    inside = _create_setup(
        db_session,
        created_user,
        account_a,
        symbol="XAUUSD",
        side="buy",
        executed_at=datetime(2026, 4, 10, 8, tzinfo=timezone.utc),
        outcome="closed_at_be",
    )
    inside.executed_at = datetime(2026, 3, 20, 8, tzinfo=timezone.utc)
    inside.order2_closed_at = datetime(2026, 4, 2, 10, tzinfo=timezone.utc)
    inside.setup_outcome_recorded_at = inside.order2_closed_at
    db_session.add(inside)

    _create_setup(
        db_session,
        created_user,
        account_a,
        symbol="EURUSD",
        side="sell",
        executed_at=datetime(2026, 4, 10, 8, tzinfo=timezone.utc),
        outcome="closed_at_be",
    )
    _create_setup(
        db_session,
        created_user,
        account_b,
        symbol="XAUUSD",
        side="buy",
        executed_at=datetime(2026, 4, 10, 8, tzinfo=timezone.utc),
        outcome="full_win",
    )
    db_session.commit()

    result = TradingViewExportService(db_session).build_export(
        TradingViewExportFilters(
            account_id=account_a.id,
            symbol="XAUUSD",
            start_date=date(2026, 4, 1),
            end_date=date(2026, 4, 30),
            outcome="closed_at_be",
        )
    )

    assert result.exported_trades == 1
    assert "closed_at_be" in result.pine_code
    assert "label.new" in result.pine_code
    assert "line.new" in result.pine_code
    assert "timestamp(2026, 3, 20" in result.pine_code


def test_export_max_trades_limit_and_be_outcome_kept_from_db(db_session, created_user):
    account = _create_account(db_session, created_user, "TV-004")
    for day in range(1, 6):
        _create_setup(
            db_session,
            created_user,
            account,
            symbol="XAUUSD",
            side="buy",
            executed_at=datetime(2026, 4, day, 8, tzinfo=timezone.utc),
            outcome="closed_at_be" if day == 5 else "full_win",
        )

    result = TradingViewExportService(db_session).build_export(
        TradingViewExportFilters(
            account_id=account.id,
            symbol="XAUUSD",
            start_date=date(2026, 4, 1),
            end_date=date(2026, 4, 30),
            max_trades=2,
        )
    )

    assert result.exported_trades == 2
    assert any("latest 2" in warning for warning in result.warnings)
    assert "closed_at_be" in result.pine_code
    assert "sl_hit" not in result.pine_code


def test_generated_pine_uses_valid_na_checks_and_function_signature(db_session, created_user):
    account = _create_account(db_session, created_user, "TV-006")
    _create_setup(
        db_session,
        created_user,
        account,
        symbol="XAUUSD",
        side="buy",
        executed_at=datetime(2026, 4, 22, 8, tzinfo=timezone.utc),
        outcome="managed_win",
    )

    result = TradingViewExportService(db_session).build_export(
        TradingViewExportFilters(
            account_id=account.id,
            symbol="XAUUSD",
            start_date=date(2026, 4, 1),
            end_date=date(2026, 4, 30),
        )
    )
    pine = result.pine_code

    assert "color outcomeColor" not in pine
    assert "outcomeColor(string outcome) =>" in pine
    assert " != na" not in pine
    assert " == na" not in pine
    assert "not na(" in pine
    assert "array.push(entryTimes" in pine
    assert "array.push(entryPrices" in pine
    assert "array.push(closeTimes" in pine
    assert "array.push(closePrices" in pine


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

    page = client.get(f"/admin/tradingview-export?symbol=XAUUSD&account_id={account.id}&max_trades=5")
    download = client.get(f"/admin/tradingview-export?symbol=XAUUSD&account_id={account.id}&download=true")

    assert page.status_code == 200
    assert "TradingView History Export" in page.text
    assert "//@version=6" in page.text
    assert download.status_code == 200
    assert "attachment; filename=" in download.headers.get("content-disposition", "")
    assert "//@version=6" in download.text
