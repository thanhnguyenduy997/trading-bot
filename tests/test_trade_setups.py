from datetime import datetime, timezone

from app.models.mt5_trade_history import MT5TradeHistory
from app.models.trade_setup import TradeSetup
from app.schemas.trade_setup import ManualTradeSetupCreate, TradeSetupCreate
from app.schemas.trading_account import TradingAccountCreate
from app.schemas.user import UserCreate
from app.services.manual_trade_setups import ManualTradeSetupService
from app.services.mt5_trade_history import DashboardFilters, DashboardService
from app.services.risk_management import RiskManagementService
from app.services.trade_setup_outcomes import TradeSetupOutcomeService
from app.services.trade_setups import create_trade_setup
from app.services.trading_accounts import create_trading_account
from app.services.users import create_user


def _create_account(db_session, user, account_number: str = "ACC-100"):
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


def _build_setup_payload(account_id: int, symbol: str = "XAUUSD") -> dict:
    return {
        "trading_account_id": account_id,
        "symbol": symbol,
        "side": "buy",
        "sl_price": 2319.2,
        "risk_mode": "fixed_money",
        "risk_value": 100,
        "rr_order2": 2,
        "estimated_entry": 2320.2,
        "r_value": 1.0,
        "tp1_price": 2321.2,
        "tp2_price": 2322.2,
        "total_risk_money": 100.0,
        "risk_per_order": 50.0,
        "order1_volume": 0.5,
        "order2_volume": 0.5,
        "status": "draft",
    }


def _login_headers(client, email: str, password: str) -> dict[str, str]:
    response = client.post("/api/auth/token", data={"username": email, "password": password})
    token = response.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


class FakePreviewAdapter:
    def __init__(self, account):
        self.account = account

    def connect(self):
        return None

    def get_account_info(self):
        return {"balance": 10000.0}

    def get_quote(self, symbol: str):
        return {"symbol": symbol, "bid": 2320.0, "ask": 2320.2}

    def get_symbol_info(self, symbol: str):
        return {
            "symbol": symbol,
            "point": 0.01,
            "digits": 2,
            "trade_contract_size": 100.0,
            "volume_min": 0.01,
            "volume_max": 100.0,
            "volume_step": 0.01,
        }

    def list_symbols(self):
        return [{"symbol": "XAUUSD", "visible": True}]

    def close(self):
        return None


class ManualTicketAdapter(FakePreviewAdapter):
    def get_position(self, *, position_ticket: int):
        return None

    def get_position_history(self, *, position_ticket: int):
        base_time = datetime(2026, 4, 23, 8, 0, tzinfo=timezone.utc)
        closed_tickets = {
            60001: [
                {"ticket": 5001, "position_id": 60001, "entry": "in", "type": "buy", "symbol": "XAUUSD", "volume": 0.4, "price": 2320.2, "time": base_time},
                {"ticket": 5002, "position_id": 60001, "entry": "out", "reason": "sl", "symbol": "XAUUSD", "volume": 0.4, "price": 2319.2, "profit": -50.0, "time": base_time.replace(minute=8), "point": 0.01},
            ],
            60002: [
                {"ticket": 5003, "position_id": 60002, "entry": "in", "type": "buy", "symbol": "XAUUSD", "volume": 0.4, "price": 2320.2, "time": base_time.replace(minute=1)},
                {"ticket": 5004, "position_id": 60002, "entry": "out", "reason": "sl", "symbol": "XAUUSD", "volume": 0.4, "price": 2319.2, "profit": -50.0, "time": base_time.replace(minute=9), "point": 0.01},
            ],
            61001: [
                {"ticket": 5101, "position_id": 61001, "entry": "in", "type": "buy", "symbol": "XAUUSD", "volume": 0.5, "price": 2320.2, "time": base_time.replace(minute=3)},
                {"ticket": 5102, "position_id": 61001, "entry": "out", "reason": "tp", "symbol": "XAUUSD", "volume": 0.5, "price": 2321.2, "profit": 50.0, "time": base_time.replace(minute=12), "point": 0.01},
            ],
            61002: [
                {"ticket": 5103, "position_id": 61002, "entry": "in", "type": "buy", "symbol": "XAUUSD", "volume": 0.5, "price": 2320.2, "time": base_time.replace(minute=4)},
                {"ticket": 5104, "position_id": 61002, "entry": "out", "reason": "sl", "symbol": "XAUUSD", "volume": 0.5, "price": 2320.19, "profit": -0.3, "time": base_time.replace(minute=20), "point": 0.01},
            ],
            62001: [
                {"ticket": 5201, "position_id": 62001, "entry": "in", "type": "buy", "symbol": "XAUUSD", "volume": 0.2, "price": 2320.2, "time": base_time},
                {"ticket": 5202, "position_id": 62001, "entry": "out", "reason": "client", "symbol": "XAUUSD", "volume": 0.2, "price": 2320.15, "profit": -2.0, "time": base_time.replace(minute=6), "point": 0.01},
            ],
        }
        return closed_tickets.get(position_ticket, [])

    def get_trade_history(self, *, date_from, date_to):
        return []


