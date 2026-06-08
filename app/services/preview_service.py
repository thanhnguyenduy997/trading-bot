from decimal import Decimal, ROUND_HALF_UP
import logging

from sqlalchemy.orm import Session

from app.execution.base import AdapterError
from app.schemas.trade_preview import TradePreviewRequest, TradePreviewResponse
from app.services.account_symbols import TradingAccountSymbolService
from app.services.execution import TradingAccountExecutionService
from app.services.risk_management import RiskManagementService
from app.services.risk_service import RiskService
from app.services.trading_accounts import get_trading_account

logger = logging.getLogger(__name__)


class PreviewService:
    def __init__(
        self,
        db: Session,
        execution_service: TradingAccountExecutionService | None = None,
        risk_service: RiskService | None = None,
    ) -> None:
        self.db = db
        self.execution_service = execution_service or TradingAccountExecutionService(db)
        self.risk_service = risk_service or RiskService()
        self.risk_management = RiskManagementService(db)
        self.account_symbols = TradingAccountSymbolService(db)

    def build_preview(self, user_id: int, payload: TradePreviewRequest) -> TradePreviewResponse:
        account = get_trading_account(self.db, payload.trading_account_id, user_id)
        if not account:
            raise LookupError("Trading account not found")
        self.account_symbols.assert_symbol_synced(
            account_id=payload.trading_account_id,
            user_id=user_id,
            symbol=payload.symbol,
        )

        try:
            market_data = self.execution_service.fetch_quote(
                payload.trading_account_id,
                user_id,
                payload.symbol,
            )
        except AdapterError as exc:
            if exc.code == "mt5_session_mismatch":
                logger.warning(
                    "preview_blocked_account_mismatch user_id=%s account_id=%s symbol=%s message=%s",
                    user_id,
                    payload.trading_account_id,
                    payload.symbol,
                    exc.message,
                )
            message = exc.message
            raise ValueError(f"Live MT5 preview unavailable: {message}") from exc

        symbol_info = market_data.symbol_info
        if symbol_info is None:
            raise ValueError("Live MT5 preview unavailable: symbol info is missing")

        bid = Decimal(str(market_data.bid))
        ask = Decimal(str(market_data.ask))
        entry = ask if payload.side == "buy" else bid
        sl_price = Decimal(str(payload.sl_price))
        digits = symbol_info.digits

        if payload.side == "buy":
            if sl_price >= entry:
                raise ValueError("For buy setups, sl_price must be below the estimated entry.")
            r_value = entry - sl_price
            tp1_price = entry + r_value
            tp2_price = entry + (Decimal(str(payload.rr_order2)) * r_value)
        else:
            if sl_price <= entry:
                raise ValueError("For sell setups, sl_price must be above the estimated entry.")
            r_value = sl_price - entry
            tp1_price = entry - r_value
            tp2_price = entry - (Decimal(str(payload.rr_order2)) * r_value)

        estimated_entry = self._round_price(entry, digits)
        tp1_price = self._round_price(tp1_price, digits)
        tp2_price = self._round_price(tp2_price, digits)

        total_risk_money = self.risk_service.calculate_total_risk(
            payload.risk_mode,
            Decimal(str(payload.risk_value)),
        )
        setup_mode = getattr(payload, "setup_mode", "split_two_orders")
        if setup_mode == "single_full_volume":
            risk_per_order = total_risk_money
            order_count = 1
        else:
            risk_per_order = total_risk_money / Decimal("2")
            order_count = 2
        trade_contract_size = self._required_decimal(symbol_info.trade_contract_size, "trade_contract_size")
        volume_min = self._required_decimal(symbol_info.volume_min, "volume_min")
        volume_max = self._required_decimal(symbol_info.volume_max, "volume_max")
        volume_step = self._required_decimal(symbol_info.volume_step, "volume_step")

        order1_volume, order1_warnings = self.risk_service.calculate_volume(
            entry,
            sl_price,
            risk_per_order,
            trade_contract_size=trade_contract_size,
            volume_min=volume_min,
            volume_max=volume_max,
            volume_step=volume_step,
        )
        if order_count == 2:
            order2_volume, order2_warnings = self.risk_service.calculate_volume(
                entry,
                sl_price,
                risk_per_order,
                trade_contract_size=trade_contract_size,
                volume_min=volume_min,
                volume_max=volume_max,
                volume_step=volume_step,
            )
        else:
            order2_volume = Decimal("0")
            order2_warnings = []

        warnings = list(dict.fromkeys(order1_warnings + order2_warnings))
        total_setup_volume = order1_volume + order2_volume
        self.risk_management.assert_preview_allowed(
            user_id=user_id,
            account=account,
            total_setup_volume=total_setup_volume,
        )

        return TradePreviewResponse(
            symbol=payload.symbol,
            side=payload.side,
            setup_mode=setup_mode,
            bid=float(bid),
            ask=float(ask),
            estimated_entry=float(estimated_entry),
            sl_price=float(sl_price),
            r_value=float(r_value),
            tp1_price=float(tp1_price),
            tp2_price=float(tp2_price),
            total_risk_money=float(total_risk_money),
            risk_per_order=float(risk_per_order),
            order1_volume=float(order1_volume),
            order2_volume=float(order2_volume),
            single_order_volume=float(order1_volume) if order_count == 1 else None,
            point=symbol_info.point,
            digits=symbol_info.digits,
            trade_contract_size=symbol_info.trade_contract_size,
            volume_min=symbol_info.volume_min,
            volume_max=symbol_info.volume_max,
            volume_step=symbol_info.volume_step,
            validation_status="valid",
            warnings=warnings,
        )

    def _required_decimal(self, value: float | None, field_name: str) -> Decimal:
        if value is None:
            raise ValueError(f"Live MT5 preview unavailable: {field_name} is missing")
        return Decimal(str(value))

    def _round_price(self, value: Decimal, digits: int | None) -> Decimal:
        if digits is None:
            return value
        quant = Decimal("1").scaleb(-digits)
        return value.quantize(quant, rounding=ROUND_HALF_UP)
