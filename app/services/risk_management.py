from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models.risk_control_log import RiskControlLog
from app.models.trade_setup import TradeSetup
from app.models.trading_account import TradingAccount
from app.models.user_daily_risk_state import UserDailyRiskState
from app.services.trade_events import create_trade_event


MAX_SETUP_VOLUME_MESSAGE = "Total setup volume exceeds the account's allowed cap."
DAILY_LOCK_MESSAGE = "You have reached the limit of 2 consecutive stoploss setups for the day."


class RiskManagementService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def assert_preview_allowed(self, *, user_id: int, account: TradingAccount, total_setup_volume: Decimal) -> None:
        self._assert_daily_lock(user_id=user_id, setup=None, for_execute=False)
        self._assert_volume_cap(user_id=user_id, account=account, total_setup_volume=total_setup_volume, setup=None, for_execute=False)

    def assert_execute_allowed(self, *, setup: TradeSetup, account: TradingAccount) -> None:
        total_setup_volume = Decimal(str(setup.order1_volume)) + Decimal(str(setup.order2_volume))
        self._assert_daily_lock(user_id=setup.user_id, setup=setup, for_execute=True)
        self._assert_volume_cap(user_id=setup.user_id, account=account, total_setup_volume=total_setup_volume, setup=setup, for_execute=True)

    def record_setup_result(self, *, setup: TradeSetup, result_status: str) -> None:
        if setup.result_status is not None:
            return

        trading_day = self._trading_day(setup.executed_at or setup.updated_at or datetime.now(timezone.utc))
        state = self._get_or_create_state(setup.user_id, trading_day)

        if result_status == "stoploss":
            self._log(setup.user_id, setup.id, "setup_stoploss_recorded", f"Setup #{setup.id} recorded as stoploss.")
            state.consecutive_stoploss_count += 1
            state.last_setup_result = "stoploss"
            self._log(
                setup.user_id,
                setup.id,
                "consecutive_stoploss_counter_updated",
                f"Consecutive stoploss count updated to {state.consecutive_stoploss_count}.",
            )
            if state.consecutive_stoploss_count >= 2 and not state.daily_lock_active:
                state.daily_lock_active = True
                self._log(setup.user_id, setup.id, "daily_lock_triggered", DAILY_LOCK_MESSAGE)
        else:
            if state.consecutive_stoploss_count > 0 or state.daily_lock_active:
                self._log(setup.user_id, setup.id, "daily_lock_reset", "Daily lock state reset after non-stoploss setup.")
            state.consecutive_stoploss_count = 0
            state.daily_lock_active = False
            state.last_setup_result = result_status

        setup.result_status = result_status
        setup.result_recorded_at = datetime.now(timezone.utc)
        self.db.add(state)
        self.db.add(setup)
        self.db.commit()
        self.db.refresh(setup)

    def get_daily_state(self, user_id: int, trading_day: date | None = None) -> UserDailyRiskState:
        return self._get_or_create_state(user_id, trading_day or self._trading_day(datetime.now(timezone.utc)))

    def _assert_volume_cap(
        self,
        *,
        user_id: int,
        account: TradingAccount,
        total_setup_volume: Decimal,
        setup: TradeSetup | None,
        for_execute: bool,
    ) -> None:
        if account.max_total_setup_volume is None:
            return
        max_volume = Decimal(str(account.max_total_setup_volume))
        if total_setup_volume <= max_volume:
            return

        event_type = "risk_limit_execute_rejected" if for_execute else "risk_limit_preview_rejected"
        self._log(
            user_id,
            setup.id if setup else None,
            event_type,
            f"{MAX_SETUP_VOLUME_MESSAGE} Requested {total_setup_volume} > cap {max_volume}.",
        )
        if setup is not None:
            create_trade_event(self.db, user_id, setup.id, event_type, MAX_SETUP_VOLUME_MESSAGE)
            self.db.commit()
        raise ValueError(MAX_SETUP_VOLUME_MESSAGE)

    def _assert_daily_lock(self, *, user_id: int, setup: TradeSetup | None, for_execute: bool) -> None:
        state = self._get_or_create_state(user_id, self._trading_day(datetime.now(timezone.utc)))
        if not state.daily_lock_active:
            return
        event_type = "daily_lock_execute_rejected" if for_execute else "daily_lock_preview_rejected"
        self._log(user_id, setup.id if setup else None, event_type, DAILY_LOCK_MESSAGE)
        if setup is not None:
            create_trade_event(self.db, user_id, setup.id, event_type, DAILY_LOCK_MESSAGE)
            self.db.commit()
        raise ValueError(DAILY_LOCK_MESSAGE)

    def _get_or_create_state(self, user_id: int, trading_day: date) -> UserDailyRiskState:
        state = (
            self.db.query(UserDailyRiskState)
            .filter(UserDailyRiskState.user_id == user_id, UserDailyRiskState.trading_day == trading_day)
            .first()
        )
        if state:
            return state
        previous_state = (
            self.db.query(UserDailyRiskState)
            .filter(UserDailyRiskState.user_id == user_id)
            .order_by(UserDailyRiskState.trading_day.desc(), UserDailyRiskState.id.desc())
            .first()
        )
        state = UserDailyRiskState(user_id=user_id, trading_day=trading_day)
        self.db.add(state)
        self.db.flush()
        if previous_state and previous_state.trading_day != trading_day and (
            previous_state.daily_lock_active or previous_state.consecutive_stoploss_count > 0
        ):
            self._log(
                user_id,
                None,
                "daily_lock_reset",
                f"Daily lock state reset for new trading day {trading_day.isoformat()}.",
            )
        return state

    def _log(self, user_id: int, setup_id: int | None, event_type: str, message: str) -> None:
        self.db.add(RiskControlLog(user_id=user_id, setup_id=setup_id, event_type=event_type, message=message))
        self.db.flush()

    def _trading_day(self, value: datetime) -> date:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).date()
