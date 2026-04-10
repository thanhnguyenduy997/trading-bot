from datetime import datetime, timedelta, timezone
import json

from sqlalchemy.orm import Session

from app.execution.base import AdapterError
from app.services.execution import default_adapter_factory
from app.services.trade_events import create_trade_event
from app.services.trade_setups import get_trade_setup
from app.services.trading_accounts import get_trading_account


class TradeSetupExecutionService:
    preview_ttl = timedelta(minutes=5)

    def __init__(self, db: Session, adapter_factory=None) -> None:
        self.db = db
        self.adapter_factory = adapter_factory or default_adapter_factory

    def execute_setup(self, setup_id: int, user_id: int):
        setup = get_trade_setup(self.db, setup_id, user_id)
        if not setup:
            raise LookupError("Trade setup not found")
        if self._is_stale_draft(setup):
            raise ValueError("Preview is older than 5 minutes. Refresh the preview before execution.")

        account = get_trading_account(self.db, setup.trading_account_id, user_id)
        if not account:
            raise LookupError("Trading account not found")

        adapter = self.adapter_factory(account)
        try:
            self._validate_quote_snapshot(adapter, setup)
            create_trade_event(self.db, user_id, setup.id, "execute_requested", "Execution requested for trade setup.")
            setup.status = "queued"
            setup.execution_error = None
            setup.execution_details = None
            setup.monitoring_status = None
            setup.order2_be_moved_at = None
            setup.order2_be_move_error = None
            setup.order1_ticket = None
            setup.order2_ticket = None
            self.db.add(setup)
            self.db.flush()

            setup.status = "executing"
            self.db.add(setup)
            self.db.flush()

            try:
                order1 = adapter.place_market_order(
                    symbol=setup.symbol,
                    side=setup.side,
                    volume=float(setup.order1_volume),
                    sl=float(setup.sl_price),
                    tp=float(setup.tp1_price),
                    comment=f"setup-{setup.id}-o1",
                )
            except AdapterError as exc:
                create_trade_event(
                    self.db,
                    user_id,
                    setup.id,
                    "order1_rejected",
                    self._error_summary(exc),
                    details=self._error_details(exc),
                )
                raise
            setup.order1_ticket = int(order1["ticket"])
            create_trade_event(
                self.db,
                user_id,
                setup.id,
                "order1_opened",
                f"Order 1 opened with ticket {setup.order1_ticket}.",
            )
            self.db.add(setup)
            self.db.flush()

            try:
                order2 = adapter.place_market_order(
                    symbol=setup.symbol,
                    side=setup.side,
                    volume=float(setup.order2_volume),
                    sl=float(setup.sl_price),
                    tp=float(setup.tp2_price),
                    comment=f"setup-{setup.id}-o2",
                )
            except AdapterError as exc:
                create_trade_event(
                    self.db,
                    user_id,
                    setup.id,
                    "order2_rejected",
                    self._error_summary(exc),
                    details=self._error_details(exc),
                )
                self._rollback_order1(adapter, setup, user_id)
                raise exc

            setup.order2_ticket = int(order2["ticket"])
            create_trade_event(
                self.db,
                user_id,
                setup.id,
                "order2_opened",
                f"Order 2 opened with ticket {setup.order2_ticket}.",
            )
            setup.status = "executed"
            setup.execution_error = None
            setup.monitoring_status = "waiting_tp1"
            setup.executed_at = datetime.now(timezone.utc)
            create_trade_event(
                self.db,
                user_id,
                setup.id,
                "execute_completed",
                f"Trade setup executed. Tickets: {setup.order1_ticket}, {setup.order2_ticket}.",
            )
        except AdapterError as exc:
            setup.status = "failed"
            setup.execution_error = setup.execution_error or self._error_summary(exc)
            setup.execution_details = setup.execution_details or self._error_details(exc)
            setup.monitoring_status = "execution_failed"
            setup.executed_at = None
            create_trade_event(
                self.db,
                user_id,
                setup.id,
                "execute_failed",
                self._error_summary(exc),
                details=self._error_details(exc),
            )
            self.db.add(setup)
            self.db.commit()
            self.db.refresh(setup)
            raise
        finally:
            close = getattr(adapter, "close", None)
            if callable(close):
                close()

        self.db.add(setup)
        self.db.commit()
        self.db.refresh(setup)
        return setup

    def _rollback_order1(self, adapter, setup, user_id: int) -> None:
        if setup.order1_ticket is None:
            return

        create_trade_event(
            self.db,
            user_id,
            setup.id,
            "rollback_started",
            f"Rollback started for order 1 ticket {setup.order1_ticket}.",
        )
        self.db.flush()

        try:
            adapter.close_position(
                symbol=setup.symbol,
                side=setup.side,
                volume=float(setup.order1_volume),
                position_ticket=int(setup.order1_ticket),
                comment=f"setup-{setup.id}-rb1",
            )
            create_trade_event(
                self.db,
                user_id,
                setup.id,
                "rollback_completed",
                f"Rollback completed for order 1 ticket {setup.order1_ticket}.",
            )
            setup.order1_ticket = None
        except AdapterError as rollback_exc:
            create_trade_event(
                self.db,
                user_id,
                setup.id,
                "rollback_failed",
                self._error_summary(rollback_exc),
                details=self._error_details(rollback_exc),
            )
            setup.execution_error = f"Rollback failed after partial execution: {self._error_summary(rollback_exc)}"
            setup.execution_details = self._error_details(rollback_exc)
            self.db.add(setup)
            self.db.flush()

    def _error_summary(self, error: AdapterError) -> str:
        return error.message

    def _error_details(self, error: AdapterError) -> str | None:
        if not error.details:
            return None
        return json.dumps(error.details, indent=2, sort_keys=True, default=str)

    def _is_stale_draft(self, setup) -> bool:
        if setup.status != "draft" or setup.updated_at is None:
            return False
        updated_at = setup.updated_at
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - updated_at > self.preview_ttl

    def _validate_quote_snapshot(self, adapter, setup) -> None:
        quote = adapter.get_quote(setup.symbol)
        quote_price = quote.get("ask") if setup.side == "buy" else quote.get("bid")
        if quote_price is None:
            return

        current_entry = float(quote_price)
        saved_entry = float(setup.estimated_entry)
        r_value = float(setup.r_value)
        max_allowed_move = max(r_value * 0.25, abs(saved_entry) * 0.0001)
        if abs(current_entry - saved_entry) > max_allowed_move:
            raise ValueError(
                "Live quote has moved materially since preview. Refresh the preview before execution."
            )
