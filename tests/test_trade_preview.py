from app.schemas.trading_account import TradingAccountCreate
from app.services.trading_accounts import create_trading_account


def _create_account(db_session, created_user):
    return create_trading_account(
        db_session,
        created_user.id,
        TradingAccountCreate(
            broker_name="Demo Broker",
            account_number="ACC-100",
            server_name="demo-server",
            password="secret-pass",
        ),
    )


def test_valid_buy_preview(client, db_session, created_user, auth_headers):
    account = _create_account(db_session, created_user)

    response = client.post(
        "/api/trade-setups/preview",
        headers=auth_headers,
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

    assert response.status_code == 200
    data = response.json()
    assert data["symbol"] == "XAUUSD"
    assert data["side"] == "buy"
    assert data["estimated_entry"] == 2320.2
    assert data["r_value"] == 1.0
    assert data["tp1_price"] == 2321.2
    assert data["tp2_price"] == 2322.2
    assert data["risk_per_order"] == 50.0
    assert data["order1_volume"] == 0.5
    assert data["order2_volume"] == 0.5
    assert data["validation_status"] == "valid"


def test_valid_sell_preview(client, db_session, created_user, auth_headers):
    account = _create_account(db_session, created_user)

    response = client.post(
        "/api/trade-setups/preview",
        headers=auth_headers,
        json={
            "trading_account_id": account.id,
            "symbol": "EURUSD",
            "side": "sell",
            "sl_price": 1.086,
            "risk_mode": "fixed_money",
            "risk_value": 100,
            "rr_order2": 2.5,
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["estimated_entry"] == 1.085
    assert data["r_value"] == 0.001
    assert data["tp1_price"] == 1.084
    assert data["tp2_price"] == 1.0825
    assert data["order1_volume"] == 0.5
    assert data["order2_volume"] == 0.5


def test_invalid_sl_for_buy(client, db_session, created_user, auth_headers):
    account = _create_account(db_session, created_user)

    response = client.post(
        "/api/trade-setups/preview",
        headers=auth_headers,
        json={
            "trading_account_id": account.id,
            "symbol": "XAUUSD",
            "side": "buy",
            "sl_price": 2320.3,
            "risk_mode": "fixed_money",
            "risk_value": 100,
            "rr_order2": 2,
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "For buy setups, sl_price must be below the estimated entry."


def test_invalid_sl_for_sell(client, db_session, created_user, auth_headers):
    account = _create_account(db_session, created_user)

    response = client.post(
        "/api/trade-setups/preview",
        headers=auth_headers,
        json={
            "trading_account_id": account.id,
            "symbol": "EURUSD",
            "side": "sell",
            "sl_price": 1.0849,
            "risk_mode": "fixed_money",
            "risk_value": 100,
            "rr_order2": 2,
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "For sell setups, sl_price must be above the estimated entry."


def test_volume_below_min_lot(client, db_session, created_user, auth_headers):
    account = _create_account(db_session, created_user)

    response = client.post(
        "/api/trade-setups/preview",
        headers=auth_headers,
        json={
            "trading_account_id": account.id,
            "symbol": "XAUUSD",
            "side": "buy",
            "sl_price": 2310.2,
            "risk_mode": "fixed_money",
            "risk_value": 10,
            "rr_order2": 2,
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Computed volume is below minimum lot size"
