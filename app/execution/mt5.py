from importlib import import_module

from app.core.crypto import decrypt_value
from app.execution.base import AdapterError, ExecutionAdapter
from app.models.trading_account import TradingAccount


class MT5ExecutionAdapter(ExecutionAdapter):
    def __init__(self, account: TradingAccount) -> None:
        self.account = account
        self._mt5 = None
        self._connected = False

    def connect(self) -> None:
        self._mt5 = self._load_mt5()
        initialize_kwargs: dict[str, str] = {}
        if self.account.terminal_path:
            initialize_kwargs["path"] = self.account.terminal_path

        if not self._mt5.initialize(**initialize_kwargs):
            raise AdapterError(
                code="mt5_initialize_failed",
                message="MT5 terminal initialization failed.",
                details={"last_error": str(self._mt5.last_error())},
            )

        try:
            account_login = int(self.account.account_number)
        except ValueError as exc:
            self._mt5.shutdown()
            raise AdapterError(
                code="invalid_account_number",
                message="Trading account number must be numeric for MT5 login.",
            ) from exc

        logged_in = self._mt5.login(
            login=account_login,
            password=decrypt_value(self.account.password_encrypted),
            server=self.account.server_name,
        )
        if not logged_in:
            last_error = str(self._mt5.last_error())
            self._mt5.shutdown()
            raise AdapterError(
                code="mt5_login_failed",
                message="MT5 login failed.",
                details={"last_error": last_error},
            )
        self._connected = True

    def get_account_info(self) -> dict[str, str | int | float | None]:
        self._ensure_connected()
        account_info = self._mt5.account_info()
        if account_info is None:
            raise AdapterError(
                code="mt5_account_info_unavailable",
                message="MT5 account info is unavailable.",
                details={"last_error": str(self._mt5.last_error())},
            )
        return {
            "login": getattr(account_info, "login", None),
            "server": getattr(account_info, "server", None),
            "balance": getattr(account_info, "balance", None),
            "equity": getattr(account_info, "equity", None),
        }

    def get_quote(self, symbol: str) -> dict[str, str | int | float | None]:
        self._ensure_connected()
        tick = self._mt5.symbol_info_tick(symbol)
        if tick is None:
            raise AdapterError(
                code="mt5_quote_unavailable",
                message=f"Quote unavailable for {symbol}.",
                details={"last_error": str(self._mt5.last_error())},
            )
        return {
            "symbol": symbol,
            "bid": getattr(tick, "bid", None),
            "ask": getattr(tick, "ask", None),
            "time": getattr(tick, "time", None),
        }

    def get_symbol_info(self, symbol: str) -> dict[str, str | int | float | None]:
        self._ensure_connected()
        info = self._mt5.symbol_info(symbol)
        if info is None:
            raise AdapterError(
                code="mt5_symbol_info_unavailable",
                message=f"Symbol info unavailable for {symbol}.",
                details={"last_error": str(self._mt5.last_error())},
            )
        return {
            "symbol": symbol,
            "trade_contract_size": getattr(info, "trade_contract_size", None),
            "volume_min": getattr(info, "volume_min", None),
            "volume_step": getattr(info, "volume_step", None),
            "digits": getattr(info, "digits", None),
        }

    def execute_setup(self, setup: object) -> dict[str, str | int | float | None]:
        try:
            self.connect()
        except AdapterError as exc:
            if exc.code == "mt5_unavailable":
                raise AdapterError(
                    code="mt5_execution_unavailable",
                    message="MT5 execution unavailable on this machine.",
                    details={"hint": "Run the app on Windows with MetaTrader5 installed to enable execution."},
                ) from exc
            raise

        raise AdapterError(
            code="mt5_execution_not_implemented",
            message="MT5 order execution is not implemented yet.",
            details={"status": "skeleton_only"},
        )

    def close(self) -> None:
        if self._mt5 is not None:
            self._mt5.shutdown()
        self._connected = False

    def _ensure_connected(self) -> None:
        if not self._connected:
            self.connect()

    def _load_mt5(self):
        try:
            return import_module("MetaTrader5")
        except ImportError as exc:
            raise AdapterError(
                code="mt5_unavailable",
                message="MetaTrader5 is not available on this machine.",
                details={"hint": "Install and run MT5 on Windows to enable live connectivity."},
            ) from exc
