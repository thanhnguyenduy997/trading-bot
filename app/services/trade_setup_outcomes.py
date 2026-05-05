from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import json
import logging

from sqlalchemy.orm import Session

from app.execution.base import AdapterError
from app.models.trade_event import TradeEvent
from app.services.app_settings import get_global_scratch_manual_threshold_r
from app.services.execution import default_adapter_factory
from app.services.mt5_session_state import persist_session_failure, persist_session_matched
from app.services.risk_management import RiskManagementService
from app.services.trade_events import create_trade_event
from app.services.trade_setups import get_trade_setup
from app.services.trading_accounts import get_trading_account


ORDER_EVENT_TYPES = {
    (1, "tp_hit"): "order1_tp_hit",
    (1, "sl_hit"): "order1_sl_hit",
    (1, "manual_close"): "order1_manual_close",
    (2, "tp_hit"): "order2_tp_hit",
    (2, "sl_hit"): "order2_sl_hit",
    (2, "closed_at_be"): "order2_closed_at_be",
    (2, "manual_close"): "order2_manual_close",
    (2, "review_required"): "order2_review_required",
}

SETUP_MILESTONE_EVENT_TYPES = {
    "full_loss": "setup_full_loss_recorded",
    "managed_win": "setup_managed_win_recorded",
    "full_win": "setup_full_win_recorded",
    "scratch_manual": "setup_scratch_manual_recorded",
    "review_required": "setup_review_required_recorded",
}

TERMINAL_SETUP_OUTCOMES = {
    "full_win",
    "managed_win",
    "full_loss",
    "scratch_manual",
    "review_required",
}

FINAL_SETUP_OUTCOMES = (
    "full_win",
    "managed_win",
    "full_loss",
    "scratch_manual",
    "review_required",
)

SETUP_OUTCOME_LABELS = {
    "full_win": "TP1 + TP2",
    "managed_win": "TP1 + BE2",
    "full_loss": "SL1 + SL2",
    "scratch_manual": "Scratch Manual",
    "review_required": "Review Required",
    "open": "Open",
    "tp1_hit_waiting_order2": "TP1 Hit, Waiting Order 2",
    "execution_failed": "Execution Failed",
}

LEGACY_SETUP_OUTCOME_MAP = {
    "tp2_hit": "full_win",
    "breakeven": "managed_win",
    "stoploss": "full_loss",
}

logger = logging.getLogger(__name__)


