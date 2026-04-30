from datetime import datetime, timedelta, timezone

from app.models.risk_control_log import RiskControlLog
from app.models.trade_event import TradeEvent
from app.models.trade_setup import TradeSetup
from app.models.user_daily_risk_state import UserDailyRiskState
from app.schemas.trade_setup import TradeSetupCreate
from app.schemas.trading_account import TradingAccountCreate
from app.services.preview_service import PreviewService
from app.services.trade_setup_monitoring import TradeSetupMonitoringService
from app.services.trade_setups import create_trade_setup
from app.services.trading_accounts import create_trading_account
from app.services.risk_management import RiskManagementService


def _create_account(db_session, user, account_number: str, max_total_setup_volume: float | None = None):
    return create_trading_account(
        db_session,
        user.id,
        TradingAccountCreate(
            broker_name="Demo Broker",
            account_number=account_number,
            server_name="demo-server",
            password="secret-pass",
            max_total_setup_volume=max_total_setup_volume,
        ),
    )


def _create_setup(db_session, user, account, status: str = "draft", order1_volume: float = 0.5, order2_volume: float = 0.5):
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
            order1_volume=order1_volume,
            order2_volume=order2_volume,
            status=status,
        ),
    )
    return setup


def _close_order(setup, *, order_index: int, ticket: int, close_time: datetime, realized_pnl: float, close_price: float = 0.0):
    setattr(setup, f"order{order_index}_ticket", ticket)
    setattr(setup, f"order{order_index}_closed_at", close_time)
    setattr(setup, f"order{order_index}_realized_pnl", realized_pnl)
    setattr(setup, f"order{order_index}_close_price", close_price)


def _record_setup_outcome(setup, outcome: str, recorded_at: datetime | None = None):
    setup.setup_outcome = outcome
    setup.setup_outcome_recorded_at = recorded_at


class FakePreviewAdapter:
    def __init__(self, account):
        self.account = account

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
        return {}

    def close(self):
        return None


class DummyExecutionAdapter(FakePreviewAdapter):
    def place_market_order(self, **kwargs):
        return {}

    def close_position(self, **kwargs):
        return {}


class LosingMonitoringAdapter(FakePreviewAdapter):
    def get_position(self, *, position_ticket: int):
        return None

    def get_position_history(self, *, position_ticket: int):
        now = datetime.now(timezone.utc)
        closed_at = now.replace(minute=5 if position_ticket % 2 else 8)
        return [
            {"entry": "out", "reason": "sl", "price": 2319.2, "point": 0.01, "profit": -50.0, "time": closed_at},
        ]

    def modify_position_sl(self, **kwargs):
        return {}


class WinningMonitoringAdapter(FakePreviewAdapter):
    def get_position(self, *, position_ticket: int):
        if position_ticket == 7002:
            return {
                "ticket": 7002,
                "symbol": "XAUUSD",
                "volume": 0.5,
                "price_open": 2320.2,
                "sl": 2319.2,
                "tp": 2322.2,
                "side": "buy",
            }
        return None

    def get_position_history(self, *, position_ticket: int):
        return [{"entry": "out", "reason": "tp", "price": 2321.2, "point": 0.01, "profit": 50.0, "time": datetime(2026, 4, 25, 9, 5, tzinfo=timezone.utc)}]

    def modify_position_sl(self, **kwargs):
        return {"sl": kwargs["sl"]}


def test_preview_rejected_when_total_setup_volume_exceeds_cap(db_session, created_user, sync_account_symbols):
    account = _create_account(db_session, created_user, "RISK-100", max_total_setup_volume=0.4)
    sync_account_symbols(account, "XAUUSD")
    preview_service = PreviewService(
        db_session,
        execution_service=None,
    )
    preview_service.execution_service.adapter_factory = lambda account: FakePreviewAdapter(account)

    try:
        preview_service.build_preview(
            created_user.id,
            payload=type(
                "Payload",
                (),
                {
                    "trading_account_id": account.id,
                    "symbol": "XAUUSD",
                    "side": "buy",
                    "sl_price": 2319.2,
                    "risk_mode": "fixed_money",
                    "risk_value": 100,
                    "rr_order2": 2,
                },
            )(),
        )
        assert False, "Expected preview rejection"
    except ValueError as exc:
        assert str(exc) == "Total setup volume exceeds the account's allowed cap."

    assert db_session.query(RiskControlLog).filter(RiskControlLog.event_type == "risk_limit_preview_rejected").count() == 1


