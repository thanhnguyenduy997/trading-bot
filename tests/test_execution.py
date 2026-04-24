from app.models.trading_account import TradingAccount
from app.execution.base import AdapterError
from app.schemas.trading_account import TradingAccountCreate
from app.schemas.user import UserCreate
from app.services.execution import TradingAccountExecutionService
from app.services.users import create_user


def _create_account(db_session, user, account_number: str = "123456"):
    response = TradingAccountCreate(
        broker_name="Demo Broker",
        account_number=account_number,
        server_name="demo-server",
        password="secret-pass",
        terminal_path=None,
    )
    from app.services.trading_accounts import create_trading_account

    return create_trading_account(db_session, user.id, response)


def _login_web_session(client, email: str, password: str = "password123") -> None:
    response = client.post(
        "/api/auth/token",
        data={"username": email, "password": password},
    )
    assert response.status_code == 200


class FakeExecutionAdapter:
    def __init__(self, account):
        self.account = account

    def connect(self) -> None:
        return None

    def get_account_info(self) -> dict[str, str | int | float | None]:
        return {
            "login": int(self.account.account_number),
            "server": self.account.server_name,
            "balance": 10000.0,
            "equity": 10050.0,
        }

    def get_quote(self, symbol: str) -> dict[str, str | int | float | None]:
        return {"symbol": symbol, "bid": 2320.0, "ask": 2320.2, "time": 1710000000}

    def get_symbol_info(self, symbol: str) -> dict[str, str | int | float | None]:
        return {
            "symbol": symbol,
            "point": 0.01,
            "digits": 2,
            "trade_contract_size": 100.0,
            "volume_min": 0.01,
            "volume_max": 100.0,
            "volume_step": 0.01,
        }

    def list_symbols(self) -> list[dict[str, str | int | float | None]]:
        return [
            {"symbol": "XAUUSD", "visible": True},
            {"symbol": "EURUSD", "visible": True},
        ]

    def close(self) -> None:
        return None


def test_unauthorized_access_to_another_users_account(client, db_session, created_user, auth_headers, monkeypatch):
    monkeypatch.setattr(
        "app.services.execution.default_adapter_factory",
        lambda account: FakeExecutionAdapter(account),
    )
    other_user = create_user(
        db_session,
        UserCreate(email="mt5-other@example.com", password="password123", full_name="Other User"),
    )
    other_account = _create_account(db_session, other_user, "999999")

    response = client.post(f"/api/trading-accounts/{other_account.id}/test-connection", headers=auth_headers)

    assert response.status_code == 404
    assert response.json()["detail"] == "Trading account not found"


def test_connection_test_result_persistence(client, db_session, created_user, auth_headers, monkeypatch):
    monkeypatch.setattr(
        "app.services.execution.default_adapter_factory",
        lambda account: FakeExecutionAdapter(account),
    )
    account = _create_account(db_session, created_user, "123456")

    response = client.post(f"/api/trading-accounts/{account.id}/test-connection", headers=auth_headers)

    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["connection_status"] == "connected"
    assert data["account_info"]["balance"] == 10000.0

    stored = db_session.query(TradingAccount).filter(TradingAccount.id == account.id).first()
    assert stored is not None
    assert stored.connection_status == "connected"
    assert stored.last_heartbeat_at is not None
    assert stored.last_error is None