class TradeSetupOutcomeService:
    be_price_tolerance_points = 3
    be_pnl_tolerance_floor = 1.0
    be_pnl_tolerance_risk_fraction = 0.05
    system_be_abnormal_price_tolerance_r = 0.35

    def __init__(self, db: Session, adapter_factory=None) -> None:
        self.db = db
        self.adapter_factory = adapter_factory or default_adapter_factory
        self.risk_management = RiskManagementService(db)

    def reconcile_setup(self, setup_id: int, user_id: int) -> dict[str, object]:
        setup = get_trade_setup(self.db, setup_id, user_id)
        if not setup:
            raise LookupError("Trade setup not found")

        if setup.status == "failed":
            previous_setup_outcome = setup.setup_outcome
            setup.setup_outcome = "execution_failed"
            if previous_setup_outcome != setup.setup_outcome:
                setup.setup_outcome_recorded_at = datetime.now(timezone.utc)
                self._create_setup_outcome_event(setup, previous_setup_outcome)
            self.db.add(setup)
            self.db.commit()
            self.db.refresh(setup)
            return self._result(setup)

        if setup.status != "executed":
            raise ValueError("Only executed or failed trade setups can be reconciled.")
        if not setup.order1_ticket:
            raise ValueError("Executed trade setup is missing MT5 order tickets.")
        if setup.order_count == 2 and not setup.order2_ticket:
            raise ValueError("Two-order trade setup is missing MT5 order tickets.")

        account = get_trading_account(self.db, setup.trading_account_id, user_id)
        if not account:
            raise LookupError("Trading account not found")

        adapter = self.adapter_factory(account)
        try:
            account_info = adapter.get_account_info()
            persist_session_matched(self.db, account, account_info=account_info)
            order1_snapshot = self._inspect_order(setup, order_index=1, adapter=adapter)
            order2_snapshot = (
                self._inspect_order(setup, order_index=2, adapter=adapter)
                if setup.order_count == 2 and setup.order2_ticket
                else self._unused_order_snapshot()
            )
        except AdapterError as exc:
            persist_session_failure(self.db, account, error=exc)
            raise
        finally:
            close = getattr(adapter, "close", None)
            if callable(close):
                close()

        previous_order1_outcome = setup.order1_outcome
        previous_order2_outcome = setup.order2_outcome
        previous_setup_outcome = setup.setup_outcome

        self._apply_snapshot(setup, order_index=1, snapshot=order1_snapshot)
        if setup.order_count == 2 and setup.order2_ticket:
            self._apply_snapshot(setup, order_index=2, snapshot=order2_snapshot)
        else:
            self._clear_order_snapshot(setup, order_index=2)

        setup.setup_outcome = self._derive_setup_outcome(setup, order1_snapshot["outcome"], order2_snapshot["outcome"])
        if previous_setup_outcome != setup.setup_outcome:
            setup.setup_outcome_recorded_at = datetime.now(timezone.utc)

        self.db.add(setup)
        self.db.flush()

        self._create_order_event_if_changed(setup, 1, previous_order1_outcome, order1_snapshot)
        if setup.order_count == 2 and setup.order2_ticket:
            self._create_order_event_if_changed(setup, 2, previous_order2_outcome, order2_snapshot)
        if previous_setup_outcome != setup.setup_outcome:
            self._create_setup_milestone_event(setup)
            self._create_setup_outcome_event(setup, previous_setup_outcome)

        self._record_result_status_if_needed(setup)

        self.db.add(setup)
        self.db.commit()
        self.db.refresh(setup)
        return self._result(setup)

    def _inspect_order(self, setup, *, order_index: int, adapter) -> dict[str, object]:
        ticket = getattr(setup, f"order{order_index}_ticket")
        position = adapter.get_position(position_ticket=int(ticket))
        if position is not None:
            open_price = self._coerce_float(position.get("price_open")) or float(setup.estimated_entry)
            return {
                "outcome": "open",
                "closed_at": None,
                "close_price": None,
                "realized_pnl": None,
                "ticket": ticket,
                "summary": {
                    "ticket": ticket,
                    "price_open": open_price,
                    "status": "open",
                },
            }

        history = self._filter_history_for_ticket(
            adapter.get_position_history(position_ticket=int(ticket)),
            ticket=int(ticket),
        )
        close_deal = self._latest_close_deal(history)
        if close_deal is None:
            return {
                "outcome": "unknown",
                "closed_at": None,
                "close_price": None,
                "realized_pnl": None,
                "ticket": ticket,
                "summary": {"ticket": ticket, "status": "closed_without_history"},
            }

        point = self._history_point(history) or 0.0
        reference_sl = float(setup.sl_price)
        reference_tp = float(setup.tp1_price if order_index == 1 else setup.tp2_price)
        reference_be = self._history_open_price(history) or float(setup.estimated_entry)
        close_price = self._coerce_float(close_deal.get("price"))
        realized_pnl = self._coerce_float(close_deal.get("profit"))
        closed_at = self._coerce_datetime(close_deal)

        if order_index == 2 and self._has_trusted_system_be_move(setup):
            outcome = self._classify_system_managed_order2_close(
                setup,
                close_deal=close_deal,
                close_price=close_price,
                reference_tp=reference_tp,
                reference_be=reference_be,
                point=point,
                realized_pnl=realized_pnl,
                closed_at=closed_at,
            )
        else:
            outcome = self._classify_inferred_order_close(
                setup,
                order_index=order_index,
                close_deal=close_deal,
                close_price=close_price,
                reference_tp=reference_tp,
                reference_sl=reference_sl,
                reference_be=reference_be,
                point=point,
                realized_pnl=realized_pnl,
            )

        return {
            "outcome": outcome,
            "closed_at": closed_at,
            "close_price": close_price,
            "realized_pnl": realized_pnl,
            "ticket": ticket,
            "summary": {
                "ticket": ticket,
                "close_reason": close_deal.get("reason"),
                "close_price": close_price,
                "reference_tp": reference_tp,
                "reference_sl": reference_sl,
                "reference_be": reference_be,
                "realized_pnl": realized_pnl,
                "closed_at": closed_at,
                "classification_branch": "system_managed_be"
                if order_index == 2 and self._has_trusted_system_be_move(setup)
                else "manual_inferred",
            },
        }

    def _apply_snapshot(self, setup, *, order_index: int, snapshot: dict[str, object]) -> None:
        setattr(setup, f"order{order_index}_outcome", snapshot["outcome"])
        setattr(setup, f"order{order_index}_closed_at", snapshot["closed_at"])
        setattr(setup, f"order{order_index}_close_price", snapshot["close_price"])
        setattr(setup, f"order{order_index}_realized_pnl", snapshot["realized_pnl"])

    def _clear_order_snapshot(self, setup, *, order_index: int) -> None:
        setattr(setup, f"order{order_index}_outcome", None)
        setattr(setup, f"order{order_index}_closed_at", None)
        setattr(setup, f"order{order_index}_close_price", None)
        setattr(setup, f"order{order_index}_realized_pnl", None)

    def _derive_setup_outcome(self, setup, order1_outcome: str, order2_outcome: str) -> str:
        if setup.status == "failed":
            return "execution_failed"
        if setup.order_count == 1:
            if order1_outcome == "open":
                return "open"
            return self._classify_terminal_setup_outcome(setup, order1_outcome, "not_used")
        if order1_outcome == "open" and order2_outcome == "open":
            return "open"
        if order1_outcome == "tp_hit" and order2_outcome == "open":
            return "tp1_hit_waiting_order2"
        return self._classify_terminal_setup_outcome(setup, order1_outcome, order2_outcome)

    def _unused_order_snapshot(self) -> dict[str, object]:
        return {
            "outcome": "not_used",
            "closed_at": None,
            "close_price": None,
            "realized_pnl": None,
            "ticket": None,
            "summary": {"status": "not_used"},
        }

    def _create_order_event_if_changed(
        self,
        setup,
        order_index: int,
        previous_outcome: str | None,
        snapshot: dict[str, object],
    ) -> None:
        current_outcome = snapshot["outcome"]
        if current_outcome == previous_outcome:
            return
        event_type = ORDER_EVENT_TYPES.get((order_index, current_outcome))
        if not event_type:
            return
        create_trade_event(
            self.db,
            setup.user_id,
            setup.id,
            event_type,
            self._order_event_message(order_index, current_outcome),
            details=json.dumps(snapshot["summary"], indent=2, sort_keys=True, default=str),
        )

    def _create_setup_milestone_event(self, setup) -> None:
        event_type = SETUP_MILESTONE_EVENT_TYPES.get(setup.setup_outcome)
        if not event_type:
            return
        create_trade_event(
            self.db,
            setup.user_id,
            setup.id,
            event_type,
            self._setup_outcome_message(setup.setup_outcome),
        )

    def _create_setup_outcome_event(self, setup, previous_setup_outcome: str | None) -> None:
        create_trade_event(
            self.db,
            setup.user_id,
            setup.id,
            "setup_outcome_updated",
            f"Setup outcome updated to {setup.setup_outcome}.",
            details=json.dumps(
                {
                    "previous_setup_outcome": previous_setup_outcome,
                    "current_setup_outcome": setup.setup_outcome,
                    "order1_outcome": setup.order1_outcome,
                    "order2_outcome": setup.order2_outcome,
                    "order1_close_price": setup.order1_close_price,
                    "order2_close_price": setup.order2_close_price,
                    "order1_realized_pnl": setup.order1_realized_pnl,
                    "order2_realized_pnl": setup.order2_realized_pnl,
                },
                indent=2,
                sort_keys=True,
                default=str,
            ),
        )

    def _record_result_status_if_needed(self, setup) -> None:
        target_result_status = None
        if setup.setup_outcome == "full_loss":
            target_result_status = "stoploss"
        elif setup.setup_outcome in TERMINAL_SETUP_OUTCOMES:
            target_result_status = "non_stoploss"

        if target_result_status is None:
            return
        if setup.result_status is None:
            self.risk_management.record_setup_result(setup=setup, result_status=target_result_status)
            return
        if setup.result_status == target_result_status:
            return

        setup.result_status = target_result_status
        setup.result_recorded_at = datetime.now(timezone.utc)
        self.db.add(setup)
        self.db.flush()

    def _matches_target(
        self,
        close_deal: dict[str, object],
        close_price: float | None,
        target_price: float,
        point: float,
        *,
        reasons: set[object],
    ) -> bool:
        tolerance = max(point, abs(target_price) * 1e-6, 1e-6)
        reason = close_deal.get("reason")
        if reason in reasons:
            return True
        if close_price is None:
            return False
        return abs(close_price - target_price) <= tolerance

    def _classify_system_managed_order2_close(
        self,
        setup,
        *,
        close_deal: dict[str, object],
        close_price: float | None,
        reference_tp: float,
        reference_be: float,
        point: float,
        realized_pnl: float | None,
        closed_at: datetime | None,
    ) -> str:
        if self._matches_target(close_deal, close_price, reference_tp, point, reasons={"tp", "take_profit", 5}):
            return "tp_hit"
        if self._has_system_be_contradiction(
            setup,
            close_price=close_price,
            closed_at=closed_at,
            reference_be=reference_be,
        ):
            return "review_required"
        return "closed_at_be"

    def _classify_inferred_order_close(
        self,
        setup,
        *,
        order_index: int,
        close_deal: dict[str, object],
        close_price: float | None,
        reference_tp: float,
        reference_sl: float,
        reference_be: float,
        point: float,
        realized_pnl: float | None,
    ) -> str:
        if self._matches_target(close_deal, close_price, reference_tp, point, reasons={"tp", "take_profit", 5}):
            return "tp_hit"
        if self._looks_like_tp_hit_from_price_and_pnl(
            setup,
            close_price=close_price,
            realized_pnl=realized_pnl,
            reference_tp=reference_tp,
            reference_sl=reference_sl,
            point=point,
        ):
            return "tp_hit"
        if self._is_inferred_closed_at_be(setup, order_index, close_deal, close_price, reference_be, point, realized_pnl):
            return "closed_at_be"
        if self._matches_stop_loss(close_deal, close_price, reference_sl, point, realized_pnl, setup):
            return "sl_hit"
        return "manual_close"

    def _closed_near_system_be_target(
        self,
        setup,
        *,
        close_deal: dict[str, object],
        close_price: float | None,
        reference_be: float,
        point: float,
        realized_pnl: float | None,
        closed_at: datetime | None,
    ) -> bool:
        if not self._close_is_after_or_unverifiable_be_move(setup, closed_at):
            return False
        tolerance = self._be_price_tolerance(reference_be, point)
        pnl_tolerance = self._be_pnl_tolerance(setup)
        if close_price is not None and abs(close_price - reference_be) <= tolerance:
            return True
        if realized_pnl is not None and abs(realized_pnl) <= pnl_tolerance:
            reason = close_deal.get("reason")
            if reason in {"sl", "stop_loss", 4, "client", "expert", "mobile"}:
                return True
        return False

    def _has_system_be_contradiction(
        self,
        setup,
        *,
        close_price: float | None,
        closed_at: datetime | None,
        reference_be: float,
    ) -> bool:
        if not self._close_is_after_or_unverifiable_be_move(setup, closed_at):
            return True
        if close_price is None:
            return False
        return abs(close_price - reference_be) > self._system_be_abnormal_price_tolerance(setup)

    def _is_inferred_closed_at_be(
        self,
        setup,
        order_index: int,
        close_deal: dict[str, object],
        close_price: float | None,
        reference_be: float,
        point: float,
        realized_pnl: float | None,
    ) -> bool:
        if order_index != 2:
            return False
        tolerance = self._be_price_tolerance(reference_be, point)
        pnl_tolerance = self._be_pnl_tolerance(setup)
        if close_price is not None and abs(close_price - reference_be) <= tolerance and realized_pnl is not None:
            return abs(realized_pnl) <= pnl_tolerance
        return realized_pnl is not None and abs(realized_pnl) <= pnl_tolerance

    def _matches_stop_loss(
        self,
        close_deal: dict[str, object],
        close_price: float | None,
        reference_sl: float,
        point: float,
        realized_pnl: float | None,
        setup,
    ) -> bool:
        if realized_pnl is not None and realized_pnl > self._be_pnl_tolerance(setup):
            return False
        return self._matches_target(close_deal, close_price, reference_sl, point, reasons={"sl", "stop_loss", 4})

    def _has_trusted_system_be_move(self, setup) -> bool:
        if setup.order2_be_move_error:
            return False
        if setup.order2_be_moved_at is not None:
            return True
        if setup.monitoring_status in {"be_moved", "be_already_moved", "be_already_set"}:
            return True
        return (
            self.db.query(TradeEvent.id)
            .filter(
                TradeEvent.setup_id == setup.id,
                TradeEvent.user_id == setup.user_id,
                TradeEvent.event_type == "be_move_completed",
            )
            .first()
            is not None
        )

    def _close_is_after_or_unverifiable_be_move(self, setup, closed_at: datetime | None) -> bool:
        if setup.order2_be_moved_at is None or closed_at is None:
            return True
        return self._normalize_datetime(closed_at) >= self._normalize_datetime(setup.order2_be_moved_at)

    def _looks_like_tp_hit_from_price_and_pnl(
        self,
        setup,
        *,
        close_price: float | None,
        realized_pnl: float | None,
        reference_tp: float,
        reference_sl: float,
        point: float,
    ) -> bool:
        tp_profit_threshold = max(self._be_pnl_tolerance(setup), abs(float(setup.risk_per_order)) * 0.5)
        if close_price is None or realized_pnl is None or realized_pnl < tp_profit_threshold:
            return False
        tp_tolerance = self._be_price_tolerance(reference_tp, point)
        if abs(close_price - reference_tp) <= tp_tolerance:
            return True
        return abs(close_price - reference_tp) < abs(close_price - reference_sl)

    def _be_price_tolerance(self, reference_price: float, point: float) -> float:
        return max(point * self.be_price_tolerance_points, abs(reference_price) * 1e-6, 1e-6)

    def _be_pnl_tolerance(self, setup) -> float:
        return max(
            self.be_pnl_tolerance_floor,
            abs(float(setup.risk_per_order)) * self.be_pnl_tolerance_risk_fraction,
        )

    def _system_be_abnormal_price_tolerance(self, setup) -> float:
        r_value = abs(float(getattr(setup, "r_value", 0.0) or 0.0))
        planned_distance = abs(float(setup.estimated_entry) - float(setup.sl_price))
        basis = max(r_value, planned_distance)
        if basis <= 0:
            return self._be_price_tolerance(float(setup.estimated_entry), self._stored_price_point(setup)) * 10
        return basis * self.system_be_abnormal_price_tolerance_r

    def _filter_history_for_ticket(self, history: list[dict[str, object]], *, ticket: int) -> list[dict[str, object]]:
        matching = [
            deal
            for deal in history
            if deal.get("position_id") in {None, ticket}
            and deal.get("position") in {None, ticket}
        ]
        return matching or history

    def _latest_close_deal(self, history: list[dict[str, object]]) -> dict[str, object] | None:
        close_entries = []
        out_entries = {1, 3, "out", "out_by", "out_by_reverse", "close"}
        for deal in history:
            if deal.get("entry") in out_entries:
                close_entries.append(deal)
        if not close_entries:
            return None
        return max(close_entries, key=self._deal_sort_key)

    def _history_open_price(self, history: list[dict[str, object]]) -> float | None:
        in_entries = {0, 2, "in", "in_by", "entry"}
        for deal in history:
            if deal.get("entry") in in_entries:
                price = self._coerce_float(deal.get("price"))
                if price is not None:
                    return price
        return None

    def _history_point(self, history: list[dict[str, object]]) -> float | None:
        for deal in history:
            point = self._coerce_float(deal.get("point"))
            if point:
                return point
        return None

    def _coerce_datetime(self, deal: dict[str, object]) -> datetime | None:
        value = deal.get("time")
        if isinstance(value, datetime):
            return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        value = deal.get("time_msc")
        if isinstance(value, (int, float)) and value > 0:
            return datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc)
        return None

    def _deal_sort_key(self, deal: dict[str, object]) -> tuple[float, float]:
        time_msc = deal.get("time_msc")
        if isinstance(time_msc, (int, float)):
            return float(time_msc), float(deal.get("ticket") or 0)
        time_value = deal.get("time")
        if isinstance(time_value, datetime):
            timestamp = time_value.timestamp()
        elif isinstance(time_value, (int, float)):
            timestamp = float(time_value)
        else:
            timestamp = 0.0
        return timestamp, float(deal.get("ticket") or 0)

    def _coerce_float(self, value) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _order_event_message(self, order_index: int, outcome: str) -> str:
        return {
            "tp_hit": f"Order {order_index} closed at take profit.",
            "sl_hit": f"Order {order_index} closed at stop loss.",
            "closed_at_be": f"Order {order_index} closed at breakeven.",
            "manual_close": f"Order {order_index} appears to have been closed manually.",
            "review_required": f"Order {order_index} close requires review.",
        }[outcome]

    def _setup_outcome_message(self, outcome: str) -> str:
        return {
            "full_loss": "Trade setup recorded as SL1 + SL2.",
            "managed_win": "Trade setup recorded as TP1 + BE2.",
            "full_win": "Trade setup recorded as TP1 + TP2.",
            "scratch_manual": "Trade setup recorded as Scratch Manual.",
            "review_required": "Trade setup recorded as Review Required.",
        }[outcome]

    def _result(self, setup) -> dict[str, object]:
        setup_realized_pnl = compute_setup_realized_pnl(setup)
        setup_1r_value = get_setup_1r_value(setup)
        scratch_threshold_r = get_global_scratch_manual_threshold_r(self.db)
        return {
            "success": True,
            "setup_id": setup.id,
            "setup_outcome": setup.setup_outcome or "unknown",
            "setup_outcome_label": setup_outcome_label(setup.setup_outcome),
            "order1_outcome": setup.order1_outcome or "unknown",
            "order2_outcome": setup.order2_outcome or "unknown",
            "order1_closed_at": setup.order1_closed_at,
            "order2_closed_at": setup.order2_closed_at,
            "order1_close_price": setup.order1_close_price,
            "order2_close_price": setup.order2_close_price,
            "order1_realized_pnl": setup.order1_realized_pnl,
            "order2_realized_pnl": setup.order2_realized_pnl,
            "setup_realized_pnl": setup_realized_pnl,
            "setup_1r_value": setup_1r_value,
            "scratch_manual_threshold_r": scratch_threshold_r,
            "classification_reason": describe_setup_classification(setup, scratch_threshold_r=scratch_threshold_r),
            "setup_outcome_recorded_at": setup.setup_outcome_recorded_at,
        }

    def _classify_terminal_setup_outcome(self, setup, order1_outcome: str, order2_outcome: str) -> str:
        if order1_outcome == "tp_hit" and order2_outcome == "tp_hit":
            return "full_win"
        if order1_outcome == "tp_hit" and order2_outcome == "closed_at_be":
            return "managed_win"
        if order1_outcome == "sl_hit" and order2_outcome == "sl_hit":
            return "full_loss"
        if self._is_scratch_manual_candidate(setup, order1_outcome, order2_outcome):
            return "scratch_manual"
        return "review_required"

    def _is_scratch_manual_candidate(self, setup, order1_outcome: str, order2_outcome: str) -> bool:
        if order1_outcome in {"open", "unknown"} or order2_outcome in {"open", "unknown"}:
            return False
        setup_realized_pnl = compute_setup_realized_pnl(setup)
        setup_1r_value = get_setup_1r_value(setup)
        if setup_realized_pnl is None or setup_1r_value <= 0:
            return False
        # Scratch Manual is reserved for genuinely manual/non-standard exits near flat.
        # Mixed TP/SL or other broker-driven close combinations should stay review_required.
        manual_like_close = any(outcome in {"manual_close", "closed_at_be"} for outcome in {order1_outcome, order2_outcome})
        if not manual_like_close:
            return False
        threshold_r = get_global_scratch_manual_threshold_r(self.db)
        scratch_limit = Decimal(str(setup_1r_value)) * Decimal(str(threshold_r))
        return Decimal(str(abs(setup_realized_pnl))) < scratch_limit

    def reconcile_historical_setups(
        self,
        *,
        user_id: int | None = None,
        trading_account_id: int | None = None,
        setup_ids: list[int] | None = None,
        limit: int | None = None,
    ) -> dict[str, object]:
        from app.models.trade_setup import TradeSetup

        query = self.db.query(TradeSetup).filter(TradeSetup.status.in_(["executed", "failed"]))
        if user_id is not None:
            query = query.filter(TradeSetup.user_id == user_id)
        if trading_account_id is not None:
            query = query.filter(TradeSetup.trading_account_id == trading_account_id)
        if setup_ids:
            query = query.filter(TradeSetup.id.in_(setup_ids))

        query = query.order_by(TradeSetup.id.asc())
        if limit is not None:
            query = query.limit(limit)

        results: list[dict[str, object]] = []
        for setup in query.all():
            if setup.setup_source == "manual" and setup.manual_confirmed_at is None:
                continue
            results.append(self.reconcile_setup(setup.id, setup.user_id))

        return {
            "count": len(results),
            "setup_ids": [int(item["setup_id"]) for item in results],
            "results": results,
        }

    def backfill_setup_outcomes_from_stored_data(
        self,
        *,
        user_id: int | None = None,
        trading_account_id: int | None = None,
        setup_ids: list[int] | None = None,
    ) -> dict[str, object]:
        from app.models.trade_setup import TradeSetup

        query = self.db.query(TradeSetup)
        if user_id is not None:
            query = query.filter(TradeSetup.user_id == user_id)
        if trading_account_id is not None:
            query = query.filter(TradeSetup.trading_account_id == trading_account_id)
        if setup_ids:
            query = query.filter(TradeSetup.id.in_(setup_ids))

        examined = 0
        updated = 0
        updated_ids: list[int] = []

        for setup in query.order_by(TradeSetup.id.asc()).all():
            examined += 1
            if setup.setup_source == "manual" and setup.manual_confirmed_at is None:
                continue
            history_changed = self._hydrate_setup_order_closes_from_linked_history(setup)
            stored_order_outcomes = self._reclassify_stored_order_outcomes(setup)
            normalized_outcome = self._normalize_stored_setup_outcome(setup, stored_order_outcomes=stored_order_outcomes)
            if normalized_outcome is None:
                continue

            changed = history_changed
            previous_outcome = setup.setup_outcome
            previous_order1_outcome = setup.order1_outcome
            previous_order2_outcome = setup.order2_outcome

            if stored_order_outcomes["order1_outcome"] and setup.order1_outcome != stored_order_outcomes["order1_outcome"]:
                setup.order1_outcome = stored_order_outcomes["order1_outcome"]
                changed = True
            if stored_order_outcomes["order2_outcome"] and setup.order2_outcome != stored_order_outcomes["order2_outcome"]:
                setup.order2_outcome = stored_order_outcomes["order2_outcome"]
                changed = True

            if setup.setup_outcome != normalized_outcome:
                setup.setup_outcome = normalized_outcome
                changed = True

            changed = self._sync_linked_trade_history_outcomes(setup) or changed

            inferred_time = self._inferred_setup_outcome_time(setup)
            if setup.setup_outcome_recorded_at is None and inferred_time is not None:
                setup.setup_outcome_recorded_at = inferred_time
                changed = True

            if changed:
                self._record_result_status_if_needed(setup)
                self.db.add(setup)
                updated += 1
                updated_ids.append(setup.id)
                logger.debug(
                    "Backfilled stored setup outcome setup_id=%s previous_outcome=%s normalized_outcome=%s "
                    "previous_order1_outcome=%s order1_outcome=%s order1_realized_pnl=%s "
                    "previous_order2_outcome=%s order2_outcome=%s order2_realized_pnl=%s trading_account_id=%s",
                    setup.id,
                    previous_outcome,
                    normalized_outcome,
                    previous_order1_outcome,
                    setup.order1_outcome,
                    setup.order1_realized_pnl,
                    previous_order2_outcome,
                    setup.order2_outcome,
                    setup.order2_realized_pnl,
                    setup.trading_account_id,
                )
            logger.debug(
                "Setup reclassification setup_id=%s order1_realized_pnl=%s order2_realized_pnl=%s "
                "order1_computed_outcome=%s order2_computed_outcome=%s final_setup_outcome=%s",
                setup.id,
                setup.order1_realized_pnl,
                setup.order2_realized_pnl,
                stored_order_outcomes["order1_outcome"],
                stored_order_outcomes["order2_outcome"],
                normalized_outcome,
            )

        if updated:
            self.db.commit()

        return {
            "examined": examined,
            "updated": updated,
            "setup_ids": updated_ids,
        }

    def _normalize_stored_setup_outcome(self, setup, *, stored_order_outcomes: dict[str, str | None] | None = None) -> str | None:
        if setup.status == "failed":
            return "execution_failed"

        stored_order_outcomes = stored_order_outcomes or self._reclassify_stored_order_outcomes(setup)
        order1_outcome = stored_order_outcomes["order1_outcome"] or setup.order1_outcome
        order2_outcome = (
            stored_order_outcomes["order2_outcome"] or setup.order2_outcome
            if setup.order_count == 2
            else "not_used"
        )

        if setup.order_count == 1:
            if order1_outcome and order1_outcome not in {"open", "unknown"}:
                return self._classify_terminal_setup_outcome(setup, order1_outcome, "not_used")
        else:
            if (
                order1_outcome
                and order2_outcome
                and order1_outcome not in {"open", "unknown"}
                and order2_outcome not in {"open", "unknown"}
            ):
                return self._classify_terminal_setup_outcome(setup, order1_outcome, order2_outcome)

        if setup.setup_outcome in FINAL_SETUP_OUTCOMES:
            return setup.setup_outcome
        if setup.setup_outcome in LEGACY_SETUP_OUTCOME_MAP:
            return LEGACY_SETUP_OUTCOME_MAP[setup.setup_outcome]
        if setup.result_status == "stoploss":
            return "full_loss"
        if setup.result_status == "non_stoploss":
            return "review_required"
        return None

    def _reclassify_stored_order_outcomes(self, setup) -> dict[str, str | None]:
        return {
            "order1_outcome": self._reclassify_stored_order_outcome(setup, order_index=1),
            "order2_outcome": (
                self._reclassify_stored_order_outcome(setup, order_index=2)
                if setup.order_count == 2 and setup.order2_ticket
                else None
            ),
        }

    def _reclassify_stored_order_outcome(self, setup, *, order_index: int) -> str | None:
        ticket = getattr(setup, f"order{order_index}_ticket", None)
        if not ticket:
            return None

        closed_at = getattr(setup, f"order{order_index}_closed_at", None)
        close_price = self._coerce_float(getattr(setup, f"order{order_index}_close_price", None))
        realized_pnl = self._coerce_float(getattr(setup, f"order{order_index}_realized_pnl", None))
        if closed_at is None:
            return "open"

        reference_tp = float(setup.tp1_price if order_index == 1 else setup.tp2_price)
        reference_sl = float(setup.sl_price)
        reference_be = float(setup.estimated_entry)
        point = self._stored_price_point(setup)

        if order_index == 2 and self._has_trusted_system_be_move(setup):
            return self._classify_stored_system_managed_order2_close(
                setup,
                close_price=close_price,
                realized_pnl=realized_pnl,
                reference_tp=reference_tp,
                reference_be=reference_be,
                point=point,
                closed_at=closed_at,
            )
        return self._classify_stored_inferred_order_close(
            setup,
            order_index=order_index,
            close_price=close_price,
            realized_pnl=realized_pnl,
            reference_tp=reference_tp,
            reference_sl=reference_sl,
            reference_be=reference_be,
            point=point,
        )

    def _classify_stored_system_managed_order2_close(
        self,
        setup,
        *,
        close_price: float | None,
        realized_pnl: float | None,
        reference_tp: float,
        reference_be: float,
        point: float,
        closed_at: datetime | None,
    ) -> str:
        if self._stored_order_hit_tp(
            setup,
            close_price=close_price,
            realized_pnl=realized_pnl,
            reference_tp=reference_tp,
            reference_sl=float(setup.sl_price),
            point=point,
        ):
            return "tp_hit"
        if self._has_system_be_contradiction(
            setup,
            close_price=close_price,
            closed_at=closed_at,
            reference_be=reference_be,
        ):
            return "review_required"
        return "closed_at_be"

    def _classify_stored_inferred_order_close(
        self,
        setup,
        *,
        order_index: int,
        close_price: float | None,
        realized_pnl: float | None,
        reference_tp: float,
        reference_sl: float,
        reference_be: float,
        point: float,
    ) -> str:
        if self._stored_order_hit_tp(
            setup,
            close_price=close_price,
            realized_pnl=realized_pnl,
            reference_tp=reference_tp,
            reference_sl=reference_sl,
            point=point,
        ):
            return "tp_hit"
        if self._stored_order_closed_at_be(
            setup,
            order_index=order_index,
            close_price=close_price,
            realized_pnl=realized_pnl,
            reference_be=reference_be,
            point=point,
        ):
            return "closed_at_be"
        if self._stored_order_hit_sl(
            setup,
            close_price=close_price,
            realized_pnl=realized_pnl,
            reference_sl=reference_sl,
            point=point,
        ):
            return "sl_hit"
        return "manual_close"

    def _stored_order_hit_tp(
        self,
        setup,
        *,
        close_price: float | None,
        realized_pnl: float | None,
        reference_tp: float,
        reference_sl: float,
        point: float,
    ) -> bool:
        if close_price is not None and abs(close_price - reference_tp) <= self._be_price_tolerance(reference_tp, point):
            return True
        expected_1r = abs(float(setup.risk_per_order or 0.0))
        if expected_1r <= 0 or realized_pnl is None:
            return False
        if realized_pnl >= expected_1r - self._be_pnl_tolerance(setup):
            return True
        return self._looks_like_tp_hit_from_price_and_pnl(
            setup,
            close_price=close_price,
            realized_pnl=realized_pnl,
            reference_tp=reference_tp,
            reference_sl=reference_sl,
            point=point,
        )

    def _stored_order_closed_at_be(
        self,
        setup,
        *,
        order_index: int,
        close_price: float | None,
        realized_pnl: float | None,
        reference_be: float,
        point: float,
    ) -> bool:
        if order_index != 2:
            return False
        if close_price is not None and abs(close_price - reference_be) <= self._be_price_tolerance(reference_be, point):
            return True
        return realized_pnl is not None and abs(realized_pnl) <= self._be_pnl_tolerance(setup)

    def _stored_system_order_closed_at_be(
        self,
        setup,
        *,
        close_price: float | None,
        realized_pnl: float | None,
        reference_be: float,
        point: float,
        closed_at: datetime | None,
    ) -> bool:
        if not self._close_is_after_or_unverifiable_be_move(setup, closed_at):
            return False
        return self._stored_order_closed_at_be(
            setup,
            order_index=2,
            close_price=close_price,
            realized_pnl=realized_pnl,
            reference_be=reference_be,
            point=point,
        )

    def _stored_order_hit_sl(
        self,
        setup,
        *,
        close_price: float | None,
        realized_pnl: float | None,
        reference_sl: float,
        point: float,
    ) -> bool:
        if realized_pnl is not None and realized_pnl > self._be_pnl_tolerance(setup):
            return False
        if close_price is not None and abs(close_price - reference_sl) <= self._be_price_tolerance(reference_sl, point):
            return True
        return realized_pnl is not None and realized_pnl < -self._be_pnl_tolerance(setup)

    def _stored_price_point(self, setup) -> float:
        price_values = [
            self._coerce_float(getattr(setup, field_name, None))
            for field_name in ("estimated_entry", "sl_price", "tp1_price", "tp2_price")
        ]
        decimal_places = 0
        for value in price_values:
            if value is None:
                continue
            text = f"{value:.10f}".rstrip("0")
            if "." in text:
                decimal_places = max(decimal_places, len(text.rsplit(".", 1)[1]))
        if decimal_places <= 0:
            return 0.01
        return 10 ** (-decimal_places)

    def _sync_linked_trade_history_outcomes(self, setup) -> bool:
        from app.models.mt5_trade_history import MT5TradeHistory

        changed = False
        order_outcomes = {}
        if setup.order1_ticket:
            order_outcomes[int(setup.order1_ticket)] = setup.order1_outcome
        if setup.order2_ticket:
            order_outcomes[int(setup.order2_ticket)] = setup.order2_outcome
        if not order_outcomes:
            return False

        trades = (
            self.db.query(MT5TradeHistory)
            .filter(MT5TradeHistory.linked_setup_id == setup.id)
            .all()
        )
        for trade in trades:
            outcome = order_outcomes.get(int(trade.position_ticket))
            if outcome and trade.close_time is not None and trade.outcome != outcome:
                trade.outcome = outcome
                self.db.add(trade)
                changed = True
        return changed

    def _hydrate_setup_order_closes_from_linked_history(self, setup) -> bool:
        from app.models.mt5_trade_history import MT5TradeHistory

        tickets = {
            int(ticket)
            for ticket in (setup.order1_ticket, setup.order2_ticket)
            if ticket is not None
        }
        if not tickets:
            return False

        trades = (
            self.db.query(MT5TradeHistory)
            .filter(
                MT5TradeHistory.linked_setup_id == setup.id,
                MT5TradeHistory.position_ticket.in_(tickets),
                MT5TradeHistory.close_time.isnot(None),
            )
            .all()
        )
        changed = False
        for trade in trades:
            if setup.order1_ticket and int(trade.position_ticket) == int(setup.order1_ticket):
                changed = self._hydrate_order_close_from_trade(setup, order_index=1, trade=trade) or changed
            elif setup.order2_ticket and int(trade.position_ticket) == int(setup.order2_ticket):
                changed = self._hydrate_order_close_from_trade(setup, order_index=2, trade=trade) or changed
        return changed

    def _hydrate_order_close_from_trade(self, setup, *, order_index: int, trade) -> bool:
        changed = False
        for setup_field, trade_field in (
            (f"order{order_index}_closed_at", "close_time"),
            (f"order{order_index}_close_price", "close_price"),
            (f"order{order_index}_realized_pnl", "realized_pnl"),
        ):
            trade_value = getattr(trade, trade_field)
            if trade_value is None:
                continue
            if getattr(setup, setup_field) != trade_value:
                setattr(setup, setup_field, trade_value)
                changed = True
        return changed

    def _inferred_setup_outcome_time(self, setup) -> datetime | None:
        if setup.setup_outcome_recorded_at is not None:
            return self._normalize_datetime(setup.setup_outcome_recorded_at)
        if setup.result_recorded_at is not None:
            return self._normalize_datetime(setup.result_recorded_at)

        close_times = [value for value in (setup.order1_closed_at, setup.order2_closed_at) if value is not None]
        if close_times:
            return max(self._normalize_datetime(value) for value in close_times)

        if setup.executed_at is not None:
            return self._normalize_datetime(setup.executed_at)
        if setup.updated_at is not None:
            return self._normalize_datetime(setup.updated_at)
        return None

    def _normalize_datetime(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value


def compute_setup_realized_pnl(setup) -> float | None:
    values = []
    for field_name in ("order1_realized_pnl", "order2_realized_pnl"):
        value = getattr(setup, field_name, None)
        if value is not None:
            values.append(float(value))
    if not values:
        return None
    return float(sum(values))


def get_setup_1r_value(setup) -> float:
    return abs(float(setup.total_risk_money or 0.0))


def setup_outcome_label(outcome: str | None) -> str:
    if not outcome:
        return "Unknown"
    return SETUP_OUTCOME_LABELS.get(outcome, outcome.replace("_", " ").title())


def describe_setup_classification(setup, *, scratch_threshold_r: float | None = None) -> str:
    effective_threshold = scratch_threshold_r if scratch_threshold_r is not None else 0.5
    setup_realized_pnl = compute_setup_realized_pnl(setup)
    setup_1r_value = get_setup_1r_value(setup)
    if setup.setup_outcome == "full_win":
        return "Order 1 hit TP1 and Order 2 hit TP2."
    if setup.setup_outcome == "managed_win":
        return "Order 1 hit TP1 and Order 2 closed at breakeven."
    if setup.setup_outcome == "full_loss":
        return "Order 1 and Order 2 both hit stop loss."
    if setup.setup_outcome == "scratch_manual":
        return (
            f"Non-standard/manual close with |setup realized pnl| below scratch threshold. "
            f"Realized PnL={setup_realized_pnl}, setup 1R={setup_1r_value}, threshold={effective_threshold}R."
        )
    if setup.setup_outcome == "review_required":
        return "Closed setup did not fit full_win, managed_win, full_loss, or scratch_manual."
    if setup.setup_outcome == "tp1_hit_waiting_order2":
        return "Order 1 hit TP1 and Order 2 is still open."
    if setup.setup_outcome == "open":
        return "Setup still has open order exposure."
    if setup.setup_outcome == "execution_failed":
        return "Execution failed before a valid setup outcome could be recorded."
    return "Outcome requires review."
