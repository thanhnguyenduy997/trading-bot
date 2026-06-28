from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re


NEWS_GUARD_WARNING_MINUTES = 30
NEWS_GUARD_BLOCK_BEFORE_MINUTES = 5
NEWS_GUARD_BLOCK_AFTER_MINUTES = 5
NEWS_GUARD_STALE_AFTER_MINUTES = 120


SYMBOL_CURRENCY_MAP: dict[str, tuple[str, ...]] = {
    "XAUUSD": ("USD",),
    "XAGUSD": ("USD",),
    "EURUSD": ("EUR", "USD"),
    "GBPUSD": ("GBP", "USD"),
    "USDJPY": ("USD", "JPY"),
    "GBPJPY": ("GBP", "JPY"),
    "EURJPY": ("EUR", "JPY"),
    "AUDUSD": ("AUD", "USD"),
    "NZDUSD": ("NZD", "USD"),
    "USDCAD": ("USD", "CAD"),
    "USDCHF": ("USD", "CHF"),
    "US30": ("USD",),
    "US100": ("USD",),
    "US500": ("USD",),
    "NAS100": ("USD",),
    "SPX500": ("USD",),
}


@dataclass(frozen=True)
class NewsEvent:
    currency: str
    name: str
    impact: str
    event_time: datetime
    source: str = "configured"


@dataclass(frozen=True)
class NewsGuardEvaluation:
    state: str
    symbol: str
    relevant_currencies: tuple[str, ...]
    server_time: datetime | None
    event: NewsEvent | None
    window_start: datetime | None
    window_end: datetime | None
    warning_starts_at: datetime | None
    seconds_until_event: int | None
    seconds_until_window: int | None
    data_status: str
    message: str
    confirmation_required: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state,
            "symbol": self.symbol,
            "relevant_currencies": list(self.relevant_currencies),
            "server_time": self.server_time.isoformat() if self.server_time else None,
            "event_time": self.event.event_time.isoformat() if self.event else None,
            "event_currency": self.event.currency if self.event else None,
            "event_name": self.event.name if self.event else None,
            "event_impact": self.event.impact if self.event else None,
            "event_source": self.event.source if self.event else None,
            "window_start": self.window_start.isoformat() if self.window_start else None,
            "window_end": self.window_end.isoformat() if self.window_end else None,
            "warning_starts_at": self.warning_starts_at.isoformat() if self.warning_starts_at else None,
            "seconds_until_event": self.seconds_until_event,
            "seconds_until_window": self.seconds_until_window,
            "data_status": self.data_status,
            "message": self.message,
            "confirmation_required": self.confirmation_required,
        }


class ConfiguredNewsEventProvider:
    def list_events(self, *, currencies: tuple[str, ...], server_time: datetime) -> list[NewsEvent] | None:
        return None


