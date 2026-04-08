from decimal import Decimal


class QuoteService:
    _quotes = {
        "XAUUSD": {"bid": Decimal("2320.00"), "ask": Decimal("2320.20")},
        "EURUSD": {"bid": Decimal("1.0850"), "ask": Decimal("1.0851")},
    }

    def get_quote(self, symbol: str) -> dict[str, Decimal]:
        quote = self._quotes.get(symbol.upper())
        if not quote:
            raise ValueError("Unsupported symbol")
        return quote
