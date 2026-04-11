from concurrent.futures import ThreadPoolExecutor, TimeoutError
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.execution.base import AdapterError
from app.models.trading_account_symbol import TradingAccountSymbol
from app.services.execution import default_adapter_factory
from app.services.trading_accounts import get_trading_account


EMPTY_SYMBOL_MESSAGE = (
    "No symbols are synced for this account yet. Add symbols in MT5 Market Watch first, "
    "then click Refresh Symbols from MT5."
)
SYNC_TIMEOUT_MESSAGE = "Symbol sync timed out. Check MT5 connection and try Refresh Symbols from MT5 again."
SYNC_FAILED_MESSAGE = "Symbol sync failed. Check MT5 connection and try Refresh Symbols from MT5 again."
SYMBOL_NOT_SYNCED_MESSAGE = (
    "Symbol is not synced for this trading account. Add it in MT5 Market Watch first, "
    "then click Refresh Symbols from MT5."
)


class TradingAccountSymbolService:
    sync_timeout_seconds = 8

    def __init__(self, db: Session, adapter_factory=None) -> None:
        self.db = db
        self.adapter_factory = adapter_factory or default_adapter_factory

    def list_synced_symbols(self, *, account_id: int, user_id: int) -> list[dict[str, str]]:
        account = get_trading_account(self.db, account_id, user_id)
        if not account:
            raise LookupError("Trading account not found")
        rows = (
            self.db.query(TradingAccountSymbol)
            .filter(TradingAccountSymbol.trading_account_id == account_id)
            .order_by(TradingAccountSymbol.symbol_name.asc())
            .all()
        )
        return [{"symbol_name": row.symbol_name} for row in rows]

    def get_symbol_message(self, *, account_id: int, user_id: int) -> str | None:
        account = get_trading_account(self.db, account_id, user_id)
        if not account:
            raise LookupError("Trading account not found")
        count = (
            self.db.query(TradingAccountSymbol)
            .filter(TradingAccountSymbol.trading_account_id == account_id)
            .count()
        )
        if count > 0:
            return None
        if account.symbols_sync_status == "failed" and account.symbols_sync_error:
            return account.symbols_sync_error
        return EMPTY_SYMBOL_MESSAGE

    def assert_symbol_synced(self, *, account_id: int, user_id: int, symbol: str) -> None:
        account = get_trading_account(self.db, account_id, user_id)
        if not account:
            raise LookupError("Trading account not found")
        normalized = symbol.upper()
        exists = (
            self.db.query(TradingAccountSymbol)
            .filter(
                TradingAccountSymbol.trading_account_id == account_id,
                TradingAccountSymbol.symbol_name == normalized,
            )
            .first()
        )
        if not exists:
            raise ValueError(SYMBOL_NOT_SYNCED_MESSAGE)

    def sync_symbols(self, *, account_id: int, user_id: int) -> list[dict[str, str]]:
        account = get_trading_account(self.db, account_id, user_id)
        if not account:
            raise LookupError("Trading account not found")

        try:
            symbols = self._fetch_market_watch_symbols(account)
        except TimeoutError as exc:
            self._mark_sync_failed(account, SYNC_TIMEOUT_MESSAGE)
            raise ValueError(SYNC_TIMEOUT_MESSAGE) from exc
        except AdapterError as exc:
            error_message = exc.message or SYNC_FAILED_MESSAGE
            self._mark_sync_failed(account, error_message)
            raise

        synced_at = datetime.now(timezone.utc)
        (
            self.db.query(TradingAccountSymbol)
            .filter(TradingAccountSymbol.trading_account_id == account_id)
            .delete(synchronize_session=False)
        )
        seen_symbols: set[str] = set()
        for item in symbols:
            normalized_symbol = str(item["symbol"]).upper()
            if normalized_symbol in seen_symbols:
                continue
            seen_symbols.add(normalized_symbol)
            self.db.add(
                TradingAccountSymbol(
                    trading_account_id=account_id,
                    symbol_name=normalized_symbol,
                    last_synced_at=synced_at,
                    sync_status="synced",
                    sync_error=None,
                )
            )

        account.symbols_last_synced_at = synced_at
        account.symbols_sync_status = "synced"
        account.symbols_sync_error = None
        self.db.add(account)
        self.db.commit()
        self.db.refresh(account)
        return self.list_synced_symbols(account_id=account_id, user_id=user_id)

    def _mark_sync_failed(self, account, error_message: str) -> None:
        account.symbols_sync_status = "failed"
        account.symbols_sync_error = error_message
        account.symbols_last_synced_at = datetime.now(timezone.utc)
        self.db.add(account)
        self.db.commit()
        self.db.refresh(account)

    def _fetch_market_watch_symbols(self, account) -> list[dict[str, object]]:
        def work() -> list[dict[str, object]]:
            adapter = self.adapter_factory(account)
            try:
                adapter.connect()
                return adapter.list_symbols()
            finally:
                close = getattr(adapter, "close", None)
                if callable(close):
                    close()

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(work)
            return future.result(timeout=self.sync_timeout_seconds)
