from decimal import ROUND_DOWN, Decimal


class RiskService:
    PLACEHOLDER_BALANCE = Decimal("10000")
    _symbol_specs = {
        "XAUUSD": {
            "contract_size": Decimal("100"),
            "lot_step": Decimal("0.01"),
            "min_lot": Decimal("0.01"),
        },
        "EURUSD": {
            "contract_size": Decimal("100000"),
            "lot_step": Decimal("0.01"),
            "min_lot": Decimal("0.01"),
        },
    }

    def calculate_total_risk(self, risk_mode: str, risk_value: Decimal) -> Decimal:
        if risk_mode == "fixed_money":
            return risk_value
        if risk_mode == "balance_percent":
            return self.PLACEHOLDER_BALANCE * (risk_value / Decimal("100"))
        raise ValueError("Unsupported risk mode")

    def calculate_volume(
        self,
        symbol: str,
        entry: Decimal,
        sl_price: Decimal,
        risk_per_order: Decimal,
    ) -> tuple[Decimal, list[str]]:
        specs = self._symbol_specs.get(symbol.upper())
        if not specs:
            raise ValueError("Unsupported symbol")

        risk_distance = abs(entry - sl_price)
        if risk_distance <= 0:
            raise ValueError("Stop loss distance must be greater than zero")

        contract_size = specs["contract_size"]
        lot_step = specs["lot_step"]
        min_lot = specs["min_lot"]
        risk_per_lot = risk_distance * contract_size
        raw_volume = risk_per_order / risk_per_lot
        stepped_volume = (raw_volume / lot_step).to_integral_value(rounding=ROUND_DOWN) * lot_step

        if stepped_volume < min_lot:
            raise ValueError("Computed volume is below minimum lot size")

        warnings: list[str] = []
        if stepped_volume < raw_volume:
            warnings.append("Volume was rounded down to the nearest lot step.")

        return stepped_volume, warnings
