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


def _login_web_session(client, email: str, password: str = "password123") -> None:
    response = client.post("/api/auth/login", data={"email": email, "password": password}, follow_redirects=False)
    assert response.status_code in {302, 303}


def _add_manual_trade_history_row(
    db_session,
    *,
    user_id: int,
    trading_account_id: int,
    position_ticket: int,
    symbol: str,
    side: str,
    volume: float,
    open_price: float,
    close_price: float,
    realized_pnl: float,
    open_time: datetime,
    close_time: datetime,
) -> MT5TradeHistory:
    trade = MT5TradeHistory(
        user_id=user_id,
        trading_account_id=trading_account_id,
        position_ticket=position_ticket,
        symbol=symbol,
        side=side,
        trade_source="manual",
        volume=volume,
        open_price=open_price,
        close_price=close_price,
        realized_pnl=realized_pnl,
        open_time=open_time,
        close_time=close_time,
    )
    db_session.add(trade)
    db_session.flush()
    return trade


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
                {"ticket": 5001, "position_id": 60001, "entry": "in", "type": "buy", "symbol": "XAUUSD", "volume": 0.4, "price": 2320.2, "time": base_time, "sl": 2319.2},
                {"ticket": 5002, "position_id": 60001, "entry": "out", "reason": "sl", "symbol": "XAUUSD", "volume": 0.4, "price": 2319.2, "profit": -50.0, "time": base_time.replace(minute=8), "point": 0.01},
            ],
            60002: [
                {"ticket": 5003, "position_id": 60002, "entry": "in", "type": "buy", "symbol": "XAUUSD", "volume": 0.4, "price": 2320.2, "time": base_time.replace(minute=1), "sl": 2319.2},
                {"ticket": 5004, "position_id": 60002, "entry": "out", "reason": "sl", "symbol": "XAUUSD", "volume": 0.4, "price": 2319.2, "profit": -50.0, "time": base_time.replace(minute=9), "point": 0.01},
            ],
            61001: [
                {"ticket": 5101, "position_id": 61001, "entry": "in", "type": "buy", "symbol": "XAUUSD", "volume": 0.5, "price": 2320.2, "time": base_time.replace(minute=3), "sl": 2319.2},
                {"ticket": 5102, "position_id": 61001, "entry": "out", "reason": "tp", "symbol": "XAUUSD", "volume": 0.5, "price": 2321.2, "profit": 50.0, "time": base_time.replace(minute=12), "point": 0.01},
            ],
            61002: [
                {"ticket": 5103, "position_id": 61002, "entry": "in", "type": "buy", "symbol": "XAUUSD", "volume": 0.5, "price": 2320.2, "time": base_time.replace(minute=4), "sl": 2320.2},
                {"ticket": 5104, "position_id": 61002, "entry": "out", "reason": "sl", "symbol": "XAUUSD", "volume": 0.5, "price": 2320.19, "profit": -0.3, "time": base_time.replace(minute=20), "point": 0.01},
            ],
            62001: [
                {"ticket": 5201, "position_id": 62001, "entry": "in", "type": "buy", "symbol": "XAUUSD", "volume": 0.2, "price": 2320.2, "time": base_time, "sl": 2319.2},
                {"ticket": 5202, "position_id": 62001, "entry": "out", "reason": "client", "symbol": "XAUUSD", "volume": 0.2, "price": 2320.15, "profit": -2.0, "time": base_time.replace(minute=6), "point": 0.01},
            ],
            63001: [
                {"ticket": 5301, "position_id": 63001, "entry": "in", "type": "buy", "symbol": "XAUUSD", "volume": 0.3, "price": 2320.0, "time": base_time.replace(hour=9)},
                {"ticket": 5302, "position_id": 63001, "entry": "out", "reason": "client", "symbol": "XAUUSD", "volume": 0.3, "price": 2320.1, "profit": 3.0, "time": base_time.replace(hour=9, minute=10), "point": 0.01},
            ],
            64001: [
                {"ticket": 5401, "position_id": 64001, "entry": "in", "type": "buy", "symbol": "XAUUSD", "volume": 0.2, "price": 2320.0, "time": base_time.replace(hour=10), "sl": 2319.2},
                {"ticket": 5402, "position_id": 64001, "entry": "out", "reason": "client", "symbol": "XAUUSD", "volume": 0.2, "price": 2320.3, "profit": 6.0, "time": base_time.replace(hour=10, minute=5), "point": 0.01},
            ],
            64002: [
                {"ticket": 5403, "position_id": 64002, "entry": "in", "type": "buy", "symbol": "XAUUSD", "volume": 0.4, "price": 2320.6, "time": base_time.replace(hour=10, minute=40), "sl": 2319.2},
                {"ticket": 5404, "position_id": 64002, "entry": "out", "reason": "client", "symbol": "XAUUSD", "volume": 0.4, "price": 2321.0, "profit": 16.0, "time": base_time.replace(hour=10, minute=55), "point": 0.01},
            ],
            65001: [
                {"ticket": 5501, "position_id": 65001, "entry": "in", "type": "buy", "symbol": "XAUUSD", "volume": 0.2, "price": 2320.1, "time": base_time.replace(hour=11), "sl": 2319.2},
                {"ticket": 5502, "position_id": 65001, "entry": "out", "reason": "client", "symbol": "XAUUSD", "volume": 0.2, "price": 2320.4, "profit": 6.0, "time": base_time.replace(hour=11, minute=8), "point": 0.01},
            ],
            65002: [
                {"ticket": 5503, "position_id": 65002, "entry": "in", "type": "buy", "symbol": "XAUUSD", "volume": 0.2, "price": 2320.2, "time": base_time.replace(hour=11, minute=2), "sl": 2318.8},
                {"ticket": 5504, "position_id": 65002, "entry": "out", "reason": "client", "symbol": "XAUUSD", "volume": 0.2, "price": 2320.6, "profit": 8.0, "time": base_time.replace(hour=11, minute=14), "point": 0.01},
            ],
            66001: [
                {"ticket": 5601, "position_id": 66001, "entry": "in", "symbol": "XAUUSD", "volume": 0.3, "price": 2320.1, "time": base_time.replace(hour=12), "sl": 2319.2},
                {"ticket": 5602, "position_id": 66001, "entry": "out", "reason": "client", "symbol": "XAUUSD", "volume": 0.3, "price": 2320.4, "profit": 9.0, "time": base_time.replace(hour=12, minute=9), "point": 0.01},
            ],
            66002: [
                {"ticket": 5603, "position_id": 66002, "entry": "in", "symbol": "XAUUSD", "volume": 0.3, "price": 2320.2, "time": base_time.replace(hour=12, minute=1), "sl": 2319.2},
                {"ticket": 5604, "position_id": 66002, "entry": "out", "reason": "client", "symbol": "XAUUSD", "volume": 0.3, "price": 2320.5, "profit": 9.0, "time": base_time.replace(hour=12, minute=10), "point": 0.01},
            ],
            67001: [
                {"ticket": 5701, "position_id": 67001, "entry": "in", "symbol": "XAUUSD", "volume": 0.3, "price": 2320.1, "time": base_time.replace(hour=13), "sl": 2321.1},
                {"ticket": 5702, "position_id": 67001, "entry": "out", "reason": "client", "symbol": "XAUUSD", "volume": 0.3, "price": 2319.8, "profit": 9.0, "time": base_time.replace(hour=13, minute=8), "point": 0.01},
            ],
        }
        return closed_tickets.get(position_ticket, [])

    def get_trade_history(self, *, date_from, date_to):
        return []


