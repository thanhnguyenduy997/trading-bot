from datetime import datetime, timedelta, timezone

from app.models.mt5_trade_history import MT5TradeHistory
from app.models.trade_setup import TradeSetup
from app.schemas.trade_setup import TradeSetupCreate
from app.schemas.trading_account import TradingAccountCreate
from app.schemas.user import UserCreate
from app.services.account_runtime_state import mark_trading_account_viewed
from app.services.mt5_trade_history import (
    DashboardAuthorizationError,
    DashboardFilters,
    DashboardService,
    MT5TradeHistorySyncService,
)
from app.services.trade_setups import create_trade_setup
from app.services.trading_accounts import create_trading_account
from app.services.users import create_user


def _create_account(db_session, user, account_number: str, *, session_status: str = "unknown", current_login: str | None = None):
    account = create_trading_account(
        db_session,
        user.id,
        TradingAccountCreate(
            broker_name="Demo Broker",
            account_number=account_number,
            server_name="demo-server",
            password="secret-pass",
        ),
    )
    account.mt5_session_status = session_status
    account.current_mt5_login = current_login
    if session_status == "matched":
        account.last_heartbeat_at = datetime.now(timezone.utc)
    db_session.add(account)
    db_session.commit()
    db_session.refresh(account)
    return account


def _create_setup(db_session, user, account, *, order1_ticket: int, order2_ticket: int, setup_outcome: str = "tp2_hit"):
    setup = create_trade_setup(
        db_session,
        user.id,
        TradeSetupCreate(
            trading_account_id=account.id,
            symbol="XAUUSD",
            side="buy",
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
            status="draft",
        ),
    )
    setup.status = "executed"
    setup.order1_ticket = order1_ticket
    setup.order2_ticket = order2_ticket
    setup.order1_outcome = "tp_hit"
    setup.order2_outcome = "closed_at_be" if setup_outcome == "breakeven" else "tp_hit"
    setup.setup_outcome = setup_outcome
    setup.executed_at = datetime.now(timezone.utc)
    db_session.add(setup)
    db_session.commit()
    db_session.refresh(setup)
    return setup


class DashboardHistoryAdapter:
    def __init__(self, account, history_map):
        self.account = account
        self.history_map = history_map

    def connect(self):
        return None

    def get_account_info(self):
        return {"login": self.account.account_number}

    def get_quote(self, symbol: str):
        return {}

    def get_symbol_info(self, symbol: str):
        return {}

    def list_symbols(self):
        return []

    def execute_setup(self, setup):
        return {}

    def place_market_order(self, **kwargs):
        return {}

    def close_position(self, **kwargs):
        return {}

    def get_position(self, *, position_ticket: int):
        return None

    def get_position_history(self, *, position_ticket: int):
        return []

    def get_trade_history(self, *, date_from: datetime, date_to: datetime):
        return self.history_map.get(self.account.id, [])

    def modify_position_sl(self, **kwargs):
        return {}

    def close(self):
        return None


def _deal(
    *,
    ticket: int,
    position_id: int,
    entry: str,
    deal_type: str,
    price: float,
    profit: float,
    when: datetime,
    comment: str,
    volume: float = 0.5,
    symbol: str = "XAUUSD",
):
    return {
        "ticket": ticket,
        "position_id": position_id,
        "entry": entry,
        "type": deal_type,
        "symbol": symbol,
        "volume": volume,
        "price": price,
        "profit": profit,
        "commission": 0.0,
        "swap": 0.0,
        "time": int(when.timestamp()),
        "comment": comment,
    }


def _login(client, email: str, password: str = "password123"):
    return client.post("/api/auth/login", data={"email": email, "password": password}, follow_redirects=False)


def _create_admin(db_session):
    return create_user(
        db_session,
        UserCreate(
            email="admin-dashboard@example.com",
            password="password123",
            full_name="Admin User",
            role="admin",
            is_active=True,
        ),
    )


