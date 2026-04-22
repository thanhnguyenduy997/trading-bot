from app.execution.base import AdapterError
from app.models.app_setting import AppSetting
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
        self.placed_orders = []
        self.closed_positions = []

    def connect(self):
        return None

    def get_account_info(self):
        return {}

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

    def execute_setup(self, setup):
        return {"status": "executed", "setup_id": setup.id}

    def place_market_order(self, **kwargs):
        ticket = 1000 + len(self.placed_orders) + 1
        self.placed_orders.append({**kwargs, "ticket": ticket})
        return {"ticket": ticket, "order": ticket, "deal": ticket + 5000}

    def close_position(self, **kwargs):
        self.closed_positions.append(kwargs)
        return {"ticket": kwargs["position_ticket"], "order": kwargs["position_ticket"]}

    def close(self):
        return None


class UnavailableExecutionAdapter(SuccessfulExecutionAdapter):
    def execute_setup(self, setup):
        raise AdapterError(
            code="mt5_execution_unavailable",
            message="MT5 execution unavailable on this machine.",
            details={"hint": "Use Windows with MetaTrader5 installed."},
        )

    def place_market_order(self, **kwargs):
        raise AdapterError(
            code="mt5_execution_unavailable",
            message="MT5 execution unavailable on this machine.",
            details={"hint": "Use Windows with MetaTrader5 installed."},
        )


class PartialFailureExecutionAdapter(SuccessfulExecutionAdapter):
    def place_market_order(self, **kwargs):
        if len(self.placed_orders) == 0:
            return super().place_market_order(**kwargs)
        raise AdapterError(
            code="mt5_order_rejected",
            message="Order placement rejected for XAUUSD.",
            details={"order": 2},
        )


class HighDriftExecutionAdapter(SuccessfulExecutionAdapter):
    def get_quote(self, symbol: str):
        return {"symbol": symbol, "bid": 2329.0, "ask": 2329.2}


class LowDriftExecutionAdapter(SuccessfulExecutionAdapter):
    def get_quote(self, symbol: str):
        return {"symbol": symbol, "bid": 2320.05, "ask": 2320.25}


class MediumDriftExecutionAdapter(SuccessfulExecutionAdapter):
    def get_quote(self, symbol: str):
        return {"symbol": symbol, "bid": 2320.10, "ask": 2320.30}


class SessionMismatchExecutionAdapter(SuccessfulExecutionAdapter):
    def connect(self):
        raise AdapterError(
            code="mt5_session_mismatch",
            message=(
                f"MT5 terminal is logged into account 999999, but this trading account expects {self.account.account_number}. "
                "Log into the correct MT5 account in the terminal first."
            ),
            details={"current_login": "999999", "expected_account": self.account.account_number},
        )


def test_executing_own_setup(client, db_session, created_user, auth_headers, monkeypatch, sync_account_symbols):
    monkeypatch.setattr(
        "app.services.trade_setup_execution.default_adapter_factory",
        lambda account: SuccessfulExecutionAdapter(account),
    )
    account = _create_account(db_session, created_user)
    sync_account_symbols(account, "XAUUSD")
    setup = _create_setup(db_session, created_user, account)

    response = client.post(f"/api/trade-setups/{setup.id}/execute", headers=auth_headers)

    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["status"] == "executed"
    assert data["order1_ticket"] == 1001
    assert data["order2_ticket"] == 1002
    stored = db_session.query(TradeSetup).filter(TradeSetup.id == setup.id).first()
    assert stored is not None
    assert stored.status == "executed"
    assert stored.executed_at is not None
    assert stored.order1_ticket == 1001
    assert stored.order2_ticket == 1002


def test_rejecting_another_users_setup(client, db_session, created_user, auth_headers, monkeypatch, sync_account_symbols):
    monkeypatch.setattr(
        "app.services.trade_setup_execution.default_adapter_factory",
        lambda account: SuccessfulExecutionAdapter(account),
    )
    current_account = _create_account(db_session, created_user, "123456")
    sync_account_symbols(current_account, "XAUUSD")
    other_user = create_user(
        db_session,
        UserCreate(email="execute-other@example.com", password="password123", full_name="Other User"),
    )
    account = _create_account(db_session, other_user, "123457")
    sync_account_symbols(account, "XAUUSD")
    setup = _create_setup(db_session, other_user, account)

    response = client.post(f"/api/trade-setups/{setup.id}/execute", headers=auth_headers)

    assert response.status_code == 404
    assert response.json()["detail"] == "Trade setup not found"


