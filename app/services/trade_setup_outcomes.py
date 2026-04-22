from __future__ import annotations

from datetime import datetime, timezone
import json

from sqlalchemy.orm import Session

from app.services.execution import default_adapter_factory
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
}

SETUP_MILESTONE_EVENT_TYPES = {
    "stoploss": "setup_stoploss_recorded",
    "breakeven": "setup_breakeven_recorded",
    "tp2_hit": "setup_tp2_recorded",
}

TERMINAL_SETUP_OUTCOMES = {
    "breakeven",
    "tp2_hit",
    "stoploss",
    "manual_close",
    "mixed",
    "unknown",
}


class TradeSetupOutcomeService:
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
        if not setup.order1_ticket or not setup.order2_ticket:
            raise ValueError("Executed trade setup is missing MT5 order tickets.")

        account = get_trading_account(self.db, setup.trading_account_id, user_id)
        if not account:
            raise LookupError("Trading account not found")

        adapter = self.adapter_factory(account)
        try:
            order1_snapshot = self._inspect_order(setup, order_index=1, adapter=adapter)
            order2_snapshot = self._inspect_order(setup, order_index=2, adapter=adapter)
        finally:
            close = getattr(adapter, "close", None)
            if callable(close):
                close()

        previous_order1_outcome = setup.order1_outcome
        previous_order2_outcome = setup.order2_outcome
        previous_setup_outcome = setup.setup_outcome

        self._apply_snapshot(setup, order_index=1, snapshot=order1_snapshot)
        self._apply_snapshot(setup, order_index=2, snapshot=order2_snapshot)

        setup.setup_outcome = self._derive_setup_outcome(setup, order1_snapshot["outcome"], order2_snapshot["outcome"])
        if previous_setup_outcome != setup.setup_outcome:
            setup.setup_outcome_recorded_at = datetime.now(timezone.utc)

        self.db.add(setup)
        self.db.flush()

        self._create_order_event_if_changed(setup, 1, previous_order1_outcome, order1_snapshot)
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

        history = adapter.get_position_history(position_ticket=int(ticket))
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

        if self._matches_target(close_deal, close_price, reference_tp, point, reasons={"tp", "take_profit", 5}):
            outcome = "tp_hit"
        elif self._is_closed_at_be(setup, order_index, close_deal, close_price, reference_be, point, realized_pnl):
            outcome = "closed_at_be"
        elif self._matches_target(close_deal, close_price, reference_sl, point, reasons={"sl", "stop_loss", 4}):
            outcome = "sl_hit"
        else:
            outcome = "manual_close"

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
            },
        }

    def _apply_snapshot(self, setup, *, order_index: int, snapshot: dict[str, object]) -> None:
        setattr(setup, f"order{order_index}_outcome", snapshot["outcome"])
        setattr(setup, f"order{order_index}_closed_at", snapshot["closed_at"])
        setattr(setup, f"order{order_index}_close_price", snapshot["close_price"])
        setattr(setup, f"order{order_index}_realized_pnl", snapshot["realized_pnl"])

    def _derive_setup_outcome(self, setup, order1_outcome: str, order2_outcome: str) -> str:
        if setup.status == "failed":
            return "execution_failed"
        if order1_outcome == "open" and order2_outcome == "open":
            return "open"
        if order1_outcome == "tp_hit" and order2_outcome == "open":
            return "tp1_hit_waiting_order2"
        if order1_outcome == "tp_hit" and order2_outcome == "closed_at_be":
            return "breakeven"
        if order1_outcome == "tp_hit" and order2_outcome == "tp_hit":
            return "tp2_hit"
        if order1_outcome == "sl_hit" and order2_outcome == "sl_hit":
            return "stoploss"
        if "manual_close" in {order1_outcome, order2_outcome}:
            other_outcome = order2_outcome if order1_outcome == "manual_close" else order1_outcome
            return "manual_close" if other_outcome in {"manual_close", "open", "unknown", "closed_at_be"} else "mixed"
        if "unknown" in {order1_outcome, order2_outcome}:
            return "unknown"
        if order1_outcome == "tp_hit" and order2_outcome in {"sl_hit", "manual_close"}:
            return "mixed"
        if order1_outcome == "sl_hit" and order2_outcome == "open":
            return "mixed"
        if order1_outcome == "closed_at_be" and order2_outcome == "closed_at_be":
            return "breakeven"
        return "mixed"

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
        if setup.result_status is not None:
            return
        if setup.setup_outcome == "stoploss":
            self.risk_management.record_setup_result(setup=setup, result_status="stoploss")
        elif setup.setup_outcome in TERMINAL_SETUP_OUTCOMES:
            self.risk_management.record_setup_result(setup=setup, result_status="non_stoploss")

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

    def _is_closed_at_be(
        self,
        setup,
        order_index: int,
        close_deal: dict[str, object],
        close_price: float | None,
        reference_be: float,
        point: float,
        realized_pnl: float | None,
    ) -> bool:
        tolerance = max(point, abs(reference_be) * 1e-6, 1e-6)
        if close_price is not None and abs(close_price - reference_be) <= tolerance:
            return True
        if realized_pnl is not None and abs(realized_pnl) <= max(tolerance, 0.01):
            reason = close_deal.get("reason")
            if reason in {"sl", "stop_loss", 4} or order_index == 2 or setup.order2_be_moved_at is not None:
                return True
        return False

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
        }[outcome]

    def _setup_outcome_message(self, outcome: str) -> str:
        return {
            "stoploss": "Trade setup recorded as stoploss.",
            "breakeven": "Trade setup recorded as breakeven.",
            "tp2_hit": "Trade setup recorded as TP2 hit.",
        }[outcome]

    def _result(self, setup) -> dict[str, object]:
        return {
            "success": True,
            "setup_outcome": setup.setup_outcome or "unknown",
            "order1_outcome": setup.order1_outcome or "unknown",
            "order2_outcome": setup.order2_outcome or "unknown",
            "order1_closed_at": setup.order1_closed_at,
            "order2_closed_at": setup.order2_closed_at,
            "order1_close_price": setup.order1_close_price,
            "order2_close_price": setup.order2_close_price,
            "order1_realized_pnl": setup.order1_realized_pnl,
            "order2_realized_pnl": setup.order2_realized_pnl,
            "setup_outcome_recorded_at": setup.setup_outcome_recorded_at,
        }
