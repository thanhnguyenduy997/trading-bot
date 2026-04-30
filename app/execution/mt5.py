from datetime import datetime, timedelta, timezone
from importlib import import_module
import os
from typing import Any

from app.execution.base import AdapterError, ExecutionAdapter
from app.models.trading_account import TradingAccount


class MT5ExecutionAdapter(ExecutionAdapter):
    def __init__(self, account: TradingAccount) -> None:
        self.account = account
        self._mt5 = None
        self._connected = False

    def connect(self) -> None:
        if self._connected:
            return
        self._mt5 = self._load_mt5()
        initialize_kwargs: dict[str, str] = {}
        if not self.account.terminal_path:
            raise AdapterError(
                code="mt5_terminal_path_missing",
                message="MT5 terminal path is required for live MT5 actions.",
            )
        if not os.path.exists(self.account.terminal_path):
            raise AdapterError(
                code="mt5_terminal_path_not_found",
                message=f"MT5 terminal path does not exist: {self.account.terminal_path}",
                details={"terminal_path": self.account.terminal_path},
            )
        initialize_kwargs["path"] = self.account.terminal_path

        if not self._mt5.initialize(**initialize_kwargs):
            raise AdapterError(
                code="mt5_initialize_failed",
                message="MT5 terminal initialization failed.",
                details=self._error_details(terminal_path=self.account.terminal_path),
            )

        account_info = self._mt5.account_info()
        if account_info is None:
            self._mt5.shutdown()
            raise AdapterError(
                code="mt5_session_disconnected",
                message="MT5 terminal is not logged into any trading account.",
                details=self._error_details(
                    terminal_path=self.account.terminal_path,
                ),
            )

        current_login = getattr(account_info, "login", None)
        expected_login = str(self.account.account_number).strip()
        current_login_text = str(current_login).strip() if current_login is not None else None
        if current_login_text != expected_login:
            current_server = getattr(account_info, "server", None)
            self._mt5.shutdown()
            raise AdapterError(
                code="mt5_session_mismatch",
                message=(
                    f"MT5 terminal is logged into account {current_login_text or 'unknown'}, "
                    f"but this trading account expects {expected_login}. "
                    "Log into the correct MT5 account in the terminal first."
                ),
                details=self._error_details(
                    terminal_path=self.account.terminal_path,
                    expected_account=expected_login,
                    current_login=current_login_text,
                    current_server=current_server,
                ),
            )
        self._connected = True

    def get_account_info(self) -> dict[str, object]:
        self._ensure_connected()
        account_info = self._mt5.account_info()
        if account_info is None:
            raise AdapterError(
                code="mt5_account_info_unavailable",
                message="MT5 account info is unavailable.",
                details=self._error_details(),
            )
        return {
            "login": getattr(account_info, "login", None),
            "server": getattr(account_info, "server", None),
            "balance": getattr(account_info, "balance", None),
            "equity": getattr(account_info, "equity", None),
        }

    def get_quote(self, symbol: str) -> dict[str, object]:
        self._ensure_connected()
        normalized_symbol = symbol.upper()
        self._ensure_symbol_selected(normalized_symbol)
        tick = self._mt5.symbol_info_tick(normalized_symbol)
        if tick is None:
            raise AdapterError(
                code="mt5_quote_unavailable",
                message=f"Quote unavailable for {normalized_symbol}.",
                details=self._error_details(symbol=normalized_symbol),
            )
        return {
            "symbol": normalized_symbol,
            "bid": getattr(tick, "bid", None),
            "ask": getattr(tick, "ask", None),
            "time": getattr(tick, "time", None),
        }

    def get_symbol_info(self, symbol: str) -> dict[str, object]:
        self._ensure_connected()
        normalized_symbol = symbol.upper()
        info = self._ensure_symbol_selected(normalized_symbol)
        return {
            "symbol": normalized_symbol,
            "point": getattr(info, "point", None),
            "digits": getattr(info, "digits", None),
            "trade_contract_size": getattr(info, "trade_contract_size", None),
            "volume_min": getattr(info, "volume_min", None),
            "volume_max": getattr(info, "volume_max", None),
            "volume_step": getattr(info, "volume_step", None),
        }

    def list_symbols(self) -> list[dict[str, object]]:
        self._ensure_connected()
        symbols = self._mt5.symbols_get()
        if symbols is None:
            raise AdapterError(
                code="mt5_symbols_unavailable",
                message="MT5 symbol list is unavailable.",
                details=self._error_details(),
            )
        result: list[dict[str, object]] = []
        for item in symbols:
            if getattr(item, "visible", False) is not True:
                continue
            result.append(
                {
                    "symbol": getattr(item, "name", None),
                    "visible": getattr(item, "visible", None),
                    "select": getattr(item, "select", None),
                    "path": getattr(item, "path", None),
                    "trade_mode": getattr(item, "trade_mode", None),
                }
            )
        return result

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

    def place_market_order(
        self,
        *,
        symbol: str,
        side: str,
        volume: float,
        sl: float,
        tp: float,
        comment: str,
    ) -> dict[str, object]:
        self._ensure_connected()
        normalized_symbol = symbol.upper()
        symbol_info = self._ensure_symbol_selected(normalized_symbol)
        quote = self.get_quote(normalized_symbol)
        order_type = self._mt5.ORDER_TYPE_BUY if side == "buy" else self._mt5.ORDER_TYPE_SELL
        price = quote["ask"] if side == "buy" else quote["bid"]
        request_base = {
            "action": self._mt5.TRADE_ACTION_DEAL,
            "symbol": normalized_symbol,
            "volume": float(volume),
            "type": order_type,
            "price": float(price),
            "sl": float(sl),
            "tp": float(tp),
            "deviation": 20,
            "magic": 20260409,
            "comment": comment[:31],
            "type_time": self._mt5.ORDER_TIME_GTC,
        }
        result, request_used = self._send_market_order_with_fallback(
            request_base=request_base,
            symbol=normalized_symbol,
            side=side,
            volume=volume,
            sl=sl,
            tp=tp,
            symbol_info=symbol_info,
            rejection_code="mt5_order_rejected",
            rejection_message=f"Order placement rejected for {normalized_symbol}.",
            send_failed_code="mt5_order_send_failed",
            send_failed_message=f"Order placement failed for {normalized_symbol}.",
        )
        ticket = int(getattr(result, "order", 0) or getattr(result, "deal", 0))
        if ticket <= 0:
            raise AdapterError(
                code="mt5_order_ticket_missing",
                message=f"Order placement returned no usable ticket for {normalized_symbol}.",
                details=self._result_details(
                    result,
                    request_used,
                    symbol=normalized_symbol,
                    side=side,
                    volume=volume,
                    sl=sl,
                    tp=tp,
                    filling_type=request_used["type_filling"],
                    symbol_info=symbol_info,
                ),
            )
        return {
            "ticket": ticket,
            "order": getattr(result, "order", None),
            "deal": getattr(result, "deal", None),
            "price": getattr(result, "price", price),
            "volume": volume,
        }

    def close_position(
        self,
        *,
        symbol: str,
        side: str,
        volume: float,
        position_ticket: int,
        comment: str,
    ) -> dict[str, object]:
        self._ensure_connected()
        normalized_symbol = symbol.upper()
        symbol_info = self._ensure_symbol_selected(normalized_symbol)
        quote = self.get_quote(normalized_symbol)
        close_side = "sell" if side == "buy" else "buy"
        order_type = self._mt5.ORDER_TYPE_SELL if side == "buy" else self._mt5.ORDER_TYPE_BUY
        price = quote["bid"] if side == "buy" else quote["ask"]
        request_base = {
            "action": self._mt5.TRADE_ACTION_DEAL,
            "symbol": normalized_symbol,
            "volume": float(volume),
            "type": order_type,
            "position": int(position_ticket),
            "price": float(price),
            "deviation": 20,
            "magic": 20260409,
            "comment": comment[:31],
            "type_time": self._mt5.ORDER_TIME_GTC,
        }
        result, _request_used = self._send_market_order_with_fallback(
            request_base=request_base,
            symbol=normalized_symbol,
            side=close_side,
            volume=volume,
            sl=None,
            tp=None,
            symbol_info=symbol_info,
            rejection_code="mt5_rollback_rejected",
            rejection_message=f"Rollback rejected for {normalized_symbol}.",
            send_failed_code="mt5_rollback_send_failed",
            send_failed_message=f"Rollback failed for {normalized_symbol}.",
        )
        return {
            "ticket": int(getattr(result, "order", 0) or getattr(result, "deal", 0)),
            "order": getattr(result, "order", None),
            "deal": getattr(result, "deal", None),
            "price": getattr(result, "price", price),
            "volume": volume,
        }

    def get_position(self, *, position_ticket: int) -> dict[str, object] | None:
        self._ensure_connected()
        positions = self._mt5.positions_get(ticket=int(position_ticket))
        if positions is None:
            raise AdapterError(
                code="mt5_positions_unavailable",
                message=f"Unable to inspect MT5 position {position_ticket}.",
                details=self._error_details(position_ticket=position_ticket),
            )
        if not positions:
            return None
        return self._position_to_dict(positions[0])

    def list_open_positions(self) -> list[dict[str, object]]:
        self._ensure_connected()
        positions = self._mt5.positions_get()
        if positions is None:
            raise AdapterError(
                code="mt5_positions_unavailable",
                message="Unable to inspect open MT5 positions.",
                details=self._error_details(),
            )
        return [self._position_to_dict(position) for position in positions]

    def get_position_history(self, *, position_ticket: int) -> list[dict[str, object]]:
        self._ensure_connected()
        date_to = datetime.now(timezone.utc)
        date_from = date_to - timedelta(days=30)
        deals = self._mt5.history_deals_get(date_from, date_to, position=int(position_ticket))
        if deals is None:
            raise AdapterError(
                code="mt5_history_unavailable",
                message=f"Unable to inspect MT5 history for position {position_ticket}.",
                details=self._error_details(position_ticket=position_ticket),
            )
        return [self._deal_to_dict(deal) for deal in deals]

    def get_trade_history(self, *, date_from: datetime, date_to: datetime) -> list[dict[str, object]]:
        self._ensure_connected()
        deals = self._mt5.history_deals_get(date_from, date_to)
        if deals is None:
            raise AdapterError(
                code="mt5_history_unavailable",
                message="Unable to inspect MT5 account trade history.",
                details=self._error_details(
                    date_from=date_from.isoformat(),
                    date_to=date_to.isoformat(),
                ),
            )
        return [self._deal_to_dict(deal) for deal in deals]

    def modify_position_sl(
        self,
        *,
        symbol: str,
        position_ticket: int,
        sl: float,
        tp: float | None,
        comment: str,
    ) -> dict[str, object]:
        self._ensure_connected()
        normalized_symbol = symbol.upper()
        symbol_info = self._ensure_symbol_selected(normalized_symbol)
        position = self.get_position(position_ticket=position_ticket)
        if position is None:
            raise AdapterError(
                code="mt5_position_not_found",
                message=f"MT5 position {position_ticket} is no longer open.",
                details=self._error_details(symbol=normalized_symbol, position_ticket=position_ticket),
            )

        request = {
            "action": self._mt5.TRADE_ACTION_SLTP,
            "symbol": normalized_symbol,
            "position": int(position_ticket),
            "sl": float(sl),
            "tp": float(tp) if tp is not None else float(position.get("tp") or 0.0),
            "magic": 20260409,
            "comment": comment[:31],
        }
        result = self._mt5.order_send(request)
        if result is None:
            raise AdapterError(
                code="mt5_modify_sl_send_failed",
                message=f"Stop-loss modification failed for {normalized_symbol}.",
                details=self._order_context_details(
                    symbol=normalized_symbol,
                    side=str(position.get("side") or ""),
                    volume=float(position.get("volume") or 0.0),
                    sl=sl,
                    tp=tp,
                    filling_type=None,
                    request=request,
                    symbol_info=symbol_info,
                ),
            )
        if getattr(result, "retcode", None) != self._mt5.TRADE_RETCODE_DONE:
            raise AdapterError(
                code="mt5_modify_sl_rejected",
                message=f"Stop-loss modification rejected for {normalized_symbol}.",
                details=self._result_details(
                    result,
                    request,
                    symbol=normalized_symbol,
                    side=str(position.get("side") or ""),
                    volume=float(position.get("volume") or 0.0),
                    sl=sl,
                    tp=tp,
                    filling_type=None,
                    symbol_info=symbol_info,
                ),
            )

        return {
            "ticket": int(position_ticket),
            "order": getattr(result, "order", None),
            "deal": getattr(result, "deal", None),
            "sl": float(sl),
            "tp": float(request["tp"]),
        }

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

    def _ensure_symbol_selected(self, symbol: str):
        info = self._mt5.symbol_info(symbol)
        if info is None:
            raise AdapterError(
                code="mt5_symbol_not_found",
                message=f"Symbol {symbol} is unavailable in MT5.",
                details=self._error_details(symbol=symbol),
            )
        if getattr(info, "visible", False):
            return info
        if not self._mt5.symbol_select(symbol, True):
            raise AdapterError(
                code="mt5_symbol_select_failed",
                message=f"Failed to enable symbol {symbol} in MT5.",
                details=self._error_details(symbol=symbol),
            )
        refreshed = self._mt5.symbol_info(symbol)
        if refreshed is None:
            raise AdapterError(
                code="mt5_symbol_info_unavailable",
                message=f"Symbol info unavailable for {symbol}.",
                details=self._error_details(symbol=symbol),
            )
        return refreshed

    def _error_details(self, **extra: Any) -> dict[str, object]:
        last_error = None
        if self._mt5 is not None and hasattr(self._mt5, "last_error"):
            code, description = self._mt5.last_error()
            last_error = {"code": code, "description": description}
        return {"last_error": last_error, **extra}

    def _resolve_filling_type(self, symbol_info: Any) -> int:
        return self._candidate_filling_types(symbol_info)[0]

    def _candidate_filling_types(self, symbol_info: Any) -> list[int]:
        supported_mask = getattr(symbol_info, "filling_mode", None)
        trade_exemode = getattr(symbol_info, "trade_exemode", None)

        symbol_fok = getattr(self._mt5, "SYMBOL_FILLING_FOK", 1)
        symbol_ioc = getattr(self._mt5, "SYMBOL_FILLING_IOC", 2)
        symbol_boc = getattr(self._mt5, "SYMBOL_FILLING_BOC", 4)

        order_fok = self._mt5.ORDER_FILLING_FOK
        order_ioc = self._mt5.ORDER_FILLING_IOC
        order_return = self._mt5.ORDER_FILLING_RETURN
        market_execution = getattr(self._mt5, "SYMBOL_TRADE_EXECUTION_MARKET", None)

        candidates: list[int] = []
        if supported_mask is not None:
            if supported_mask & symbol_ioc:
                candidates.append(order_ioc)
            if supported_mask & symbol_fok:
                candidates.append(order_fok)
            if (
                trade_exemode is not None
                and market_execution is not None
                and trade_exemode != market_execution
            ):
                candidates.append(order_return)
            if supported_mask & symbol_boc:
                pass

        if not candidates:
            candidates.extend([order_ioc, order_fok])
            if (
                trade_exemode is not None
                and market_execution is not None
                and trade_exemode != market_execution
            ):
                candidates.append(order_return)

        unique_candidates: list[int] = []
        for candidate in candidates:
            if candidate not in unique_candidates:
                unique_candidates.append(candidate)
        return unique_candidates

    def _send_market_order_with_fallback(
        self,
        *,
        request_base: dict[str, object],
        symbol: str,
        side: str,
        volume: float,
        sl: float | None,
        tp: float | None,
        symbol_info: Any,
        rejection_code: str,
        rejection_message: str,
        send_failed_code: str,
        send_failed_message: str,
    ) -> tuple[Any, dict[str, object]]:
        candidates = self._candidate_filling_types(symbol_info)[:2]
        attempts: list[dict[str, object]] = []
        last_result = None
        last_request = None

        for filling_type in candidates:
            request = {**request_base, "type_filling": filling_type}
            result = self._mt5.order_send(request)
            last_result = result
            last_request = request
            if result is None:
                attempts.append(
                    {
                        "type_filling": filling_type,
                        "result": None,
                        "last_error": self._error_details().get("last_error"),
                    }
                )
                continue

            attempts.append(
                {
                    "type_filling": filling_type,
                    "retcode": getattr(result, "retcode", None),
                    "comment": getattr(result, "comment", None),
                }
            )
            if getattr(result, "retcode", None) == self._mt5.TRADE_RETCODE_DONE:
                return result, request
            if getattr(result, "retcode", None) != 10030:
                raise AdapterError(
                    code=rejection_code,
                    message=rejection_message,
                    details=self._result_details(
                        result,
                        request,
                        symbol=symbol,
                        side=side,
                        volume=volume,
                        sl=sl,
                        tp=tp,
                        filling_type=filling_type,
                        symbol_info=symbol_info,
                        attempted_filling_modes=candidates,
                        attempts=attempts,
                    ),
                )

        if last_result is None or last_request is None:
            raise AdapterError(
                code=send_failed_code,
                message=send_failed_message,
                details=self._order_context_details(
                    symbol=symbol,
                    side=side,
                    volume=volume,
                    sl=sl,
                    tp=tp,
                    filling_type=candidates[0] if candidates else None,
                    request=request_base,
                    symbol_info=symbol_info,
                    attempted_filling_modes=candidates,
                    attempts=attempts,
                ),
            )

        raise AdapterError(
            code=rejection_code,
            message=rejection_message,
            details=self._result_details(
                last_result,
                last_request,
                symbol=symbol,
                side=side,
                volume=volume,
                sl=sl,
                tp=tp,
                filling_type=last_request["type_filling"],
                symbol_info=symbol_info,
                attempted_filling_modes=candidates,
                attempts=attempts,
            ),
        )

    def _result_details(self, result: Any, request: dict[str, object], **context: Any) -> dict[str, object]:
        return {
            **self._order_context_details(request=request, **context),
            "retcode": getattr(result, "retcode", None),
            "comment": getattr(result, "comment", None),
            "order": getattr(result, "order", None),
            "deal": getattr(result, "deal", None),
            "volume_result": getattr(result, "volume", None),
            "price_result": getattr(result, "price", None),
        }

    def _order_context_details(
        self,
        *,
        symbol: str,
        side: str,
        volume: float,
        sl: float | None,
        tp: float | None,
        filling_type: int | None,
        request: dict[str, object],
        symbol_info: Any,
        attempted_filling_modes: list[int] | None = None,
        attempts: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        return {
            **self._error_details(),
            "symbol": symbol,
            "side": side,
            "volume": volume,
            "sl": sl,
            "tp": tp,
            "filling_mode_used": filling_type,
            "attempted_filling_modes": attempted_filling_modes,
            "attempts": attempts,
            "request": request,
            "symbol_info": {
                "trade_mode": getattr(symbol_info, "trade_mode", None),
                "trade_exemode": getattr(symbol_info, "trade_exemode", None),
                "filling_mode": getattr(symbol_info, "filling_mode", None),
                "volume_min": getattr(symbol_info, "volume_min", None),
                "volume_step": getattr(symbol_info, "volume_step", None),
                "trade_stops_level": getattr(symbol_info, "trade_stops_level", None),
            },
        }

    def _position_to_dict(self, position: Any) -> dict[str, object]:
        raw = self._namedtuple_to_dict(position)
        position_type = raw.get("type")
        return {
            **raw,
            "ticket": raw.get("ticket"),
            "symbol": raw.get("symbol"),
            "volume": raw.get("volume"),
            "price_open": raw.get("price_open"),
            "sl": raw.get("sl"),
            "tp": raw.get("tp"),
            "type": position_type,
            "side": "buy" if position_type == getattr(self._mt5, "POSITION_TYPE_BUY", 0) else "sell",
        }

    def _deal_to_dict(self, deal: Any) -> dict[str, object]:
        raw = self._namedtuple_to_dict(deal)
        return raw

    def _namedtuple_to_dict(self, value: Any) -> dict[str, object]:
        if hasattr(value, "_asdict"):
            return dict(value._asdict())
        if hasattr(value, "_fields"):
            return {field: getattr(value, field) for field in value._fields}
        return dict(value)
