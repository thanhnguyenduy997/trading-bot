def test_register_and_get_me(client):
    register_response = client.post(
        "/api/auth/register",
        json={
            "email": "new-user@example.com",
            "password": "password123",
            "full_name": "New User",
        },
    )
    assert register_response.status_code == 201
    assert register_response.json()["email"] == "new-user@example.com"

    login_response = client.post(
        "/api/auth/token",
        data={"username": "new-user@example.com", "password": "password123"},
    )
    assert login_response.status_code == 200
    token = login_response.json()["access_token"]

    me_response = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me_response.status_code == 200
    assert me_response.json()["email"] == "new-user@example.com"
