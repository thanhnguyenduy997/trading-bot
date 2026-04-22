from datetime import datetime, timezone

from app.models.trade_event import TradeEvent
from app.models.trade_setup import TradeSetup
from app.schemas.trade_setup import TradeSetupCreate
from app.schemas.trading_account import TradingAccountCreate
from app.services.trade_setups import create_trade_setup
from app.services.trading_accounts import create_trading_account


def _create_account(db_session, user, account_number: str = "OUT-100"):
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


def _create_setup(db_session, user, account, *, status: str = "executed") -> TradeSetup:
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
            status=status,
        ),
    )
    if status == "executed":
        setup.status = "executed"
        setup.order1_ticket = 9001
        setup.order2_ticket = 9002
        setup.executed_at = datetime.now(timezone.utc)
        setup.monitoring_status = "waiting_tp1"
    elif status == "failed":
        setup.status = "failed"
        setup.execution_error = "MT5 execution unavailable on this machine."
        setup.monitoring_status = "execution_failed"
    db_session.add(setup)
    db_session.commit()
    db_session.refresh(setup)
    return setup


class OutcomeAdapter:
    def __init__(self, account, *, positions=None, histories=None):
        self.account = account
        self.positions = positions or {}
        self.histories = histories or {}

    def connect(self):
        return None

    def get_account_info(self):
        return {}

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

    def modify_position_sl(self, **kwargs):
        return {}

    def get_position(self, *, position_ticket: int):
        return self.positions.get(position_ticket)

    def get_position_history(self, *, position_ticket: int):
        return self.histories.get(position_ticket, [])

    def close(self):
        return None


def _open_position(ticket: int, tp: float) -> dict[str, object]:
    return {
        "ticket": ticket,
        "symbol": "XAUUSD",
        "volume": 0.5,
        "price_open": 2320.2,
        "sl": 2319.2,
        "tp": tp,
        "side": "buy",
    }


def _history(open_price: float, close_price: float, reason: str, profit: float, *, close_time: int = 1713771000):
    return [
        {
            "ticket": 1,
            "entry": "in",
            "price": open_price,
            "time": close_time - 60,
            "point": 0.1,
        },
        {
            "ticket": 2,
            "entry": "out",
            "reason": reason,
            "price": close_price,
            "profit": profit,
            "time": close_time,
            "point": 0.1,
        },
    ]


def test_reconcile_open_setup_when_both_orders_are_still_open(client, db_session, created_user, auth_headers, monkeypatch):
    monkeypatch.setattr(
        "app.services.trade_setup_outcomes.default_adapter_factory",
        lambda account: OutcomeAdapter(
            account,
            positions={9001: _open_position(9001, 2321.2), 9002: _open_position(9002, 2322.2)},
        ),
    )
    account = _create_account(db_session, created_user)
    setup = _create_setup(db_session, created_user, account)

    response = client.post(f"/api/trade-setups/{setup.id}/reconcile", headers=auth_headers)

    assert response.status_code == 200
    data = response.json()
    assert data["setup_outcome"] == "open"
    assert data["order1_outcome"] == "open"
    assert data["order2_outcome"] == "open"


def test_reconcile_tp1_hit_waiting_order2(client, db_session, created_user, auth_headers, monkeypatch):
    monkeypatch.setattr(
        "app.services.trade_setup_outcomes.default_adapter_factory",
        lambda account: OutcomeAdapter(
            account,
            positions={9002: _open_position(9002, 2322.2)},
            histories={9001: _history(2320.2, 2321.2, "tp", 50.0)},
        ),
    )
    account = _create_account(db_session, created_user, "OUT-101")
    setup = _create_setup(db_session, created_user, account)

    response = client.post(f"/api/trade-setups/{setup.id}/reconcile", headers=auth_headers)

    assert response.status_code == 200
    data = response.json()
    assert data["setup_outcome"] == "tp1_hit_waiting_order2"
    assert data["order1_outcome"] == "tp_hit"
    assert data["order2_outcome"] == "open"