def test_execute_rejected_when_total_setup_volume_exceeds_cap(client, db_session, created_user, auth_headers, monkeypatch, sync_account_symbols):
    monkeypatch.setattr(
        "app.services.trade_setup_execution.default_adapter_factory",
        lambda account: DummyExecutionAdapter(account),
    )
    account = _create_account(db_session, created_user, "RISK-101", max_total_setup_volume=0.4)
    sync_account_symbols(account, "XAUUSD")
    setup = _create_setup(db_session, created_user, account, order1_volume=0.3, order2_volume=0.3)

    response = client.post(f"/api/trade-setups/{setup.id}/execute", headers=auth_headers)

    assert response.status_code == 409
    assert response.json()["detail"] == "Total setup volume exceeds the account's allowed cap."
    assert db_session.query(TradeEvent).filter(TradeEvent.setup_id == setup.id, TradeEvent.event_type == "risk_limit_execute_rejected").count() == 1


def test_one_losing_setup_increments_consecutive_stoploss_count(db_session, created_user, monkeypatch):
    monkeypatch.setattr(
        "app.services.trade_setup_monitoring.default_adapter_factory",
        lambda account: LosingMonitoringAdapter(account),
    )
    account = _create_account(db_session, created_user, "RISK-102")
    setup = _create_setup(db_session, created_user, account, status="executed")
    setup.status = "executed"
    setup.order1_ticket = 7001
    setup.order2_ticket = 7002
    setup.executed_at = datetime.now(timezone.utc)
    db_session.add(setup)
    db_session.commit()

    TradeSetupMonitoringService(db_session).process_setup(setup.id, created_user.id)

    state = (
        db_session.query(UserDailyRiskState)
        .filter(UserDailyRiskState.user_id == created_user.id, UserDailyRiskState.trading_day == datetime.now(timezone.utc).date())
        .first()
    )
    assert state is not None
    assert state.consecutive_stoploss_count == 1
    assert state.daily_lock_active is False
    stored_setup = db_session.query(TradeSetup).filter(TradeSetup.id == setup.id).first()
    assert stored_setup.result_status == "stoploss"


def test_two_consecutive_losing_setups_trigger_user_daily_lock_and_other_account_cannot_bypass(
    db_session,
    created_user,
    monkeypatch,
    sync_account_symbols,
):
    monkeypatch.setattr(
        "app.services.trade_setup_monitoring.default_adapter_factory",
        lambda account: LosingMonitoringAdapter(account),
    )
    account1 = _create_account(db_session, created_user, "RISK-103")
    account2 = _create_account(db_session, created_user, "RISK-104")
    sync_account_symbols(account1, "XAUUSD")
    sync_account_symbols(account2, "XAUUSD")
    setup1 = _create_setup(db_session, created_user, account1, status="executed")
    setup2 = _create_setup(db_session, created_user, account2, status="executed")
    for index, setup in enumerate([setup1, setup2], start=1):
        setup.status = "executed"
        setup.order1_ticket = 7100 + index
        setup.order2_ticket = 7200 + index
        setup.executed_at = datetime.now(timezone.utc)
        db_session.add(setup)
    db_session.commit()

    monitor = TradeSetupMonitoringService(db_session)
    monitor.process_setup(setup1.id, created_user.id)
    monitor.process_setup(setup2.id, created_user.id)

    state = (
        db_session.query(UserDailyRiskState)
        .filter(UserDailyRiskState.user_id == created_user.id, UserDailyRiskState.trading_day == datetime.now(timezone.utc).date())
        .first()
    )
    assert state is not None
    assert state.consecutive_stoploss_count == 2
    assert state.daily_lock_active is True
    assert db_session.query(RiskControlLog).filter(RiskControlLog.event_type == "daily_lock_triggered").count() == 1

    preview_service = PreviewService(db_session)
    preview_service.execution_service.adapter_factory = lambda account: FakePreviewAdapter(account)
    try:
        preview_service.build_preview(
            created_user.id,
            payload=type(
                "Payload",
                (),
                {
                    "trading_account_id": account2.id,
                    "symbol": "XAUUSD",
                    "side": "buy",
                    "sl_price": 2319.2,
                    "risk_mode": "fixed_money",
                    "risk_value": 100,
                    "rr_order2": 2,
                },
            )(),
        )
        assert False, "Expected daily lock rejection"
    except ValueError as exc:
        assert str(exc) == "You have reached the limit of 2 consecutive stoploss setups for the day."


