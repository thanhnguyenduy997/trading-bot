from datetime import datetime, timezone

from app.models.trading_account import TradingAccount
from app.models.trade_setup import TradeSetup
from app.models.user import User
from app.schemas.trade_setup import TradeSetupCreate
from app.schemas.trading_account import TradingAccountCreate
from app.schemas.user import UserCreate
from app.services.trade_setups import create_trade_setup
from app.services.trading_accounts import create_trading_account
from app.services.users import create_user


def _login(client, email: str, password: str = "password123"):
    return client.post("/api/auth/login", data={"email": email, "password": password}, follow_redirects=False)


def _create_admin(db_session):
    return create_user(
        db_session,
        UserCreate(
            email="admin@example.com",
            password="password123",
            full_name="Admin User",
            role="admin",
            is_active=True,
        ),
    )


def _create_account(db_session, user, account_number: str = "ACC-001"):
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


def test_admin_can_access_admin_pages(client, db_session):
    admin = _create_admin(db_session)
    _login(client, admin.email)

    response = client.get("/admin/users")

    assert response.status_code == 200
    assert "Admin Users" in response.text


def test_normal_user_cannot_access_admin_pages(client, created_user):
    _login(client, created_user.email)

    response = client.get("/admin/users")

    assert response.status_code == 403


def test_admin_can_create_user(client, db_session):
    admin = _create_admin(db_session)
    _login(client, admin.email)

    response = client.post(
        "/admin/users/new",
        data={
            "email": "managed@example.com",
            "full_name": "Managed User",
            "role": "user",
            "password": "password123",
            "is_active": "true",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    created = db_session.query(User).filter(User.email == "managed@example.com").first()
    assert created is not None
    assert created.role == "user"
    assert created.is_active is True


def test_admin_can_create_trading_account_for_user(client, db_session):
    admin = _create_admin(db_session)
    user = create_user(
        db_session,
        UserCreate(email="account-owner@example.com", password="password123", full_name="Owner"),
    )
    _login(client, admin.email)

    response = client.post(
        f"/admin/users/{user.id}/trading-accounts",
        data={
            "broker_name": "Demo Broker",
            "account_number": "900001",
            "server_name": "demo-server",
            "terminal_path": "",
            "password": "secret-pass",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    account = db_session.query(TradingAccount).filter(TradingAccount.user_id == user.id).first()
    assert account is not None
    assert account.account_number == "900001"


def test_impersonation_start_stop_preserves_admin_identity(client, db_session):
    admin = _create_admin(db_session)
    user = create_user(
        db_session,
        UserCreate(email="impersonated@example.com", password="password123", full_name="Impersonated User"),
    )
    _login(client, admin.email)

    start = client.post(f"/admin/users/{user.id}/impersonate", follow_redirects=False)

    assert start.status_code == 303
    dashboard = client.get("/dashboard")
    assert dashboard.status_code == 200
    assert "You are acting as impersonated@example.com." in dashboard.text
    admin_page = client.get("/admin/users")
    assert admin_page.status_code == 200

    stop = client.post("/admin/impersonation/stop", follow_redirects=False)

    assert stop.status_code == 303
    dashboard_after = client.get("/dashboard")
    assert "You are acting as" not in dashboard_after.text
    assert "Admin User" in dashboard_after.text


def test_expired_session_on_admin_page_redirects_to_login(client):
    client.cookies.set("access_token", "expired-token")

    response = client.get("/admin/users", follow_redirects=False)

    assert response.status_code == 303


def test_admin_can_recalculate_setup_outcomes_for_one_account(client, db_session):
    admin = _create_admin(db_session)
    user = create_user(
        db_session,
        UserCreate(email="recalc-owner@example.com", password="password123", full_name="Owner"),
    )
    account = _create_account(db_session, user, "RECALC-001")
    setup = create_trade_setup(
        db_session,
        user.id,
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
            status="draft",
        ),
    )
    setup.status = "executed"
    setup.order1_outcome = "sl_hit"
    setup.order2_outcome = "sl_hit"
    setup.order1_closed_at = datetime(2026, 4, 22, 8, tzinfo=timezone.utc)
    setup.order2_closed_at = datetime(2026, 4, 22, 9, tzinfo=timezone.utc)
    setup.order1_realized_pnl = -50.0
    setup.order2_realized_pnl = -50.0
    setup.setup_outcome = None
    db_session.add(setup)
    db_session.commit()

    _login(client, admin.email)
    response = client.post(
        "/admin/setup-outcomes/recalculate",
        data={"trading_account_id": str(account.id)},
        follow_redirects=False,
    )

    assert response.status_code == 303
    db_session.refresh(setup)
    assert setup.setup_outcome == "full_loss"
    assert response.headers["location"].startswith("/admin/settings?message=")
