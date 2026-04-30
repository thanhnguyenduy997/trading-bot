from datetime import datetime, timezone

from app.models.trade_event import TradeEvent
from app.models.trade_setup import TradeSetup
from app.schemas.trade_setup import LiveManualRecoveryCreate
from app.schemas.trading_account import TradingAccountCreate
from app.services.manual_trade_setups import ManualTradeSetupService
from app.services.trade_setup_monitoring import TradeSetupMonitoringService
from app.services.trade_setups import list_trade_setups
from app.services.trading_accounts import create_trading_account


def _create_account(db_session, user):
    return create_trading_account(
        db_session,
        user.id,
        TradingAccountCreate(
            broker_name="Demo Broker",
            account_number="LIVE-RECOVERY",
            server_name="demo-server",
            password="secret-pass",
        ),
    )


def _position(ticket: int, *, symbol="XAUUSD", side="buy", volume=0.5, sl=2319.2, tp=2321.2):
    return {
        "ticket": ticket,
        "symbol": symbol,
        "side": side,
        "volume": volume,
        "price_open": 2320.2,
        "time": datetime(2026, 4, 25, 8, tzinfo=timezone.utc),
        "sl": sl,
        "tp": tp,
        "profit": 0.0,
    }


class RecoveryAdapter:
    def __init__(self, account, *, positions=None, histories=None):
        self.account = account
        self.positions = positions or {}
        self.histories = histories or {}
        self.placed_orders = []
        self.modified_positions = []

    def connect(self):
        return None

    def get_account_info(self):
        return {}

    def get_symbol_info(self, symbol: str):
        return {"trade_contract_size": 100.0}

    def list_open_positions(self):
        return list(self.positions.values())

    def get_position(self, *, position_ticket: int):
        return self.positions.get(position_ticket)

    def get_position_history(self, *, position_ticket: int):
        return self.histories.get(position_ticket, [])

    def modify_position_sl(self, **kwargs):
        self.modified_positions.append(kwargs)
        position = self.positions[kwargs["position_ticket"]]
        position["sl"] = kwargs["sl"]
        return {"sl": kwargs["sl"]}

    def place_market_order(self, **kwargs):
        self.placed_orders.append(kwargs)
        raise AssertionError("Live recovery must not place entry orders")

    def close_position(self, **kwargs):
        return {}

    def get_quote(self, symbol: str):
        return {"symbol": symbol, "bid": 2320.0, "ask": 2320.2}

    def list_symbols(self):
        return [{"symbol": "XAUUSD", "visible": True}]

    def execute_setup(self, setup):
        return {}

    def get_trade_history(self, **kwargs):
        return []

    def close(self):
        return None


def _payload(account, *, order1=1001, order2=1002, monitoring_mode="monitor_and_move_be"):
    return LiveManualRecoveryCreate(
        trading_account_id=account.id,
        symbol="XAUUSD",
        side="buy",
        estimated_entry=2320.2,
        sl_price=2319.2,
        total_risk_money=100.0,
        rr_order2=2,
        tp1_price=2321.2,
        tp2_price=2322.2,
        order_count=2,
        order1_ticket=order1,
        order2_ticket=order2,
        monitoring_mode=monitoring_mode,
    )


def test_selecting_two_open_mt5_orders_creates_recovered_live_setup(db_session, created_user):
    account = _create_account(db_session, created_user)
    adapter = RecoveryAdapter(account, positions={1001: _position(1001), 1002: _position(1002, tp=2322.2)})
    service = ManualTradeSetupService(db_session, adapter_factory=lambda account: adapter)

    setup = service.recover_live_manual_setup(user_id=created_user.id, payload=_payload(account))

    assert setup.setup_source == "manual"
    assert setup.status == "executed"
    assert setup.monitoring_status == "waiting_tp1"
    assert setup.order1_ticket == 1001
    assert setup.order2_ticket == 1002
    assert setup.setup_outcome == "open"
    assert adapter.placed_orders == []
    event = db_session.query(TradeEvent).filter(TradeEvent.setup_id == setup.id, TradeEvent.event_type == "manual_live_recovered").first()
    assert event is not None


def test_recovery_rejects_mixed_symbol_or_side_selection(db_session, created_user):
    account = _create_account(db_session, created_user)
    adapter = RecoveryAdapter(account, positions={1001: _position(1001), 1002: _position(1002, symbol="EURUSD")})
    service = ManualTradeSetupService(db_session, adapter_factory=lambda account: adapter)

    try:
        service.recover_live_manual_setup(user_id=created_user.id, payload=_payload(account))
        assert False, "Expected mixed symbol rejection"
    except ValueError as exc:
        assert "same symbol" in str(exc)

    adapter = RecoveryAdapter(account, positions={1001: _position(1001), 1002: _position(1002, side="sell")})
    service = ManualTradeSetupService(db_session, adapter_factory=lambda account: adapter)
    try:
        service.recover_live_manual_setup(user_id=created_user.id, payload=_payload(account))
        assert False, "Expected mixed side rejection"
    except ValueError as exc:
        assert "same side" in str(exc)