def test_reconcile_breakeven_when_order2_closes_at_be(client, db_session, created_user, auth_headers, monkeypatch):
    monkeypatch.setattr(
        "app.services.trade_setup_outcomes.default_adapter_factory",
        lambda account: OutcomeAdapter(
            account,
            histories={
                9001: _history(2320.2, 2321.2, "tp", 50.0),
                9002: _history(2320.2, 2320.2, "sl", 0.0),
            },
        ),
    )
    account = _create_account(db_session, created_user, "OUT-102")
    setup = _create_setup(db_session, created_user, account)
    setup.order2_be_moved_at = datetime.now(timezone.utc)
    db_session.add(setup)
    db_session.commit()

    response = client.post(f"/api/trade-setups/{setup.id}/reconcile", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["setup_outcome"] == "breakeven"
    stored = db_session.query(TradeSetup).filter(TradeSetup.id == setup.id).first()
    assert stored is not None
    assert stored.order2_outcome == "closed_at_be"
    event_types = {
        event.event_type
        for event in db_session.query(TradeEvent).filter(TradeEvent.setup_id == setup.id).all()
    }
    assert "order2_closed_at_be" in event_types
    assert "setup_breakeven_recorded" in event_types


def test_reconcile_stoploss_when_both_orders_hit_sl(client, db_session, created_user, auth_headers, monkeypatch):
    monkeypatch.setattr(
        "app.services.trade_setup_outcomes.default_adapter_factory",
        lambda account: OutcomeAdapter(
            account,
            histories={
                9001: _history(2320.2, 2319.2, "sl", -50.0),
                9002: _history(2320.2, 2319.2, "sl", -50.0),
            },
        ),
    )
    account = _create_account(db_session, created_user, "OUT-103")
    setup = _create_setup(db_session, created_user, account)

    response = client.post(f"/api/trade-setups/{setup.id}/reconcile", headers=auth_headers)

    assert response.status_code == 200
    data = response.json()
    assert data["setup_outcome"] == "stoploss"
    stored = db_session.query(TradeSetup).filter(TradeSetup.id == setup.id).first()
    assert stored is not None
    assert stored.result_status == "stoploss"
    event_types = {
        event.event_type
        for event in db_session.query(TradeEvent).filter(TradeEvent.setup_id == setup.id).all()
    }
    assert "order1_sl_hit" in event_types
    assert "order2_sl_hit" in event_types
    assert "setup_stoploss_recorded" in event_types
    assert "setup_outcome_updated" in event_types


def test_reconcile_tp2_hit_when_both_orders_hit_tp(client, db_session, created_user, auth_headers, monkeypatch):
    monkeypatch.setattr(
        "app.services.trade_setup_outcomes.default_adapter_factory",
        lambda account: OutcomeAdapter(
            account,
            histories={
                9001: _history(2320.2, 2321.2, "tp", 50.0),
                9002: _history(2320.2, 2322.2, "tp", 100.0),
            },
        ),
    )
    account = _create_account(db_session, created_user, "OUT-104")
    setup = _create_setup(db_session, created_user, account)

    response = client.post(f"/api/trade-setups/{setup.id}/reconcile", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["setup_outcome"] == "tp2_hit"
    event_types = {
        event.event_type
        for event in db_session.query(TradeEvent).filter(TradeEvent.setup_id == setup.id).all()
    }
    assert "order1_tp_hit" in event_types
    assert "order2_tp_hit" in event_types
    assert "setup_tp2_recorded" in event_types


def test_manual_close_is_distinguished_from_stoploss(client, db_session, created_user, auth_headers, monkeypatch):
    monkeypatch.setattr(
        "app.services.trade_setup_outcomes.default_adapter_factory",
        lambda account: OutcomeAdapter(
            account,
            histories={
                9001: _history(2320.2, 2320.6, "client", 20.0),
                9002: _history(2320.2, 2320.2, "client", 0.0),
            },
        ),
    )
    account = _create_account(db_session, created_user, "OUT-105")
    setup = _create_setup(db_session, created_user, account)

    response = client.post(f"/api/trade-setups/{setup.id}/reconcile", headers=auth_headers)

    assert response.status_code == 200
    data = response.json()
    assert data["setup_outcome"] == "manual_close"
    assert data["order1_outcome"] == "manual_close"


def test_execute_failure_is_distinguished_from_stoploss(client, db_session, created_user, auth_headers, monkeypatch):
    monkeypatch.setattr(
        "app.services.trade_setup_outcomes.default_adapter_factory",
        lambda account: OutcomeAdapter(account),
    )
    account = _create_account(db_session, created_user, "OUT-106")
    setup = _create_setup(db_session, created_user, account, status="failed")

    response = client.post(f"/api/trade-setups/{setup.id}/reconcile", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["setup_outcome"] == "execution_failed"
    stored = db_session.query(TradeSetup).filter(TradeSetup.id == setup.id).first()
    assert stored is not None
    assert stored.setup_outcome == "execution_failed"
    assert stored.result_status is None
