from datetime import datetime, timedelta, timezone
import json

from app.models.trade_event import TradeEvent
from app.models.trade_setup import TradeSetup
from app.schemas.trade_preview import TradePreviewRequest
from app.schemas.trade_setup import TradeSetupCreate
from app.schemas.trading_account import TradingAccountCreate
from app.services.news_guard import NewsEvent, NewsGuardService
from app.services.preview_service import PreviewService
from app.services.trade_setups import create_trade_setup
from app.services.trading_accounts import create_trading_account


SERVER_TIME = datetime(2026, 6, 28, 13, 0, tzinfo=timezone.utc)


def _create_account(db_session, user, account_number: str = "NG-100"):
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


def _create_setup(db_session, user, account, symbol: str = "XAUUSD"):
    return create_trade_setup(
        db_session,
        user.id,
        TradeSetupCreate(
            trading_account_id=account.id,
            symbol=symbol,
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


class NewsProvider:
    def __init__(self, events):
        self.events = events

    def list_events(self, *, currencies, server_time):
        return [event for event in self.events if event.currency in currencies]


class NewsExecutionAdapter:
    def __init__(self, account):
        self.account = account
        self.placed_orders = []

    def connect(self):
        return None

    def get_account_info(self):
        return {"login": self.account.account_number}

    def get_server_time(self, symbol=None):
        return SERVER_TIME

    def get_quote(self, symbol: str):
        return {"symbol": symbol, "bid": 2320.0, "ask": 2320.2, "time": int(SERVER_TIME.timestamp())}

    def get_symbol_info(self, symbol: str):
        return {
            "symbol": symbol,
            "point": 0.01,
            "digits": 2,
            "trade_contract_size": 100.0,
            "volume_min": 0.01,
            "volume_max": 100.0,
            "volume_step": 0.01,
        }

    def list_symbols(self):
        return [{"symbol": "XAUUSD", "visible": True}]

    def execute_setup(self, setup):
        return {"status": "not_used"}

    def place_market_order(self, **kwargs):
        ticket = 4000 + len(self.placed_orders) + 1
        self.placed_orders.append(kwargs)
        return {"ticket": ticket, "order": ticket, "deal": ticket + 5000}

    def close_position(self, **kwargs):
        return {"ticket": kwargs["position_ticket"]}

    def close(self):
        return None


def _patch_news_provider(monkeypatch, event_time):
    event = NewsEvent(currency="USD", name="US CPI", impact="red/high", event_time=event_time, source="test")
    monkeypatch.setattr(
        "app.services.news_guard.ConfiguredNewsEventProvider.list_events",
        lambda self, *, currencies, server_time: [event] if "USD" in currencies else [],
    )
    return event


def _patch_execution_adapter(monkeypatch):
    adapters = []

    def factory(account):
        adapter = NewsExecutionAdapter(account)
        adapters.append(adapter)
        return adapter

    monkeypatch.setattr("app.services.execution.default_adapter_factory", factory)
    monkeypatch.setattr("app.services.trade_setup_execution.default_adapter_factory", factory)
    return adapters


def test_warning_state_before_news_uses_server_time(db_session, created_user, sync_account_symbols, monkeypatch):
    _patch_news_provider(monkeypatch, SERVER_TIME + timedelta(minutes=20))
    account = _create_account(db_session, created_user, "NG-101")
    sync_account_symbols(account, "XAUUSD")
    adapters = _patch_execution_adapter(monkeypatch)

    preview = PreviewService(db_session).build_preview(
        created_user.id,
        TradePreviewRequest(
            trading_account_id=account.id,
            symbol="XAUUSD",
            side="buy",
            sl_price=2319.2,
            risk_mode="fixed_money",
            risk_value=100,
            rr_order2=2,
        ),
    )

    assert adapters
    assert preview.server_time == SERVER_TIME
    assert preview.news_guard["state"] == "warning"
    assert preview.news_guard["seconds_until_window"] == 15 * 60
    assert "High-impact news is approaching" in preview.warnings[0]


def test_symbol_news_relevance_mapping_includes_major_pairs_and_indices():
    service = NewsGuardService(provider=NewsProvider([]))

    assert service.relevant_currencies("XAUUSD") == ("USD",)
    assert service.relevant_currencies("EURUSD") == ("EUR", "USD")
    assert service.relevant_currencies("GBPJPY") == ("GBP", "JPY")
    assert service.relevant_currencies("US30") == ("USD",)


def test_execute_time_recheck_inside_news_window_requires_confirmation(
    client,
    db_session,
    created_user,
    auth_headers,
    sync_account_symbols,
    monkeypatch,
):
    event = _patch_news_provider(monkeypatch, SERVER_TIME + timedelta(minutes=1))
    _patch_execution_adapter(monkeypatch)
    account = _create_account(db_session, created_user, "NG-102")
    sync_account_symbols(account, "XAUUSD")
    setup = _create_setup(db_session, created_user, account)

    response = client.post(f"/api/trade-setups/{setup.id}/execute", headers=auth_headers)

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["state"] == "news_guard_warning"
    assert detail["news_guard"]["event_time"] == event.event_time.isoformat()
    stored = db_session.get(TradeSetup, setup.id)
    assert stored.status == "draft"
    assert stored.order1_ticket is None


def test_user_canceling_after_news_warning_submits_no_order(
    client,
    db_session,
    created_user,
    sync_account_symbols,
    monkeypatch,
):
    _patch_news_provider(monkeypatch, SERVER_TIME + timedelta(minutes=1))
    adapters = _patch_execution_adapter(monkeypatch)
    account = _create_account(db_session, created_user, "NG-103")
    sync_account_symbols(account, "XAUUSD")
    setup = _create_setup(db_session, created_user, account)
    client.post("/api/auth/login", data={"email": created_user.email, "password": "password123"})

    response = client.post(
        f"/trade-setups/{setup.id}/execute/modal",
        data={"accept_news_override": "false"},
    )

    assert response.status_code == 409
    assert response.json()["state"] == "news_guard_warning"
    assert [order for adapter in adapters for order in adapter.placed_orders] == []


def test_user_explicitly_overriding_executes_and_logs_audit_event(
    client,
    db_session,
    created_user,
    auth_headers,
    sync_account_symbols,
    monkeypatch,
):
    event = _patch_news_provider(monkeypatch, SERVER_TIME + timedelta(minutes=1))
    _patch_execution_adapter(monkeypatch)
    account = _create_account(db_session, created_user, "NG-104")
    sync_account_symbols(account, "XAUUSD")
    setup = _create_setup(db_session, created_user, account)

    response = client.post(
        f"/api/trade-setups/{setup.id}/execute?accept_news_override=true",
        headers=auth_headers,
    )

    assert response.status_code == 200
    stored = db_session.get(TradeSetup, setup.id)
    assert stored.status == "executed"
    assert stored.order1_ticket == 4001
    assert stored.order2_ticket == 4002
    override_event = (
        db_session.query(TradeEvent)
        .filter(TradeEvent.setup_id == setup.id, TradeEvent.event_type == "news_guard_override_execute")
        .first()
    )
    assert override_event is not None
    details = json.loads(override_event.details)
    assert details["news_override"] is True
    assert details["user_id"] == created_user.id
    assert details["trading_account_id"] == account.id
    assert details["symbol"] == "XAUUSD"
    assert details["setup_id"] == setup.id
    assert details["event_time"] == event.event_time.isoformat()
    assert details["server_time_at_execution"] == SERVER_TIME.isoformat()
    assert details["related_news_currency"] == "USD"
    assert details["related_news_event"] == "US CPI"
    assert details["preview_timestamp"] is not None


def test_split_two_order_entry_requires_one_confirmation_for_both_orders(
    client,
    db_session,
    created_user,
    auth_headers,
    sync_account_symbols,
    monkeypatch,
):
    _patch_news_provider(monkeypatch, SERVER_TIME + timedelta(minutes=1))
    adapters = _patch_execution_adapter(monkeypatch)
    account = _create_account(db_session, created_user, "NG-105")
    sync_account_symbols(account, "XAUUSD")
    setup = _create_setup(db_session, created_user, account)

    response = client.post(
        f"/api/trade-setups/{setup.id}/execute?accept_news_override=true",
        headers=auth_headers,
    )

    assert response.status_code == 200
    placed_orders = [order for adapter in adapters for order in adapter.placed_orders]
    assert len(placed_orders) == 2


def test_existing_trade_management_is_not_blocked_by_news_guard(
    client,
    db_session,
    created_user,
    sync_account_symbols,
    monkeypatch,
):
    _patch_news_provider(monkeypatch, SERVER_TIME + timedelta(minutes=1))
    _patch_execution_adapter(monkeypatch)
    account = _create_account(db_session, created_user, "NG-106")
    sync_account_symbols(account, "XAUUSD")
    setup = _create_setup(db_session, created_user, account)
    setup.status = "executed"
    setup.order1_ticket = 4001
    setup.order2_ticket = 4002
    db_session.add(setup)
    db_session.commit()
    client.post("/api/auth/login", data={"email": created_user.email, "password": "password123"})

    response = client.get(f"/trade-setups/{setup.id}")

    assert response.status_code == 200
    assert "Trade Setup" in response.text