def test_non_stoploss_setup_resets_counter(db_session, created_user, monkeypatch):
    monkeypatch.setattr(
        "app.services.trade_setup_monitoring.default_adapter_factory",
        lambda account: WinningMonitoringAdapter(account),
    )
    account = _create_account(db_session, created_user, "RISK-105")
    setup = _create_setup(db_session, created_user, account, status="executed")
    setup.status = "executed"
    setup.order1_ticket = 7001
    setup.order2_ticket = 7002
    setup.executed_at = datetime.now(timezone.utc)
    db_session.add(setup)
    db_session.commit()

    yesterday = datetime.now(timezone.utc).date()
    state = UserDailyRiskState(
        user_id=created_user.id,
        trading_day=yesterday,
        consecutive_stoploss_count=1,
        daily_lock_active=False,
        last_setup_result="stoploss",
    )
    db_session.add(state)
    db_session.commit()

    TradeSetupMonitoringService(db_session).process_setup(setup.id, created_user.id)

    refreshed = db_session.query(UserDailyRiskState).filter(UserDailyRiskState.user_id == created_user.id).first()
    assert refreshed is not None
    assert refreshed.consecutive_stoploss_count == 0
    assert refreshed.daily_lock_active is False
    assert db_session.query(RiskControlLog).filter(RiskControlLog.event_type == "daily_lock_reset").count() >= 1


def test_new_day_resets_lock(db_session, created_user, sync_account_symbols):
    account = _create_account(db_session, created_user, "RISK-106")
    sync_account_symbols(account, "XAUUSD")
    yesterday = datetime.now(timezone.utc).date() - timedelta(days=1)
    db_session.add(
        UserDailyRiskState(
            user_id=created_user.id,
            trading_day=yesterday,
            consecutive_stoploss_count=2,
            daily_lock_active=True,
            last_setup_result="stoploss",
        )
    )
    db_session.commit()

    preview_service = PreviewService(db_session)
    preview_service.execution_service.adapter_factory = lambda account: FakePreviewAdapter(account)
    preview = preview_service.build_preview(
        created_user.id,
        payload=type(
            "Payload",
            (),
            {
                "trading_account_id": account.id,
                "symbol": "XAUUSD",
                "side": "buy",
                "sl_price": 2319.2,
                "risk_mode": "fixed_money",
                "risk_value": 100,
                "rr_order2": 2,
            },
        )(),
    )

    assert preview.symbol == "XAUUSD"
    today_state = (
        db_session.query(UserDailyRiskState)
        .filter(UserDailyRiskState.user_id == created_user.id, UserDailyRiskState.trading_day == datetime.now(timezone.utc).date())
        .first()
    )
    assert today_state is not None
    assert today_state.daily_lock_active is False
    assert db_session.query(RiskControlLog).filter(RiskControlLog.event_type == "daily_lock_reset").count() >= 1


