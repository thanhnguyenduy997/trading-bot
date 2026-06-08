from datetime import datetime, timezone

from app.models.trade_event import TradeEvent
from app.models.app_setting import AppSetting
from app.models.mt5_trade_history import MT5TradeHistory
from app.models.trade_setup import TradeSetup
from app.schemas.trade_setup import TradeSetupCreate
from app.schemas.trading_account import TradingAccountCreate
from app.services.trade_setup_outcomes import TradeSetupOutcomeService
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


def _create_single_setup(db_session, user, account, *, status: str = "executed") -> TradeSetup:
    setup = create_trade_setup(
        db_session,
        user.id,
        TradeSetupCreate(
            trading_account_id=account.id,
            setup_mode="single_full_volume",
            order_count=1,
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
            risk_per_order=100.0,
            order1_volume=1.0,
            order2_volume=0.0,
            status=status,
        ),
    )
    if status == "executed":
        setup.status = "executed"
        setup.order1_ticket = 9901
        setup.executed_at = datetime.now(timezone.utc)
        setup.monitoring_status = "not_applicable"
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


def test_single_full_volume_classifies_tp_hit(client, db_session, created_user, auth_headers, monkeypatch):
    account = _create_account(db_session, created_user, "OUT-SINGLE-TP")
    setup = _create_single_setup(db_session, created_user, account)
    monkeypatch.setattr(
        "app.services.trade_setup_outcomes.default_adapter_factory",
        lambda account: OutcomeAdapter(account, histories={9901: _history(2320.2, 2322.2, "tp", 100.0)}),
    )

    response = client.post(f"/api/trade-setups/{setup.id}/reconcile", headers=auth_headers)

    assert response.status_code == 200
    data = response.json()
    assert data["setup_outcome"] == "single_tp_hit"
    assert data["order1_outcome"] == "tp_hit"
    db_session.refresh(setup)
    assert setup.result_status == "non_stoploss"


def test_single_full_volume_classifies_sl_hit(client, db_session, created_user, auth_headers, monkeypatch):
    account = _create_account(db_session, created_user, "OUT-SINGLE-SL")
    setup = _create_single_setup(db_session, created_user, account)
    monkeypatch.setattr(
        "app.services.trade_setup_outcomes.default_adapter_factory",
        lambda account: OutcomeAdapter(account, histories={9901: _history(2320.2, 2319.2, "sl", -100.0)}),
    )

    response = client.post(f"/api/trade-setups/{setup.id}/reconcile", headers=auth_headers)

    assert response.status_code == 200
    data = response.json()
    assert data["setup_outcome"] == "single_sl_hit"
    db_session.refresh(setup)
    assert setup.result_status == "stoploss"


