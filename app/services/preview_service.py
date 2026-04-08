from decimal import Decimal

from sqlalchemy.orm import Session

from app.schemas.trade_preview import TradePreviewRequest, TradePreviewResponse
from app.services.quote_service import QuoteService
from app.services.risk_service import RiskService
from app.services.trading_accounts import get_trading_account


class PreviewService:
    def __init__(
        self,
        db: Session,
        quote_service: QuoteService | None = None,
        risk_service: RiskService | None = None,
    ) -> None:
        self.db = db
        self.quote_service = quote_service or QuoteService()
        self.risk_service = risk_service or RiskService()

    def build_preview(self, user_id: int, payload: TradePreviewRequest) -> TradePreviewResponse:
        account = get_trading_account(self.db, payload.trading_account_id, user_id)
        if not account:
            raise LookupError("Trading account not found")

        quote = self.quote_service.get_quote(payload.symbol)
        entry = quote["ask"] if payload.side == "buy" else quote["bid"]
        sl_price = Decimal(str(payload.sl_price))

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

        total_risk_money = self.risk_service.calculate_total_risk(
            payload.risk_mode,
            Decimal(str(payload.risk_value)),
        )
        risk_per_order = total_risk_money / Decimal("2")
        order1_volume, order1_warnings = self.risk_service.calculate_volume(
            payload.symbol,
            entry,
            sl_price,
            risk_per_order,
        )
        order2_volume, order2_warnings = self.risk_service.calculate_volume(
            payload.symbol,
            entry,
            sl_price,
            risk_per_order,
        )

        warnings = list(dict.fromkeys(order1_warnings + order2_warnings))

        return TradePreviewResponse(
            symbol=payload.symbol,
            side=payload.side,
            estimated_entry=float(entry),
            sl_price=float(sl_price),
            r_value=float(r_value),
            tp1_price=float(tp1_price),
            tp2_price=float(tp2_price),
            total_risk_money=float(total_risk_money),
            risk_per_order=float(risk_per_order),
            order1_volume=float(order1_volume),
            order2_volume=float(order2_volume),
            validation_status="valid",
            warnings=warnings,
        )
