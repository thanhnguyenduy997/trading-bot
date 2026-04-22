from app.execution.base import AdapterError
from app.models.trade_event import TradeEvent
from app.models.trade_setup import TradeSetup
from app.schemas.trade_setup import TradeSetupCreate
from app.schemas.trading_account import TradingAccountCreate
from app.services.trade_setups import create_trade_setup
from app.services.trading_accounts import create_trading_account
from app.services.users import create_user
from app.schemas.user import UserCreate


def _create_account(db_session, user, account_number: str = "223456"):
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


def _create_executed_setup(db_session, user, account):
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
            status="executed",
        ),
    )
    setup.status = "executed"
    setup.order1_ticket = 8001
    setup.order2_ticket = 8002
    setup.monitoring_status = "waiting_tp1"
    db_session.add(setup)
    db_session.commit()
    db_session.refresh(setup)
    return setup


class MonitoringAdapter:
    def __init__(self, account, *, order1_open=False, order2_sl=2319.2, modify_error: AdapterError | None = None):
        self.account = account
        self.order1_open = order1_open
        self.order2_sl = order2_sl
        self.modify_error = modify_error
        self.modified = []

    def connect(self):
        return None

    def get_account_info(self):
        return {}

    def get_quote(self, symbol: str):
        return {}

    def get_symbol_info(self, symbol: str):
        return {}

    def execute_setup(self, setup):
        return {}

    def place_market_order(self, **kwargs):
        return {}

    def close_position(self, **kwargs):
        return {}

    def get_position(self, *, position_ticket: int):
        if position_ticket == 8001 and self.order1_open:
            return {
                "ticket": 8001,
                "symbol": "XAUUSD",
                "volume": 0.5,
                "price_open": 2320.2,
                "sl": 2319.2,
                "tp": 2321.2,
                "side": "buy",
            }
        if position_ticket == 8002:
            return {
                "ticket": 8002,
                "symbol": "XAUUSD",
                "volume": 0.5,
                "price_open": 2320.2,
                "sl": self.order2_sl,
                "tp": 2322.2,
                "side": "buy",
            }
        return None

    def get_position_history(self, *, position_ticket: int):
        if position_ticket != 8001:
            return []
        return [
            {
                "position_id": 8001,
                "entry": "out",
                "reason": "tp",
                "price": 2321.2,
                "point": 0.1,
            }
        ]

    def modify_position_sl(self, **kwargs):
        if self.modify_error is not None:
            raise self.modify_error
        self.modified.append(kwargs)
        return {"ticket": kwargs["position_ticket"], "sl": kwargs["sl"], "tp": kwargs["tp"]}

    def close(self):
        return None


class SessionMismatchMonitoringAdapter(MonitoringAdapter):
    def get_account_info(self):
        raise AdapterError(
            code="mt5_session_mismatch",
            message=(
                f"MT5 terminal is logged into account 999999, but this trading account expects {self.account.account_number}. "
                "Log into the correct MT5 account in the terminal first."
            ),
            details={"current_login": "999999", "expected_account": self.account.account_number},
        )


def test_tp1_hit_moves_order2_to_breakeven(client, db_session, created_user, auth_headers, monkeypatch):
    monkeypatch.setattr(
        "app.services.trade_setup_monitoring.default_adapter_factory",
        lambda account: MonitoringAdapter(account),
    )
    account = _create_account(db_session, created_user)
    setup = _create_executed_setup(db_session, created_user, account)

    response = client.post(f"/api/trade-setups/{setup.id}/monitor", headers=auth_headers)

    assert response.status_code == 200
    data = response.json()
    assert data["monitoring_status"] == "be_moved"
    assert data["be_moved"] is True
    stored = db_session.query(TradeSetup).filter(TradeSetup.id == setup.id).first()
    assert stored is not None
    assert stored.monitoring_status == "be_moved"
    assert stored.order2_be_moved_at is not None
    assert stored.order2_be_move_error is None
    events = (
        db_session.query(TradeEvent)
        .filter(TradeEvent.setup_id == setup.id)
        .order_by(TradeEvent.id.asc())
        .all()
    )
    event_types = [event.event_type for event in events]
    assert "tp1_hit" in event_types
    assert "be_move_requested" in event_types
    assert "be_move_completed" in event_types


