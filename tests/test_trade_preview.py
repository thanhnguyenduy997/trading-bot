from app.execution.base import AdapterError
from app.schemas.trade_preview import TradePreviewRequest
from app.schemas.trading_account import TradingAccountCreate
from app.services.preview_service import PreviewService
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


class FakePreviewAdapter:
    def __init__(self, account):
        self.account = account

    def connect(self):
        return None

    def get_account_info(self):
        return {"balance": 10000.0}

    def get_quote(self, symbol: str):
        quotes = {
            "XAUUSD": {"symbol": "XAUUSD", "bid": 2320.0, "ask": 2320.2},
            "EURUSD": {"symbol": "EURUSD", "bid": 1.0850, "ask": 1.0851},
        }
        return quotes[symbol]

    def get_symbol_info(self, symbol: str):
        symbol_info = {
            "XAUUSD": {
                "symbol": "XAUUSD",
                "point": 0.01,
                "digits": 2,
                "trade_contract_size": 100.0,
                "volume_min": 0.01,
                "volume_max": 100.0,
                "volume_step": 0.01,
            },
            "EURUSD": {
                "symbol": "EURUSD",
                "point": 0.0001,
                "digits": 4,
                "trade_contract_size": 100000.0,
                "volume_min": 0.01,
                "volume_max": 100.0,
                "volume_step": 0.01,
            },
        }
        return symbol_info[symbol]

    def list_symbols(self):
        return [
            {"symbol": "XAUUSD", "visible": True},
            {"symbol": "EURUSD", "visible": True},
        ]

    def execute_setup(self, setup):
        return {"status": "not_used"}

    def close(self):
        return None


class FailingPreviewAdapter(FakePreviewAdapter):
    def get_quote(self, symbol: str):
        raise AdapterError(
            code="mt5_quote_unavailable",
            message=f"Quote unavailable for {symbol}.",
            details=None,
        )


def test_valid_buy_preview(client, db_session, created_user, auth_headers, monkeypatch, allow_symbol):
    allow_symbol("XAUUSD")
    monkeypatch.setattr(
        "app.services.execution.default_adapter_factory",
        lambda account: FakePreviewAdapter(account),
    )
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
    assert data["bid"] == 2320.0
    assert data["ask"] == 2320.2
    assert data["estimated_entry"] == 2320.2
    assert data["r_value"] == 1.0
    assert data["tp1_price"] == 2321.2
    assert data["tp2_price"] == 2322.2
    assert data["risk_per_order"] == 50.0
    assert data["order1_volume"] == 0.5
    assert data["order2_volume"] == 0.5
    assert data["digits"] == 2
    assert data["volume_step"] == 0.01
    assert data["validation_status"] == "valid"


def test_valid_sell_preview(client, db_session, created_user, auth_headers, monkeypatch, allow_symbol):
    allow_symbol("EURUSD")
    monkeypatch.setattr(
        "app.services.execution.default_adapter_factory",
        lambda account: FakePreviewAdapter(account),
    )
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


def test_invalid_sl_for_buy(client, db_session, created_user, auth_headers, monkeypatch, allow_symbol):
    allow_symbol("XAUUSD")
    monkeypatch.setattr(
        "app.services.execution.default_adapter_factory",
        lambda account: FakePreviewAdapter(account),
    )
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


def test_invalid_sl_for_sell(client, db_session, created_user, auth_headers, monkeypatch, allow_symbol):
    allow_symbol("EURUSD")
    monkeypatch.setattr(
        "app.services.execution.default_adapter_factory",
        lambda account: FakePreviewAdapter(account),
    )
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


def test_volume_below_min_lot(client, db_session, created_user, auth_headers, monkeypatch, allow_symbol):
    allow_symbol("XAUUSD")
    monkeypatch.setattr(
        "app.services.execution.default_adapter_factory",
        lambda account: FakePreviewAdapter(account),
    )
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


