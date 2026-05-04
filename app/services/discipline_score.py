from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.mt5_trade_history import MT5TradeHistory
from app.models.risk_control_log import RiskControlLog
from app.models.trade_event import TradeEvent
from app.models.trade_setup import TradeSetup
from app.models.trading_account import TradingAccount
from app.models.user import User


ENTRY_MAX = 30
RISK_MAX = 30
MANAGEMENT_MAX = 25
PROCESS_MAX = 15


@dataclass(frozen=True)
class DisciplineDeduction:
    category: str
    code: str
    label: str
    count: int
    points_each: int
    points: int


@dataclass
class DisciplineCategoryScore:
    key: str
    label: str
    max_points: int
    score: int
    deductions: list[DisciplineDeduction] = field(default_factory=list)


@dataclass
class DisciplineScore:
    total_score: int
    status_label: str
    categories: list[DisciplineCategoryScore]
    deductions: list[DisciplineDeduction]
    active_rules: list[str]
    placeholder_rules: list[str]


class DisciplineScoreService:
    """Behavior/process score for one user, one account, and one dashboard date range."""

    ACTIVE_RULES = [
        "unlinked_manual_trade_entry",
        "daily_lock_trade_attempt_entry",
        "daily_lock_or_risk_block_attempt",
        "excessive_preview_drift_accepted",
        "scratch_manual_setup",
        "review_required_setup",
        "missing_final_outcome",
        "unlinked_raw_manual_trade_process",
        "manual_setup_update_process",
    ]
    PLACEHOLDER_RULES = [
        "manual_trade_entered_outside_tool_not_recovered_when_distinct_from_unlinked_history",
        "execution_failure_followed_by_manual_entry_never_recovered",
        "actual_risk_deviates_from_planned_risk",
        "risk_worsening_manual_sl_modification",
        "manual_close_without_structured_reason",
        "manual_reconcile_or_backfill_due_to_incomplete_workflow",
        "generic_process_inconsistency_linkage_event",
    ]

    def __init__(self, db: Session) -> None:
        self.db = db

    def compute(
        self,
        *,
        actor: User,
        selected_account: TradingAccount,
        range_start: datetime,
        range_end: datetime,
    ) -> DisciplineScore:
        range_start = self._normalize_datetime(range_start)
        range_end = self._normalize_datetime(range_end)
        user_id = actor.id
        account_id = selected_account.id

        unlinked_manual_trades = self._unlinked_manual_trades(user_id, account_id, range_start, range_end)
        setups = self._setups_in_range(user_id, account_id, range_start, range_end)
        risk_logs = self._risk_logs_in_range(user_id, account_id, range_start, range_end)
        trade_events = self._trade_events_in_range(user_id, account_id, range_start, range_end)

        entry_deductions = [
            self._deduction(
                "entry",
                "unlinked_manual_trade_entry",
                "Unlinked manual trade",
                len(unlinked_manual_trades),
                5,
            ),
            self._deduction(
                "entry",
                "daily_lock_trade_attempt_entry",
                "Attempted trade while daily lock was active",
                self._count_events(risk_logs, {"daily_lock_preview_rejected", "daily_lock_execute_rejected"}),
                15,
            ),
        ]

        risk_deductions = [
            self._deduction(
                "risk",
                "daily_lock_or_risk_block_attempt",
                "Trade attempt blocked by daily/risk rule",
                self._count_events(
                    risk_logs,
                    {
                        "daily_lock_preview_rejected",
                        "daily_lock_execute_rejected",
                        "risk_limit_preview_rejected",
                        "risk_limit_execute_rejected",
                    },
                ),
                15,
            ),
            self._deduction(
                "risk",
                "excessive_preview_drift_accepted",
                "Accepted excessive preview drift",
                self._count_events(trade_events, {"preview_drift_accept_execute"}),
                3,
            ),
        ]

        management_deductions = [
            self._deduction(
                "management",
                "scratch_manual_setup",
                "Scratch manual setup",
                self._count_setups(setups, "scratch_manual"),
                4,
            ),
            self._deduction(
                "management",
                "review_required_setup",
                "Review required setup",
                self._count_setups(setups, "review_required"),
                5,
            ),
            self._deduction(
                "management",
                "manual_handling_required",
                "Setup required manual handling",
                self._count_events(trade_events, {"manual_setup_registered", "manual_setup_updated"}),
                4,
            ),
        ]

        process_deductions = [
            self._deduction(
                "process",
                "missing_final_outcome",
                "Setup missing final outcome classification",
                self._count_missing_outcomes(setups),
                3,
            ),
            self._deduction(
                "process",
                "unlinked_raw_manual_trade_process",
                "Raw manual trade still not linked or recovered",
                len(unlinked_manual_trades),
                3,
            ),
            self._deduction(
                "process",
                "manual_setup_update_process",
                "Manual setup update required",
                self._count_events(trade_events, {"manual_setup_updated"}),
                2,
            ),
        ]

        categories = [
            self._category("entry", "Entry Discipline", ENTRY_MAX, entry_deductions),
            self._category("risk", "Risk Discipline", RISK_MAX, risk_deductions),
            self._category("management", "Management Discipline", MANAGEMENT_MAX, management_deductions),
            self._category("process", "Process Discipline", PROCESS_MAX, process_deductions),
        ]
        deductions = [deduction for category in categories for deduction in category.deductions]
        total_score = sum(category.score for category in categories)
        return DisciplineScore(
            total_score=total_score,
            status_label=self.status_label(total_score),
            categories=categories,
            deductions=deductions,
            active_rules=self.ACTIVE_RULES,
            placeholder_rules=self.PLACEHOLDER_RULES,
        )

    @staticmethod
    def status_label(score: int) -> str:
        if score >= 90:
            return "Rất kỷ luật"
        if score >= 75:
            return "Tốt"
        if score >= 60:
            return "Cần siết lại"
        return "Mất kỷ luật"

    def _category(
        self,
        key: str,
        label: str,
        max_points: int,
        deductions: list[DisciplineDeduction | None],
    ) -> DisciplineCategoryScore:
        active_deductions = [deduction for deduction in deductions if deduction is not None]
        total_deduction = sum(deduction.points for deduction in active_deductions)
        return DisciplineCategoryScore(
            key=key,
            label=label,
            max_points=max_points,
            score=max(0, max_points - total_deduction),
            deductions=active_deductions,
        )

    def _deduction(
        self,
        category: str,
        code: str,
        label: str,
        count: int,
        points_each: int,
    ) -> DisciplineDeduction | None:
        if count <= 0:
            return None
        return DisciplineDeduction(
            category=category,
            code=code,
            label=label,
            count=count,
            points_each=points_each,
            points=count * points_each,
        )

    def _unlinked_manual_trades(
        self,
        user_id: int,
        account_id: int,
        range_start: datetime,
        range_end: datetime,
    ) -> list[MT5TradeHistory]:
        return (
            self.db.query(MT5TradeHistory)
            .filter(
                MT5TradeHistory.user_id == user_id,
                MT5TradeHistory.trading_account_id == account_id,
                MT5TradeHistory.trade_source == "manual",
                MT5TradeHistory.linked_setup_id.is_(None),
                MT5TradeHistory.close_time.isnot(None),
                MT5TradeHistory.close_time >= range_start,
                MT5TradeHistory.close_time <= range_end,
            )
            .all()
        )

    def _setups_in_range(
        self,
        user_id: int,
        account_id: int,
        range_start: datetime,
        range_end: datetime,
    ) -> list[TradeSetup]:
        setups = (
            self.db.query(TradeSetup)
            .filter(
                TradeSetup.user_id == user_id,
                TradeSetup.trading_account_id == account_id,
            )
            .all()
        )
        return [
            setup
            for setup in setups
            if (timestamp := self._setup_discipline_timestamp(setup)) is not None
            and range_start <= timestamp <= range_end
        ]

    def _risk_logs_in_range(
        self,
        user_id: int,
        account_id: int,
        range_start: datetime,
        range_end: datetime,
    ) -> list[RiskControlLog]:
        return (
            self.db.query(RiskControlLog)
            .join(TradeSetup, TradeSetup.id == RiskControlLog.setup_id)
            .filter(
                RiskControlLog.user_id == user_id,
                TradeSetup.trading_account_id == account_id,
                RiskControlLog.created_at >= range_start,
                RiskControlLog.created_at <= range_end,
            )
            .all()
        )

    def _trade_events_in_range(
        self,
        user_id: int,
        account_id: int,
        range_start: datetime,
        range_end: datetime,
    ) -> list[TradeEvent]:
        return (
            self.db.query(TradeEvent)
            .join(TradeSetup, TradeSetup.id == TradeEvent.setup_id)
            .filter(
                TradeEvent.user_id == user_id,
                TradeSetup.trading_account_id == account_id,
                TradeEvent.created_at >= range_start,
                TradeEvent.created_at <= range_end,
            )
            .all()
        )

    def _setup_discipline_timestamp(self, setup: TradeSetup) -> datetime | None:
        for value in [setup.setup_outcome_recorded_at, setup.result_recorded_at, setup.manual_confirmed_at]:
            if value is not None:
                return self._normalize_datetime(value)
        close_times = [self._normalize_datetime(value) for value in [setup.order1_closed_at, setup.order2_closed_at] if value is not None]
        if close_times:
            return max(close_times)
        for value in [setup.executed_at, setup.created_at, setup.updated_at]:
            if value is not None:
                return self._normalize_datetime(value)
        return None

    def _count_missing_outcomes(self, setups: list[TradeSetup]) -> int:
        return sum(
            1
            for setup in setups
            if self._requires_final_outcome(setup)
            and (setup.setup_outcome is None or setup.setup_outcome in {"", "open", "tp1_hit_waiting_order2"})
        )

    def _requires_final_outcome(self, setup: TradeSetup) -> bool:
        if setup.status in {"failed", "execution_failed"}:
            return True
        if setup.status in {"executed", "closed"}:
            return bool(setup.order1_closed_at or setup.order2_closed_at or setup.result_recorded_at)
        return bool(setup.result_recorded_at)

    def _count_setups(self, setups: list[TradeSetup], outcome: str) -> int:
        return sum(1 for setup in setups if setup.setup_outcome == outcome)

    def _count_events(self, events: list[RiskControlLog] | list[TradeEvent], event_types: set[str]) -> int:
        return sum(1 for event in events if event.event_type in event_types)

    def _normalize_datetime(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
