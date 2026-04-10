from datetime import datetime, timezone
from urllib import parse, request
import logging

from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.models.trade_event import TradeEvent
from app.models.trade_setup import TradeSetup


logger = logging.getLogger(__name__)

NOTIFIABLE_EVENT_TYPES = {
    "execute_completed",
    "execute_failed",
    "tp1_hit",
    "be_move_completed",
    "be_move_failed",
}


class TelegramNotifier:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    @property
    def enabled(self) -> bool:
        return bool(self.settings.telegram_bot_token and self.settings.telegram_chat_id)

    def send_message(self, text: str) -> None:
        if not self.enabled:
            return

        token = self.settings.telegram_bot_token
        chat_id = self.settings.telegram_chat_id
        if not token or not chat_id:
            return

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = parse.urlencode(
            {
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": "true",
            }
        ).encode()
        telegram_request = request.Request(url, data=payload, method="POST")
        with request.urlopen(telegram_request, timeout=self.settings.telegram_timeout_seconds) as response:
            response.read()


def notify_pending_trade_events(
    db: Session,
    notifier: TelegramNotifier | None = None,
    limit: int = 25,
) -> int:
    notifier = notifier or TelegramNotifier()
    if not notifier.enabled:
        return 0

    events = (
        db.query(TradeEvent)
        .filter(
            TradeEvent.telegram_notified_at.is_(None),
            TradeEvent.event_type.in_(NOTIFIABLE_EVENT_TYPES),
        )
        .order_by(TradeEvent.created_at.asc(), TradeEvent.id.asc())
        .limit(limit)
        .all()
    )

    sent_count = 0
    for event in events:
        try:
            setup = db.query(TradeSetup).filter(TradeSetup.id == event.setup_id).first()
            notifier.send_message(_format_trade_event_message(event, setup))
            event.telegram_notified_at = datetime.now(timezone.utc)
            db.add(event)
            db.commit()
            sent_count += 1
        except Exception:
            db.rollback()
            logger.exception("Telegram notification failed for trade event %s", event.id)
    return sent_count


def _format_trade_event_message(event: TradeEvent, setup: TradeSetup | None) -> str:
    prefix = {
        "execute_completed": "Execution completed",
        "execute_failed": "Execution failed",
        "tp1_hit": "TP1 hit",
        "be_move_completed": "Breakeven SL moved",
        "be_move_failed": "Breakeven SL move failed",
    }.get(event.event_type, event.event_type)

    if setup is None:
        return f"{prefix}\nSetup #{event.setup_id}\n{event.message or ''}".strip()

    lines = [
        prefix,
        f"Setup #{setup.id}: {setup.symbol} {setup.side.upper()}",
        f"Status: {setup.status}",
    ]
    if setup.order1_ticket or setup.order2_ticket:
        lines.append(f"Tickets: {setup.order1_ticket or '-'} / {setup.order2_ticket or '-'}")
    if event.message:
        lines.append(event.message)
    return "\n".join(lines)
