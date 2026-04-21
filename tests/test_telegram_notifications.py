from types import SimpleNamespace

from app.core.crypto import encrypt_value
from app.models.trade_event import TradeEvent
from app.schemas.trade_setup import TradeSetupCreate
from app.schemas.trading_account import TradingAccountCreate
from app.services.notification_templates import format_trade_event_notification
from app.services.notifications import TelegramNotifier, notify_pending_trade_events
from app.services.trade_setups import create_trade_setup
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


def test_trade_event_notification_is_formatted_in_vietnamese_for_success():
    setup = SimpleNamespace(
        id=12,
        symbol="XAUUSD",
        side="buy",
        order1_ticket=1001,
        order2_ticket=1002,
        trading_account=SimpleNamespace(account_number="ACC-100"),
    )
    event = SimpleNamespace(
        event_type="execute_completed",
        setup_id=12,
        message="Trade setup executed. Tickets: 1001, 1002.",
    )

    message = format_trade_event_notification(event, setup)

    assert "✅ Thực thi thành công" in message
    assert "Tài khoản: ACC-100" in message
    assert "Setup: #12" in message
    assert "Mã lệnh: XAUUSD" in message
    assert "Chiều: BUY" in message
    assert "Ticket: 1001 / 1002" in message


def test_trade_event_notification_is_formatted_in_vietnamese_for_be_and_drift_reject():
    setup = SimpleNamespace(
        id=44,
        symbol="EURUSD",
        side="sell",
        order1_ticket=None,
        order2_ticket=8802,
        trading_account=SimpleNamespace(account_number="ACC-200"),
    )
    be_event = SimpleNamespace(
        event_type="be_move_completed",
        setup_id=44,
        message="Order 2 stop loss moved to breakeven at 1.085.",
    )
    drift_event = SimpleNamespace(
        event_type="preview_drift_reject",
        setup_id=44,
        message="Market conditions have changed beyond the allowed threshold. Please preview again before executing. Detected drift: 10.00% (allowed: 5.00%).",
    )

    be_message = format_trade_event_notification(be_event, setup)
    drift_message = format_trade_event_notification(drift_event, setup)

    assert "🔒 Đã dời SL về BE" in be_message
    assert "Kết quả: Lệnh 2 đã dời SL về BE" in be_message
    assert "⚠️ Từ chối do lệch preview" in drift_message
    assert "Kết quả: Bắt buộc preview lại trước khi Execute" in drift_message
    assert "Lý do:" in drift_message


def test_notify_pending_trade_events_sends_vietnamese_messages(db_session):
    account = create_trading_account(
        db_session,
        1,
        TradingAccountCreate(
            broker_name="Demo Broker",
            account_number="ACC-777",
            server_name="demo-server",
            password="secret-pass",
            telegram_enabled=True,
        ),
    )
    setup = create_trade_setup(
        db_session,
        1,
        TradeSetupCreate(
            trading_account_id=account.id,
            symbol="XAUUSD",
            side="buy",
            sl_price=2319.2,
            risk_mode="fixed_money",
            risk_value=100,
            rr_order2=2,
            estimated_entry=2320.2,
            r_value=1.0,
            tp1_price=2321.2,
            tp2_price=2322.2,
            total_risk_money=100.0,
            risk_per_order=50.0,
            order1_volume=0.5,
            order2_volume=0.5,
            status="failed",
        ),
    )
    setup.status = "failed"
    db_session.add(setup)
    db_session.commit()
    event = TradeEvent(
        user_id=1,
        setup_id=setup.id,
        event_type="execute_failed",
        message="MT5 execution unavailable on this machine.",
    )
    db_session.add(event)
    db_session.commit()

    sent_messages = []

    class FakeNotifier(TelegramNotifier):
        def __init__(self):
            super().__init__(_settings())

        def send_message(self, text: str, account=None) -> bool:
            sent_messages.append(text)
            return True

    count = notify_pending_trade_events(db_session, notifier=FakeNotifier())

    assert count == 1
    assert len(sent_messages) == 1
    assert "❌ Thực thi thất bại" in sent_messages[0]
    assert "Tài khoản: ACC-777" in sent_messages[0]
    assert "Mã lệnh: XAUUSD" in sent_messages[0]