def test_single_full_volume_manual_close_within_half_r_classifies_scratch_manual(
    client,
    db_session,
    created_user,
    auth_headers,
    monkeypatch,
):
    account = _create_account(db_session, created_user, "OUT-SINGLE-SCRATCH")
    setup = _create_single_setup(db_session, created_user, account)
    monkeypatch.setattr(
        "app.services.trade_setup_outcomes.default_adapter_factory",
        lambda account: OutcomeAdapter(account, histories={9901: _history(2320.2, 2320.25, "client", 50.0)}),
    )

    response = client.post(f"/api/trade-setups/{setup.id}/reconcile", headers=auth_headers)

    assert response.status_code == 200
    data = response.json()
    assert data["setup_outcome"] == "scratch_manual"
    assert data["scratch_manual_threshold_r"] == 0.5
    db_session.refresh(setup)
    assert setup.result_status is None
    assert data["order1_outcome"] == "manual_close"
    assert data["order2_outcome"] == "unknown"


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
    setup.order2_be_moved_at = datetime.fromtimestamp(1713770940, tz=timezone.utc)
    db_session.add(setup)
    db_session.commit()

    response = client.post(f"/api/trade-setups/{setup.id}/reconcile", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["setup_outcome"] == "managed_win"
    stored = db_session.query(TradeSetup).filter(TradeSetup.id == setup.id).first()
    assert stored is not None
    assert stored.order2_outcome == "closed_at_be"
    event_types = {
        event.event_type
        for event in db_session.query(TradeEvent).filter(TradeEvent.setup_id == setup.id).all()
    }
    assert "order2_closed_at_be" in event_types
    assert "setup_managed_win_recorded" in event_types
    assert stored.result_status == "non_stoploss"


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
    assert data["setup_outcome"] == "full_loss"
    stored = db_session.query(TradeSetup).filter(TradeSetup.id == setup.id).first()
    assert stored is not None
    assert stored.result_status == "stoploss"
    event_types = {
        event.event_type
        for event in db_session.query(TradeEvent).filter(TradeEvent.setup_id == setup.id).all()
    }
    assert "order1_sl_hit" in event_types
    assert "order2_sl_hit" in event_types
    assert "setup_full_loss_recorded" in event_types
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
    assert response.json()["setup_outcome"] == "full_win"
    event_types = {
        event.event_type
        for event in db_session.query(TradeEvent).filter(TradeEvent.setup_id == setup.id).all()
    }
    assert "order1_tp_hit" in event_types
    assert "order2_tp_hit" in event_types
    assert "setup_full_win_recorded" in event_types


def test_system_managed_be_branch_still_classifies_tp2_when_target_reached(
    client,
    db_session,
    created_user,
    auth_headers,
    monkeypatch,
):
    monkeypatch.setattr(
        "app.services.trade_setup_outcomes.default_adapter_factory",
        lambda account: OutcomeAdapter(
            account,
            histories={
                9011: _history(2320.2, 2321.2, "tp", 50.0, close_time=1713771000),
                9012: _history(2320.2, 2322.2, "tp", 100.0, close_time=1713771800),
            },
        ),
    )
    account = _create_account(db_session, created_user, "OUT-104B")
    setup = _create_setup(db_session, created_user, account)
    setup.order1_ticket = 9011
    setup.order2_ticket = 9012
    setup.order2_be_moved_at = datetime.fromtimestamp(1713771200, tz=timezone.utc)
    db_session.add(setup)
    db_session.commit()

    response = client.post(f"/api/trade-setups/{setup.id}/reconcile", headers=auth_headers)

    assert response.status_code == 200
    data = response.json()
    assert data["order2_outcome"] == "tp_hit"
    assert data["setup_outcome"] == "full_win"


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
    assert data["setup_outcome"] == "scratch_manual"
    assert data["order1_outcome"] == "manual_close"


def test_no_be_event_order2_true_stoploss_uses_inferred_branch(
    client,
    db_session,
    created_user,
    auth_headers,
    monkeypatch,
):
    monkeypatch.setattr(
        "app.services.trade_setup_outcomes.default_adapter_factory",
        lambda account: OutcomeAdapter(
            account,
            histories={
                9021: _history(2320.2, 2319.2, "sl", -50.0),
                9022: _history(2320.2, 2319.2, "sl", -50.0),
            },
        ),
    )
    account = _create_account(db_session, created_user, "OUT-105B")
    setup = _create_setup(db_session, created_user, account)
    setup.order1_ticket = 9021
    setup.order2_ticket = 9022
    db_session.add(setup)
    db_session.commit()

    response = client.post(f"/api/trade-setups/{setup.id}/reconcile", headers=auth_headers)

    assert response.status_code == 200
    data = response.json()
    assert data["order2_outcome"] == "sl_hit"
    assert data["setup_outcome"] == "full_loss"
    event = (
        db_session.query(TradeEvent)
        .filter(TradeEvent.setup_id == setup.id, TradeEvent.event_type == "order2_sl_hit")
        .first()
    )
    assert event is not None
    assert '"classification_branch": "manual_inferred"' in (event.details or "")


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


def test_tp1_then_be_with_tiny_negative_pnl_is_still_breakeven(client, db_session, created_user, auth_headers, monkeypatch):
    monkeypatch.setattr(
        "app.services.trade_setup_outcomes.default_adapter_factory",
        lambda account: OutcomeAdapter(
            account,
            histories={
                537841502: _history(2320.2, 2321.18, "sl", 48.0, close_time=1713771200),
                537841506: _history(2320.2, 2320.17, "sl", -0.45, close_time=1713771800),
            },
        ),
    )
    account = _create_account(db_session, created_user, "OUT-107")
    setup = _create_setup(db_session, created_user, account)
    setup.order1_ticket = 537841502
    setup.order2_ticket = 537841506
    setup.order2_be_moved_at = datetime.fromtimestamp(1713771740, tz=timezone.utc)
    db_session.add(setup)
    db_session.commit()

    response = client.post(f"/api/trade-setups/{setup.id}/reconcile", headers=auth_headers)

    assert response.status_code == 200
    data = response.json()
    assert data["order1_outcome"] == "tp_hit"
    assert data["order2_outcome"] == "closed_at_be"
    assert data["setup_outcome"] == "managed_win"

    stored = db_session.query(TradeSetup).filter(TradeSetup.id == setup.id).first()
    assert stored is not None
    assert stored.result_status == "non_stoploss"
    assert float(stored.order1_close_price) == 2321.18
    assert float(stored.order2_close_price) == 2320.17
    assert float(stored.order2_realized_pnl) == -0.45


def test_system_be_success_defaults_to_be_despite_small_negative_pnl_and_price_drift(
    client,
    db_session,
    created_user,
    auth_headers,
    monkeypatch,
):
    monkeypatch.setattr(
        "app.services.trade_setup_outcomes.default_adapter_factory",
        lambda account: OutcomeAdapter(
            account,
            histories={
                9031: _history(2320.2, 2321.2, "tp", 50.0, close_time=1713771000),
                9032: _history(2320.2, 2320.45, "sl", -0.8, close_time=1713771800),
            },
        ),
    )
    account = _create_account(db_session, created_user, "OUT-107B")
    setup = _create_setup(db_session, created_user, account)
    setup.order1_ticket = 9031
    setup.order2_ticket = 9032
    setup.order2_be_moved_at = datetime.fromtimestamp(1713771200, tz=timezone.utc)
    db_session.add(setup)
    db_session.commit()

    response = client.post(f"/api/trade-setups/{setup.id}/reconcile", headers=auth_headers)

    assert response.status_code == 200
    data = response.json()
    assert data["order1_outcome"] == "tp_hit"
    assert data["order2_outcome"] == "closed_at_be"
    assert data["setup_outcome"] == "managed_win"


def test_system_be_contradictory_timeline_requires_review(
    client,
    db_session,
    created_user,
    auth_headers,
    monkeypatch,
):
    monkeypatch.setattr(
        "app.services.trade_setup_outcomes.default_adapter_factory",
        lambda account: OutcomeAdapter(
            account,
            histories={
                9041: _history(2320.2, 2321.2, "tp", 50.0, close_time=1713771000),
                9042: _history(2320.2, 2320.2, "sl", -0.5, close_time=1713771100),
            },
        ),
    )
    account = _create_account(db_session, created_user, "OUT-107C")
    setup = _create_setup(db_session, created_user, account)
    setup.order1_ticket = 9041
    setup.order2_ticket = 9042
    setup.order2_be_moved_at = datetime.fromtimestamp(1713771200, tz=timezone.utc)
    db_session.add(setup)
    db_session.commit()

    response = client.post(f"/api/trade-setups/{setup.id}/reconcile", headers=auth_headers)

    assert response.status_code == 200
    data = response.json()
    assert data["order2_outcome"] == "review_required"
    assert data["setup_outcome"] == "review_required"


def test_stored_backfill_upgrades_system_be_review_required_to_managed_win(db_session, created_user):
    account = _create_account(db_session, created_user, "OUT-107D")
    setup = _create_setup(db_session, created_user, account)
    setup.order1_ticket = 9051
    setup.order2_ticket = 9052
    setup.order1_outcome = "tp_hit"
    setup.order2_outcome = "review_required"
    setup.setup_outcome = "review_required"
    setup.order1_closed_at = datetime(2026, 4, 22, 8, tzinfo=timezone.utc)
    setup.order2_closed_at = datetime(2026, 4, 22, 9, tzinfo=timezone.utc)
    setup.order1_close_price = 2321.2
    setup.order2_close_price = 2320.45
    setup.order1_realized_pnl = 50.0
    setup.order2_realized_pnl = -0.8
    setup.order2_be_moved_at = datetime(2026, 4, 22, 8, 30, tzinfo=timezone.utc)
    setup.setup_outcome_recorded_at = None
    db_session.add(setup)
    db_session.commit()

    result = TradeSetupOutcomeService(db_session).backfill_setup_outcomes_from_stored_data(
        user_id=created_user.id,
        trading_account_id=account.id,
    )

    db_session.refresh(setup)
    assert result["updated"] == 1
    assert setup.order2_outcome == "closed_at_be"
    assert setup.setup_outcome == "managed_win"


def test_reconciliation_does_not_leak_close_data_between_setups(client, db_session, created_user, auth_headers, monkeypatch):
    monkeypatch.setattr(
        "app.services.trade_setup_outcomes.default_adapter_factory",
        lambda account: OutcomeAdapter(
            account,
            histories={
                9101: _history(2320.2, 2321.2, "tp", 50.0, close_time=1713771000),
                9102: _history(2320.2, 2320.18, "sl", -0.35, close_time=1713771600),
                9201: _history(2320.2, 2319.2, "sl", -50.0, close_time=1713772200),
                9202: _history(2320.2, 2319.2, "sl", -50.0, close_time=1713772260),
            },
        ),
    )
    account = _create_account(db_session, created_user, "OUT-108")
    setup1 = _create_setup(db_session, created_user, account)
    setup1.order1_ticket = 9101
    setup1.order2_ticket = 9102
    setup1.order2_be_moved_at = datetime.fromtimestamp(1713771540, tz=timezone.utc)
    setup2 = _create_setup(db_session, created_user, account)
    setup2.order1_ticket = 9201
    setup2.order2_ticket = 9202
    db_session.add_all([setup1, setup2])
    db_session.commit()

    response1 = client.post(f"/api/trade-setups/{setup1.id}/reconcile", headers=auth_headers)
    response2 = client.post(f"/api/trade-setups/{setup2.id}/reconcile", headers=auth_headers)

    assert response1.status_code == 200
    assert response2.status_code == 200

    stored1 = db_session.query(TradeSetup).filter(TradeSetup.id == setup1.id).first()
    stored2 = db_session.query(TradeSetup).filter(TradeSetup.id == setup2.id).first()
    assert stored1 is not None and stored2 is not None
    assert stored1.setup_outcome == "managed_win"
    assert stored2.setup_outcome == "full_loss"
    assert float(stored1.order1_close_price) == 2321.2
    assert float(stored1.order2_close_price) == 2320.18
    assert float(stored2.order1_close_price) == 2319.2
    assert float(stored2.order2_close_price) == 2319.2
    assert stored1.order1_closed_at != stored2.order1_closed_at
    assert stored1.order2_closed_at != stored2.order2_closed_at


def test_reconciliation_corrects_contradictory_stoploss_result_to_non_stoploss(
    client,
    db_session,
    created_user,
    auth_headers,
    monkeypatch,
):
    monkeypatch.setattr(
        "app.services.trade_setup_outcomes.default_adapter_factory",
        lambda account: OutcomeAdapter(
            account,
            histories={
                9301: _history(2320.2, 2321.2, "tp", 50.0, close_time=1713772400),
                9302: _history(2320.2, 2320.19, "sl", -0.25, close_time=1713772600),
            },
        ),
    )
    account = _create_account(db_session, created_user, "OUT-109")
    setup = _create_setup(db_session, created_user, account)
    setup.order1_ticket = 9301
    setup.order2_ticket = 9302
    setup.order2_be_moved_at = datetime.fromtimestamp(1713772540, tz=timezone.utc)
    setup.result_status = "stoploss"
    db_session.add(setup)
    db_session.commit()

    response = client.post(f"/api/trade-setups/{setup.id}/reconcile", headers=auth_headers)

    assert response.status_code == 200
    stored = db_session.query(TradeSetup).filter(TradeSetup.id == setup.id).first()
    assert stored is not None
    assert stored.order1_outcome == "tp_hit"
    assert stored.order2_outcome == "closed_at_be"
    assert stored.setup_outcome == "managed_win"
    assert stored.result_status == "non_stoploss"


def test_reconcile_review_required_for_non_standard_closed_setup(client, db_session, created_user, auth_headers, monkeypatch):
    monkeypatch.setattr(
        "app.services.trade_setup_outcomes.default_adapter_factory",
        lambda account: OutcomeAdapter(
            account,
            histories={
                9001: _history(2320.2, 2321.2, "tp", 50.0),
                9002: _history(2320.2, 2319.8, "sl", -30.0),
            },
        ),
    )
    account = _create_account(db_session, created_user, "OUT-110")
    setup = _create_setup(db_session, created_user, account)

    response = client.post(f"/api/trade-setups/{setup.id}/reconcile", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["setup_outcome"] == "review_required"


def test_scratch_manual_threshold_changes_classification(client, db_session, created_user, auth_headers, monkeypatch):
    monkeypatch.setattr(
        "app.services.trade_setup_outcomes.default_adapter_factory",
        lambda account: OutcomeAdapter(
            account,
            histories={
                9001: _history(2320.2, 2320.6, "client", 20.0),
                9002: _history(2320.2, 2320.15, "client", -2.0),
            },
        ),
    )
    record = db_session.query(AppSetting).filter(AppSetting.id == 1).first()
    if record is None:
        record = AppSetting(id=1)
    record.scratch_manual_threshold_r = 0.1
    db_session.add(record)
    db_session.commit()

    account = _create_account(db_session, created_user, "OUT-111")
    setup = _create_setup(db_session, created_user, account)

    response = client.post(f"/api/trade-setups/{setup.id}/reconcile", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["setup_outcome"] == "review_required"


def test_confirmed_manual_setup_is_classified_with_same_final_outcome_model(client, db_session, created_user, auth_headers, monkeypatch):
    monkeypatch.setattr(
        "app.services.trade_setup_outcomes.default_adapter_factory",
        lambda account: OutcomeAdapter(
            account,
            histories={
                9901: _history(2320.2, 2321.2, "tp", 50.0),
                9902: _history(2320.2, 2320.2, "sl", 0.0),
            },
        ),
    )
    account = _create_account(db_session, created_user, "OUT-112")
    setup = _create_setup(db_session, created_user, account)
    setup.setup_source = "manual"
    setup.manual_confirmed_at = datetime.now(timezone.utc)
    setup.order1_ticket = 9901
    setup.order2_ticket = 9902
    setup.order2_be_moved_at = datetime.fromtimestamp(1713770940, tz=timezone.utc)
    db_session.add(setup)
    db_session.commit()

    response = client.post(f"/api/trade-setups/{setup.id}/reconcile", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["setup_outcome"] == "managed_win"


def test_reconcile_historical_setups_backfills_existing_setup_outcomes(db_session, created_user, monkeypatch):
    monkeypatch.setattr(
        "app.services.trade_setup_outcomes.default_adapter_factory",
        lambda account: OutcomeAdapter(
            account,
            histories={
                9911: _history(2320.2, 2321.2, "tp", 50.0),
                9912: _history(2320.2, 2322.2, "tp", 100.0),
                9921: _history(2320.2, 2319.2, "sl", -50.0),
                9922: _history(2320.2, 2319.2, "sl", -50.0),
            },
        ),
    )
    account = _create_account(db_session, created_user, "OUT-113")
    setup_win = _create_setup(db_session, created_user, account)
    setup_win.order1_ticket = 9911
    setup_win.order2_ticket = 9912
    setup_loss = _create_setup(db_session, created_user, account)
    setup_loss.order1_ticket = 9921
    setup_loss.order2_ticket = 9922
    db_session.add_all([setup_win, setup_loss])
    db_session.commit()

    result = TradeSetupOutcomeService(db_session).reconcile_historical_setups(
        user_id=created_user.id,
        trading_account_id=account.id,
    )

    assert result["count"] == 2
    assert result["setup_ids"] == [setup_win.id, setup_loss.id]

    db_session.refresh(setup_win)
    db_session.refresh(setup_loss)
    assert setup_win.setup_outcome == "full_win"
    assert setup_loss.setup_outcome == "full_loss"


def test_stored_backfill_reclassifies_stale_review_required_tp1_be_setup(db_session, created_user):
    account = _create_account(db_session, created_user, "OUT-114")
    setup = _create_setup(db_session, created_user, account)
    setup.order1_ticket = 9941
    setup.order2_ticket = 9942
    setup.order1_outcome = "sl_hit"
    setup.order2_outcome = "open"
    setup.setup_outcome = "review_required"
    setup.order1_closed_at = datetime(2026, 4, 22, 8, tzinfo=timezone.utc)
    setup.order2_closed_at = datetime(2026, 4, 22, 9, tzinfo=timezone.utc)
    setup.order1_close_price = 2321.18
    setup.order2_close_price = 2320.19
    setup.order1_realized_pnl = 48.0
    setup.order2_realized_pnl = -0.35
    setup.setup_outcome_recorded_at = None
    db_session.add_all(
        [
            setup,
            MT5TradeHistory(
                user_id=created_user.id,
                trading_account_id=account.id,
                linked_setup_id=setup.id,
                position_ticket=9941,
                symbol="XAUUSD",
                side="buy",
                trade_source="system",
                outcome="sl_hit",
                volume=0.5,
                open_price=2320.2,
                close_price=2321.18,
                realized_pnl=48.0,
                open_time=datetime(2026, 4, 22, 7, tzinfo=timezone.utc),
                close_time=datetime(2026, 4, 22, 8, tzinfo=timezone.utc),
            ),
            MT5TradeHistory(
                user_id=created_user.id,
                trading_account_id=account.id,
                linked_setup_id=setup.id,
                position_ticket=9942,
                symbol="XAUUSD",
                side="buy",
                trade_source="system",
                outcome="open",
                volume=0.5,
                open_price=2320.2,
                close_price=2320.19,
                realized_pnl=-0.35,
                open_time=datetime(2026, 4, 22, 7, tzinfo=timezone.utc),
                close_time=datetime(2026, 4, 22, 9, tzinfo=timezone.utc),
            ),
        ]
    )
    db_session.commit()

    result = TradeSetupOutcomeService(db_session).backfill_setup_outcomes_from_stored_data(
        user_id=created_user.id,
        trading_account_id=account.id,
    )

    db_session.refresh(setup)
    trades = {
        trade.position_ticket: trade
        for trade in db_session.query(MT5TradeHistory).filter(MT5TradeHistory.linked_setup_id == setup.id).all()
    }
    assert result["updated"] == 1
    assert setup.order1_outcome == "tp_hit"
    assert setup.order2_outcome == "closed_at_be"
    assert setup.setup_outcome == "managed_win"
    assert setup.result_status == "non_stoploss"
    assert trades[9941].outcome == "tp_hit"
    assert trades[9942].outcome == "closed_at_be"
