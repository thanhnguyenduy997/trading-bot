from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.execution.base import AdapterError
from app.services.execution import default_adapter_factory
from app.services.trade_events import create_trade_event
from app.services.trade_setups import get_trade_setup
from app.services.trading_accounts import get_trading_account


class TradeSetupExecutionService:
    def __init__(self, db: Session, adapter_factory=None) -> None:
        self.db = db
        self.adapter_factory = adapter_factory or default_adapter_factory

    def execute_setup(self, setup_id: int, user_id: int):
        setup = get_trade_setup(self.db, setup_id, user_id)
        if not setup:
            raise LookupError("Trade setup not found")

        account = get_trading_account(self.db, setup.trading_account_id, user_id)
        if not account:
            raise LookupError("Trading account not found")

        adapter = self.adapter_factory(account)
        create_trade_event(self.db, user_id, setup.id, "execute_requested", "Execution requested for trade setup.")
        setup.status = "queued"
        setup.execution_error = None
        self.db.add(setup)
        self.db.flush()

        try:
            setup.status = "executing"
            self.db.add(setup)
            self.db.flush()

            adapter.execute_setup(setup)
            setup.status = "executed"
            setup.execution_error = None
            setup.executed_at = datetime.now(timezone.utc)
            create_trade_event(self.db, user_id, setup.id, "execute_completed", "Trade setup execution completed.")
        except AdapterError as exc:
            setup.status = "failed"
            setup.execution_error = exc.message
            setup.executed_at = None
            create_trade_event(self.db, user_id, setup.id, "execute_failed", exc.message)
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