def test_non_admin_user_only_sees_owned_trading_accounts(client, db_session, created_user):
    owned_account = _create_account(db_session, created_user, "OWN-001")
    other_user = create_user(
        db_session,
        UserCreate(email="other-dashboard@example.com", password="password123", full_name="Other User"),
    )
    _create_account(db_session, other_user, "OTHER-001")
    _login(client, created_user.email)

    response = client.get(f"/dashboard?account_id={owned_account.id}")

    assert response.status_code == 200
    assert "OWN-001" in response.text
    assert "OTHER-001" not in response.text
    assert "All Users" not in response.text


def test_dashboard_defaults_to_matched_owned_account(db_session, created_user):
    service = DashboardService(db_session)
    first = _create_account(db_session, created_user, "MATCH-001", session_status="unknown")
    matched = _create_account(db_session, created_user, "MATCH-002", session_status="matched", current_login="MATCH-002")

    selected = service.resolve_selected_account(actor=created_user, requested_account_id=None)

    assert selected is not None
    assert selected.id == matched.id
    assert selected.id != first.id


def test_dashboard_defaults_to_all_time_when_no_range_is_provided(client, db_session, created_user):
    account = _create_account(db_session, created_user, "ALL-001")
    db_session.add_all(
        [
            MT5TradeHistory(
                user_id=created_user.id,
                trading_account_id=account.id,
                position_ticket=501,
                symbol="XAUUSD",
                side="buy",
                trade_source="manual",
                outcome="take_profit",
                volume=0.4,
                open_price=2300.0,
                close_price=2302.0,
                realized_pnl=20.0,
                open_time=datetime(2026, 4, 10, 9, tzinfo=timezone.utc),
                close_time=datetime(2026, 4, 10, 10, tzinfo=timezone.utc),
            ),
            MT5TradeHistory(
                user_id=created_user.id,
                trading_account_id=account.id,
                position_ticket=502,
                symbol="XAUUSD",
                side="sell",
                trade_source="manual",
                outcome="stoploss",
                volume=0.3,
                open_price=2310.0,
                close_price=2312.0,
                realized_pnl=-10.0,
                open_time=datetime(2026, 4, 22, 9, tzinfo=timezone.utc),
                close_time=datetime(2026, 4, 22, 10, tzinfo=timezone.utc),
            ),
        ]
    )
    db_session.commit()
    _login(client, created_user.email)

    response = client.get(f"/dashboard?account_id={account.id}")

    assert response.status_code == 200
    assert 'option value="all_time" selected' in response.text
    assert "All Time" in response.text
    assert ">2<" in response.text


def test_dashboard_blank_or_unknown_range_normalizes_to_all_time(client, db_session, created_user):
    account = _create_account(db_session, created_user, "ALL-005")
    db_session.add(
        MT5TradeHistory(
            user_id=created_user.id,
            trading_account_id=account.id,
            position_ticket=503,
            symbol="XAUUSD",
            side="buy",
            trade_source="manual",
            outcome="take_profit",
            volume=0.4,
            open_price=2300.0,
            close_price=2302.0,
            realized_pnl=20.0,
            open_time=datetime(2026, 4, 10, 9, tzinfo=timezone.utc),
            close_time=datetime(2026, 4, 10, 10, tzinfo=timezone.utc),
        )
    )
    db_session.commit()
    _login(client, created_user.email)

    blank_response = client.get(f"/dashboard?account_id={account.id}&range=")
    unknown_response = client.get(f"/dashboard?account_id={account.id}&range=unexpected")

    assert blank_response.status_code == 200
    assert unknown_response.status_code == 200
    assert 'option value="all_time" selected' in blank_response.text
    assert 'option value="all_time" selected' in unknown_response.text
    assert "All Time" in blank_response.text
    assert "All Time" in unknown_response.text


def test_dashboard_defaults_to_most_recent_owned_account_when_no_match(db_session, created_user):
    service = DashboardService(db_session, now_provider=lambda: datetime(2026, 4, 22, 8, tzinfo=timezone.utc))
    older = _create_account(db_session, created_user, "RECENT-001")
    recent = _create_account(db_session, created_user, "RECENT-002")
    db_session.add(
        MT5TradeHistory(
            user_id=created_user.id,
            trading_account_id=recent.id,
            position_ticket=90001,
            symbol="XAUUSD",
            side="buy",
            trade_source="manual",
            outcome="take_profit",
            volume=0.5,
            open_price=2320.0,
            close_price=2322.0,
            realized_pnl=25.0,
            open_time=datetime(2026, 4, 21, 10, tzinfo=timezone.utc),
            close_time=datetime(2026, 4, 21, 11, tzinfo=timezone.utc),
            synced_at=datetime(2026, 4, 22, 7, tzinfo=timezone.utc),
        )
    )
    db_session.commit()

    selected = service.resolve_selected_account(actor=created_user, requested_account_id=None)

    assert selected is not None
    assert selected.id == recent.id
    assert selected.id != older.id


