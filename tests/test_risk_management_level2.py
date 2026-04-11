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
        return [{"entry": "out", "reason": "sl", "price": 2319.2, "point": 0.01}]

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
        return [{"entry": "out", "reason": "tp", "price": 2321.2, "point": 0.01}]

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

    state = db_session.query(UserDailyRiskState).filter(UserDailyRiskState.user_id == created_user.id).first()
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

    state = db_session.query(UserDailyRiskState).filter(UserDailyRiskState.user_id == created_user.id).first()
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