def test_recovered_setup_is_visible_in_setup_list(client, db_session, created_user):
    account = _create_account(db_session, created_user)
    adapter = RecoveryAdapter(account, positions={1001: _position(1001), 1002: _position(1002)})
    setup = ManualTradeSetupService(db_session, adapter_factory=lambda account: adapter).recover_live_manual_setup(
        user_id=created_user.id,
        payload=_payload(account),
    )

    setups = list_trade_setups(db_session, created_user.id)

    assert setup.id in [item.id for item in setups]


def test_tp1_triggers_be_move_on_order2_when_enabled(db_session, created_user):
    account = _create_account(db_session, created_user)
    history = [{"entry": "out", "reason": "tp", "price": 2321.2, "point": 0.01, "profit": 50.0, "time": datetime(2026, 4, 25, 9, tzinfo=timezone.utc)}]
    adapter = RecoveryAdapter(
        account,
        positions={1002: _position(1002, tp=2322.2)},
        histories={1001: history},
    )
    setup = ManualTradeSetupService(db_session, adapter_factory=lambda account: RecoveryAdapter(account, positions={1001: _position(1001), 1002: _position(1002)})).recover_live_manual_setup(
        user_id=created_user.id,
        payload=_payload(account),
    )

    TradeSetupMonitoringService(db_session, adapter_factory=lambda account: adapter).process_setup(setup.id, created_user.id)

    assert len(adapter.modified_positions) == 1
    assert adapter.modified_positions[0]["position_ticket"] == 1002
    assert adapter.modified_positions[0]["sl"] == 2320.2


def test_be_move_is_idempotent_for_recovered_setup(db_session, created_user):
    account = _create_account(db_session, created_user)
    history = [{"entry": "out", "reason": "tp", "price": 2321.2, "point": 0.01, "profit": 50.0, "time": datetime(2026, 4, 25, 9, tzinfo=timezone.utc)}]
    recovery_adapter = RecoveryAdapter(account, positions={1001: _position(1001), 1002: _position(1002)})
    setup = ManualTradeSetupService(db_session, adapter_factory=lambda account: recovery_adapter).recover_live_manual_setup(
        user_id=created_user.id,
        payload=_payload(account),
    )
    monitor_adapter = RecoveryAdapter(account, positions={1002: _position(1002, sl=2320.2, tp=2322.2)}, histories={1001: history})

    TradeSetupMonitoringService(db_session, adapter_factory=lambda account: monitor_adapter).process_setup(setup.id, created_user.id)

    assert monitor_adapter.modified_positions == []
    stored = db_session.query(TradeSetup).filter(TradeSetup.id == setup.id).first()
    assert stored.monitoring_status == "be_already_set"


def test_recovering_after_order1_already_hit_tp1_still_moves_be(db_session, created_user):
    account = _create_account(db_session, created_user)
    order1_history = [
        {"entry": "in", "type": "buy", "symbol": "XAUUSD", "volume": 0.5, "price": 2320.2, "time": datetime(2026, 4, 25, 8, tzinfo=timezone.utc), "position_id": 1001, "position": 1001},
        {"entry": "out", "reason": "tp", "price": 2321.2, "point": 0.01, "profit": 50.0, "time": datetime(2026, 4, 25, 9, tzinfo=timezone.utc), "position_id": 1001, "position": 1001},
    ]
    recovery_adapter = RecoveryAdapter(account, positions={1002: _position(1002, tp=2322.2)}, histories={1001: order1_history})
    setup = ManualTradeSetupService(db_session, adapter_factory=lambda account: recovery_adapter).recover_live_manual_setup(
        user_id=created_user.id,
        payload=_payload(account),
    )

    assert setup.order1_outcome == "tp_hit"
    monitor_adapter = RecoveryAdapter(account, positions={1002: _position(1002, tp=2322.2)}, histories={1001: order1_history})
    TradeSetupMonitoringService(db_session, adapter_factory=lambda account: monitor_adapter).process_setup(setup.id, created_user.id)

    assert len(monitor_adapter.modified_positions) == 1
    assert monitor_adapter.modified_positions[0]["sl"] == 2320.2


def test_recovery_flow_does_not_place_duplicate_entry_orders(db_session, created_user):
    account = _create_account(db_session, created_user)
    adapter = RecoveryAdapter(account, positions={1001: _position(1001), 1002: _position(1002)})

    ManualTradeSetupService(db_session, adapter_factory=lambda account: adapter).recover_live_manual_setup(
        user_id=created_user.id,
        payload=_payload(account),
    )

    assert adapter.placed_orders == []