def test_sl_then_be_then_sl_does_not_trigger_two_consecutive_stoploss_lock(db_session, created_user):
    account = _create_account(db_session, created_user, "RISK-107")
    first = _create_setup(db_session, created_user, account, status="executed")
    second = _create_setup(db_session, created_user, account, status="executed")
    third = _create_setup(db_session, created_user, account, status="executed")
    base = datetime(2026, 4, 25, 8, 0, tzinfo=timezone.utc)

    for setup in (first, second, third):
        setup.status = "executed"
        setup.executed_at = base

    _close_order(first, order_index=1, ticket=8101, close_time=base.replace(minute=5), realized_pnl=-50.0, close_price=2319.2)
    _close_order(second, order_index=1, ticket=8102, close_time=base.replace(minute=10), realized_pnl=-0.3, close_price=2320.19)
    _close_order(third, order_index=1, ticket=8103, close_time=base.replace(minute=15), realized_pnl=-50.0, close_price=2319.2)
    _record_setup_outcome(first, "full_loss")
    _record_setup_outcome(second, "managed_win")
    _record_setup_outcome(third, "full_loss")
    db_session.add_all([first, second, third])
    db_session.commit()

    state = RiskManagementService(db_session).get_daily_state(created_user.id, trading_day=base.date())

    assert state.consecutive_stoploss_count == 1
    assert state.daily_lock_active is False


def test_sl_then_sl_triggers_daily_lock(db_session, created_user):
    account = _create_account(db_session, created_user, "RISK-108")
    first = _create_setup(db_session, created_user, account, status="executed")
    second = _create_setup(db_session, created_user, account, status="executed")
    base = datetime(2026, 4, 25, 9, 0, tzinfo=timezone.utc)

    for setup in (first, second):
        setup.status = "executed"
        setup.executed_at = base

    _close_order(first, order_index=1, ticket=8201, close_time=base.replace(minute=5), realized_pnl=-50.0, close_price=2319.2)
    _close_order(second, order_index=1, ticket=8202, close_time=base.replace(minute=10), realized_pnl=-49.0, close_price=2319.2)
    _record_setup_outcome(first, "full_loss")
    _record_setup_outcome(second, "full_loss")
    db_session.add_all([first, second])
    db_session.commit()

    state = RiskManagementService(db_session).get_daily_state(created_user.id, trading_day=base.date())

    assert state.consecutive_stoploss_count == 2
    assert state.daily_lock_active is True


def test_positive_pnl_row_labeled_stoploss_does_not_count_toward_lock(db_session, created_user):
    account = _create_account(db_session, created_user, "RISK-109")
    first = _create_setup(db_session, created_user, account, status="executed")
    second = _create_setup(db_session, created_user, account, status="executed")
    base = datetime(2026, 4, 25, 10, 0, tzinfo=timezone.utc)

    for setup in (first, second):
        setup.status = "executed"
        setup.executed_at = base
        setup.result_status = "stoploss"

    _close_order(first, order_index=1, ticket=8301, close_time=base.replace(minute=5), realized_pnl=-50.0, close_price=2319.2)
    _close_order(second, order_index=1, ticket=8302, close_time=base.replace(minute=10), realized_pnl=12.0, close_price=2321.0)
    db_session.add_all([first, second])
    db_session.commit()

    state = RiskManagementService(db_session).get_daily_state(created_user.id, trading_day=base.date())

    assert state.consecutive_stoploss_count == 0
    assert state.daily_lock_active is False


def test_breakeven_resets_stoploss_streak(db_session, created_user):
    account = _create_account(db_session, created_user, "RISK-110")
    first = _create_setup(db_session, created_user, account, status="executed")
    second = _create_setup(db_session, created_user, account, status="executed")
    base = datetime(2026, 4, 25, 11, 0, tzinfo=timezone.utc)

    for setup in (first, second):
        setup.status = "executed"
        setup.executed_at = base

    _close_order(first, order_index=1, ticket=8401, close_time=base.replace(minute=5), realized_pnl=-50.0, close_price=2319.2)
    _close_order(second, order_index=1, ticket=8402, close_time=base.replace(minute=8), realized_pnl=-5.0, close_price=2320.1)
    _record_setup_outcome(first, "full_loss")
    _record_setup_outcome(second, "managed_win")
    db_session.add_all([first, second])
    db_session.commit()

    state = RiskManagementService(db_session).get_daily_state(created_user.id, trading_day=base.date())

    assert state.consecutive_stoploss_count == 0
    assert state.daily_lock_active is False