def test_dashboard_data_is_scoped_to_selected_account_only(db_session, created_user):
    service = DashboardService(db_session, now_provider=lambda: datetime(2026, 4, 22, 12, tzinfo=timezone.utc))
    selected_account = _create_account(db_session, created_user, "SCOPE-001")
    other_account = _create_account(db_session, created_user, "SCOPE-002")
    db_session.add_all(
        [
            MT5TradeHistory(
                user_id=created_user.id,
                trading_account_id=selected_account.id,
                position_ticket=1001,
                symbol="XAUUSD",
                side="buy",
                trade_source="manual",
                outcome="take_profit",
                volume=0.5,
                open_price=2320.0,
                close_price=2322.0,
                realized_pnl=20.0,
                open_time=datetime(2026, 4, 22, 1, tzinfo=timezone.utc),
                close_time=datetime(2026, 4, 22, 2, tzinfo=timezone.utc),
            ),
            MT5TradeHistory(
                user_id=created_user.id,
                trading_account_id=other_account.id,
                position_ticket=1002,
                symbol="EURUSD",
                side="sell",
                trade_source="manual",
                outcome="stoploss",
                volume=0.3,
                open_price=1.1,
                close_price=1.11,
                realized_pnl=-10.0,
                open_time=datetime(2026, 4, 22, 3, tzinfo=timezone.utc),
                close_time=datetime(2026, 4, 22, 4, tzinfo=timezone.utc),
            ),
        ]
    )
    db_session.commit()

    dashboard = service.build_dashboard(
        actor=created_user,
        filters=DashboardFilters(range_key="today", trading_account_id=selected_account.id),
        selected_account=selected_account,
    )

    assert dashboard["summary"]["trade_count"] == 1
    assert dashboard["table_rows"][0]["position_ticket"] == 1001
    assert dashboard["table_rows"][0]["account_display"] == "SCOPE-001"


def test_all_time_returns_full_available_dataset(db_session, created_user):
    service = DashboardService(db_session, now_provider=lambda: datetime(2026, 4, 22, 12, tzinfo=timezone.utc))
    account = _create_account(db_session, created_user, "ALL-002")
    db_session.add_all(
        [
            MT5TradeHistory(
                user_id=created_user.id,
                trading_account_id=account.id,
                position_ticket=601,
                symbol="XAUUSD",
                side="buy",
                trade_source="manual",
                outcome="take_profit",
                volume=0.5,
                open_price=2300.0,
                close_price=2301.0,
                realized_pnl=12.0,
                open_time=datetime(2026, 3, 1, 9, tzinfo=timezone.utc),
                close_time=datetime(2026, 3, 1, 10, tzinfo=timezone.utc),
            ),
            MT5TradeHistory(
                user_id=created_user.id,
                trading_account_id=account.id,
                position_ticket=602,
                symbol="EURUSD",
                side="sell",
                trade_source="system",
                outcome="tp_hit",
                volume=0.2,
                open_price=1.1,
                close_price=1.09,
                realized_pnl=8.0,
                open_time=datetime(2026, 4, 22, 9, tzinfo=timezone.utc),
                close_time=datetime(2026, 4, 22, 10, tzinfo=timezone.utc),
            ),
        ]
    )
    db_session.commit()

    dashboard = service.build_dashboard(
        actor=created_user,
        filters=DashboardFilters(range_key="all_time", trading_account_id=account.id),
        selected_account=account,
    )

    assert dashboard["summary"]["trade_count"] == 2
    assert len(dashboard["table_rows"]) == 2
    assert len(dashboard["trade_count_chart"]) == 2
    assert round(dashboard["summary"]["total_realized_pnl"], 2) == 20.0