class RawClosingSideAdapter(ManualTicketAdapter):
    def get_position_history(self, *, position_ticket: int):
        history = super().get_position_history(position_ticket=position_ticket)
        if not history:
            return history
        rewritten = []
        inferred_open_type = next((deal.get("type") for deal in history if deal.get("entry") == "in"), None)
        for deal in history:
            copied = dict(deal)
            if copied.get("entry") == "in":
                copied.pop("type", None)
            elif copied.get("entry") in {"out", "out_by"} and copied.get("type") is None:
                if str(inferred_open_type).lower() in {"buy", "0"}:
                    copied["type"] = "sell"
                elif str(inferred_open_type).lower() in {"sell", "1"}:
                    copied["type"] = "buy"
            rewritten.append(copied)
        return rewritten


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
    _add_manual_trade_history_row(
        db_session,
        user_id=created_user.id,
        trading_account_id=account.id,
        position_ticket=60001,
        symbol="XAUUSD",
        side="BUY",
        volume=0.4,
        open_price=2320.2,
        close_price=2319.2,
        realized_pnl=-50.0,
        open_time=datetime(2026, 4, 23, 8, 0, tzinfo=timezone.utc),
        close_time=datetime(2026, 4, 23, 8, 8, tzinfo=timezone.utc),
    )
    _add_manual_trade_history_row(
        db_session,
        user_id=created_user.id,
        trading_account_id=account.id,
        position_ticket=60002,
        symbol="XAUUSD",
        side="BUY",
        volume=0.4,
        open_price=2320.2,
        close_price=2319.2,
        realized_pnl=-50.0,
        open_time=datetime(2026, 4, 23, 8, 1, tzinfo=timezone.utc),
        close_time=datetime(2026, 4, 23, 8, 9, tzinfo=timezone.utc),
    )
    db_session.commit()

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
    assert data["setup_outcome"] == "full_loss"


def test_synced_buy_trade_registers_successfully_even_if_live_side_lookup_is_blank(client, db_session, created_user, auth_headers, monkeypatch):
    monkeypatch.setattr("app.services.manual_trade_setups.default_adapter_factory", lambda account: ManualTicketAdapter(account))
    monkeypatch.setattr("app.services.trade_setup_outcomes.default_adapter_factory", lambda account: ManualTicketAdapter(account))
    account = _create_account(db_session, created_user, "MAN-REG-100")
    trade = MT5TradeHistory(
        user_id=created_user.id,
        trading_account_id=account.id,
        position_ticket=66001,
        symbol="XAUUSD",
        side="BUY",
        trade_source="manual",
        volume=0.3,
        open_price=2320.1,
        close_price=2320.4,
        realized_pnl=9.0,
        open_time=datetime(2026, 4, 23, 12, 0, tzinfo=timezone.utc),
        close_time=datetime(2026, 4, 23, 12, 9, tzinfo=timezone.utc),
    )
    db_session.add(trade)
    db_session.commit()

    response = client.post(
        "/api/trade-setups/manual",
        headers=auth_headers,
        json={
            "trading_account_id": account.id,
            "symbol": "XAUUSD",
            "side": "buy",
            "estimated_entry": 2320.1,
            "sl_price": 2319.2,
            "total_risk_money": 27,
            "rr_order2": 2,
            "order_count": 1,
            "order1_ticket": 66001,
        },
    )

    assert response.status_code == 201
    assert response.json()["setup_source"] == "manual"


