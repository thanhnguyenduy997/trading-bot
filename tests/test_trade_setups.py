from app.schemas.trade_setup import TradeSetupCreate
from app.schemas.trading_account import TradingAccountCreate
from app.schemas.user import UserCreate
from app.services.trade_setups import create_trade_setup
from app.services.trading_accounts import create_trading_account
from app.services.users import create_user


def _create_account(db_session, user, account_number: str = "ACC-100"):
    return create_trading_account(
        db_session,
        user.id,
        TradingAccountCreate(
            broker_name="Demo Broker",
            account_number=account_number,
            server_name="demo-server",
            password="secret-pass",
        ),
    )


def _build_setup_payload(account_id: int, symbol: str = "XAUUSD") -> dict:
    return {
        "trading_account_id": account_id,
        "symbol": symbol,
        "side": "buy",
        "sl_price": 2319.2,
        "risk_mode": "fixed_money",
        "risk_value": 100,
        "rr_order2": 2,
        "estimated_entry": 2320.2,
        "r_value": 1.0,
        "tp1_price": 2321.2,
        "tp2_price": 2322.2,
        "total_risk_money": 100.0,
        "risk_per_order": 50.0,
        "order1_volume": 0.5,
        "order2_volume": 0.5,
        "status": "draft",
    }


def _login_headers(client, email: str, password: str) -> dict[str, str]:
    response = client.post("/api/auth/token", data={"username": email, "password": password})
    token = response.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_save_setup_successfully(client, db_session, created_user, auth_headers):
    account = _create_account(db_session, created_user)

    response = client.post(
        "/api/trade-setups",
        headers=auth_headers,
        json=_build_setup_payload(account.id),
    )

    assert response.status_code == 201
    data = response.json()
    assert data["trading_account_id"] == account.id
    assert data["symbol"] == "XAUUSD"
    assert data["status"] == "draft"


def test_list_only_current_users_setups(client, db_session, created_user, auth_headers):
    current_user_account = _create_account(db_session, created_user, "ACC-101")
    create_trade_setup(db_session, created_user.id, TradeSetupCreate(**_build_setup_payload(current_user_account.id)))

    other_user = create_user(
        db_session,
        UserCreate(email="other@example.com", password="password123", full_name="Other User"),
    )
    other_account = _create_account(db_session, other_user, "ACC-202")
    create_trade_setup(db_session, other_user.id, TradeSetupCreate(**_build_setup_payload(other_account.id, "EURUSD")))

    response = client.get("/api/trade-setups", headers=auth_headers)

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["user_id"] == created_user.id
    assert data[0]["trading_account_id"] == current_user_account.id


def test_reject_access_to_another_users_setup_detail(client, db_session, created_user, auth_headers):
    other_user = create_user(
        db_session,
        UserCreate(email="other2@example.com", password="password123", full_name="Other User"),
    )
    other_account = _create_account(db_session, other_user, "ACC-303")
    other_setup = create_trade_setup(db_session, other_user.id, TradeSetupCreate(**_build_setup_payload(other_account.id)))

    response = client.get(f"/api/trade-setups/{other_setup.id}", headers=auth_headers)

    assert response.status_code == 404
    assert response.json()["detail"] == "Trade setup not found"


def test_saved_setup_fields_match_preview_values(client, db_session, created_user):
    account = _create_account(db_session, created_user, "ACC-404")
    headers = _login_headers(client, created_user.email, "password123")

    preview_response = client.post(
        "/api/trade-setups/preview",
        headers=headers,
        json={
            "trading_account_id": account.id,
            "symbol": "XAUUSD",
            "side": "buy",
            "sl_price": 2319.2,
            "risk_mode": "fixed_money",
            "risk_value": 100,
            "rr_order2": 2,
        },
    )
    preview_data = preview_response.json()

    save_payload = {
        "trading_account_id": account.id,
        "symbol": "XAUUSD",
        "side": "buy",
        "sl_price": 2319.2,
        "risk_mode": "fixed_money",
        "risk_value": 100,
        "rr_order2": 2,
        "estimated_entry": preview_data["estimated_entry"],
        "r_value": preview_data["r_value"],
        "tp1_price": preview_data["tp1_price"],
        "tp2_price": preview_data["tp2_price"],
        "total_risk_money": preview_data["total_risk_money"],
        "risk_per_order": preview_data["risk_per_order"],
        "order1_volume": preview_data["order1_volume"],
        "order2_volume": preview_data["order2_volume"],
        "status": "draft",
    }

    save_response = client.post("/api/trade-setups", headers=headers, json=save_payload)

    assert preview_response.status_code == 200
    assert save_response.status_code == 201
    saved = save_response.json()
    assert saved["estimated_entry"] == preview_data["estimated_entry"]
    assert saved["r_value"] == preview_data["r_value"]
    assert saved["tp1_price"] == preview_data["tp1_price"]
    assert saved["tp2_price"] == preview_data["tp2_price"]
    assert saved["risk_per_order"] == preview_data["risk_per_order"]
    assert saved["order1_volume"] == preview_data["order1_volume"]
    assert saved["order2_volume"] == preview_data["order2_volume"]
