from app.execution.base import AdapterError
from app.models.trade_event import TradeEvent
from app.models.trade_setup import TradeSetup
from app.schemas.trade_setup import TradeSetupCreate
from app.schemas.trading_account import TradingAccountCreate
from app.schemas.user import UserCreate
from app.services.trade_setups import create_trade_setup
from app.services.trading_accounts import create_trading_account
from app.services.users import create_user


def _create_account(db_session, user, account_number: str = "123456"):
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


def _create_setup(db_session, user, account):
    return create_trade_setup(
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


class SuccessfulExecutionAdapter:
    def __init__(self, account):
        self.account = account

    def connect(self):
        return None

    def get_account_info(self):
        return {}

    def get_quote(self, symbol: str):
        return {}

    def get_symbol_info(self, symbol: str):
        return {}

    def execute_setup(self, setup):
        return {"status": "executed", "setup_id": setup.id}

    def close(self):
        return None


class UnavailableExecutionAdapter(SuccessfulExecutionAdapter):
    def execute_setup(self, setup):
        raise AdapterError(
            code="mt5_execution_unavailable",
            message="MT5 execution unavailable on this machine.",
            details={"hint": "Use Windows with MetaTrader5 installed."},
        )


def test_executing_own_setup(client, db_session, created_user, auth_headers, monkeypatch):
    monkeypatch.setattr(
        "app.services.trade_setup_execution.default_adapter_factory",
        lambda account: SuccessfulExecutionAdapter(account),
    )
    account = _create_account(db_session, created_user)
    setup = _create_setup(db_session, created_user, account)

    response = client.post(f"/api/trade-setups/{setup.id}/execute", headers=auth_headers)

    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["status"] == "executed"
    stored = db_session.query(TradeSetup).filter(TradeSetup.id == setup.id).first()
    assert stored is not None
    assert stored.status == "executed"
    assert stored.executed_at is not None


def test_rejecting_another_users_setup(client, db_session, created_user, auth_headers, monkeypatch):
    monkeypatch.setattr(
        "app.services.trade_setup_execution.default_adapter_factory",
        lambda account: SuccessfulExecutionAdapter(account),
    )
    other_user = create_user(
        db_session,
        UserCreate(email="execute-other@example.com", password="password123", full_name="Other User"),
    )
    account = _create_account(db_session, other_user, "123457")
    setup = _create_setup(db_session, other_user, account)

    response = client.post(f"/api/trade-setups/{setup.id}/execute", headers=auth_headers)

    assert response.status_code == 404
    assert response.json()["detail"] == "Trade setup not found"


def test_graceful_failure_when_execution_unavailable(client, db_session, created_user, auth_headers, monkeypatch):
    monkeypatch.setattr(
        "app.services.trade_setup_execution.default_adapter_factory",
        lambda account: UnavailableExecutionAdapter(account),
    )
    account = _create_account(db_session, created_user, "123458")
    setup = _create_setup(db_session, created_user, account)

    response = client.post(f"/api/trade-setups/{setup.id}/execute", headers=auth_headers)

    assert response.status_code == 503
    assert response.json()["detail"]["message"] == "MT5 execution unavailable on this machine."
    stored = db_session.query(TradeSetup).filter(TradeSetup.id == setup.id).first()
    assert stored is not None
    assert stored.status == "failed"
    assert stored.execution_error == "MT5 execution unavailable on this machine."


def test_event_creation_on_execution_attempt(client, db_session, created_user, auth_headers, monkeypatch):
    monkeypatch.setattr(
        "app.services.trade_setup_execution.default_adapter_factory",
        lambda account: UnavailableExecutionAdapter(account),
    )
    account = _create_account(db_session, created_user, "123459")
    setup = _create_setup(db_session, created_user, account)

    response = client.post(f"/api/trade-setups/{setup.id}/execute", headers=auth_headers)

    assert response.status_code == 503
    events = (
        db_session.query(TradeEvent)
        .filter(TradeEvent.setup_id == setup.id)
        .order_by(TradeEvent.id.asc())
        .all()
    )
    event_types = [event.event_type for event in events]
    assert "setup_saved" in event_types
    assert "execute_requested" in event_types
    assert "execute_failed" in event_types