def test_synced_sell_trade_registers_successfully_even_if_live_side_lookup_is_blank(client, db_session, created_user, auth_headers, monkeypatch):
    monkeypatch.setattr("app.services.manual_trade_setups.default_adapter_factory", lambda account: ManualTicketAdapter(account))
    monkeypatch.setattr("app.services.trade_setup_outcomes.default_adapter_factory", lambda account: ManualTicketAdapter(account))
    account = _create_account(db_session, created_user, "MAN-REG-101")
    trade = MT5TradeHistory(
        user_id=created_user.id,
        trading_account_id=account.id,
        position_ticket=67001,
        symbol="XAUUSD",
        side="SELL",
        trade_source="manual",
        volume=0.3,
        open_price=2320.1,
        close_price=2319.8,
        realized_pnl=9.0,
        open_time=datetime(2026, 4, 23, 13, 0, tzinfo=timezone.utc),
        close_time=datetime(2026, 4, 23, 13, 8, tzinfo=timezone.utc),
    )
    db_session.add(trade)
    db_session.commit()

    response = client.post(
        "/api/trade-setups/manual",
        headers=auth_headers,
        json={
            "trading_account_id": account.id,
            "symbol": "XAUUSD",
            "side": "sell",
            "estimated_entry": 2320.1,
            "sl_price": 2321.1,
            "total_risk_money": 30,
            "rr_order2": 2,
            "order_count": 1,
            "order1_ticket": 67001,
        },
    )

    assert response.status_code == 201
    assert response.json()["setup_source"] == "manual"


def test_linked_manual_setup_participates_in_outcome_tracking(db_session, created_user, monkeypatch):
    monkeypatch.setattr("app.services.manual_trade_setups.default_adapter_factory", lambda account: ManualTicketAdapter(account))
    monkeypatch.setattr("app.services.trade_setup_outcomes.default_adapter_factory", lambda account: ManualTicketAdapter(account))
    account = _create_account(db_session, created_user, "MAN-101")
    _add_manual_trade_history_row(
        db_session,
        user_id=created_user.id,
        trading_account_id=account.id,
        position_ticket=61001,
        symbol="XAUUSD",
        side="BUY",
        volume=0.5,
        open_price=2320.2,
        close_price=2321.2,
        realized_pnl=50.0,
        open_time=datetime(2026, 4, 23, 8, 3, tzinfo=timezone.utc),
        close_time=datetime(2026, 4, 23, 8, 12, tzinfo=timezone.utc),
    )
    _add_manual_trade_history_row(
        db_session,
        user_id=created_user.id,
        trading_account_id=account.id,
        position_ticket=61002,
        symbol="XAUUSD",
        side="BUY",
        volume=0.5,
        open_price=2320.2,
        close_price=2320.19,
        realized_pnl=-0.3,
        open_time=datetime(2026, 4, 23, 8, 4, tzinfo=timezone.utc),
        close_time=datetime(2026, 4, 23, 8, 20, tzinfo=timezone.utc),
    )
    db_session.commit()

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
    assert result["setup_outcome"] == "managed_win"
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
    _add_manual_trade_history_row(
        db_session,
        user_id=created_user.id,
        trading_account_id=account.id,
        position_ticket=62001,
        symbol="XAUUSD",
        side="BUY",
        volume=0.2,
        open_price=2320.2,
        close_price=2320.15,
        realized_pnl=-2.0,
        open_time=datetime(2026, 4, 23, 8, 0, tzinfo=timezone.utc),
        close_time=datetime(2026, 4, 23, 8, 6, tzinfo=timezone.utc),
    )
    db_session.commit()

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


