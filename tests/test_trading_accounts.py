from app.core.crypto import decrypt_value
from app.models.trading_account import TradingAccount
from app.schemas.trading_account import TradingAccountCreate
from app.services.trading_accounts import DUPLICATE_TRADING_ACCOUNT_MESSAGE, create_trading_account


def _login(client, email: str, password: str = "password123"):
    return client.post("/api/auth/login", data={"email": email, "password": password}, follow_redirects=False)


def _create_account(db_session, user, account_number: str = "123456"):
    return create_trading_account(
        db_session,
        user.id,
        TradingAccountCreate(
            broker_name="Demo Broker",
            account_number=account_number,
            server_name="demo-server",
            platform="mt5",
            terminal_path="C:/MT5/terminal64.exe",
            password="secret-pass",
            telegram_enabled=True,
            telegram_chat_id="12345",
            telegram_bot_token="token-1",
            max_total_setup_volume=0.5,
        ),
    )


def test_trading_account_api_crud(client, db_session, auth_headers):
    create_response = client.post(
        "/api/trading-accounts/",
        headers=auth_headers,
        json={
            "broker_name": "Demo Broker",
            "account_number": "123456",
            "server_name": "demo-server",
            "password": "secret-pass",
        },
    )
    assert create_response.status_code == 201
    account_id = create_response.json()["id"]

    stored = db_session.query(TradingAccount).filter(TradingAccount.id == account_id).first()
    assert stored is not None
    assert stored.password_encrypted != "secret-pass"

    list_response = client.get("/api/trading-accounts/", headers=auth_headers)
    assert list_response.status_code == 200
    assert len(list_response.json()) == 1

    update_response = client.put(
        f"/api/trading-accounts/{account_id}",
        headers=auth_headers,
        json={"server_name": "live-server", "password": "new-secret"},
    )
    assert update_response.status_code == 200
    assert update_response.json()["server_name"] == "live-server"

    delete_response = client.delete(f"/api/trading-accounts/{account_id}", headers=auth_headers)
    assert delete_response.status_code == 204

    not_found_response = client.get(f"/api/trading-accounts/{account_id}", headers=auth_headers)
    assert not_found_response.status_code == 404


def test_editing_existing_trading_account_page_updates_fields(client, db_session, created_user):
    account = _create_account(db_session, created_user)
    _login(client, created_user.email)

    response = client.post(
        f"/trading-accounts/id/{account.id}/edit",
        data={
            "broker_name": "Updated Broker",
            "account_number": "654321",
            "server_name": "live-server",
            "platform": "mt4",
            "terminal_path": "D:/Apps/terminal.exe",
            "password": "",
            "telegram_enabled": "true",
            "telegram_chat_id": "99999",
            "telegram_bot_token": "token-2",
            "max_total_setup_volume": "1.25",
        },
    )

    assert response.status_code == 200
    assert "Trading account updated." in response.text

    db_session.refresh(account)
    assert account.broker_name == "Updated Broker"
    assert account.account_number == "654321"
    assert account.server_name == "live-server"
    assert account.platform == "mt4"
    assert account.terminal_path == "D:/Apps/terminal.exe"
    assert account.telegram_enabled is True
    assert account.telegram_chat_id == "99999"
    assert decrypt_value(account.telegram_bot_token_encrypted) == "token-2"
    assert float(account.max_total_setup_volume) == 1.25


def test_duplicate_create_attempt_returns_friendly_validation_message(client, db_session, created_user):
    _create_account(db_session, created_user, account_number="DUP-100")
    _login(client, created_user.email)

    response = client.post(
        "/trading-accounts/create",
        data={
            "broker_name": "Demo Broker",
            "account_number": "DUP-100",
            "server_name": "demo-server",
            "platform": "mt5",
            "terminal_path": "",
            "password": "secret-pass",
            "telegram_enabled": "true",
            "telegram_chat_id": "",
            "telegram_bot_token": "",
            "max_total_setup_volume": "",
        },
    )

    assert response.status_code == 409
    assert DUPLICATE_TRADING_ACCOUNT_MESSAGE in response.text


def test_blank_password_on_edit_keeps_existing_encrypted_value(client, db_session, created_user):
    account = _create_account(db_session, created_user)
    original_password_encrypted = account.password_encrypted
    _login(client, created_user.email)

    response = client.post(
        f"/trading-accounts/id/{account.id}/edit",
        data={
            "broker_name": account.broker_name,
            "account_number": account.account_number,
            "server_name": "updated-server",
            "platform": account.platform,
            "terminal_path": account.terminal_path,
            "password": "",
            "telegram_enabled": "true",
            "telegram_chat_id": account.telegram_chat_id,
            "telegram_bot_token": "",
            "max_total_setup_volume": "0.5",
        },
    )

    assert response.status_code == 200
    db_session.refresh(account)
    assert account.server_name == "updated-server"
    assert account.password_encrypted == original_password_encrypted
    assert decrypt_value(account.password_encrypted) == "secret-pass"


def test_changed_password_on_edit_updates_encrypted_value_correctly(client, db_session, created_user):
    account = _create_account(db_session, created_user)
    original_password_encrypted = account.password_encrypted
    _login(client, created_user.email)

    response = client.post(
        f"/trading-accounts/id/{account.id}/edit",
        data={
            "broker_name": account.broker_name,
            "account_number": account.account_number,
            "server_name": account.server_name,
            "platform": account.platform,
            "terminal_path": account.terminal_path,
            "password": "new-secret-pass",
            "telegram_enabled": "true",
            "telegram_chat_id": account.telegram_chat_id,
            "telegram_bot_token": "",
            "max_total_setup_volume": "0.5",
        },
    )

    assert response.status_code == 200
    db_session.refresh(account)
    assert account.password_encrypted != original_password_encrypted
    assert decrypt_value(account.password_encrypted) == "new-secret-pass"