def test_be_move_failure_creates_event_and_error(client, db_session, created_user, auth_headers, monkeypatch):
    monkeypatch.setattr(
        "app.services.trade_setup_monitoring.default_adapter_factory",
        lambda account: MonitoringAdapter(
            account,
            modify_error=AdapterError(
                code="mt5_modify_sl_rejected",
                message="Stop-loss modification rejected for XAUUSD.",
                details={"retcode": 10016},
            ),
        ),
    )
    account = _create_account(db_session, created_user, "223457")
    setup = _create_executed_setup(db_session, created_user, account)

    response = client.post(f"/api/trade-setups/{setup.id}/monitor", headers=auth_headers)

    assert response.status_code == 503
    assert response.json()["detail"]["message"] == "Stop-loss modification rejected for XAUUSD."
    stored = db_session.query(TradeSetup).filter(TradeSetup.id == setup.id).first()
    assert stored is not None
    assert stored.monitoring_status == "be_move_failed"
    assert stored.order2_be_move_error == "Stop-loss modification rejected for XAUUSD."
    events = (
        db_session.query(TradeEvent)
        .filter(TradeEvent.setup_id == setup.id)
        .order_by(TradeEvent.id.asc())
        .all()
    )
    assert any(event.event_type == "be_move_failed" for event in events)


def test_no_be_move_when_order1_is_still_open(client, db_session, created_user, auth_headers, monkeypatch):
    monkeypatch.setattr(
        "app.services.trade_setup_monitoring.default_adapter_factory",
        lambda account: MonitoringAdapter(account, order1_open=True),
    )
    account = _create_account(db_session, created_user, "223458")
    setup = _create_executed_setup(db_session, created_user, account)

    response = client.post(f"/api/trade-setups/{setup.id}/monitor", headers=auth_headers)

    assert response.status_code == 200
    data = response.json()
    assert data["monitoring_status"] == "waiting_tp1"
    assert data["be_moved"] is False
    stored = db_session.query(TradeSetup).filter(TradeSetup.id == setup.id).first()
    assert stored is not None
    assert stored.order2_be_moved_at is None


def test_no_unnecessary_modification_when_order2_already_at_be(
    client,
    db_session,
    created_user,
    auth_headers,
    monkeypatch,
):
    monkeypatch.setattr(
        "app.services.trade_setup_monitoring.default_adapter_factory",
        lambda account: MonitoringAdapter(account, order2_sl=2320.2),
    )
    account = _create_account(db_session, created_user, "223459")
    setup = _create_executed_setup(db_session, created_user, account)

    response = client.post(f"/api/trade-setups/{setup.id}/monitor", headers=auth_headers)

    assert response.status_code == 200
    data = response.json()
    assert data["monitoring_status"] == "be_already_set"
    assert data["be_moved"] is False
    stored = db_session.query(TradeSetup).filter(TradeSetup.id == setup.id).first()
    assert stored is not None
    assert stored.order2_be_moved_at is None


def test_reject_monitoring_another_users_setup(client, db_session, created_user, auth_headers, monkeypatch):
    monkeypatch.setattr(
        "app.services.trade_setup_monitoring.default_adapter_factory",
        lambda account: MonitoringAdapter(account),
    )
    other_user = create_user(
        db_session,
        UserCreate(email="monitor-other@example.com", password="password123", full_name="Other User"),
    )
    account = _create_account(db_session, other_user, "223460")
    setup = _create_executed_setup(db_session, other_user, account)

    response = client.post(f"/api/trade-setups/{setup.id}/monitor", headers=auth_headers)

    assert response.status_code == 404
    assert response.json()["detail"] == "Trade setup not found"


def test_monitoring_session_mismatch_creates_skip_event(client, db_session, created_user, auth_headers, monkeypatch):
    monkeypatch.setattr(
        "app.services.trade_setup_monitoring.default_adapter_factory",
        lambda account: SessionMismatchMonitoringAdapter(account),
    )
    account = _create_account(db_session, created_user, "223461")
    setup = _create_executed_setup(db_session, created_user, account)

    response = client.post(f"/api/trade-setups/{setup.id}/monitor", headers=auth_headers)

    assert response.status_code == 503
    events = (
        db_session.query(TradeEvent)
        .filter(TradeEvent.setup_id == setup.id)
        .order_by(TradeEvent.id.asc())
        .all()
    )
    event_types = [event.event_type for event in events]
    assert "monitor_skipped_account_mismatch" in event_types