def test_setup_winrate_uses_only_full_win_managed_win_and_full_loss(db_session, created_user):
    service = DashboardService(db_session, now_provider=lambda: datetime(2026, 4, 22, 12, tzinfo=timezone.utc))
    account = _create_account(db_session, created_user, "SETUP-001")
    setup_full_win = _create_setup(db_session, created_user, account, order1_ticket=91001, order2_ticket=91002, setup_outcome="tp2_hit")
    setup_managed = _create_setup(db_session, created_user, account, order1_ticket=92001, order2_ticket=92002, setup_outcome="breakeven")
    setup_loss = _create_setup(db_session, created_user, account, order1_ticket=93001, order2_ticket=93002, setup_outcome="stoploss")
    setup_review = _create_setup(db_session, created_user, account, order1_ticket=94001, order2_ticket=94002, setup_outcome="mixed")
    for setup, outcome, pnl1, pnl2 in [
        (setup_full_win, "full_win", 50.0, 100.0),
        (setup_managed, "managed_win", 50.0, 0.0),
        (setup_loss, "full_loss", -50.0, -50.0),
        (setup_review, "review_required", 50.0, -20.0),
    ]:
        setup.setup_outcome = outcome
        setup.order1_closed_at = datetime(2026, 4, 22, 9, tzinfo=timezone.utc)
        setup.order2_closed_at = datetime(2026, 4, 22, 10, tzinfo=timezone.utc)
        setup.order1_realized_pnl = pnl1
        setup.order2_realized_pnl = pnl2
        setup.setup_outcome_recorded_at = datetime(2026, 4, 22, 10, tzinfo=timezone.utc)
        db_session.add(setup)
    db_session.commit()

    dashboard = service.build_dashboard(
        actor=created_user,
        filters=DashboardFilters(range_key="today", trading_account_id=account.id),
        selected_account=account,
    )

    assert dashboard["setup_summary"]["full_win_count"] == 1
    assert dashboard["setup_summary"]["managed_win_count"] == 1
    assert dashboard["setup_summary"]["full_loss_count"] == 1
    assert dashboard["setup_summary"]["review_required_count"] == 1
    assert dashboard["setup_summary"]["setup_win_rate"] == 66.67


def test_dashboard_backfills_legacy_setup_outcomes_for_historical_setup_metrics(db_session, created_user):
    service = DashboardService(db_session, now_provider=lambda: datetime(2026, 4, 22, 12, tzinfo=timezone.utc))
    account = _create_account(db_session, created_user, "SETUP-LEGACY-001")

    setup_full_win = _create_setup(db_session, created_user, account, order1_ticket=95001, order2_ticket=95002, setup_outcome="tp2_hit")
    setup_managed = _create_setup(db_session, created_user, account, order1_ticket=96001, order2_ticket=96002, setup_outcome="breakeven")
    setup_loss = _create_setup(db_session, created_user, account, order1_ticket=97001, order2_ticket=97002, setup_outcome="stoploss")

    setup_full_win.order1_closed_at = datetime(2026, 4, 21, 9, tzinfo=timezone.utc)
    setup_full_win.order2_closed_at = datetime(2026, 4, 21, 10, tzinfo=timezone.utc)
    setup_full_win.order1_realized_pnl = 50.0
    setup_full_win.order2_realized_pnl = 100.0

    setup_managed.order1_outcome = "tp_hit"
    setup_managed.order2_outcome = "closed_at_be"
    setup_managed.setup_outcome = None
    setup_managed.order1_closed_at = datetime(2026, 4, 21, 11, tzinfo=timezone.utc)
    setup_managed.order2_closed_at = datetime(2026, 4, 21, 12, tzinfo=timezone.utc)
    setup_managed.order1_realized_pnl = 50.0
    setup_managed.order2_realized_pnl = 0.0

    setup_loss.order1_outcome = "sl_hit"
    setup_loss.order2_outcome = "sl_hit"
    setup_loss.order1_closed_at = datetime(2026, 4, 21, 13, tzinfo=timezone.utc)
    setup_loss.order2_closed_at = datetime(2026, 4, 21, 14, tzinfo=timezone.utc)
    setup_loss.order1_realized_pnl = -50.0
    setup_loss.order2_realized_pnl = -50.0

    db_session.add_all([setup_full_win, setup_managed, setup_loss])
    db_session.commit()

    dashboard = service.build_dashboard(
        actor=created_user,
        filters=DashboardFilters(range_key="all_time", trading_account_id=account.id),
        selected_account=account,
    )

    db_session.refresh(setup_full_win)
    db_session.refresh(setup_managed)
    db_session.refresh(setup_loss)

    assert setup_full_win.setup_outcome == "full_win"
    assert setup_managed.setup_outcome == "managed_win"
    assert setup_loss.setup_outcome == "full_loss"
    assert dashboard["setup_summary"]["full_win_count"] == 1
    assert dashboard["setup_summary"]["managed_win_count"] == 1
    assert dashboard["setup_summary"]["full_loss_count"] == 1
    assert len(dashboard["setup_rows"]) == 3


