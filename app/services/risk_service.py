from decimal import ROUND_DOWN, Decimal


class RiskService:
    PLACEHOLDER_BALANCE = Decimal("10000")

    def calculate_total_risk(self, risk_mode: str, risk_value: Decimal) -> Decimal:
        if risk_mode == "fixed_money":
            return risk_value
        if risk_mode == "balance_percent":
            return self.PLACEHOLDER_BALANCE * (risk_value / Decimal("100"))
        raise ValueError("Unsupported risk mode")

    def calculate_volume(
        self,
        entry: Decimal,
        sl_price: Decimal,
        risk_per_order: Decimal,
        *,
        trade_contract_size: Decimal,
        volume_min: Decimal,
        volume_max: Decimal,
        volume_step: Decimal,
    ) -> tuple[Decimal, list[str]]:
        risk_distance = abs(entry - sl_price)
        if risk_distance <= 0:
            raise ValueError("Stop loss distance must be greater than zero")

        if trade_contract_size <= 0:
            raise ValueError("Live symbol info is missing trade contract size")
        if volume_min <= 0 or volume_max <= 0 or volume_step <= 0:
            raise ValueError("Live symbol info is missing valid volume limits")

        risk_per_lot = risk_distance * trade_contract_size
        raw_volume = risk_per_order / risk_per_lot
        stepped_volume = (raw_volume / volume_step).to_integral_value(rounding=ROUND_DOWN) * volume_step

        if stepped_volume < volume_min:
            raise ValueError("Computed volume is below minimum lot size")
        if stepped_volume > volume_max:
            raise ValueError("Computed volume is above maximum lot size")

        warnings: list[str] = []
        if stepped_volume < raw_volume:
            warnings.append("Volume was rounded down to the nearest lot step.")

        return stepped_volume, warnings
