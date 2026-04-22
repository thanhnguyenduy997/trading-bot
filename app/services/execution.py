from sqlalchemy.orm import Session

from app.execution.base import AdapterError, ExecutionAdapter
from app.execution.mt5 import MT5ExecutionAdapter
from app.models.trading_account import TradingAccount
from app.schemas.trading_account import (
    TradingAccountConnectionTestRead,
    TradingAccountQuoteRead,
    TradingAccountSymbolInfoRead,
)
from app.services.trading_accounts import get_trading_account
from app.services.mt5_session_state import persist_session_failure, persist_session_matched


def default_adapter_factory(account: TradingAccount) -> ExecutionAdapter:
    return MT5ExecutionAdapter(account)


class TradingAccountExecutionService:
    def __init__(self, db: Session, adapter_factory=None) -> None:
        self.db = db
        self.adapter_factory = adapter_factory or default_adapter_factory

    def test_connection(self, account_id: int, user_id: int) -> TradingAccountConnectionTestRead:
        account = self._get_owned_account(account_id, user_id)
        adapter = self.adapter_factory(account)
        try:
            adapter.connect()
            account_info = adapter.get_account_info()
            persist_session_matched(self.db, account, account_info=account_info)
            return TradingAccountConnectionTestRead(
                success=True,
                connection_status=account.connection_status,
                mt5_session_status=account.mt5_session_status,
                current_mt5_login=account.current_mt5_login,
                last_heartbeat_at=account.last_heartbeat_at,
                last_error=account.last_error,
                account_info=account_info,
                error=None,
            )
        except AdapterError as exc:
            persist_session_failure(self.db, account, error=exc)
            return TradingAccountConnectionTestRead(
                success=False,
                connection_status=account.connection_status,
                mt5_session_status=account.mt5_session_status,
                current_mt5_login=account.current_mt5_login,
                last_heartbeat_at=account.last_heartbeat_at,
                last_error=account.last_error,
                account_info=None,
                error=exc.to_dict(),
            )
        finally:
            close = getattr(adapter, "close", None)
            if callable(close):
                close()

    def fetch_quote(self, account_id: int, user_id: int, symbol: str) -> TradingAccountQuoteRead:
        account = self._get_owned_account(account_id, user_id)
        adapter = self.adapter_factory(account)
        try:
            adapter.connect()
            normalized_symbol = symbol.upper()
            quote = adapter.get_quote(normalized_symbol)
            symbol_info = adapter.get_symbol_info(normalized_symbol)
            account_info = adapter.get_account_info()
            persist_session_matched(self.db, account, account_info=account_info)
            return TradingAccountQuoteRead(
                symbol=str(quote["symbol"]),
                bid=float(quote["bid"]),
                ask=float(quote["ask"]),
                connection_status=account.connection_status,
                mt5_session_status=account.mt5_session_status,
                current_mt5_login=account.current_mt5_login,
                last_heartbeat_at=account.last_heartbeat_at,
                last_error=account.last_error,
                symbol_info=TradingAccountSymbolInfoRead.model_validate(symbol_info),
            )
        except AdapterError as exc:
            persist_session_failure(self.db, account, error=exc)
            raise
        finally:
            close = getattr(adapter, "close", None)
            if callable(close):
                close()

    def list_symbols(self, account_id: int, user_id: int) -> list[dict[str, object]]:
        account = self._get_owned_account(account_id, user_id)
        adapter = self.adapter_factory(account)
        try:
            adapter.connect()
            symbols = adapter.list_symbols()
            account_info = adapter.get_account_info()
            persist_session_matched(self.db, account, account_info=account_info)
            return symbols
        except AdapterError as exc:
            persist_session_failure(self.db, account, error=exc)
            raise
        finally:
            close = getattr(adapter, "close", None)
            if callable(close):
                close()

    def _get_owned_account(self, account_id: int, user_id: int) -> TradingAccount:
        account = get_trading_account(self.db, account_id, user_id)
        if not account:
            raise LookupError("Trading account not found")
        return account