def test_dashboard_setup_metrics_use_order_close_time_when_outcome_recorded_at_missing(db_session, created_user):
    service = DashboardService(db_session, now_provider=lambda: datetime(2026, 4, 22, 12, tzinfo=timezone.utc))
    account = _create_account(db_session, created_user, "SETUP-TIME-001")
    setup = _create_setup(db_session, created_user, account, order1_ticket=98001, order2_ticket=98002, setup_outcome=None)
    setup.order1_outcome = "sl_hit"
    setup.order2_outcome = "sl_hit"
    setup.order1_closed_at = datetime(2026, 4, 22, 8, tzinfo=timezone.utc)
    setup.order2_closed_at = datetime(2026, 4, 22, 9, tzinfo=timezone.utc)
    setup.order1_realized_pnl = -50.0
    setup.order2_realized_pnl = -50.0
    setup.setup_outcome = None
    setup.setup_outcome_recorded_at = None
    db_session.add(setup)
    db_session.commit()

    dashboard = service.build_dashboard(
        actor=created_user,
        filters=DashboardFilters(range_key="today", trading_account_id=account.id),
        selected_account=account,
    )

    db_session.refresh(setup)
    assert setup.setup_outcome == "full_loss"
    assert dashboard["setup_summary"]["full_loss_count"] == 1
    assert len(dashboard["setup_rows"]) == 1


def test_explicit_range_in_query_params_overrides_all_time_default(db_session, created_user):
    service = DashboardService(db_session, now_provider=lambda: datetime(2026, 4, 22, 12, tzinfo=timezone.utc))
    account = _create_account(db_session, created_user, "ALL-003")
    db_session.add_all(
        [
            MT5TradeHistory(
                user_id=created_user.id,
                trading_account_id=account.id,
                position_ticket=701,
                symbol="XAUUSD",
                side="buy",
                trade_source="manual",
                outcome="take_profit",
                volume=0.5,
                open_price=2300.0,
                close_price=2301.0,
                realized_pnl=12.0,
                open_time=datetime(2026, 4, 10, 9, tzinfo=timezone.utc),
                close_time=datetime(2026, 4, 10, 10, tzinfo=timezone.utc),
            ),
            MT5TradeHistory(
                user_id=created_user.id,
                trading_account_id=account.id,
                position_ticket=702,
                symbol="XAUUSD",
                side="buy",
                trade_source="manual",
                outcome="take_profit",
                volume=0.5,
                open_price=2300.0,
                close_price=2301.0,
                realized_pnl=15.0,
                open_time=datetime(2026, 4, 22, 9, tzinfo=timezone.utc),
                close_time=datetime(2026, 4, 22, 10, tzinfo=timezone.utc),
            ),
        ]
    )
    db_session.commit()

    dashboard = service.build_dashboard(
        actor=created_user,
        filters=DashboardFilters(range_key="today", trading_account_id=account.id),
        selected_account=account,
    )

    assert dashboard["summary"]["trade_count"] == 1
    assert dashboard["table_rows"][0]["position_ticket"] == 702


