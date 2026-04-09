from datetime import datetime, timezone

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
            self._mark_connected(account)
            return TradingAccountConnectionTestRead(
                success=True,
                connection_status=account.connection_status,
                last_heartbeat_at=account.last_heartbeat_at,
                last_error=account.last_error,
                account_info=account_info,
                error=None,
            )
        except AdapterError as exc:
            self._mark_connection_failure(account, exc)
            return TradingAccountConnectionTestRead(
                success=False,
                connection_status=account.connection_status,
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
            self._mark_connected(account)
            return TradingAccountQuoteRead(
                symbol=str(quote["symbol"]),
                bid=float(quote["bid"]),
                ask=float(quote["ask"]),
                connection_status=account.connection_status,
                last_heartbeat_at=account.last_heartbeat_at,
                last_error=account.last_error,
                symbol_info=TradingAccountSymbolInfoRead.model_validate(symbol_info),
            )
        except AdapterError as exc:
            self._mark_quote_failure(account, exc)
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

    def _mark_connected(self, account: TradingAccount) -> None:
        self._persist_status(account, status_value="connected", error_message=None)

    def _mark_disconnected(self, account: TradingAccount, error: AdapterError) -> None:
        self._persist_status(account, status_value="disconnected", error_message=self._format_error(error))

    def _mark_error(self, account: TradingAccount, error: AdapterError) -> None:
        self._persist_status(account, status_value="error", error_message=self._format_error(error))

    def _mark_connection_failure(self, account: TradingAccount, error: AdapterError) -> None:
        if error.code in {"mt5_initialize_failed", "mt5_login_failed", "invalid_account_number"}:
            self._mark_disconnected(account, error)
            return
        self._mark_error(account, error)

    def _mark_quote_failure(self, account: TradingAccount, error: AdapterError) -> None:
        if error.code in {"mt5_initialize_failed", "mt5_login_failed", "invalid_account_number"}:
            self._mark_disconnected(account, error)
            return
        if error.code in {
            "mt5_symbol_not_found",
            "mt5_symbol_select_failed",
            "mt5_symbol_info_unavailable",
            "mt5_quote_unavailable",
        }:
            self._persist_status(account, status_value="connected", error_message=self._format_error(error))
            return
        self._mark_error(account, error)

    def _persist_status(self, account: TradingAccount, status_value: str, error_message: str | None) -> None:
        account.connection_status = status_value
        account.last_heartbeat_at = datetime.now(timezone.utc)
        account.last_error = error_message
        self.db.add(account)
        self.db.commit()
        self.db.refresh(account)

    def _format_error(self, error: AdapterError) -> str:
        return f"{error.code}: {error.message}"
