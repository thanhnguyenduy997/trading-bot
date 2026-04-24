import asyncio
import logging
from contextlib import suppress
from threading import Lock

from app.core.config import Settings, get_settings
from app.core.database import SessionLocal
from app.execution.base import AdapterError
from app.services.mt5_trade_history import MT5TradeHistorySyncService
from app.services.notifications import notify_pending_trade_events
from app.services.trade_events import create_trade_event, event_exists
from app.services.trade_setup_monitoring import TradeSetupMonitoringService
from app.services.trade_setup_outcomes import TradeSetupOutcomeService
from app.services.trade_setups import list_setups_requiring_monitoring, list_setups_requiring_outcome_reconciliation


logger = logging.getLogger(__name__)
_auto_sync_cycle_lock = Lock()


class TradeMonitorRunner:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None

    def start(self) -> None:
        if not self.settings.trade_monitor_enabled:
            logger.info("Trade monitor disabled.")
            return
        if self._task is not None and not self._task.done():
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run())
        logger.info("Trade monitor started.")

    async def stop(self) -> None:
        if self._task is None or self._stop_event is None:
            return
        self._stop_event.set()
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        self._stop_event = None
        logger.info("Trade monitor stopped.")

    async def _run(self) -> None:
        interval = max(5, self.settings.trade_monitor_interval_seconds)
        next_auto_sync_at = 0.0
        while self._stop_event is not None and not self._stop_event.is_set():
            await asyncio.to_thread(run_monitoring_cycle)
            if self.settings.mt5_history_auto_sync_enabled:
                loop_time = asyncio.get_running_loop().time()
                if loop_time >= next_auto_sync_at:
                    await asyncio.to_thread(run_auto_sync_cycle, self.settings)
                    next_auto_sync_at = loop_time + max(30, self.settings.mt5_history_auto_sync_interval_seconds)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
            except TimeoutError:
                continue


def run_monitoring_cycle() -> int:
    processed_count = 0
    db = SessionLocal()
    try:
        setups = list_setups_requiring_monitoring(db)
        for setup in setups:
            service = TradeSetupMonitoringService(db)
            try:
                service.process_setup(setup.id, setup.user_id)
                processed_count += 1
            except (AdapterError, LookupError, ValueError) as exc:
                if isinstance(exc, AdapterError) and exc.code == "mt5_session_mismatch":
                    if not event_exists(db, setup.id, setup.user_id, "monitor_skipped_account_mismatch"):
                        create_trade_event(
                            db,
                            setup.user_id,
                            setup.id,
                            "monitor_skipped_account_mismatch",
                            exc.message,
                        )
                        db.commit()
                logger.exception("Trade setup monitoring failed for setup %s", setup.id)
            except Exception:
                logger.exception("Unexpected trade setup monitoring error for setup %s", setup.id)
        for setup in list_setups_requiring_outcome_reconciliation(db):
            service = TradeSetupOutcomeService(db)
            try:
                service.reconcile_setup(setup.id, setup.user_id)
                processed_count += 1
            except (AdapterError, LookupError, ValueError) as exc:
                if isinstance(exc, AdapterError) and exc.code == "mt5_session_mismatch":
                    if not event_exists(db, setup.id, setup.user_id, "monitor_skipped_account_mismatch"):
                        create_trade_event(
                            db,
                            setup.user_id,
                            setup.id,
                            "monitor_skipped_account_mismatch",
                            exc.message,
                        )
                        db.commit()
                logger.exception("Trade setup outcome reconciliation failed for setup %s", setup.id)
            except Exception:
                logger.exception("Unexpected trade setup reconciliation error for setup %s", setup.id)
        notify_pending_trade_events(db)
    finally:
        db.close()
    return processed_count


def run_auto_sync_cycle(settings: Settings | None = None) -> int:
    effective_settings = settings or get_settings()
    if not effective_settings.mt5_history_auto_sync_enabled:
        return 0
    if not _auto_sync_cycle_lock.acquire(blocking=False):
        logger.info("Skipping MT5 auto-sync cycle because another cycle is still running.")
        return 0

    db = SessionLocal()
    try:
        result = MT5TradeHistorySyncService(db).auto_sync_active_accounts(
            max_accounts=effective_settings.mt5_history_auto_sync_max_accounts_per_cycle,
            stale_after_seconds=effective_settings.mt5_history_auto_sync_stale_after_seconds,
        )
        logger.info(
            "MT5 auto-sync cycle finished: selected=%s synced_accounts=%s synced_trades=%s skipped_busy=%s",
            result["selected_accounts"],
            result["synced_accounts"],
            result["synced_trades"],
            result["skipped_busy"],
        )
        return int(result["synced_accounts"])
    finally:
        db.close()
        _auto_sync_cycle_lock.release()