def test_custom_still_requires_start_and_end_dates(db_session, created_user):
    service = DashboardService(db_session, now_provider=lambda: datetime(2026, 4, 22, 12, tzinfo=timezone.utc))

    try:
        service.resolve_time_range(DashboardFilters(range_key="custom"))
    except ValueError as exc:
        assert str(exc) == "Custom date range requires both Start Date and End Date."
    else:
        raise AssertionError("Expected ValueError for incomplete custom range.")


def test_summary_cards_charts_and_table_all_use_same_all_time_scope(db_session, created_user):
    service = DashboardService(db_session, now_provider=lambda: datetime(2026, 4, 22, 12, tzinfo=timezone.utc))
    account = _create_account(db_session, created_user, "ALL-004")
    db_session.add_all(
        [
            MT5TradeHistory(
                user_id=created_user.id,
                trading_account_id=account.id,
                position_ticket=801,
                symbol="XAUUSD",
                side="buy",
                trade_source="manual",
                outcome="take_profit",
                volume=0.5,
                open_price=2300.0,
                close_price=2301.0,
                realized_pnl=10.0,
                open_time=datetime(2026, 2, 1, 9, tzinfo=timezone.utc),
                close_time=datetime(2026, 2, 1, 10, tzinfo=timezone.utc),
            ),
            MT5TradeHistory(
                user_id=created_user.id,
                trading_account_id=account.id,
                position_ticket=802,
                symbol="EURUSD",
                side="sell",
                trade_source="manual",
                outcome="breakeven",
                volume=0.4,
                open_price=1.1,
                close_price=1.1,
                realized_pnl=0.0,
                open_time=datetime(2026, 4, 1, 9, tzinfo=timezone.utc),
                close_time=datetime(2026, 4, 1, 10, tzinfo=timezone.utc),
            ),
        ]
    )
    db_session.commit()

    dashboard = service.build_dashboard(
        actor=created_user,
        filters=DashboardFilters(range_key="all_time", trading_account_id=account.id),
        selected_account=account,
    )

    assert dashboard["summary"]["trade_count"] == 2
    assert len(dashboard["table_rows"]) == 2
    assert sum(item["value"] for item in dashboard["trade_count_chart"]) == 2
    assert round(dashboard["summary"]["total_realized_pnl"], 2) == sum(
        row["realized_pnl"] for row in dashboard["table_rows"]
    )


def test_unrelated_account_warnings_are_not_shown(client, db_session, created_user):
    selected_account = _create_account(db_session, created_user, "WARN-001", session_status="matched", current_login="WARN-001")
    _create_account(db_session, created_user, "WARN-002", session_status="mismatch", current_login="999999")
    _login(client, created_user.email)

    response = client.get(f"/dashboard?account_id={selected_account.id}")

    assert response.status_code == 200
    assert "Log into WARN-002 before live actions." not in response.text
    assert "Matched" in response.text


def test_admin_impersonation_uses_effective_user_scope(client, db_session):
    admin = _create_admin(db_session)
    target_user = create_user(
        db_session,
        UserCreate(email="target-dashboard@example.com", password="password123", full_name="Target User"),
    )
    admin_account = _create_account(db_session, admin, "ADMIN-001")
    target_account = _create_account(db_session, target_user, "TARGET-001")
    _login(client, admin.email)
    client.post(f"/admin/users/{target_user.id}/impersonate", follow_redirects=False)

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert "TARGET-001" in response.text
    assert "ADMIN-001" not in response.text
    assert "effective dashboard scope" in response.text
    assert admin_account.id != target_account.id


def test_unauthorized_account_id_is_rejected_safely(client, db_session, created_user):
    owned_account = _create_account(db_session, created_user, "SAFE-001")
    other_user = create_user(
        db_session,
        UserCreate(email="safe-other@example.com", password="password123", full_name="Other User"),
    )
    other_account = _create_account(db_session, other_user, "SAFE-999")
    _login(client, created_user.email)

    response = client.get(f"/dashboard?account_id={other_account.id}")

    assert response.status_code == 200
    assert "Trading account not found for the current signed-in user." in response.text
    assert "SAFE-999" not in response.text
    assert "SAFE-001" in response.text or owned_account.account_number in response.text


