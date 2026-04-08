from app.models.trading_account import TradingAccount


def test_trading_account_crud(client, db_session, auth_headers):
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
