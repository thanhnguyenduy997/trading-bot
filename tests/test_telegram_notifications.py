from types import SimpleNamespace

from app.core.crypto import encrypt_value
from app.schemas.trading_account import TradingAccountCreate
from app.services.notifications import TelegramNotifier
from app.services.trading_accounts import create_trading_account


def _settings(token: str | None = "global-token", chat_id: str | None = "global-chat"):
    return SimpleNamespace(
        telegram_bot_token=token,
        telegram_chat_id=chat_id,
        telegram_timeout_seconds=5,
    )


def _account(**overrides):
    values = {
        "telegram_enabled": True,
        "telegram_chat_id": None,
        "telegram_bot_token_encrypted": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_account_specific_chat_uses_global_bot_token_fallback():
    notifier = TelegramNotifier(_settings(token="global-token", chat_id="global-chat"))
    account = _account(telegram_chat_id="account-chat")

    assert notifier.resolve_destination(account) == ("global-token", "account-chat")


def test_account_specific_bot_token_and_chat_are_used():
    notifier = TelegramNotifier(_settings(token="global-token", chat_id="global-chat"))
    account = _account(
        telegram_chat_id="account-chat",
        telegram_bot_token_encrypted=encrypt_value("account-token"),
    )

    assert notifier.resolve_destination(account) == ("account-token", "account-chat")


def test_account_telegram_disabled_skips_notification():
    notifier = TelegramNotifier(_settings(token="global-token", chat_id="global-chat"))
    account = _account(telegram_enabled=False, telegram_chat_id="account-chat")

    assert notifier.resolve_destination(account) is None


def test_missing_token_or_chat_fails_safely():
    notifier = TelegramNotifier(_settings(token=None, chat_id=None))
    account = _account()

    assert notifier.resolve_destination(account) is None


def test_test_notification_endpoint_works(client, db_session, created_user, auth_headers, monkeypatch):
    account = create_trading_account(
        db_session,
        created_user.id,
        TradingAccountCreate(
            broker_name="Demo Broker",
            account_number="555001",
            server_name="demo-server",
            password="secret-pass",
            telegram_enabled=True,
            telegram_chat_id="account-chat",
        ),
    )

    monkeypatch.setattr(
        "app.api.routes.trading_accounts.send_trading_account_test_notification",
        lambda account: (True, "Telegram test notification sent."),
    )

    response = client.post(f"/api/trading-accounts/{account.id}/test-telegram", headers=auth_headers)

    assert response.status_code == 200
    assert response.json() == {"success": True, "message": "Telegram test notification sent."}