def test_sync_classifies_system_manual_and_unknown_trades(db_session, created_user):
    account = _create_account(db_session, created_user, "SYNC-001")
    setup_one = _create_setup(db_session, created_user, account, order1_ticket=70001, order2_ticket=70002, setup_outcome="tp2_hit")
    setup_two = _create_setup(db_session, created_user, account, order1_ticket=71001, order2_ticket=71002, setup_outcome="stoploss")
    now = datetime.now(timezone.utc)
    history_map = {
        account.id: [
            _deal(ticket=11, position_id=70001, entry="in", deal_type="buy", price=2320.2, profit=0.0, when=now - timedelta(hours=2), comment=f"setup-{setup_one.id}-o1"),
            _deal(ticket=12, position_id=70001, entry="out", deal_type="sell", price=2321.2, profit=50.0, when=now - timedelta(hours=1), comment=f"setup-{setup_one.id}-o1"),
            _deal(ticket=21, position_id=90001, entry="in", deal_type="buy", price=2300.0, profit=0.0, when=now - timedelta(hours=4), comment="manual scalp"),
            _deal(ticket=22, position_id=90001, entry="out", deal_type="sell", price=2302.0, profit=40.0, when=now - timedelta(hours=3), comment="manual scalp"),
            _deal(ticket=31, position_id=91001, entry="in", deal_type="buy", price=2290.0, profit=0.0, when=now - timedelta(hours=6), comment=f"setup-{setup_one.id}-o1"),
            _deal(ticket=32, position_id=91001, entry="out", deal_type="sell", price=2288.0, profit=-20.0, when=now - timedelta(hours=5), comment=f"setup-{setup_two.id}-o1"),
        ]
    }
    service = MT5TradeHistorySyncService(
        db_session,
        adapter_factory=lambda current_account: DashboardHistoryAdapter(current_account, history_map),
    )

    result = service.sync_account_history(
        account_id=account.id,
        user_id=created_user.id,
        range_start=now - timedelta(days=1),
        range_end=now,
    )

    assert result["synced_count"] == 3
    rows = (
        db_session.query(MT5TradeHistory)
        .filter(MT5TradeHistory.trading_account_id == account.id)
        .order_by(MT5TradeHistory.position_ticket.asc())
        .all()
    )
    assert [row.trade_source for row in rows] == ["system", "manual", "unknown"]


def test_dashboard_page_shows_linked_setup_trade_for_selected_account(client, db_session, created_user):
    account = _create_account(db_session, created_user, "PAGE-001")
    setup = _create_setup(db_session, created_user, account, order1_ticket=82001, order2_ticket=82002, setup_outcome="tp2_hit")
    long_comment = "System trade comment that is intentionally long to verify truncation and tooltip behavior in the dashboard trade table."
    db_session.add(
        MT5TradeHistory(
            user_id=created_user.id,
            trading_account_id=account.id,
            linked_setup_id=setup.id,
            position_ticket=82001,
            symbol="XAUUSD",
            side="buy",
            trade_source="system",
            outcome="tp_hit",
            volume=0.5,
            open_price=2320.0,
            close_price=2321.0,
            realized_pnl=50.0,
            open_time=datetime.now(timezone.utc) - timedelta(hours=2),
            close_time=datetime.now(timezone.utc) - timedelta(hours=1),
            comment=long_comment,
        )
    )
    db_session.commit()
    _login(client, created_user.email)

    response = client.get(f"/dashboard?account_id={account.id}")

    assert response.status_code == 200
    assert "Trading Dashboard" in response.text
    assert f"/trade-setups/{setup.id}" in response.text
    assert "PAGE-001" in response.text
    assert "dashboard-trade-table" in response.text
    assert "trade-pill trade-pill-source trade-pill-source-system" in response.text
    assert "trade-pill trade-pill-outcome trade-pill-outcome-tp-hit" in response.text
    assert long_comment in response.text