class NewsGuardService:
    def __init__(self, provider: ConfiguredNewsEventProvider | None = None) -> None:
        self.provider = provider or ConfiguredNewsEventProvider()

    def evaluate(self, *, symbol: str, server_time: datetime | None) -> NewsGuardEvaluation:
        normalized_symbol = self._normalize_symbol(symbol)
        relevant_currencies = self.relevant_currencies(normalized_symbol)
        if not server_time:
            return NewsGuardEvaluation(
                state="warning",
                symbol=normalized_symbol,
                relevant_currencies=relevant_currencies,
                server_time=None,
                event=None,
                window_start=None,
                window_end=None,
                warning_starts_at=None,
                seconds_until_event=None,
                seconds_until_window=None,
                data_status="server_time_missing",
                message="News Guard cannot verify the broker server time. Treat this as high risk before placing a new order.",
                confirmation_required=False,
            )
        server_time = server_time if server_time.tzinfo else server_time.replace(tzinfo=timezone.utc)
        if not relevant_currencies:
            return self._safe(normalized_symbol, relevant_currencies, server_time, "No configured news currency mapping for this symbol.")

        events = self.provider.list_events(currencies=relevant_currencies, server_time=server_time)
        if events is None:
            return NewsGuardEvaluation(
                state="warning",
                symbol=normalized_symbol,
                relevant_currencies=relevant_currencies,
                server_time=server_time,
                event=None,
                window_start=None,
                window_end=None,
                warning_starts_at=None,
                seconds_until_event=None,
                seconds_until_window=None,
                data_status="unavailable",
                message="News Guard calendar data is unavailable. Do not assume this setup is news-safe.",
                confirmation_required=False,
            )
        if not events:
            return self._safe(normalized_symbol, relevant_currencies, server_time, "No relevant high-impact news event is currently configured.")

        nearest = min(events, key=lambda event: abs((self._aware(event.event_time) - server_time).total_seconds()))
        event_time = self._aware(nearest.event_time)
        data_age_seconds = abs((server_time - event_time).total_seconds())
        if data_age_seconds > NEWS_GUARD_STALE_AFTER_MINUTES * 60:
            return NewsGuardEvaluation(
                state="warning",
                symbol=normalized_symbol,
                relevant_currencies=relevant_currencies,
                server_time=server_time,
                event=nearest,
                window_start=None,
                window_end=None,
                warning_starts_at=None,
                seconds_until_event=int((event_time - server_time).total_seconds()),
                seconds_until_window=None,
                data_status="stale",
                message="News Guard data is stale or outside the active watch range. Do not assume the setup is news-safe.",
                confirmation_required=False,
            )

        window_start = event_time - timedelta(minutes=NEWS_GUARD_BLOCK_BEFORE_MINUTES)
        window_end = event_time + timedelta(minutes=NEWS_GUARD_BLOCK_AFTER_MINUTES)
        warning_starts_at = event_time - timedelta(minutes=NEWS_GUARD_WARNING_MINUTES)
        seconds_until_event = int((event_time - server_time).total_seconds())
        seconds_until_window = int((window_start - server_time).total_seconds())

        if window_start <= server_time <= window_end:
            return NewsGuardEvaluation(
                state="danger",
                symbol=normalized_symbol,
                relevant_currencies=relevant_currencies,
                server_time=server_time,
                event=nearest,
                window_start=window_start,
                window_end=window_end,
                warning_starts_at=warning_starts_at,
                seconds_until_event=seconds_until_event,
                seconds_until_window=0,
                data_status="fresh",
                message="This setup is inside the strict prohibited/high-risk news window. Explicit confirmation is required before execution.",
                confirmation_required=True,
            )
        if warning_starts_at <= server_time < window_start:
            return NewsGuardEvaluation(
                state="warning",
                symbol=normalized_symbol,
                relevant_currencies=relevant_currencies,
                server_time=server_time,
                event=nearest,
                window_start=window_start,
                window_end=window_end,
                warning_starts_at=warning_starts_at,
                seconds_until_event=seconds_until_event,
                seconds_until_window=max(seconds_until_window, 0),
                data_status="fresh",
                message="High-impact news is approaching for this symbol. Review account/program rules before placing a new order.",
                confirmation_required=False,
            )
        return self._safe(normalized_symbol, relevant_currencies, server_time, "No active News Guard warning for this symbol.")

    def relevant_currencies(self, symbol: str) -> tuple[str, ...]:
        normalized = self._normalize_symbol(symbol)
        if normalized in SYMBOL_CURRENCY_MAP:
            return SYMBOL_CURRENCY_MAP[normalized]
        parts = re.findall(r"[A-Z]{3}", normalized)
        return tuple(dict.fromkeys(part for part in parts if part in self._known_currencies()))

    def _safe(
        self,
        symbol: str,
        relevant_currencies: tuple[str, ...],
        server_time: datetime,
        message: str,
    ) -> NewsGuardEvaluation:
        return NewsGuardEvaluation(
            state="safe",
            symbol=symbol,
            relevant_currencies=relevant_currencies,
            server_time=server_time,
            event=None,
            window_start=None,
            window_end=None,
            warning_starts_at=None,
            seconds_until_event=None,
            seconds_until_window=None,
            data_status="fresh",
            message=message,
        )

    def _normalize_symbol(self, symbol: str) -> str:
        return re.sub(r"[^A-Z0-9]", "", symbol.upper())

    def _aware(self, value: datetime) -> datetime:
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    def _known_currencies(self) -> set[str]:
        return {"USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF"}