def test_quote_retrieval_via_adapter_abstraction(client, db_session, created_user, auth_headers, monkeypatch, sync_account_symbols):
    monkeypatch.setattr(
        "app.services.execution.default_adapter_factory",
        lambda account: FakeExecutionAdapter(account),
    )
    account = _create_account(db_session, created_user, "123457")
    sync_account_symbols(account, "XAUUSD")

    response = client.get(
        f"/api/trading-accounts/{account.id}/quote",
        headers=auth_headers,
        params={"symbol": "XAUUSD"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["symbol"] == "XAUUSD"
    assert data["bid"] == 2320.0
    assert data["ask"] == 2320.2
    assert data["connection_status"] == "connected"
    assert data["symbol_info"]["digits"] == 2


class FailingConnectAdapter(FakeExecutionAdapter):
    def connect(self) -> None:
        raise AdapterError(code="mt5_login_failed", message="MT5 login failed.")


class MissingSymbolAdapter(FakeExecutionAdapter):
    def get_quote(self, symbol: str) -> dict[str, str | int | float | None]:
        raise AdapterError(code="mt5_symbol_not_found", message=f"Symbol {symbol} is unavailable in MT5.")


class SessionMismatchAdapter(FakeExecutionAdapter):
    def connect(self) -> None:
        raise AdapterError(
            code="mt5_session_mismatch",
            message=(
                f"MT5 terminal is logged into account 999999, but this trading account expects {self.account.account_number}. "
                "Log into the correct MT5 account in the terminal first."
            ),
            details={"current_login": "999999", "expected_account": self.account.account_number},
        )


def test_connection_failure_marks_account_disconnected(db_session, created_user):
    account = _create_account(db_session, created_user, "123458")
    service = TradingAccountExecutionService(db_session, adapter_factory=lambda current: FailingConnectAdapter(current))

    result = service.test_connection(account.id, created_user.id)

    assert result.success is False
    assert result.connection_status == "disconnected"
    stored = db_session.query(TradingAccount).filter(TradingAccount.id == account.id).first()
    assert stored is not None
    assert stored.connection_status == "disconnected"
    assert stored.last_error == "mt5_login_failed: MT5 login failed."


def test_symbol_failure_preserves_connected_status_with_error(db_session, created_user):
    account = _create_account(db_session, created_user, "123459")
    service = TradingAccountExecutionService(db_session, adapter_factory=lambda current: MissingSymbolAdapter(current))

    try:
        service.fetch_quote(account.id, created_user.id, "BTCUSD")
    except AdapterError as exc:
        assert exc.code == "mt5_symbol_not_found"
    else:
        raise AssertionError("Expected symbol lookup to fail")

    stored = db_session.query(TradingAccount).filter(TradingAccount.id == account.id).first()
    assert stored is not None
    assert stored.connection_status == "connected"
    assert stored.last_error == "mt5_symbol_not_found: Symbol BTCUSD is unavailable in MT5."


def test_connection_mismatch_marks_account_as_mismatch(db_session, created_user):
    account = _create_account(db_session, created_user, "123470")
    service = TradingAccountExecutionService(db_session, adapter_factory=lambda current: SessionMismatchAdapter(current))

    result = service.test_connection(account.id, created_user.id)

    assert result.success is False
    assert result.connection_status == "connected"
    assert result.mt5_session_status == "mismatch"
    assert result.current_mt5_login == "999999"
    stored = db_session.query(TradingAccount).filter(TradingAccount.id == account.id).first()
    assert stored is not None
    assert stored.mt5_session_status == "mismatch"
    assert stored.current_mt5_login == "999999"


def test_web_session_connection_route_uses_cookie_auth(client, db_session, created_user, monkeypatch):
    monkeypatch.setattr(
        "app.services.execution.default_adapter_factory",
        lambda account: FakeExecutionAdapter(account),
    )
    account = _create_account(db_session, created_user, "123460")
    _login_web_session(client, created_user.email)

    response = client.post(f"/trading-accounts/id/{account.id}/test-connection")

    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["connection_status"] == "connected"


def test_web_session_quote_route_rejects_other_users_account(client, db_session, created_user, monkeypatch):
    monkeypatch.setattr(
        "app.services.execution.default_adapter_factory",
        lambda account: FakeExecutionAdapter(account),
    )
    other_user = create_user(
        db_session,
        UserCreate(email="web-other@example.com", password="password123", full_name="Other User"),
    )
    other_account = _create_account(db_session, other_user, "123461")
    _login_web_session(client, created_user.email)

    response = client.get(f"/trading-accounts/id/{other_account.id}/quote", params={"symbol": "XAUUSD"})

    assert response.status_code == 404
    assert response.json()["detail"] == "Trading account not found"


def test_trading_account_detail_page_uses_dynamic_symbol_list(client, db_session, created_user, monkeypatch, sync_account_symbols):
    monkeypatch.setattr(
        "app.services.execution.default_adapter_factory",
        lambda account: FakeExecutionAdapter(account),
    )
    account = _create_account(db_session, created_user, "123464")
    sync_account_symbols(account, "XAUUSD")
    _login_web_session(client, created_user.email)

    response = client.get(f"/trading-accounts/id/{account.id}")

    assert response.status_code == 200
    assert "XAUUSD" in response.text
    assert "BTCUSD" not in response.text


def test_quote_retrieval_rejects_symbol_not_in_synced_list(
    client,
    db_session,
    created_user,
    auth_headers,
    monkeypatch,
):
    monkeypatch.setattr(
        "app.services.execution.default_adapter_factory",
        lambda account: FakeExecutionAdapter(account),
    )
    account = _create_account(db_session, created_user, "123462")

    response = client.get(
        f"/api/trading-accounts/{account.id}/quote",
        headers=auth_headers,
        params={"symbol": "BTCUSD"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Symbol is not synced for this trading account. Add it in MT5 Market Watch first, then click Refresh Symbols from MT5."


def test_quote_retrieval_rejects_symbol_when_live_validation_fails(
    client,
    db_session,
    created_user,
    auth_headers,
    monkeypatch,
    sync_account_symbols,
):
    monkeypatch.setattr(
        "app.services.execution.default_adapter_factory",
        lambda account: MissingSymbolAdapter(account),
    )
    account = _create_account(db_session, created_user, "123463")
    sync_account_symbols(account, "BTCUSD")

    response = client.get(
        f"/api/trading-accounts/{account.id}/quote",
        headers=auth_headers,
        params={"symbol": "BTCUSD"},
    )

    assert response.status_code == 503
    assert response.json()["detail"]["message"] == "Symbol BTCUSD is unavailable in MT5."


def test_preview_symbol_list_is_scoped_to_selected_account(client, db_session, created_user, monkeypatch, sync_account_symbols):
    monkeypatch.setattr(
        "app.services.execution.default_adapter_factory",
        lambda account: FakeExecutionAdapter(account),
    )
    account1 = _create_account(db_session, created_user, "123467")
    account2 = _create_account(db_session, created_user, "123468")
    sync_account_symbols(account1, "XAUUSD")
    sync_account_symbols(account2, "EURUSD")
    _login_web_session(client, created_user.email)

    response = client.get(f"/trading-accounts/id/{account2.id}/symbols")

    assert response.status_code == 200
    data = response.json()
    assert [item["symbol_name"] for item in data["symbols"]] == ["EURUSD"]


def test_account_symbol_sync_stores_market_watch_symbols(client, db_session, created_user, monkeypatch):
    monkeypatch.setattr(
        "app.services.account_symbols.default_adapter_factory",
        lambda account: FakeExecutionAdapter(account),
    )
    account = _create_account(db_session, created_user, "123465")
    _login_web_session(client, created_user.email)

    response = client.post(f"/trading-accounts/id/{account.id}/refresh-symbols")

    assert response.status_code == 200
    data = response.json()
    assert [item["symbol_name"] for item in data["symbols"]] == ["EURUSD", "XAUUSD"]
    assert data["symbols_sync_status"] == "synced"


def test_account_symbol_sync_is_blocked_when_mt5_session_mismatches(client, db_session, created_user, monkeypatch):
    monkeypatch.setattr(
        "app.services.account_symbols.default_adapter_factory",
        lambda account: SessionMismatchAdapter(account),
    )
    account = _create_account(db_session, created_user, "123471")
    _login_web_session(client, created_user.email)

    response = client.post(f"/trading-accounts/id/{account.id}/refresh-symbols")

    assert response.status_code == 503
    data = response.json()
    assert data["detail"]["message"].startswith("MT5 terminal is logged into account 999999")
    assert data["detail"]["mt5_session_status"] == "mismatch"
    assert data["detail"]["current_mt5_login"] == "999999"


def test_empty_synced_symbol_list_shows_clear_message(client, db_session, created_user, monkeypatch):
    monkeypatch.setattr(
        "app.services.execution.default_adapter_factory",
        lambda account: FakeExecutionAdapter(account),
    )
    account = _create_account(db_session, created_user, "123466")
    _login_web_session(client, created_user.email)

    response = client.get(f"/trading-accounts/id/{account.id}")

    assert response.status_code == 200
    assert "No symbols are synced for this account yet. Add symbols in MT5 Market Watch first, then click Refresh Symbols from MT5." in response.text


def test_trading_account_detail_page_shows_session_mismatch_warning(client, db_session, created_user):
    account = _create_account(db_session, created_user, "123472")
    account.mt5_session_status = "mismatch"
    account.current_mt5_login = "999999"
    account.connection_status = "connected"
    account.last_error = "mt5_session_mismatch: MT5 terminal is logged into account 999999."
    db_session.add(account)
    db_session.commit()
    _login_web_session(client, created_user.email)

    response = client.get(f"/trading-accounts/id/{account.id}")

    assert response.status_code == 200
    assert "Session status" in response.text
    assert "mismatch" in response.text
    assert "Current MT5 login" in response.text
    assert "999999" in response.text
    assert "Live MT5 actions are blocked." in response.text


def test_trading_account_detail_page_shows_auto_refresh_and_history_sync_status(client, db_session, created_user):
    account = _create_account(db_session, created_user, "123473")
    account.symbols_last_synced_at = None
    db_session.add(account)
    db_session.commit()
    _login_web_session(client, created_user.email)

    response = client.get(f"/trading-accounts/id/{account.id}")

    assert response.status_code == 200
    assert 'data-auto-refresh-interval="60"' in response.text
    assert "MT5 history last synced" in response.text
    assert "Every 60s" in response.text