def test_graceful_failure_when_execution_unavailable(client, db_session, created_user, auth_headers, monkeypatch, sync_account_symbols):
    monkeypatch.setattr(
        "app.services.trade_setup_execution.default_adapter_factory",
        lambda account: UnavailableExecutionAdapter(account),
    )
    account = _create_account(db_session, created_user, "123458")
    sync_account_symbols(account, "XAUUSD")
    setup = _create_setup(db_session, created_user, account)

    response = client.post(f"/api/trade-setups/{setup.id}/execute", headers=auth_headers)

    assert response.status_code == 503
    assert response.json()["detail"]["message"] == "MT5 execution unavailable on this machine."
    stored = db_session.query(TradeSetup).filter(TradeSetup.id == setup.id).first()
    assert stored is not None
    assert stored.status == "failed"
    assert "MT5 execution unavailable on this machine." in stored.execution_error
    events = (
        db_session.query(TradeEvent)
        .filter(TradeEvent.setup_id == setup.id)
        .order_by(TradeEvent.id.asc())
        .all()
    )
    event_types = [event.event_type for event in events]
    assert "order1_rejected" in event_types
    assert "execute_failed" in event_types


def test_order1_succeeds_order2_fails_and_rollback_happens(
    client,
    db_session,
    created_user,
    auth_headers,
    monkeypatch,
    sync_account_symbols,
):
    monkeypatch.setattr(
        "app.services.trade_setup_execution.default_adapter_factory",
        lambda account: PartialFailureExecutionAdapter(account),
    )
    account = _create_account(db_session, created_user, "123459")
    sync_account_symbols(account, "XAUUSD")
    setup = _create_setup(db_session, created_user, account)

    response = client.post(f"/api/trade-setups/{setup.id}/execute", headers=auth_headers)

    assert response.status_code == 503
    assert response.json()["detail"]["message"] == "Order placement rejected for XAUUSD."
    stored = db_session.query(TradeSetup).filter(TradeSetup.id == setup.id).first()
    assert stored is not None
    assert stored.status == "failed"
    assert stored.order1_ticket is None
    assert stored.order2_ticket is None

    events = (
        db_session.query(TradeEvent)
        .filter(TradeEvent.setup_id == setup.id)
        .order_by(TradeEvent.id.asc())
        .all()
    )
    event_types = [event.event_type for event in events]
    assert "setup_saved" in event_types
    assert "execute_requested" in event_types
    assert "order1_opened" in event_types
    assert "order2_rejected" in event_types
    assert "rollback_started" in event_types
    assert "rollback_completed" in event_types
    assert "execute_failed" in event_types


def test_execute_succeeds_when_drift_is_within_threshold(
    client,
    db_session,
    created_user,
    auth_headers,
    monkeypatch,
    sync_account_symbols,
):
    monkeypatch.setattr(
        "app.services.trade_setup_execution.default_adapter_factory",
        lambda account: LowDriftExecutionAdapter(account),
    )
    db_session.add(AppSetting(id=1, max_preview_drift_percent=25))
    db_session.commit()
    account = _create_account(db_session, created_user, "123460")
    sync_account_symbols(account, "XAUUSD")
    setup = _create_setup(db_session, created_user, account)

    response = client.post(f"/api/trade-setups/{setup.id}/execute", headers=auth_headers)

    assert response.status_code == 200
    stored = db_session.query(TradeSetup).filter(TradeSetup.id == setup.id).first()
    assert stored is not None
    assert stored.status == "executed"


def test_execute_is_rejected_when_drift_exceeds_threshold(
    client,
    db_session,
    created_user,
    auth_headers,
    monkeypatch,
    sync_account_symbols,
):
    monkeypatch.setattr(
        "app.services.trade_setup_execution.default_adapter_factory",
        lambda account: HighDriftExecutionAdapter(account),
    )
    db_session.add(AppSetting(id=1, max_preview_drift_percent=5))
    db_session.commit()
    account = _create_account(db_session, created_user, "123461")
    sync_account_symbols(account, "XAUUSD")
    setup = _create_setup(db_session, created_user, account)

    response = client.post(f"/api/trade-setups/{setup.id}/execute", headers=auth_headers)

    assert response.status_code == 409
    assert "Market conditions have changed beyond the allowed threshold. Please preview again before executing." in response.json()["detail"]
    assert "Detected drift:" in response.json()["detail"]


