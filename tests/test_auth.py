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


def test_expired_session_on_protected_html_page_redirects_to_login_with_next(client, created_user):
    client.cookies.set("access_token", "expired-token")

    response = client.get("/trade-setups/preview", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login?message=Phi%C3%AAn%20%C4%91%C4%83ng%20nh%E1%BA%ADp%20%C4%91%C3%A3%20h%E1%BA%BFt%20h%E1%BA%A1n%2C%20vui%20l%C3%B2ng%20%C4%91%C4%83ng%20nh%E1%BA%ADp%20l%E1%BA%A1i.&next=/trade-setups/preview"


def test_expired_session_on_async_endpoint_returns_clean_401(client):
    client.cookies.set("access_token", "expired-token")

    response = client.get(
        "/trading-accounts/id/1/symbols",
        headers={"X-Requested-With": "XMLHttpRequest"},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Phiên đăng nhập đã hết hạn, vui lòng đăng nhập lại."
    assert response.json()["login_url"].startswith("/login?")


def test_successful_login_redirects_to_next_page(client, created_user):
    response = client.post(
        "/api/auth/login",
        data={
            "email": created_user.email,
            "password": "password123",
            "next": "/trade-setups/preview",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/trade-setups/preview"


def test_vietnamese_expired_session_message_is_shown_on_login_page(client):
    response = client.get(
        "/login?message=Phi%C3%AAn%20%C4%91%C4%83ng%20nh%E1%BA%ADp%20%C4%91%C3%A3%20h%E1%BA%BFt%20h%E1%BA%A1n%2C%20vui%20l%C3%B2ng%20%C4%91%C4%83ng%20nh%E1%BA%ADp%20l%E1%BA%A1i.",
    )

    assert response.status_code == 200
    assert "Phiên đăng nhập đã hết hạn, vui lòng đăng nhập lại." in response.text


def test_login_page_does_not_redirect_loop_with_stale_cookie(client):
    client.cookies.set("access_token", "expired-token")

    response = client.get("/login", follow_redirects=False)

    assert response.status_code == 200
    assert "Đăng nhập" in response.text
