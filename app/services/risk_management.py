import logging
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
logger = logging.getLogger(__name__)


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
        setup.result_status = result_status
        setup.result_recorded_at = datetime.now(timezone.utc)
        self.db.add(setup)
        self.db.flush()

        if result_status == "stoploss":
            self._log(setup.user_id, setup.id, "setup_stoploss_recorded", f"Setup #{setup.id} recorded as stoploss.")

        if self._counts_toward_risk_logic(setup):
            self._refresh_daily_state_for_setup(setup, trigger_setup_id=setup.id)

        self.db.commit()
        self.db.refresh(setup)

    def get_daily_state(self, user_id: int, trading_day: date | None = None) -> UserDailyRiskState:
        effective_day = trading_day or self._trading_day(datetime.now(timezone.utc))
        state = self._get_or_create_state(user_id, effective_day)
        self._recalculate_daily_state(user_id=user_id, trading_day=effective_day)
        self.db.commit()
        self.db.refresh(state)
        return state

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
        state = self.get_daily_state(user_id, self._trading_day(datetime.now(timezone.utc)))
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

    def _counts_toward_risk_logic(self, setup: TradeSetup) -> bool:
        if setup.setup_source != "manual":
            return True
        return setup.manual_confirmed_at is not None

    def refresh_daily_state_for_setup(self, setup: TradeSetup) -> None:
        if not self._counts_toward_risk_logic(setup):
            return
        self._refresh_daily_state_for_setup(setup)
        self.db.commit()

    def _refresh_daily_state_for_setup(self, setup: TradeSetup, trigger_setup_id: int | None = None) -> None:
        trading_days: set[date] = set()
        for value in [setup.order1_closed_at, setup.order2_closed_at, setup.executed_at, setup.updated_at]:
            if value is not None:
                trading_days.add(self._trading_day(value))
        if not trading_days:
            trading_days.add(self._trading_day(datetime.now(timezone.utc)))
        for trading_day in trading_days:
            self._recalculate_daily_state(user_id=setup.user_id, trading_day=trading_day, trigger_setup_id=trigger_setup_id)

    def _recalculate_daily_state(self, *, user_id: int, trading_day: date, trigger_setup_id: int | None = None) -> UserDailyRiskState:
        state = self._get_or_create_state(user_id, trading_day)
        previous_count = state.consecutive_stoploss_count
        previous_lock = state.daily_lock_active
        previous_result = state.last_setup_result

        streak = 0
        daily_lock_active = False
        last_result = None
        events = self._setup_events_for_day(user_id=user_id, trading_day=trading_day)

        for event in events:
            streak_before = streak
            risk_result = self._risk_result_for_setup_outcome(event["setup_outcome"])
            if risk_result == "stoploss":
                streak += 1
                last_result = "stoploss"
            elif risk_result == "non_stoploss":
                streak = 0
                last_result = "non_stoploss"
            daily_lock_active = daily_lock_active or streak >= 2
            logger.debug(
                "Daily lock setup streak evaluation setup_id=%s setup_outcome=%s setup_time=%s streak_before=%s streak_after=%s counted_as_stoploss=%s reset_streak=%s",
                event["setup_id"],
                event["setup_outcome"],
                event["setup_time"],
                streak_before,
                streak,
                risk_result == "stoploss",
                risk_result == "non_stoploss",
            )

        state.consecutive_stoploss_count = streak
        state.daily_lock_active = daily_lock_active
        state.last_setup_result = last_result
        self.db.add(state)
        self.db.flush()

        if (previous_count != streak) or (previous_result != last_result):
            self._log(
                user_id,
                trigger_setup_id,
                "consecutive_stoploss_counter_updated",
                f"Consecutive stoploss count recalculated to {streak}.",
            )
        if not previous_lock and daily_lock_active:
            self._log(user_id, trigger_setup_id, "daily_lock_triggered", DAILY_LOCK_MESSAGE)
        if (previous_lock and not daily_lock_active) or (previous_count > streak):
            self._log(user_id, trigger_setup_id, "daily_lock_reset", "Daily lock state recalculated after non-stoploss setup.")
        return state

    def _setup_events_for_day(self, *, user_id: int, trading_day: date) -> list[dict[str, object]]:
        start = datetime.combine(trading_day, datetime.min.time(), tzinfo=timezone.utc)
        end = datetime.combine(trading_day, datetime.max.time(), tzinfo=timezone.utc)
        setups = (
            self.db.query(TradeSetup)
            .filter(TradeSetup.user_id == user_id)
            .all()
        )
        events: list[dict[str, object]] = []
        for setup in setups:
            if not self._counts_toward_risk_logic(setup):
                continue
            risk_result = self._risk_result_for_setup_outcome(setup.setup_outcome)
            if risk_result is None:
                continue
            setup_time = self._setup_risk_timestamp(setup)
            if setup_time is None:
                continue
            normalized_time = self._normalize_datetime(setup_time)
            if not (start <= normalized_time <= end):
                continue
            events.append(
                {
                    "setup_id": setup.id,
                    "setup_outcome": setup.setup_outcome,
                    "setup_time": normalized_time,
                    "risk_result": risk_result,
                }
            )
        events.sort(
            key=lambda event: (
                event["setup_time"],
                int(event["setup_id"]),
            )
        )
        return events

    def _risk_result_for_setup_outcome(self, setup_outcome: str | None) -> str | None:
        if setup_outcome == "full_loss":
            return "stoploss"
        if setup_outcome in {"managed_win", "full_win", "scratch_manual"}:
            return "non_stoploss"
        return None

    def _setup_risk_timestamp(self, setup: TradeSetup) -> datetime | None:
        close_times = [value for value in (setup.order1_closed_at, setup.order2_closed_at) if value is not None]
        if close_times:
            return max(self._normalize_datetime(value) for value in close_times)
        if setup.setup_outcome_recorded_at is not None:
            return setup.setup_outcome_recorded_at
        if setup.result_recorded_at is not None:
            return setup.result_recorded_at
        return setup.executed_at or setup.updated_at

    def _normalize_datetime(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
