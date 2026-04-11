import os

os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("ENCRYPTION_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.main import app
from app.models.allowed_symbol import AllowedSymbol
from app.models import admin_audit_log, allowed_symbol, risk_control_log, trade_event, trade_setup, trading_account, user, user_daily_risk_state  # noqa: F401
from app.schemas.user import UserCreate
from app.services.users import create_user


engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


@pytest.fixture(autouse=True)
def reset_database() -> None:
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def db_session():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def client(db_session):
    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def created_user(db_session):
    return create_user(
        db_session,
        UserCreate(email="user@example.com", password="password123", full_name="Test User"),
    )


@pytest.fixture
def auth_headers(client, created_user):
    response = client.post(
        "/api/auth/token",
        data={"username": created_user.email, "password": "password123"},
    )
    token = response.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def allow_symbol(db_session):
    def _allow(symbol_name: str, *, active: bool = True, display_name: str | None = None, notes: str | None = None):
        symbol = AllowedSymbol(
            symbol_name=symbol_name.upper(),
            is_active=active,
            display_name=display_name,
            notes=notes,
        )
        db_session.add(symbol)
        db_session.commit()
        db_session.refresh(symbol)
        return symbol

    return _allow
