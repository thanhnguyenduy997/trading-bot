from datetime import datetime, timedelta, timezone
import json

from sqlalchemy.orm import Session

from app.execution.base import AdapterError
from app.services.account_symbols import TradingAccountSymbolService
from app.services.app_settings import get_effective_max_preview_drift_percent
from app.services.execution import TradingAccountExecutionService
from app.services.preview_service import PreviewService
from app.services.execution import default_adapter_factory
from app.services.mt5_session_state import persist_session_failure, persist_session_matched
from app.services.risk_management import RiskManagementService
from app.services.trade_events import create_trade_event
from app.services.trade_setups import get_trade_setup
from app.services.trading_accounts import get_trading_account


class PreviewDriftExceededError(ValueError):
    def __init__(self, message: str, drift: dict[str, object]) -> None:
        super().__init__(message)
        self.drift = drift


class NewsGuardConfirmationRequiredError(ValueError):
    def __init__(self, message: str, news_guard: dict[str, object]) -> None:
        super().__init__(message)
        self.news_guard = news_guard


class TradeSetupExecutionService:
    preview_ttl = timedelta(minutes=5)
    PREVIEW_DRIFT_REJECT_MESSAGE = (
        "Market conditions have changed beyond the allowed threshold. "
        "Please preview again before executing."
    )

    def __init__(self, db: Session, adapter_factory=None) -> None:
        self.db = db
        self.adapter_factory = adapter_factory or default_adapter_factory
        self.risk_management = RiskManagementService(db)
        self.account_symbols = TradingAccountSymbolService(db)

    def execute_setup(
        self,
        setup_id: int,
        user_id: int,
        *,
        accept_preview_drift: bool = False,
        accept_news_override: bool = False,
    ):
        setup = get_trade_setup(self.db, setup_id, user_id)
        if not setup:
            raise LookupError("Trade setup not found")
        if setup.setup_source == "manual":
            raise ValueError("Manual trade setups cannot be executed from the app.")
        if self._is_stale_draft(setup):
            raise ValueError("Preview is older than 5 minutes. Refresh the preview before execution.")

        account = get_trading_account(self.db, setup.trading_account_id, user_id)
        if not account:
            raise LookupError("Trading account not found")
        self.account_symbols.assert_symbol_synced(
            account_id=setup.trading_account_id,
            user_id=user_id,
            symbol=setup.symbol,
        )

        adapter = self.adapter_factory(account)
        try:
            self.risk_management.assert_execute_allowed(setup=setup, account=account)
            drift = self.evaluate_preview_drift(setup=setup, account=account, user_id=user_id)
            if drift["exceeds_threshold"] and not accept_preview_drift:
                self._record_preview_drift_reject(setup=setup, user_id=user_id, drift=drift)
                raise PreviewDriftExceededError(self._format_preview_drift_reject_message(drift["detected_drift_percent"], drift["threshold_percent"]), drift)
            if drift["exceeds_threshold"] and accept_preview_drift:
                self._apply_live_preview_to_setup(setup, drift["live_preview"])
                self._record_preview_drift_accept(setup=setup, user_id=user_id, drift=drift)
            news_guard = drift["live_preview"].news_guard or {}
            if news_guard.get("confirmation_required") and not accept_news_override:
                self._record_news_guard_reject(setup=setup, user_id=user_id, news_guard=news_guard)
                raise NewsGuardConfirmationRequiredError(str(news_guard.get("message") or "News Guard confirmation is required."), news_guard)
            if news_guard.get("confirmation_required") and accept_news_override:
                self._record_news_guard_override(setup=setup, user_id=user_id, news_guard=news_guard)
            account_info = adapter.get_account_info()
            persist_session_matched(self.db, account, account_info=account_info)
            create_trade_event(self.db, user_id, setup.id, "execute_requested", "Execution requested for trade setup.")
            setup.status = "queued"
            setup.execution_error = None
            setup.execution_details = None
            setup.monitoring_status = None
            setup.order2_be_moved_at = None
            setup.order2_be_move_error = None
            setup.order1_ticket = None
            setup.order2_ticket = None
            setup.order1_outcome = None
            setup.order2_outcome = None
            setup.order1_closed_at = None
            setup.order2_closed_at = None
            setup.order1_close_price = None
            setup.order2_close_price = None
            setup.order1_realized_pnl = None
            setup.order2_realized_pnl = None
            setup.setup_outcome = None
            setup.setup_outcome_recorded_at = None
            setup.result_status = None
            setup.result_recorded_at = None
            self.db.add(setup)
            self.db.flush()

            setup.status = "executing"
            self.db.add(setup)
            self.db.flush()

            is_single_mode = setup.setup_mode == "single_full_volume"
            try:
                order1 = adapter.place_market_order(
                    symbol=setup.symbol,
                    side=setup.side,
                    volume=float(setup.order1_volume),
                    sl=float(setup.sl_price),
                    tp=float(setup.tp2_price if is_single_mode else setup.tp1_price),
                    comment=f"setup-{setup.id}-single" if is_single_mode else f"setup-{setup.id}-o1",
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

            if is_single_mode:
                setup.order2_ticket = None
                setup.status = "executed"
                setup.execution_error = None
                setup.monitoring_status = "not_applicable"
                setup.executed_at = datetime.now(timezone.utc)
                setup.order1_outcome = "open"
                setup.order2_outcome = None
                setup.setup_outcome = "open"
                setup.setup_outcome_recorded_at = setup.executed_at
                create_trade_event(
                    self.db,
                    user_id,
                    setup.id,
                    "execute_completed",
                    f"Single full-volume trade setup executed. Ticket: {setup.order1_ticket}.",
                )
                self.db.add(setup)
                self.db.flush()
                return self._commit_and_return(setup)

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
            setup.order1_outcome = "open"
            setup.order2_outcome = "open"
            setup.setup_outcome = "open"
            setup.setup_outcome_recorded_at = setup.executed_at
            create_trade_event(
                self.db,
                user_id,
                setup.id,
                "execute_completed",
                f"Trade setup executed. Tickets: {setup.order1_ticket}, {setup.order2_ticket}.",
            )
        except AdapterError as exc:
            persist_session_failure(self.db, account, error=exc)
            if exc.code == "mt5_session_mismatch":
                create_trade_event(
                    self.db,
                    user_id,
                    setup.id,
                    "execute_blocked_account_mismatch",
                    exc.message,
                    details=self._error_details(exc),
                )
            setup.status = "failed"
            setup.execution_error = setup.execution_error or self._error_summary(exc)
            setup.execution_details = setup.execution_details or self._error_details(exc)
            setup.monitoring_status = "execution_failed"
            setup.order1_outcome = None
            setup.order2_outcome = None
            setup.setup_outcome = "execution_failed"
            setup.setup_outcome_recorded_at = datetime.now(timezone.utc)
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
        except ValueError as exc:
            setup.status = "draft"
            setup.execution_error = str(exc)
            setup.execution_details = None
            setup.monitoring_status = None
            setup.executed_at = None
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

    def _commit_and_return(self, setup):
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

    def evaluate_preview_drift(self, *, setup, account, user_id: int) -> dict[str, object]:
        execution_service = TradingAccountExecutionService(self.db, adapter_factory=self.adapter_factory)
        try:
            live_preview = PreviewService(self.db, execution_service=execution_service).build_preview(
                user_id,
                payload=self._preview_request_from_setup(setup),
            )
        except ValueError as exc:
            self.db.refresh(account)
            if account.mt5_session_status == "mismatch":
                raise AdapterError(
                    code="mt5_session_mismatch",
                    message=account.last_error.split(": ", 1)[1] if account.last_error and ": " in account.last_error else str(exc),
                    details={
                        "expected_account": account.account_number,
                        "current_login": account.current_mt5_login,
                    },
                ) from exc
            raise

        saved_stop_distance = float(setup.r_value)
        live_stop_distance = float(live_preview.r_value)
        saved_total_volume = float(setup.order1_volume) + float(setup.order2_volume or 0)
        live_total_volume = float(live_preview.order1_volume) + float(live_preview.order2_volume or 0)

        stop_distance_drift_percent = self._percent_drift(saved_stop_distance, live_stop_distance)
        total_setup_volume_drift_percent = self._percent_drift(saved_total_volume, live_total_volume)
        detected_drift_percent = max(stop_distance_drift_percent, total_setup_volume_drift_percent)
        threshold_percent = get_effective_max_preview_drift_percent(self.db, account)

        details = {
            "setup_id": setup.id,
            "account_id": account.id,
            "user_id": user_id,
            "configured_threshold_percent": round(threshold_percent, 4),
            "detected_drift_percent": round(detected_drift_percent, 4),
            "comparison_basis": "max(stop_distance_drift_percent, total_setup_volume_drift_percent)",
            "preview": {
                "symbol": setup.symbol,
                "side": setup.side,
                "estimated_entry": float(setup.estimated_entry),
                "stop_distance": saved_stop_distance,
                "total_risk_money": float(setup.total_risk_money),
                "order1_volume": float(setup.order1_volume),
                "order2_volume": float(setup.order2_volume or 0),
                "total_setup_volume": saved_total_volume,
                "setup_mode": setup.setup_mode,
            },
            "live": {
                "symbol": live_preview.symbol,
                "side": live_preview.side,
                "estimated_entry": float(live_preview.estimated_entry),
                "stop_distance": live_stop_distance,
                "total_risk_money": float(live_preview.total_risk_money),
                "order1_volume": float(live_preview.order1_volume),
                "order2_volume": float(live_preview.order2_volume or 0),
                "total_setup_volume": live_total_volume,
                "setup_mode": live_preview.setup_mode,
            },
            "drift_components": {
                "stop_distance_drift_percent": round(stop_distance_drift_percent, 4),
                "total_setup_volume_drift_percent": round(total_setup_volume_drift_percent, 4),
            },
        }
        return {
            "exceeds_threshold": detected_drift_percent > threshold_percent,
            "detected_drift_percent": detected_drift_percent,
            "threshold_percent": threshold_percent,
            "stop_distance_drift_percent": stop_distance_drift_percent,
            "total_setup_volume_drift_percent": total_setup_volume_drift_percent,
            "live_preview": live_preview,
            "details": details,
        }

    def _record_preview_drift_reject(self, *, setup, user_id: int, drift: dict[str, object]) -> None:
        create_trade_event(
            self.db,
            user_id,
            setup.id,
            "preview_drift_reject",
            self._format_preview_drift_reject_message(drift["detected_drift_percent"], drift["threshold_percent"]),
            details=json.dumps(drift["details"], indent=2, sort_keys=True),
        )

    def _record_preview_drift_accept(self, *, setup, user_id: int, drift: dict[str, object]) -> None:
        details = dict(drift["details"])
        details["user_explicitly_accepted_drift"] = True
        create_trade_event(
            self.db,
            user_id,
            setup.id,
            "preview_drift_accept_execute",
            "User accepted recalculated preview values and execution continued.",
            details=json.dumps(details, indent=2, sort_keys=True),
        )

    def _record_news_guard_reject(self, *, setup, user_id: int, news_guard: dict[str, object]) -> None:
        details = self._news_guard_audit_details(setup=setup, user_id=user_id, news_guard=news_guard, news_override=False)
        create_trade_event(
            self.db,
            user_id,
            setup.id,
            "news_guard_confirmation_required",
            "News Guard required explicit confirmation before execution. No order was submitted.",
            details=json.dumps(details, indent=2, sort_keys=True, default=str),
        )

    def _record_news_guard_override(self, *, setup, user_id: int, news_guard: dict[str, object]) -> None:
        details = self._news_guard_audit_details(setup=setup, user_id=user_id, news_guard=news_guard, news_override=True)
        create_trade_event(
            self.db,
            user_id,
            setup.id,
            "news_guard_override_execute",
            "news_override=true: user explicitly confirmed News Guard risk and execution continued.",
            details=json.dumps(details, indent=2, sort_keys=True, default=str),
        )

    def _news_guard_audit_details(
        self,
        *,
        setup,
        user_id: int,
        news_guard: dict[str, object],
        news_override: bool,
    ) -> dict[str, object]:
        preview_timestamp = setup.updated_at
        if preview_timestamp is not None and preview_timestamp.tzinfo is None:
            preview_timestamp = preview_timestamp.replace(tzinfo=timezone.utc)
        return {
            "news_override": news_override,
            "user_id": user_id,
            "trading_account_id": setup.trading_account_id,
            "symbol": setup.symbol,
            "setup_id": setup.id,
            "event_time": news_guard.get("event_time"),
            "server_time_at_execution": news_guard.get("server_time"),
            "related_news_currency": news_guard.get("event_currency"),
            "related_news_event": news_guard.get("event_name"),
            "related_news_impact": news_guard.get("event_impact"),
            "news_guard_state": news_guard.get("state"),
            "news_guard_data_status": news_guard.get("data_status"),
            "news_guard_window_start": news_guard.get("window_start"),
            "news_guard_window_end": news_guard.get("window_end"),
            "preview_timestamp": preview_timestamp.isoformat() if preview_timestamp else None,
        }

    def _apply_live_preview_to_setup(self, setup, live_preview) -> None:
        setup.estimated_entry = live_preview.estimated_entry
        setup.r_value = live_preview.r_value
        setup.tp1_price = live_preview.tp1_price
        setup.tp2_price = live_preview.tp2_price
        setup.total_risk_money = live_preview.total_risk_money
        setup.risk_per_order = live_preview.risk_per_order
        setup.order1_volume = live_preview.order1_volume
        setup.order2_volume = live_preview.order2_volume
        self.db.add(setup)
        self.db.flush()

    def _preview_request_from_setup(self, setup):
        from app.schemas.trade_preview import TradePreviewRequest

        return TradePreviewRequest(
            trading_account_id=setup.trading_account_id,
            symbol=setup.symbol,
            side=setup.side,
            sl_price=float(setup.sl_price),
            risk_mode=setup.risk_mode,
            risk_value=float(setup.risk_value),
            rr_order2=float(setup.rr_order2),
            setup_mode=setup.setup_mode,
        )

    def _percent_drift(self, saved_value: float, live_value: float) -> float:
        baseline = abs(saved_value)
        if baseline == 0:
            return 0.0 if live_value == 0 else 100.0
        return abs(live_value - saved_value) / baseline * 100

    def _format_preview_drift_reject_message(self, detected_drift_percent: float, threshold_percent: float) -> str:
        return (
            f"{self.PREVIEW_DRIFT_REJECT_MESSAGE} "
            f"Detected drift: {detected_drift_percent:.2f}% "
            f"(allowed: {threshold_percent:.2f}%)."
        )
