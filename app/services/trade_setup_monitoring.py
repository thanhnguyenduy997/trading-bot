from datetime import datetime, timezone
import json

from sqlalchemy.orm import Session

from app.execution.base import AdapterError
from app.services.execution import default_adapter_factory
from app.services.risk_management import RiskManagementService
from app.services.trade_events import create_trade_event, event_exists
from app.services.trade_setups import get_trade_setup
from app.services.trading_accounts import get_trading_account


class TradeSetupMonitoringService:
    def __init__(self, db: Session, adapter_factory=None) -> None:
        self.db = db
        self.adapter_factory = adapter_factory or default_adapter_factory
        self.risk_management = RiskManagementService(db)

    def process_setup(self, setup_id: int, user_id: int):
        setup = get_trade_setup(self.db, setup_id, user_id)
        if not setup:
            raise LookupError("Trade setup not found")
        if setup.status != "executed":
            raise ValueError("Only executed trade setups can be monitored.")
        if not setup.order1_ticket or not setup.order2_ticket:
            raise ValueError("Executed trade setup is missing MT5 order tickets.")

        account = get_trading_account(self.db, setup.trading_account_id, user_id)
        if not account:
            raise LookupError("Trading account not found")

        adapter = self.adapter_factory(account)
        try:
            order1_position = adapter.get_position(position_ticket=int(setup.order1_ticket))
            order2_position = adapter.get_position(position_ticket=int(setup.order2_ticket))

            if order1_position is not None:
                setup.monitoring_status = "waiting_tp1"
                setup.order2_be_move_error = None
                self._persist(setup)
                return self._result(setup, order1_status="open", order2_status=self._position_status(order2_position))

            if order2_position is None:
                setup.monitoring_status = "order2_closed"
                setup.order2_be_move_error = None
                if setup.result_status is None:
                    stoploss_hit, _ = self._was_closed_by_stoploss(setup, adapter.get_position_history(position_ticket=int(setup.order1_ticket)))
                    self.risk_management.record_setup_result(
                        setup=setup,
                        result_status="stoploss" if stoploss_hit else "non_stoploss",
                    )
                self._persist(setup)
                return self._result(setup, order1_status="closed", order2_status="closed")

            order1_history = adapter.get_position_history(position_ticket=int(setup.order1_ticket))
            tp1_hit, tp1_details = self._was_closed_by_tp1(setup, order1_history)
            if not tp1_hit:
                setup.monitoring_status = "order1_closed_not_tp1"
                setup.order2_be_move_error = None
                self._persist(setup)
                return self._result(setup, order1_status="closed", order2_status="open")

            if not event_exists(self.db, setup.id, user_id, "tp1_hit"):
                create_trade_event(
                    self.db,
                    user_id,
                    setup.id,
                    "tp1_hit",
                    "Order 1 appears to have closed at TP1.",
                    details=json.dumps(tp1_details, indent=2, sort_keys=True, default=str),
                )
            if setup.result_status is None:
                self.risk_management.record_setup_result(setup=setup, result_status="non_stoploss")

            be_price = float(order2_position["price_open"])
            current_sl = float(order2_position.get("sl") or 0.0)
            if setup.order2_be_moved_at is not None:
                setup.monitoring_status = "be_already_moved"
                setup.order2_be_move_error = None
                self._persist(setup)
                return self._result(setup, order1_status="closed", order2_status="open")

            if self._is_at_or_better_than_be(setup.side, current_sl, be_price, tp1_details["point"]):
                setup.monitoring_status = "be_already_set"
                setup.order2_be_move_error = None
                self._persist(setup)
                return self._result(setup, order1_status="closed", order2_status="open")

            create_trade_event(
                self.db,
                user_id,
                setup.id,
                "be_move_requested",
                f"Breakeven SL update requested for order 2 ticket {setup.order2_ticket}.",
                details=json.dumps(
                    {
                        "position_ticket": setup.order2_ticket,
                        "current_sl": current_sl,
                        "breakeven_sl": be_price,
                        "current_tp": order2_position.get("tp"),
                    },
                    indent=2,
                    sort_keys=True,
                    default=str,
                ),
            )

            try:
                adapter.modify_position_sl(
                    symbol=setup.symbol,
                    position_ticket=int(setup.order2_ticket),
                    sl=be_price,
                    tp=float(order2_position.get("tp")) if order2_position.get("tp") is not None else None,
                    comment=f"setup-{setup.id}-be",
                )
            except AdapterError as exc:
                setup.monitoring_status = "be_move_failed"
                setup.order2_be_move_error = self._error_summary(exc)
                create_trade_event(
                    self.db,
                    user_id,
                    setup.id,
                    "be_move_failed",
                    self._error_summary(exc),
                    details=self._error_details(exc),
                )
                self._persist(setup)
                raise

            setup.monitoring_status = "be_moved"
            setup.order2_be_moved_at = datetime.now(timezone.utc)
            setup.order2_be_move_error = None
            create_trade_event(
                self.db,
                user_id,
                setup.id,
                "be_move_completed",
                f"Order 2 stop loss moved to breakeven at {be_price}.",
            )
            self._persist(setup)
            return self._result(setup, order1_status="closed", order2_status="open", be_moved=True)
        finally:
            close = getattr(adapter, "close", None)
            if callable(close):
                close()

    def _was_closed_by_tp1(self, setup, history: list[dict[str, object]]) -> tuple[bool, dict[str, object]]:
        point = self._history_point(history) or 0.0
        tp1 = float(setup.tp1_price)
        tolerance = max(point, abs(tp1) * 1e-6, 1e-6)
        out_entries = {1, 3}
        if history:
            sample_entry = history[0].get("entry")
            if isinstance(sample_entry, str):
                out_entries = {"out", "out_by", "out_by_reverse", "close"}

        for deal in history:
            entry = deal.get("entry")
            if entry not in out_entries:
                continue
            reason = deal.get("reason")
            price = deal.get("price")
            if reason in {5, "tp", "take_profit"}:
                return True, {"deal": deal, "point": tolerance}
            if price is not None and abs(float(price) - tp1) <= tolerance:
                return True, {"deal": deal, "point": tolerance}
        return False, {"history": history, "point": tolerance}

    def _history_point(self, history: list[dict[str, object]]) -> float | None:
        for deal in history:
            point = deal.get("point")
            if point:
                return float(point)
        return None

    def _was_closed_by_stoploss(self, setup, history: list[dict[str, object]]) -> tuple[bool, dict[str, object]]:
        sl_price = float(setup.sl_price)
        point = self._history_point(history) or 0.0
        tolerance = max(point, abs(sl_price) * 1e-6, 1e-6)
        out_entries = {1, 3}
        if history:
            sample_entry = history[0].get("entry")
            if isinstance(sample_entry, str):
                out_entries = {"out", "out_by", "out_by_reverse", "close"}

        for deal in history:
            if deal.get("entry") not in out_entries:
                continue
            reason = deal.get("reason")
            price = deal.get("price")
            if reason in {4, "sl", "stop_loss"}:
                return True, {"deal": deal, "point": tolerance}
            if price is not None and abs(float(price) - sl_price) <= tolerance:
                return True, {"deal": deal, "point": tolerance}
        return False, {"history": history, "point": tolerance}

    def _is_at_or_better_than_be(self, side: str, current_sl: float, be_price: float, point: float) -> bool:
        tolerance = max(point, 1e-6)
        if current_sl <= 0:
            return False
        if side == "buy":
            return current_sl >= be_price - tolerance
        return current_sl <= be_price + tolerance

    def _position_status(self, position: dict[str, object] | None) -> str:
        return "open" if position is not None else "closed"

    def _persist(self, setup) -> None:
        self.db.add(setup)
        self.db.commit()
        self.db.refresh(setup)

    def _result(
        self,
        setup,
        *,
        order1_status: str,
        order2_status: str,
        be_moved: bool | None = None,
    ) -> dict[str, object]:
        return {
            "success": setup.monitoring_status not in {"be_move_failed"},
            "monitoring_status": setup.monitoring_status or "unknown",
            "order1_status": order1_status,
            "order2_status": order2_status,
            "be_moved": setup.order2_be_moved_at is not None if be_moved is None else be_moved,
            "order2_be_moved_at": setup.order2_be_moved_at,
            "order2_be_move_error": setup.order2_be_move_error,
        }

    def _error_summary(self, error: AdapterError) -> str:
        return error.message

    def _error_details(self, error: AdapterError) -> str | None:
        if not error.details:
            return None
        return json.dumps(error.details, indent=2, sort_keys=True, default=str)