def test_save_setup_successfully(client, db_session, created_user, auth_headers):
    account = _create_account(db_session, created_user)

    response = client.post(
        "/api/trade-setups",
        headers=auth_headers,
        json=_build_setup_payload(account.id),
    )

    assert response.status_code == 201
    data = response.json()
    assert data["trading_account_id"] == account.id
    assert data["symbol"] == "XAUUSD"
    assert data["status"] == "draft"


def test_list_only_current_users_setups(client, db_session, created_user, auth_headers):
    current_user_account = _create_account(db_session, created_user, "ACC-101")
    create_trade_setup(db_session, created_user.id, TradeSetupCreate(**_build_setup_payload(current_user_account.id)))

    other_user = create_user(
        db_session,
        UserCreate(email="other@example.com", password="password123", full_name="Other User"),
    )
    other_account = _create_account(db_session, other_user, "ACC-202")
    create_trade_setup(db_session, other_user.id, TradeSetupCreate(**_build_setup_payload(other_account.id, "EURUSD")))

    response = client.get("/api/trade-setups", headers=auth_headers)

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["user_id"] == created_user.id
    assert data[0]["trading_account_id"] == current_user_account.id


def test_reject_access_to_another_users_setup_detail(client, db_session, created_user, auth_headers):
    other_user = create_user(
        db_session,
        UserCreate(email="other2@example.com", password="password123", full_name="Other User"),
    )
    other_account = _create_account(db_session, other_user, "ACC-303")
    other_setup = create_trade_setup(db_session, other_user.id, TradeSetupCreate(**_build_setup_payload(other_account.id)))

    response = client.get(f"/api/trade-setups/{other_setup.id}", headers=auth_headers)

    assert response.status_code == 404
    assert response.json()["detail"] == "Trade setup not found"


def test_saved_setup_fields_match_preview_values(client, db_session, created_user, monkeypatch, sync_account_symbols):
    monkeypatch.setattr(
        "app.services.execution.default_adapter_factory",
        lambda account: FakePreviewAdapter(account),
    )
    account = _create_account(db_session, created_user, "ACC-404")
    sync_account_symbols(account, "XAUUSD")
    headers = _login_headers(client, created_user.email, "password123")

    preview_response = client.post(
        "/api/trade-setups/preview",
        headers=headers,
        json={
            "trading_account_id": account.id,
            "symbol": "XAUUSD",
            "side": "buy",
            "sl_price": 2319.2,
            "risk_mode": "fixed_money",
            "risk_value": 100,
            "rr_order2": 2,
        },
    )
    preview_data = preview_response.json()

    save_payload = {
        "trading_account_id": account.id,
        "symbol": "XAUUSD",
        "side": "buy",
        "sl_price": 2319.2,
        "risk_mode": "fixed_money",
        "risk_value": 100,
        "rr_order2": 2,
        "estimated_entry": preview_data["estimated_entry"],
        "r_value": preview_data["r_value"],
        "tp1_price": preview_data["tp1_price"],
        "tp2_price": preview_data["tp2_price"],
        "total_risk_money": preview_data["total_risk_money"],
        "risk_per_order": preview_data["risk_per_order"],
        "order1_volume": preview_data["order1_volume"],
        "order2_volume": preview_data["order2_volume"],
        "status": "draft",
    }

    save_response = client.post("/api/trade-setups", headers=headers, json=save_payload)

    assert preview_response.status_code == 200
    assert save_response.status_code == 201
    saved = save_response.json()
    assert saved["estimated_entry"] == preview_data["estimated_entry"]
    assert saved["r_value"] == preview_data["r_value"]
    assert saved["tp1_price"] == preview_data["tp1_price"]
    assert saved["tp2_price"] == preview_data["tp2_price"]
    assert saved["risk_per_order"] == preview_data["risk_per_order"]
    assert saved["order1_volume"] == preview_data["order1_volume"]
    assert saved["order2_volume"] == preview_data["order2_volume"]


def test_manual_setup_creation_links_two_tickets_into_one_setup(client, db_session, created_user, auth_headers, monkeypatch):
    monkeypatch.setattr("app.services.manual_trade_setups.default_adapter_factory", lambda account: ManualTicketAdapter(account))
    monkeypatch.setattr("app.services.trade_setup_outcomes.default_adapter_factory", lambda account: ManualTicketAdapter(account))
    account = _create_account(db_session, created_user, "MAN-100")

    response = client.post(
        "/api/trade-setups/manual",
        headers=auth_headers,
        json={
            "trading_account_id": account.id,
            "symbol": "XAUUSD",
            "side": "buy",
            "estimated_entry": 2320.2,
            "sl_price": 2319.2,
            "total_risk_money": 100,
            "rr_order2": 2,
            "order_count": 2,
            "order1_ticket": 60001,
            "order2_ticket": 60002,
        },
    )

    assert response.status_code == 201
    data = response.json()
    assert data["setup_source"] == "manual"
    assert data["order_count"] == 2
    assert data["manual_confirmed_at"] is not None
    assert data["order1_ticket"] == 60001
    assert data["order2_ticket"] == 60002
    assert data["order1_volume"] == 0.4
    assert data["order2_volume"] == 0.4
    assert data["setup_outcome"] == "stoploss"


