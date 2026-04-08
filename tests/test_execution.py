from app.models.trading_account import TradingAccount
from app.schemas.trading_account import TradingAccountCreate
from app.schemas.user import UserCreate
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
        return {"symbol": symbol, "digits": 2}

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


def test_quote_retrieval_via_adapter_abstraction(client, db_session, created_user, auth_headers, monkeypatch):
    monkeypatch.setattr(
        "app.services.execution.default_adapter_factory",
        lambda account: FakeExecutionAdapter(account),
    )
    account = _create_account(db_session, created_user, "123457")

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