def test_register_time_real_synced_side_mismatch_is_still_rejected(client, db_session, created_user, auth_headers, monkeypatch):
    monkeypatch.setattr("app.services.manual_trade_setups.default_adapter_factory", lambda account: ManualTicketAdapter(account))
    account = _create_account(db_session, created_user, "MAN-REG-102")
    trade = MT5TradeHistory(
        user_id=created_user.id,
        trading_account_id=account.id,
        position_ticket=60001,
        symbol="XAUUSD",
        side="SELL",
        trade_source="manual",
        volume=0.4,
        open_price=2320.2,
        close_price=2319.2,
        realized_pnl=-50.0,
        open_time=datetime(2026, 4, 23, 8, 0, tzinfo=timezone.utc),
        close_time=datetime(2026, 4, 23, 8, 8, tzinfo=timezone.utc),
    )
    db_session.add(trade)
    db_session.commit()

    response = client.post(
        "/api/trade-setups/manual",
        headers=auth_headers,
        json={
            "trading_account_id": account.id,
            "symbol": "XAUUSD",
            "side": "buy",
            "estimated_entry": 2320.2,
            "sl_price": 2319.2,
            "total_risk_money": 40,
            "rr_order2": 2,
            "order_count": 1,
            "order1_ticket": 60001,
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Ticket 60001 belongs to side SELL, not BUY."


def test_closed_manual_history_trades_can_be_registered_into_one_manual_setup(client, db_session, created_user, auth_headers, monkeypatch):
    monkeypatch.setattr("app.services.manual_trade_setups.default_adapter_factory", lambda account: ManualTicketAdapter(account))
    monkeypatch.setattr("app.services.trade_setup_outcomes.default_adapter_factory", lambda account: ManualTicketAdapter(account))
    account = _create_account(db_session, created_user, "MAN-REG-103")
    trade1 = MT5TradeHistory(
        user_id=created_user.id,
        trading_account_id=account.id,
        position_ticket=66001,
        symbol="XAUUSD",
        side="BUY",
        trade_source="manual",
        volume=0.3,
        open_price=2320.1,
        close_price=2320.4,
        realized_pnl=9.0,
        open_time=datetime(2026, 4, 23, 12, 0, tzinfo=timezone.utc),
        close_time=datetime(2026, 4, 23, 12, 9, tzinfo=timezone.utc),
    )
    trade2 = MT5TradeHistory(
        user_id=created_user.id,
        trading_account_id=account.id,
        position_ticket=66002,
        symbol="XAUUSD",
        side="BUY",
        trade_source="manual",
        volume=0.3,
        open_price=2320.2,
        close_price=2320.5,
        realized_pnl=9.0,
        open_time=datetime(2026, 4, 23, 12, 1, tzinfo=timezone.utc),
        close_time=datetime(2026, 4, 23, 12, 10, tzinfo=timezone.utc),
    )
    db_session.add_all([trade1, trade2])
    db_session.commit()

    response = client.post(
        "/api/trade-setups/manual",
        headers=auth_headers,
        json={
            "trading_account_id": account.id,
            "symbol": "XAUUSD",
            "side": "buy",
            "estimated_entry": 2320.15,
            "sl_price": 2319.2,
            "total_risk_money": 57,
            "rr_order2": 2,
            "order_count": 2,
            "order1_ticket": 66001,
            "order2_ticket": 66002,
        },
    )

    assert response.status_code == 201
    data = response.json()
    assert data["order1_ticket"] == 66001
    assert data["order2_ticket"] == 66002
    assert data["setup_source"] == "manual"


def test_buy_trade_closed_by_sell_deal_still_registers_as_buy(client, db_session, created_user, auth_headers, monkeypatch):
    monkeypatch.setattr("app.services.manual_trade_setups.default_adapter_factory", lambda account: RawClosingSideAdapter(account))
    monkeypatch.setattr("app.services.trade_setup_outcomes.default_adapter_factory", lambda account: RawClosingSideAdapter(account))
    account = _create_account(db_session, created_user, "MAN-REG-RAW-BUY")
    trade = MT5TradeHistory(
        user_id=created_user.id,
        trading_account_id=account.id,
        position_ticket=60001,
        symbol="XAUUSD",
        side="BUY",
        trade_source="manual",
        volume=0.4,
        open_price=2320.2,
        close_price=2319.2,
        realized_pnl=-50.0,
        open_time=datetime(2026, 4, 23, 8, 0, tzinfo=timezone.utc),
        close_time=datetime(2026, 4, 23, 8, 8, tzinfo=timezone.utc),
    )
    db_session.add(trade)
    db_session.commit()

    response = client.post(
        "/api/trade-setups/manual",
        headers=auth_headers,
        json={
            "trading_account_id": account.id,
            "symbol": "XAUUSD",
            "side": "buy",
            "estimated_entry": 2320.2,
            "sl_price": 2319.2,
            "total_risk_money": 40,
            "rr_order2": 2,
            "order_count": 1,
            "order1_ticket": 60001,
        },
    )

    assert response.status_code == 201
    assert response.json()["setup_source"] == "manual"


def test_sell_trade_closed_by_buy_deal_still_registers_as_sell(client, db_session, created_user, auth_headers, monkeypatch):
    monkeypatch.setattr("app.services.manual_trade_setups.default_adapter_factory", lambda account: RawClosingSideAdapter(account))
    monkeypatch.setattr("app.services.trade_setup_outcomes.default_adapter_factory", lambda account: RawClosingSideAdapter(account))
    account = _create_account(db_session, created_user, "MAN-REG-RAW-SELL")
    trade = MT5TradeHistory(
        user_id=created_user.id,
        trading_account_id=account.id,
        position_ticket=67001,
        symbol="XAUUSD",
        side="SELL",
        trade_source="manual",
        volume=0.3,
        open_price=2320.1,
        close_price=2319.8,
        realized_pnl=9.0,
        open_time=datetime(2026, 4, 23, 13, 0, tzinfo=timezone.utc),
        close_time=datetime(2026, 4, 23, 13, 8, tzinfo=timezone.utc),
    )
    db_session.add(trade)
    db_session.commit()

    response = client.post(
        "/api/trade-setups/manual",
        headers=auth_headers,
        json={
            "trading_account_id": account.id,
            "symbol": "XAUUSD",
            "side": "sell",
            "estimated_entry": 2320.1,
            "sl_price": 2321.1,
            "total_risk_money": 30,
            "rr_order2": 2,
            "order_count": 1,
            "order1_ticket": 67001,
        },
    )

    assert response.status_code == 201
    assert response.json()["setup_source"] == "manual"


def test_closed_synced_manual_trades_with_raw_closing_side_can_be_grouped(client, db_session, created_user, auth_headers, monkeypatch):
    monkeypatch.setattr("app.services.manual_trade_setups.default_adapter_factory", lambda account: RawClosingSideAdapter(account))
    monkeypatch.setattr("app.services.trade_setup_outcomes.default_adapter_factory", lambda account: RawClosingSideAdapter(account))
    account = _create_account(db_session, created_user, "MAN-REG-RAW-GROUP")
    trade1 = MT5TradeHistory(
        user_id=created_user.id,
        trading_account_id=account.id,
        position_ticket=66001,
        symbol="XAUUSD",
        side="BUY",
        trade_source="manual",
        volume=0.3,
        open_price=2320.1,
        close_price=2320.4,
        realized_pnl=9.0,
        open_time=datetime(2026, 4, 23, 12, 0, tzinfo=timezone.utc),
        close_time=datetime(2026, 4, 23, 12, 9, tzinfo=timezone.utc),
    )
    trade2 = MT5TradeHistory(
        user_id=created_user.id,
        trading_account_id=account.id,
        position_ticket=66002,
        symbol="XAUUSD",
        side="BUY",
        trade_source="manual",
        volume=0.3,
        open_price=2320.2,
        close_price=2320.5,
        realized_pnl=9.0,
        open_time=datetime(2026, 4, 23, 12, 1, tzinfo=timezone.utc),
        close_time=datetime(2026, 4, 23, 12, 10, tzinfo=timezone.utc),
    )
    db_session.add_all([trade1, trade2])
    db_session.commit()

    response = client.post(
        "/api/trade-setups/manual",
        headers=auth_headers,
        json={
            "trading_account_id": account.id,
            "symbol": "XAUUSD",
            "side": "buy",
            "estimated_entry": 2320.15,
            "sl_price": 2319.2,
            "total_risk_money": 57,
            "rr_order2": 2,
            "order_count": 2,
            "order1_ticket": 66001,
            "order2_ticket": 66002,
        },
    )

    assert response.status_code == 201
    assert response.json()["order_count"] == 2


def test_single_selected_manual_trade_autofills_entry_and_sl(db_session, created_user, monkeypatch):
    monkeypatch.setattr("app.services.manual_trade_setups.default_adapter_factory", lambda account: ManualTicketAdapter(account))
    account = _create_account(db_session, created_user, "MAN-200")
    account.default_rr_order_2 = 2.5
    db_session.add(account)
    db_session.flush()
    trade = MT5TradeHistory(
            user_id=created_user.id,
            trading_account_id=account.id,
            position_ticket=60001,
            symbol="XAUUSD",
            side="buy",
            trade_source="manual",
            volume=0.4,
            open_price=2320.2,
            close_price=2319.2,
            realized_pnl=-50.0,
            open_time=datetime(2026, 4, 23, 8, 0, tzinfo=timezone.utc),
            close_time=datetime(2026, 4, 23, 8, 8, tzinfo=timezone.utc),
        )
    db_session.add(trade)
    db_session.commit()

    prefill = ManualTradeSetupService(db_session).build_prefill_from_selected_trades(user_id=created_user.id, selected_trade_ids=[trade.id])

    assert prefill["form_data"]["estimated_entry"] == 2320.2
    assert prefill["form_data"]["sl_price"] == 2319.2
    assert prefill["form_data"]["rr_order2"] == 2.5
    assert prefill["form_data"]["tp1_price"] == 2321.2
    assert prefill["form_data"]["tp2_price"] == 2322.7


def test_two_selected_manual_trades_autofill_combined_logic_and_total_risk(db_session, created_user, monkeypatch):
    monkeypatch.setattr("app.services.manual_trade_setups.default_adapter_factory", lambda account: ManualTicketAdapter(account))
    account = _create_account(db_session, created_user, "MAN-201")
    account.default_rr_order_2 = 2.0
    db_session.add(account)
    db_session.flush()
    trade1 = MT5TradeHistory(
        user_id=created_user.id,
        trading_account_id=account.id,
        position_ticket=64001,
        symbol="XAUUSD",
        side="buy",
        trade_source="manual",
        volume=0.2,
        open_price=2320.0,
        close_price=2320.3,
        realized_pnl=6.0,
        open_time=datetime(2026, 4, 23, 10, 0, tzinfo=timezone.utc),
        close_time=datetime(2026, 4, 23, 10, 5, tzinfo=timezone.utc),
    )
    trade2 = MT5TradeHistory(
        user_id=created_user.id,
        trading_account_id=account.id,
        position_ticket=64002,
        symbol="XAUUSD",
        side="buy",
        trade_source="manual",
        volume=0.4,
        open_price=2320.6,
        close_price=2321.0,
        realized_pnl=16.0,
        open_time=datetime(2026, 4, 23, 10, 40, tzinfo=timezone.utc),
        close_time=datetime(2026, 4, 23, 10, 55, tzinfo=timezone.utc),
    )
    db_session.add_all(
        [
            trade1,
            trade2,
        ]
    )
    db_session.commit()

    prefill = ManualTradeSetupService(db_session).build_prefill_from_selected_trades(user_id=created_user.id, selected_trade_ids=[trade1.id, trade2.id])

    assert round(prefill["form_data"]["estimated_entry"], 5) == round((2320.0 * 0.2 + 2320.6 * 0.4) / 0.6, 5)
    assert prefill["form_data"]["sl_price"] == 2319.2
    assert prefill["form_data"]["tp1_price"] == round(prefill["form_data"]["estimated_entry"] + (prefill["form_data"]["estimated_entry"] - 2319.2), 10)
    assert round(prefill["form_data"]["total_risk_money"], 2) == round(((2320.0 - 2319.2) * 100 * 0.2) + ((2320.6 - 2319.2) * 100 * 0.4), 2)
    assert prefill["warnings"] == ["Selected MT5 trades were opened more than 15 minutes apart. Review whether they belong to one setup."]


def test_missing_sl_requires_manual_input(db_session, created_user, monkeypatch):
    monkeypatch.setattr("app.services.manual_trade_setups.default_adapter_factory", lambda account: ManualTicketAdapter(account))
    account = _create_account(db_session, created_user, "MAN-202")
    db_session.add(account)
    db_session.flush()
    trade = MT5TradeHistory(
            user_id=created_user.id,
            trading_account_id=account.id,
            position_ticket=63001,
            symbol="XAUUSD",
            side="buy",
            trade_source="manual",
            volume=0.3,
            open_price=2320.0,
            close_price=2320.1,
            realized_pnl=3.0,
            open_time=datetime(2026, 4, 23, 9, 0, tzinfo=timezone.utc),
            close_time=datetime(2026, 4, 23, 9, 10, tzinfo=timezone.utc),
        )
    db_session.add(trade)
    db_session.commit()

    prefill = ManualTradeSetupService(db_session).build_prefill_from_selected_trades(user_id=created_user.id, selected_trade_ids=[trade.id])

    assert prefill["form_data"]["sl_price"] == ""
    assert prefill["form_data"]["total_risk_money"] == ""
    assert "Stop loss could not be derived from the selected MT5 trades. Enter it manually." in prefill["messages"]


def test_conflicting_selected_orders_are_rejected(db_session, created_user, monkeypatch):
    monkeypatch.setattr("app.services.manual_trade_setups.default_adapter_factory", lambda account: ManualTicketAdapter(account))
    account = _create_account(db_session, created_user, "MAN-203")
    db_session.add(account)
    db_session.flush()
    trade1 = MT5TradeHistory(
        user_id=created_user.id,
        trading_account_id=account.id,
        position_ticket=65001,
        symbol="XAUUSD",
        side="buy",
        trade_source="manual",
        volume=0.2,
        open_price=2320.1,
        close_price=2320.4,
        realized_pnl=6.0,
        open_time=datetime(2026, 4, 23, 11, 0, tzinfo=timezone.utc),
        close_time=datetime(2026, 4, 23, 11, 8, tzinfo=timezone.utc),
    )
    trade2 = MT5TradeHistory(
        user_id=created_user.id,
        trading_account_id=account.id,
        position_ticket=65002,
        symbol="XAUUSD",
        side="buy",
        trade_source="manual",
        volume=0.2,
        open_price=2320.2,
        close_price=2320.6,
        realized_pnl=8.0,
        open_time=datetime(2026, 4, 23, 11, 2, tzinfo=timezone.utc),
        close_time=datetime(2026, 4, 23, 11, 14, tzinfo=timezone.utc),
    )
    db_session.add_all(
        [
            trade1,
            trade2,
        ]
    )
    db_session.commit()

    try:
        ManualTradeSetupService(db_session).build_prefill_from_selected_trades(user_id=created_user.id, selected_trade_ids=[trade1.id, trade2.id])
        assert False, "Expected conflicting SL rejection"
    except ValueError as exc:
        assert str(exc) == "Selected MT5 trades have conflicting stop loss values. Review them manually before creating one setup."


def test_two_matching_buy_trades_can_be_selected_successfully_even_with_uppercase_db_side(db_session, created_user, monkeypatch):
    monkeypatch.setattr("app.services.manual_trade_setups.default_adapter_factory", lambda account: ManualTicketAdapter(account))
    account = _create_account(db_session, created_user, "MAN-204")
    db_session.add(account)
    db_session.flush()
    trade1 = MT5TradeHistory(
        user_id=created_user.id,
        trading_account_id=account.id,
        position_ticket=60001,
        symbol="xauusd",
        side="BUY",
        trade_source="manual",
        volume=0.4,
        open_price=2320.2,
        close_price=2319.2,
        realized_pnl=-50.0,
        open_time=datetime(2026, 4, 23, 8, 0, tzinfo=timezone.utc),
        close_time=datetime(2026, 4, 23, 8, 8, tzinfo=timezone.utc),
    )
    trade2 = MT5TradeHistory(
        user_id=created_user.id,
        trading_account_id=account.id,
        position_ticket=60002,
        symbol="XAUUSD",
        side="buy",
        trade_source="manual",
        volume=0.4,
        open_price=2320.2,
        close_price=2319.2,
        realized_pnl=-50.0,
        open_time=datetime(2026, 4, 23, 8, 1, tzinfo=timezone.utc),
        close_time=datetime(2026, 4, 23, 8, 9, tzinfo=timezone.utc),
    )
    db_session.add_all([trade1, trade2])
    db_session.commit()

    prefill = ManualTradeSetupService(db_session).build_prefill_from_selected_trades(
        user_id=created_user.id,
        selected_trade_ids=[trade1.id, trade2.id],
    )

    assert prefill["form_data"]["side"] == "buy"
    assert prefill["form_data"]["symbol"] == "XAUUSD"
    assert prefill["form_data"]["order_count"] == 2


def test_stale_ui_payload_does_not_block_valid_matching_trade_selection(client, db_session, created_user, monkeypatch):
    monkeypatch.setattr("app.services.manual_trade_setups.default_adapter_factory", lambda account: ManualTicketAdapter(account))
    account = _create_account(db_session, created_user, "MAN-205")
    trade1 = MT5TradeHistory(
        user_id=created_user.id,
        trading_account_id=account.id,
        position_ticket=60001,
        symbol="XAUUSD",
        side="BUY",
        trade_source="manual",
        volume=0.4,
        open_price=2320.2,
        close_price=2319.2,
        realized_pnl=-50.0,
        open_time=datetime(2026, 4, 23, 8, 0, tzinfo=timezone.utc),
        close_time=datetime(2026, 4, 23, 8, 8, tzinfo=timezone.utc),
    )
    trade2 = MT5TradeHistory(
        user_id=created_user.id,
        trading_account_id=account.id,
        position_ticket=60002,
        symbol="XAUUSD",
        side="BUY",
        trade_source="manual",
        volume=0.4,
        open_price=2320.2,
        close_price=2319.2,
        realized_pnl=-50.0,
        open_time=datetime(2026, 4, 23, 8, 1, tzinfo=timezone.utc),
        close_time=datetime(2026, 4, 23, 8, 9, tzinfo=timezone.utc),
    )
    db_session.add_all([trade1, trade2])
    db_session.commit()
    client.post("/api/auth/login", data={"email": created_user.email, "password": "password123"}, follow_redirects=False)

    response = client.get(
        f"/trade-setups/manual/create?selected_trade={trade1.id}&selected_trade={trade2.id}"
    )

    assert response.status_code == 200
    assert "side mismatch" not in response.text
    assert "Order 1 ticket" in response.text
    assert 'value="60001"' in response.text
    assert 'value="60002"' in response.text


def test_real_mixed_buy_sell_trades_are_still_rejected(db_session, created_user, monkeypatch):
    monkeypatch.setattr("app.services.manual_trade_setups.default_adapter_factory", lambda account: ManualTicketAdapter(account))
    account = _create_account(db_session, created_user, "MAN-206")
    db_session.add(account)
    db_session.flush()
    trade1 = MT5TradeHistory(
        user_id=created_user.id,
        trading_account_id=account.id,
        position_ticket=60001,
        symbol="XAUUSD",
        side="BUY",
        trade_source="manual",
        volume=0.4,
        open_price=2320.2,
        close_price=2319.2,
        realized_pnl=-50.0,
        open_time=datetime(2026, 4, 23, 8, 0, tzinfo=timezone.utc),
        close_time=datetime(2026, 4, 23, 8, 8, tzinfo=timezone.utc),
    )
    trade2 = MT5TradeHistory(
        user_id=created_user.id,
        trading_account_id=account.id,
        position_ticket=60002,
        symbol="XAUUSD",
        side="SELL",
        trade_source="manual",
        volume=0.4,
        open_price=2320.2,
        close_price=2319.2,
        realized_pnl=-50.0,
        open_time=datetime(2026, 4, 23, 8, 1, tzinfo=timezone.utc),
        close_time=datetime(2026, 4, 23, 8, 9, tzinfo=timezone.utc),
    )
    db_session.add_all([trade1, trade2])
    db_session.commit()

    try:
        ManualTradeSetupService(db_session).build_prefill_from_selected_trades(
            user_id=created_user.id,
            selected_trade_ids=[trade1.id, trade2.id],
        )
        assert False, "Expected mixed-side rejection"
    except ValueError as exc:
        assert str(exc) == "Selected MT5 trades must have the same side."


def test_synced_buy_with_blank_live_side_is_allowed(db_session, created_user, monkeypatch):
    monkeypatch.setattr("app.services.manual_trade_setups.default_adapter_factory", lambda account: ManualTicketAdapter(account))
    account = _create_account(db_session, created_user, "MAN-207")
    db_session.add(account)
    db_session.flush()
    trade1 = MT5TradeHistory(
        user_id=created_user.id,
        trading_account_id=account.id,
        position_ticket=66001,
        symbol="XAUUSD",
        side="BUY",
        trade_source="manual",
        volume=0.3,
        open_price=2320.1,
        close_price=2320.4,
        realized_pnl=9.0,
        open_time=datetime(2026, 4, 23, 12, 0, tzinfo=timezone.utc),
        close_time=datetime(2026, 4, 23, 12, 9, tzinfo=timezone.utc),
    )
    trade2 = MT5TradeHistory(
        user_id=created_user.id,
        trading_account_id=account.id,
        position_ticket=66002,
        symbol="XAUUSD",
        side="BUY",
        trade_source="manual",
        volume=0.3,
        open_price=2320.2,
        close_price=2320.5,
        realized_pnl=9.0,
        open_time=datetime(2026, 4, 23, 12, 1, tzinfo=timezone.utc),
        close_time=datetime(2026, 4, 23, 12, 10, tzinfo=timezone.utc),
    )
    db_session.add_all([trade1, trade2])
    db_session.commit()

    prefill = ManualTradeSetupService(db_session).build_prefill_from_selected_trades(
        user_id=created_user.id,
        selected_trade_ids=[trade1.id, trade2.id],
    )

    assert prefill["form_data"]["side"] == "buy"
    assert prefill["form_data"]["order_count"] == 2


def test_synced_sell_with_blank_live_side_is_allowed(db_session, created_user, monkeypatch):
    monkeypatch.setattr("app.services.manual_trade_setups.default_adapter_factory", lambda account: ManualTicketAdapter(account))
    account = _create_account(db_session, created_user, "MAN-208")
    db_session.add(account)
    db_session.flush()
    trade = MT5TradeHistory(
        user_id=created_user.id,
        trading_account_id=account.id,
        position_ticket=67001,
        symbol="XAUUSD",
        side="SELL",
        trade_source="manual",
        volume=0.3,
        open_price=2320.1,
        close_price=2319.8,
        realized_pnl=9.0,
        open_time=datetime(2026, 4, 23, 13, 0, tzinfo=timezone.utc),
        close_time=datetime(2026, 4, 23, 13, 8, tzinfo=timezone.utc),
    )
    db_session.add(trade)
    db_session.commit()

    prefill = ManualTradeSetupService(db_session).build_prefill_from_selected_trades(
        user_id=created_user.id,
        selected_trade_ids=[trade.id],
    )

    assert prefill["form_data"]["side"] == "sell"
    assert prefill["form_data"]["order1_ticket"] == 67001


def test_manual_derived_fields_recompute_after_stop_loss_is_entered(db_session, created_user, monkeypatch):
    monkeypatch.setattr("app.services.manual_trade_setups.default_adapter_factory", lambda account: ManualTicketAdapter(account))
    account = _create_account(db_session, created_user, "MAN-209")
    account.default_rr_order_2 = 2.0
    db_session.add(account)
    db_session.commit()

    result = ManualTradeSetupService(db_session).derive_fields_from_form(
        user_id=created_user.id,
        trading_account_id=account.id,
        symbol="XAUUSD",
        side="buy",
        estimated_entry=2320.0,
        sl_price=2319.2,
        rr_order2=2.0,
        order_count=1,
        order1_ticket=63001,
        order2_ticket=None,
    )

    assert result["tp1_price"] == 2320.8
    assert result["tp2_price"] == 2321.6
    assert result["total_risk_money"] == 24.0


def test_manual_derived_fields_recompute_after_rr_change(db_session, created_user, monkeypatch):
    monkeypatch.setattr("app.services.manual_trade_setups.default_adapter_factory", lambda account: ManualTicketAdapter(account))
    account = _create_account(db_session, created_user, "MAN-210")
    db_session.add(account)
    db_session.commit()

    result = ManualTradeSetupService(db_session).derive_fields_from_form(
        user_id=created_user.id,
        trading_account_id=account.id,
        symbol="XAUUSD",
        side="buy",
        estimated_entry=2320.2,
        sl_price=2319.2,
        rr_order2=3.0,
        order_count=1,
        order1_ticket=60001,
        order2_ticket=None,
    )

    assert result["tp1_price"] == 2321.2
    assert result["tp2_price"] == 2323.2
    assert result["total_risk_money"] == 40.0


def test_manual_derived_fields_use_selected_order_volumes_for_combined_risk(db_session, created_user, monkeypatch):
    monkeypatch.setattr("app.services.manual_trade_setups.default_adapter_factory", lambda account: ManualTicketAdapter(account))
    account = _create_account(db_session, created_user, "MAN-211")
    db_session.add(account)
    db_session.commit()

    result = ManualTradeSetupService(db_session).derive_fields_from_form(
        user_id=created_user.id,
        trading_account_id=account.id,
        symbol="XAUUSD",
        side="buy",
        estimated_entry=2320.4,
        sl_price=2319.2,
        rr_order2=2.0,
        order_count=2,
        order1_ticket=64001,
        order2_ticket=64002,
    )

    assert result["tp1_price"] == 2321.6
    assert result["tp2_price"] == 2322.8
    assert result["total_risk_money"] == 72.0


def test_trade_setup_pages_show_auto_refresh_indicators(client, db_session, created_user):
    account = _create_account(db_session, created_user, "MAN-212")
    setup = create_trade_setup(
        db_session,
        created_user.id,
        TradeSetupCreate(**_build_setup_payload(account.id)),
    )
    db_session.commit()
    _login_web_session(client, created_user.email)

    list_response = client.get("/trade-setups")
    detail_response = client.get(f"/trade-setups/{setup.id}")

    assert list_response.status_code == 200
    assert 'data-auto-refresh-interval="45"' in list_response.text
    assert "Auto refresh every 45s" in list_response.text
    assert detail_response.status_code == 200
    assert 'data-auto-refresh-interval="30"' in detail_response.text
    assert "Auto refresh every 30s" in detail_response.text


def test_trade_setup_pages_include_mobile_cards_and_preview_is_single_column_ready(client, db_session, created_user):
    account = _create_account(db_session, created_user, "MAN-213")
    create_trade_setup(
        db_session,
        created_user.id,
        TradeSetupCreate(**_build_setup_payload(account.id)),
    )
    _add_manual_trade_history_row(
        db_session,
        user_id=created_user.id,
        trading_account_id=account.id,
        position_ticket=68001,
        symbol="XAUUSD",
        side="BUY",
        volume=0.3,
        open_price=2320.1,
        close_price=2320.4,
        realized_pnl=9.0,
        open_time=datetime(2026, 4, 23, 12, 0, tzinfo=timezone.utc),
        close_time=datetime(2026, 4, 23, 12, 9, tzinfo=timezone.utc),
    )
    db_session.commit()
    _login_web_session(client, created_user.email)

    setup_list_response = client.get("/trade-setups")
    manual_response = client.get("/trade-setups/manual/create")
    preview_response = client.get("/trade-setups/preview")

    assert setup_list_response.status_code == 200
    assert "mobile-data-card" in setup_list_response.text
    assert manual_response.status_code == 200
    assert "mobile-select-card" in manual_response.text
    assert preview_response.status_code == 200
    assert "preview-primary-grid" in preview_response.text
    assert 'class="action-row"' in preview_response.text