def test_streak_evaluates_by_close_time_then_ticket(db_session, created_user):
    account = _create_account(db_session, created_user, "RISK-111")
    stoploss_first = _create_setup(db_session, created_user, account, status="executed")
    breakeven_same_time = _create_setup(db_session, created_user, account, status="executed")
    later_stoploss = _create_setup(db_session, created_user, account, status="executed")
    base = datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc)

    for setup in (stoploss_first, breakeven_same_time, later_stoploss):
        setup.status = "executed"
        setup.executed_at = base

    same_close_time = base.replace(minute=5)
    _close_order(stoploss_first, order_index=1, ticket=8501, close_time=same_close_time, realized_pnl=-50.0, close_price=2319.2)
    _close_order(breakeven_same_time, order_index=1, ticket=8502, close_time=same_close_time, realized_pnl=-0.2, close_price=2320.19)
    _close_order(later_stoploss, order_index=1, ticket=8503, close_time=base.replace(minute=10), realized_pnl=-50.0, close_price=2319.2)
    _record_setup_outcome(stoploss_first, "full_loss")
    _record_setup_outcome(breakeven_same_time, "managed_win")
    _record_setup_outcome(later_stoploss, "full_loss")
    db_session.add_all([stoploss_first, breakeven_same_time, later_stoploss])
    db_session.commit()

    state = RiskManagementService(db_session).get_daily_state(created_user.id, trading_day=base.date())

    assert state.consecutive_stoploss_count == 1
    assert state.daily_lock_active is False


def test_full_loss_setup_with_two_sl_orders_counts_as_one_stoploss_setup(db_session, created_user):
    account = _create_account(db_session, created_user, "RISK-112")
    setup = _create_setup(db_session, created_user, account, status="executed")
    base = datetime(2026, 4, 25, 13, 0, tzinfo=timezone.utc)
    setup.status = "executed"
    setup.executed_at = base
    _close_order(setup, order_index=1, ticket=8601, close_time=base.replace(minute=5), realized_pnl=-50.0, close_price=2319.2)
    _close_order(setup, order_index=2, ticket=8602, close_time=base.replace(minute=6), realized_pnl=-50.0, close_price=2319.2)
    _record_setup_outcome(setup, "full_loss")
    db_session.add(setup)
    db_session.commit()

    state = RiskManagementService(db_session).get_daily_state(created_user.id, trading_day=base.date())

    assert state.consecutive_stoploss_count == 1
    assert state.daily_lock_active is False


def test_one_full_loss_setup_does_not_activate_two_stoploss_daily_lock(db_session, created_user):
    account = _create_account(db_session, created_user, "RISK-113")
    setup = _create_setup(db_session, created_user, account, status="executed")
    base = datetime(2026, 4, 25, 14, 0, tzinfo=timezone.utc)
    setup.status = "executed"
    setup.executed_at = base
    _close_order(setup, order_index=1, ticket=8701, close_time=base.replace(minute=5), realized_pnl=-50.0, close_price=2319.2)
    _close_order(setup, order_index=2, ticket=8702, close_time=base.replace(minute=5), realized_pnl=-50.0, close_price=2319.2)
    _record_setup_outcome(setup, "full_loss")
    db_session.add(setup)
    db_session.commit()

    state = RiskManagementService(db_session).get_daily_state(created_user.id, trading_day=base.date())

    assert state.consecutive_stoploss_count == 1
    assert state.daily_lock_active is False


def test_two_separate_full_loss_setups_activate_daily_lock(db_session, created_user):
    account = _create_account(db_session, created_user, "RISK-114")
    first = _create_setup(db_session, created_user, account, status="executed")
    second = _create_setup(db_session, created_user, account, status="executed")
    base = datetime(2026, 4, 25, 15, 0, tzinfo=timezone.utc)
    for setup in (first, second):
        setup.status = "executed"
        setup.executed_at = base

    _close_order(first, order_index=1, ticket=8801, close_time=base.replace(minute=5), realized_pnl=-50.0, close_price=2319.2)
    _close_order(first, order_index=2, ticket=8802, close_time=base.replace(minute=6), realized_pnl=-50.0, close_price=2319.2)
    _record_setup_outcome(first, "full_loss")
    _close_order(second, order_index=1, ticket=8803, close_time=base.replace(minute=10), realized_pnl=-50.0, close_price=2319.2)
    _close_order(second, order_index=2, ticket=8804, close_time=base.replace(minute=11), realized_pnl=-50.0, close_price=2319.2)
    _record_setup_outcome(second, "full_loss")
    db_session.add_all([first, second])
    db_session.commit()

    state = RiskManagementService(db_session).get_daily_state(created_user.id, trading_day=base.date())

    assert state.consecutive_stoploss_count == 2
    assert state.daily_lock_active is True