def test_per_account_drift_override_takes_precedence_over_global_default(
    client,
    db_session,
    created_user,
    auth_headers,
    monkeypatch,
    sync_account_symbols,
):
    monkeypatch.setattr(
        "app.services.trade_setup_execution.default_adapter_factory",
        lambda account: MediumDriftExecutionAdapter(account),
    )
    db_session.add(AppSetting(id=1, max_preview_drift_percent=5))
    db_session.commit()
    account = _create_account(db_session, created_user, "123462")
    account.max_preview_drift_percent_override = 15
    db_session.add(account)
    db_session.commit()
    db_session.refresh(account)
    sync_account_symbols(account, "XAUUSD")
    setup = _create_setup(db_session, created_user, account)

    response = client.post(f"/api/trade-setups/{setup.id}/execute", headers=auth_headers)

    assert response.status_code == 200


def test_preview_drift_reject_event_is_logged(
    client,
    db_session,
    created_user,
    auth_headers,
    monkeypatch,
    sync_account_symbols,
):
    monkeypatch.setattr(
        "app.services.trade_setup_execution.default_adapter_factory",
        lambda account: HighDriftExecutionAdapter(account),
    )
    db_session.add(AppSetting(id=1, max_preview_drift_percent=5))
    db_session.commit()
    account = _create_account(db_session, created_user, "123463")
    sync_account_symbols(account, "XAUUSD")
    setup = _create_setup(db_session, created_user, account)

    response = client.post(f"/api/trade-setups/{setup.id}/execute", headers=auth_headers)

    assert response.status_code == 409
    event = (
        db_session.query(TradeEvent)
        .filter(TradeEvent.setup_id == setup.id, TradeEvent.event_type == "preview_drift_reject")
        .first()
    )
    assert event is not None
    assert '"account_id": {}'.format(account.id) in (event.details or "")
    assert '"configured_threshold_percent": 5.0' in (event.details or "")


def test_preview_drift_rejection_message_is_shown_on_html_execute_flow(
    client,
    db_session,
    created_user,
    monkeypatch,
    sync_account_symbols,
):
    monkeypatch.setattr(
        "app.services.trade_setup_execution.default_adapter_factory",
        lambda account: HighDriftExecutionAdapter(account),
    )
    db_session.add(AppSetting(id=1, max_preview_drift_percent=5))
    db_session.commit()
    account = _create_account(db_session, created_user, "123464")
    sync_account_symbols(account, "XAUUSD")
    setup = _create_setup(db_session, created_user, account)
    client.post("/api/auth/login", data={"email": created_user.email, "password": "password123"}, follow_redirects=False)

    response = client.post(f"/trade-setups/{setup.id}/execute")

    assert response.status_code == 409
    assert "Market conditions have changed beyond the allowed threshold. Please preview again before executing." in response.text
    assert "Preview Again" in response.text


def test_execute_is_blocked_when_mt5_session_does_not_match(
    client,
    db_session,
    created_user,
    auth_headers,
    monkeypatch,
    sync_account_symbols,
):
    monkeypatch.setattr(
        "app.services.trade_setup_execution.default_adapter_factory",
        lambda account: SessionMismatchExecutionAdapter(account),
    )
    account = _create_account(db_session, created_user, "123465")
    sync_account_symbols(account, "XAUUSD")
    setup = _create_setup(db_session, created_user, account)

    response = client.post(f"/api/trade-setups/{setup.id}/execute", headers=auth_headers)

    assert response.status_code == 503
    assert response.json()["detail"]["message"].startswith("MT5 terminal is logged into account 999999")
    stored_setup = db_session.query(TradeSetup).filter(TradeSetup.id == setup.id).first()
    assert stored_setup is not None
    assert stored_setup.status == "failed"
    events = (
        db_session.query(TradeEvent)
        .filter(TradeEvent.setup_id == setup.id)
        .order_by(TradeEvent.id.asc())
        .all()
    )
    event_types = [event.event_type for event in events]
    assert "execute_blocked_account_mismatch" in event_types
    db_session.refresh(account)
    assert account.mt5_session_status == "mismatch"
    assert account.current_mt5_login == "999999"