def test_linked_manual_setup_participates_in_outcome_tracking(db_session, created_user, monkeypatch):
    monkeypatch.setattr("app.services.manual_trade_setups.default_adapter_factory", lambda account: ManualTicketAdapter(account))
    monkeypatch.setattr("app.services.trade_setup_outcomes.default_adapter_factory", lambda account: ManualTicketAdapter(account))
    account = _create_account(db_session, created_user, "MAN-101")

    setup = ManualTradeSetupService(db_session).create_manual_setup(
        user_id=created_user.id,
        payload=ManualTradeSetupCreate(
            trading_account_id=account.id,
            symbol="XAUUSD",
            side="buy",
            estimated_entry=2320.2,
            sl_price=2319.2,
            total_risk_money=100,
            rr_order2=2,
            order_count=2,
            order1_ticket=61001,
            order2_ticket=61002,
        ),
    )
    setup.order2_be_moved_at = datetime.now(timezone.utc)
    db_session.add(setup)
    db_session.commit()

    result = TradeSetupOutcomeService(db_session).reconcile_setup(setup.id, created_user.id)

    assert result["order1_outcome"] == "tp_hit"
    assert result["order2_outcome"] == "closed_at_be"
    assert result["setup_outcome"] == "breakeven"
    stored = db_session.query(TradeSetup).filter(TradeSetup.id == setup.id).first()
    assert stored is not None
    assert stored.result_status == "non_stoploss"


def test_unlinked_manual_trades_do_not_affect_stoploss_streak_logic(db_session, created_user):
    account = _create_account(db_session, created_user, "MAN-102")
    db_session.add_all(
        [
            MT5TradeHistory(
                user_id=created_user.id,
                trading_account_id=account.id,
                position_ticket=90001,
                symbol="XAUUSD",
                side="buy",
                trade_source="manual",
                outcome="stoploss",
                volume=0.5,
                open_price=2320.2,
                close_price=2319.2,
                realized_pnl=-50.0,
                open_time=datetime(2026, 4, 23, 8, 0, tzinfo=timezone.utc),
                close_time=datetime(2026, 4, 23, 8, 5, tzinfo=timezone.utc),
                comment="raw manual trade",
            ),
            MT5TradeHistory(
                user_id=created_user.id,
                trading_account_id=account.id,
                position_ticket=90002,
                symbol="XAUUSD",
                side="buy",
                trade_source="manual",
                outcome="stoploss",
                volume=0.5,
                open_price=2320.2,
                close_price=2319.2,
                realized_pnl=-60.0,
                open_time=datetime(2026, 4, 23, 9, 0, tzinfo=timezone.utc),
                close_time=datetime(2026, 4, 23, 9, 6, tzinfo=timezone.utc),
                comment="raw manual trade 2",
            ),
        ]
    )
    db_session.commit()

    dashboard = DashboardService(db_session).build_dashboard(
        actor=created_user,
        filters=DashboardFilters(trading_account_id=account.id, range_key="all_time"),
        selected_account=account,
    )
    state = RiskManagementService(db_session).get_daily_state(created_user.id)

    assert dashboard["summary"]["stoploss_count"] == 2
    assert state.consecutive_stoploss_count == 0
    assert state.daily_lock_active is False


def test_duplicate_manual_ticket_link_is_blocked(client, db_session, created_user, auth_headers, monkeypatch):
    monkeypatch.setattr("app.services.manual_trade_setups.default_adapter_factory", lambda account: ManualTicketAdapter(account))
    monkeypatch.setattr("app.services.trade_setup_outcomes.default_adapter_factory", lambda account: ManualTicketAdapter(account))
    account = _create_account(db_session, created_user, "MAN-103")

    first = client.post(
        "/api/trade-setups/manual",
        headers=auth_headers,
        json={
            "trading_account_id": account.id,
            "symbol": "XAUUSD",
            "side": "buy",
            "estimated_entry": 2320.2,
            "sl_price": 2319.2,
            "total_risk_money": 100,
            "rr_order2": 2,
            "order_count": 1,
            "order1_ticket": 62001,
        },
    )
    first_setup_id = first.json()["id"]
    duplicate = client.post(
        "/api/trade-setups/manual",
        headers=auth_headers,
        json={
            "trading_account_id": account.id,
            "symbol": "XAUUSD",
            "side": "buy",
            "estimated_entry": 2320.2,
            "sl_price": 2319.2,
            "total_risk_money": 100,
            "rr_order2": 2,
            "order_count": 1,
            "order1_ticket": 62001,
        },
    )

    assert first.status_code == 201
    assert duplicate.status_code == 400
    assert duplicate.json()["detail"] == f"Ticket 62001 is already linked to setup #{first_setup_id}."