def test_managed_win_resets_setup_level_stoploss_streak(db_session, created_user):
    account = _create_account(db_session, created_user, "RISK-115")
    first = _create_setup(db_session, created_user, account, status="executed")
    second = _create_setup(db_session, created_user, account, status="executed")
    base = datetime(2026, 4, 25, 16, 0, tzinfo=timezone.utc)
    for setup in (first, second):
        setup.status = "executed"
        setup.executed_at = base

    _close_order(first, order_index=1, ticket=8901, close_time=base.replace(minute=5), realized_pnl=-50.0, close_price=2319.2)
    _record_setup_outcome(first, "full_loss")
    _close_order(second, order_index=1, ticket=8902, close_time=base.replace(minute=8), realized_pnl=50.0, close_price=2321.2)
    _close_order(second, order_index=2, ticket=8903, close_time=base.replace(minute=9), realized_pnl=0.0, close_price=2320.2)
    _record_setup_outcome(second, "managed_win")
    db_session.add_all([first, second])
    db_session.commit()

    state = RiskManagementService(db_session).get_daily_state(created_user.id, trading_day=base.date())

    assert state.consecutive_stoploss_count == 0
    assert state.daily_lock_active is False


def test_full_win_resets_setup_level_stoploss_streak(db_session, created_user):
    account = _create_account(db_session, created_user, "RISK-116")
    first = _create_setup(db_session, created_user, account, status="executed")
    second = _create_setup(db_session, created_user, account, status="executed")
    base = datetime(2026, 4, 25, 17, 0, tzinfo=timezone.utc)
    for setup in (first, second):
        setup.status = "executed"
        setup.executed_at = base

    _close_order(first, order_index=1, ticket=9001, close_time=base.replace(minute=5), realized_pnl=-50.0, close_price=2319.2)
    _record_setup_outcome(first, "full_loss")
    _close_order(second, order_index=1, ticket=9002, close_time=base.replace(minute=8), realized_pnl=50.0, close_price=2321.2)
    _close_order(second, order_index=2, ticket=9003, close_time=base.replace(minute=9), realized_pnl=100.0, close_price=2322.2)
    _record_setup_outcome(second, "full_win")
    db_session.add_all([first, second])
    db_session.commit()

    state = RiskManagementService(db_session).get_daily_state(created_user.id, trading_day=base.date())

    assert state.consecutive_stoploss_count == 0
    assert state.daily_lock_active is False


def test_same_linked_setup_is_never_counted_twice_for_daily_risk_state(db_session, created_user):
    account = _create_account(db_session, created_user, "RISK-117")
    setup = _create_setup(db_session, created_user, account, status="executed")
    base = datetime(2026, 4, 25, 18, 0, tzinfo=timezone.utc)
    setup.status = "executed"
    setup.executed_at = base
    _close_order(setup, order_index=1, ticket=9101, close_time=base.replace(minute=5), realized_pnl=-50.0, close_price=2319.2)
    _close_order(setup, order_index=2, ticket=9102, close_time=base.replace(minute=20), realized_pnl=-50.0, close_price=2319.2)
    _record_setup_outcome(setup, "full_loss")
    db_session.add(setup)
    db_session.commit()

    service = RiskManagementService(db_session)
    first_state = service.get_daily_state(created_user.id, trading_day=base.date())
    second_state = service.get_daily_state(created_user.id, trading_day=base.date())

    assert first_state.consecutive_stoploss_count == 1
    assert second_state.consecutive_stoploss_count == 1
    assert second_state.daily_lock_active is False
