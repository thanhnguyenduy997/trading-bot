from datetime import datetime, timezone
from urllib import parse, request
import logging

from sqlalchemy.orm import Session

from app.core.crypto import decrypt_value
from app.core.config import Settings, get_settings
from app.models.trade_event import TradeEvent
from app.models.trade_setup import TradeSetup
from app.models.trading_account import TradingAccount
from app.services.notification_templates import format_trade_event_notification


logger = logging.getLogger(__name__)

NOTIFIABLE_EVENT_TYPES = {
    "execute_completed",
    "execute_failed",
    "tp1_hit",
    "be_move_completed",
    "be_move_failed",
    "preview_drift_reject",
    "risk_limit_execute_rejected",
    "daily_lock_execute_rejected",
}


class TelegramNotifier:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    @property
    def enabled(self) -> bool:
        return bool(self.settings.telegram_bot_token and self.settings.telegram_chat_id)

    def send_message(self, text: str, account: TradingAccount | None = None) -> bool:
        destination = self.resolve_destination(account)
        if destination is None:
            return False
        token, chat_id = destination

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
        return True

    def resolve_destination(self, account: TradingAccount | None = None) -> tuple[str, str] | None:
        if account is not None and not account.telegram_enabled:
            return None

        token = self.settings.telegram_bot_token
        chat_id = self.settings.telegram_chat_id
        if account is not None:
            if account.telegram_chat_id:
                chat_id = account.telegram_chat_id
            if account.telegram_bot_token_encrypted:
                token = decrypt_value(account.telegram_bot_token_encrypted)

        if not token or not chat_id:
            return None
        return token, chat_id


def send_trading_account_test_notification(
    account: TradingAccount,
    notifier: TelegramNotifier | None = None,
) -> tuple[bool, str]:
    notifier = notifier or TelegramNotifier()
    if notifier.resolve_destination(account) is None:
        return False, "No usable Telegram bot token/chat id is configured for this account."
    try:
        sent = notifier.send_message(
            f"Thông báo test Telegram\nTài khoản: {account.account_number} ({account.broker_name})",
            account=account,
        )
    except Exception:
        logger.exception("Telegram test notification failed for account %s", account.id)
        return False, "Telegram test notification failed."
    if not sent:
        return False, "Telegram notifications are disabled for this account."
    return True, "Telegram test notification sent."


def notify_pending_trade_events(
    db: Session,
    notifier: TelegramNotifier | None = None,
    limit: int = 25,
) -> int:
    notifier = notifier or TelegramNotifier()

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
            account = setup.trading_account if setup is not None else None
            sent = notifier.send_message(_format_trade_event_message(event, setup), account=account)
            if sent:
                event.telegram_notified_at = datetime.now(timezone.utc)
                db.add(event)
                db.commit()
                sent_count += 1
        except Exception:
            db.rollback()
            logger.exception("Telegram notification failed for trade event %s", event.id)
    return sent_count


def _format_trade_event_message(event: TradeEvent, setup: TradeSetup | None) -> str:
    return format_trade_event_notification(event, setup)