def test_positive_pnl_linked_row_does_not_inherit_setup_stoploss_label(db_session, created_user):
    service = DashboardService(db_session, now_provider=lambda: datetime(2026, 4, 22, 12, tzinfo=timezone.utc))
    account = _create_account(db_session, created_user, "PAGE-002")
    setup = _create_setup(db_session, created_user, account, order1_ticket=83001, order2_ticket=83002, setup_outcome="stoploss")
    db_session.add(
        MT5TradeHistory(
            user_id=created_user.id,
            trading_account_id=account.id,
            linked_setup_id=setup.id,
            position_ticket=83001,
            symbol="XAUUSD",
            side="buy",
            trade_source="system",
            outcome="tp_hit",
            volume=0.5,
            open_price=2320.0,
            close_price=2321.0,
            realized_pnl=50.0,
            open_time=datetime(2026, 4, 22, 8, tzinfo=timezone.utc),
            close_time=datetime(2026, 4, 22, 9, tzinfo=timezone.utc),
        )
    )
    db_session.commit()

    dashboard = service.build_dashboard(
        actor=created_user,
        filters=DashboardFilters(range_key="today", trading_account_id=account.id),
        selected_account=account,
    )

    assert dashboard["table_rows"][0]["effective_outcome"] == "tp_hit"


def test_dashboard_page_shows_history_freshness_and_auto_refresh(client, db_session, created_user):
    account = _create_account(db_session, created_user, "FRESH-001")
    db_session.add(
        MT5TradeHistory(
            user_id=created_user.id,
            trading_account_id=account.id,
            position_ticket=82011,
            symbol="XAUUSD",
            side="buy",
            trade_source="manual",
            outcome="take_profit",
            volume=0.5,
            open_price=2320.0,
            close_price=2321.0,
            realized_pnl=25.0,
            open_time=datetime(2026, 4, 22, 8, tzinfo=timezone.utc),
            close_time=datetime(2026, 4, 22, 9, tzinfo=timezone.utc),
            synced_at=datetime(2026, 4, 22, 9, 5, tzinfo=timezone.utc),
        )
    )
    db_session.commit()
    _login(client, created_user.email)

    response = client.get(f"/dashboard?account_id={account.id}")

    assert response.status_code == 200
    assert "Last MT5 History Sync" in response.text
    assert "History Freshness" in response.text
    assert 'data-auto-refresh-interval="45"' in response.text
    assert "Every 45s" in response.text
    assert "mobile-data-card trade-mobile-card" in response.text


def test_auto_sync_prefers_recently_viewed_matched_accounts_and_skips_fresh_accounts(db_session, created_user):
    service = MT5TradeHistorySyncService(db_session)
    now = datetime(2026, 4, 22, 12, tzinfo=timezone.utc)
    matched_stale = _create_account(db_session, created_user, "AUTO-001", session_status="matched", current_login="AUTO-001")
    _create_account(db_session, created_user, "AUTO-002", session_status="disconnected")
    fresh_matched = _create_account(db_session, created_user, "AUTO-003", session_status="matched", current_login="AUTO-003")
    db_session.add(
        MT5TradeHistory(
            user_id=created_user.id,
            trading_account_id=fresh_matched.id,
            position_ticket=82012,
            symbol="XAUUSD",
            side="buy",
            trade_source="manual",
            outcome="take_profit",
            volume=0.5,
            open_price=2320.0,
            close_price=2321.0,
            realized_pnl=25.0,
            open_time=now - timedelta(hours=2),
            close_time=now - timedelta(hours=1),
            synced_at=now - timedelta(seconds=60),
        )
    )
    db_session.commit()
    mark_trading_account_viewed(matched_stale.id, viewed_at=now - timedelta(minutes=5))

    selected = service.select_accounts_for_auto_sync(
        max_accounts=2,
        stale_after_seconds=180,
        now=now,
    )

    selected_ids = [account.id for account in selected]
    assert matched_stale.id in selected_ids
    assert fresh_matched.id not in selected_ids
    assert len(selected_ids) <= 2


def test_dashboard_authorization_error_raised_for_unowned_account(db_session, created_user):
    service = DashboardService(db_session)
    other_user = create_user(
        db_session,
        UserCreate(email="guard@example.com", password="password123", full_name="Guarded User"),
    )
    other_account = _create_account(db_session, other_user, "GUARD-001")

    try:
        service.resolve_selected_account(actor=created_user, requested_account_id=other_account.id)
    except DashboardAuthorizationError as exc:
        assert str(exc) == "Trading account not found for the current signed-in user."
    else:
        raise AssertionError("Expected DashboardAuthorizationError for unowned account.")