def test_preview_service_uses_live_symbol_info_with_mocked_adapter(db_session, created_user, allow_symbol):
    from app.services.execution import TradingAccountExecutionService

    allow_symbol("XAUUSD")
    account = _create_account(db_session, created_user)
    execution_service = TradingAccountExecutionService(
        db_session,
        adapter_factory=lambda adapter_account: FakePreviewAdapter(adapter_account),
    )
    preview = PreviewService(db_session, execution_service=execution_service).build_preview(
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

    assert preview.estimated_entry == 2320.2
    assert preview.trade_contract_size == 100.0
    assert preview.volume_min == 0.01
    assert preview.volume_max == 100.0
    assert preview.volume_step == 0.01


def test_preview_service_handles_live_quote_failure(db_session, created_user, allow_symbol):
    from app.services.execution import TradingAccountExecutionService

    allow_symbol("XAUUSD")
    account = _create_account(db_session, created_user)
    execution_service = TradingAccountExecutionService(
        db_session,
        adapter_factory=lambda adapter_account: FailingPreviewAdapter(adapter_account),
    )

    try:
        PreviewService(db_session, execution_service=execution_service).build_preview(
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
    except ValueError as exc:
        assert str(exc) == "Live MT5 preview unavailable: Quote unavailable for XAUUSD."
    else:
        raise AssertionError("Expected preview service to raise ValueError for live quote failure")


def test_preview_rejects_symbol_not_allowed_by_admin_policy(client, db_session, created_user, auth_headers, monkeypatch):
    monkeypatch.setattr(
        "app.services.execution.default_adapter_factory",
        lambda account: FakePreviewAdapter(account),
    )
    account = _create_account(db_session, created_user)

    response = client.post(
        "/api/trade-setups/preview",
        headers=auth_headers,
        json={
            "trading_account_id": account.id,
            "symbol": "BTCUSD",
            "side": "buy",
            "sl_price": 2319.2,
            "risk_mode": "fixed_money",
            "risk_value": 100,
            "rr_order2": 2,
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Symbol is not allowed by admin policy."


def test_preview_rejects_symbol_not_available_on_selected_mt5_account(
    client,
    db_session,
    created_user,
    auth_headers,
    monkeypatch,
    allow_symbol,
):
    allow_symbol("BTCUSD")
    monkeypatch.setattr(
        "app.services.execution.default_adapter_factory",
        lambda account: FakePreviewAdapter(account),
    )
    account = _create_account(db_session, created_user)

    response = client.post(
        "/api/trade-setups/preview",
        headers=auth_headers,
        json={
            "trading_account_id": account.id,
            "symbol": "BTCUSD",
            "side": "buy",
            "sl_price": 2319.2,
            "risk_mode": "fixed_money",
            "risk_value": 100,
            "rr_order2": 2,
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Symbol is not available on the selected MT5 account."


def test_disabled_symbol_is_hidden_and_rejected(client, db_session, created_user, auth_headers, monkeypatch, allow_symbol):
    allow_symbol("XAUUSD", active=False, display_name="Gold Spot")
    monkeypatch.setattr(
        "app.services.execution.default_adapter_factory",
        lambda account: FakePreviewAdapter(account),
    )
    account = _create_account(db_session, created_user)
    client.post("/api/auth/login", data={"email": created_user.email, "password": "password123"}, follow_redirects=False)

    page = client.get("/trade-setups/preview")
    api_response = client.post(
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

    assert page.status_code == 200
    assert "Gold Spot" not in page.text
    assert api_response.status_code == 400
    assert api_response.json()["detail"] == "Symbol is not allowed by admin policy."


def test_preview_page_dropdown_uses_allowed_and_available_symbols(
    client,
    db_session,
    created_user,
    monkeypatch,
    allow_symbol,
):
    allow_symbol("XAUUSD", display_name="Gold Spot")
    allow_symbol("BTCUSD", display_name="Bitcoin", active=True)
    monkeypatch.setattr(
        "app.services.execution.default_adapter_factory",
        lambda account: FakePreviewAdapter(account),
    )
    account = _create_account(db_session, created_user)
    client.post("/api/auth/login", data={"email": created_user.email, "password": "password123"}, follow_redirects=False)

    response = client.get("/trade-setups/preview")

    assert response.status_code == 200
    assert "Gold Spot" in response.text
    assert "Bitcoin" not in response.text
