from app.execution.base import AdapterError
from app.schemas.trade_preview import TradePreviewRequest
from app.schemas.trading_account import TradingAccountCreate
from app.services.preview_service import PreviewService
from app.services.trading_accounts import create_trading_account


def _create_account(db_session, created_user, **overrides):
    payload = {
        "broker_name": "Demo Broker",
        "account_number": "ACC-100",
        "server_name": "demo-server",
        "password": "secret-pass",
    }
    payload.update(overrides)
    return create_trading_account(
        db_session,
        created_user.id,
        TradingAccountCreate(**payload),
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


class MissingSymbolPreviewAdapter(FakePreviewAdapter):
    def get_quote(self, symbol: str):
        raise AdapterError(
            code="mt5_quote_unavailable",
            message=f"Quote unavailable for {symbol}.",
            details=None,
        )


class SessionMismatchPreviewAdapter(FakePreviewAdapter):
    def connect(self):
        raise AdapterError(
            code="mt5_session_mismatch",
            message=(
                f"MT5 terminal is logged into account 999999, but this trading account expects {self.account.account_number}. "
                "Log into the correct MT5 account in the terminal first."
            ),
            details={"current_login": "999999", "expected_account": self.account.account_number},
        )


def test_valid_buy_preview(client, db_session, created_user, auth_headers, monkeypatch, sync_account_symbols):
    monkeypatch.setattr(
        "app.services.execution.default_adapter_factory",
        lambda account: FakePreviewAdapter(account),
    )
    account = _create_account(db_session, created_user)
    sync_account_symbols(account, "XAUUSD")

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


def test_valid_sell_preview(client, db_session, created_user, auth_headers, monkeypatch, sync_account_symbols):
    monkeypatch.setattr(
        "app.services.execution.default_adapter_factory",
        lambda account: FakePreviewAdapter(account),
    )
    account = _create_account(db_session, created_user)
    sync_account_symbols(account, "EURUSD")

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


def test_invalid_sl_for_buy(client, db_session, created_user, auth_headers, monkeypatch, sync_account_symbols):
    monkeypatch.setattr(
        "app.services.execution.default_adapter_factory",
        lambda account: FakePreviewAdapter(account),
    )
    account = _create_account(db_session, created_user)
    sync_account_symbols(account, "XAUUSD")

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


def test_invalid_sl_for_sell(client, db_session, created_user, auth_headers, monkeypatch, sync_account_symbols):
    monkeypatch.setattr(
        "app.services.execution.default_adapter_factory",
        lambda account: FakePreviewAdapter(account),
    )
    account = _create_account(db_session, created_user)
    sync_account_symbols(account, "EURUSD")

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


def test_volume_below_min_lot(client, db_session, created_user, auth_headers, monkeypatch, sync_account_symbols):
    monkeypatch.setattr(
        "app.services.execution.default_adapter_factory",
        lambda account: FakePreviewAdapter(account),
    )
    account = _create_account(db_session, created_user)
    sync_account_symbols(account, "XAUUSD")

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


def test_preview_service_uses_live_symbol_info_with_mocked_adapter(db_session, created_user, sync_account_symbols):
    from app.services.execution import TradingAccountExecutionService

    account = _create_account(db_session, created_user)
    sync_account_symbols(account, "XAUUSD")
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


def test_preview_service_handles_live_quote_failure(db_session, created_user, sync_account_symbols):
    from app.services.execution import TradingAccountExecutionService

    account = _create_account(db_session, created_user)
    sync_account_symbols(account, "XAUUSD")
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


def test_preview_service_blocks_when_mt5_session_does_not_match(db_session, created_user, sync_account_symbols):
    from app.services.execution import TradingAccountExecutionService

    account = _create_account(db_session, created_user)
    sync_account_symbols(account, "XAUUSD")
    execution_service = TradingAccountExecutionService(
        db_session,
        adapter_factory=lambda adapter_account: SessionMismatchPreviewAdapter(adapter_account),
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
        assert "MT5 terminal is logged into account 999999" in str(exc)
    else:
        raise AssertionError("Expected preview service to block on MT5 session mismatch")


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
    assert response.json()["detail"] == "Symbol is not synced for this trading account. Add it in MT5 Market Watch first, then click Refresh Symbols from MT5."


def test_preview_rejects_symbol_when_live_validation_fails(
    client,
    db_session,
    created_user,
    auth_headers,
    monkeypatch,
    sync_account_symbols,
):
    monkeypatch.setattr(
        "app.services.execution.default_adapter_factory",
        lambda account: MissingSymbolPreviewAdapter(account),
    )
    account = _create_account(db_session, created_user)
    sync_account_symbols(account, "BTCUSD")

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
    assert response.json()["detail"] == "Live MT5 preview unavailable: Quote unavailable for BTCUSD."


def test_preview_page_dropdown_uses_allowed_and_available_symbols(
    client,
    db_session,
    created_user,
    monkeypatch,
    sync_account_symbols,
):
    monkeypatch.setattr(
        "app.services.execution.default_adapter_factory",
        lambda account: FakePreviewAdapter(account),
    )
    account = _create_account(db_session, created_user)
    sync_account_symbols(account, "XAUUSD")
    client.post("/api/auth/login", data={"email": created_user.email, "password": "password123"}, follow_redirects=False)

    response = client.get("/trade-setups/preview")

    assert response.status_code == 200
    assert "XAUUSD" in response.text
    assert "BTCUSD" not in response.text


def test_preview_page_empty_synced_symbol_list_shows_clear_message(client, db_session, created_user, monkeypatch):
    monkeypatch.setattr(
        "app.services.execution.default_adapter_factory",
        lambda account: FakePreviewAdapter(account),
    )
    _create_account(db_session, created_user)
    client.post("/api/auth/login", data={"email": created_user.email, "password": "password123"}, follow_redirects=False)

    response = client.get("/trade-setups/preview")

    assert response.status_code == 200
    assert "No symbols are synced for this account yet. Add symbols in MT5 Market Watch first, then click Refresh Symbols from MT5." in response.text


def test_preview_page_uses_account_defaults_on_initial_load(
    client,
    db_session,
    created_user,
    monkeypatch,
    sync_account_symbols,
):
    monkeypatch.setattr(
        "app.services.execution.default_adapter_factory",
        lambda account: FakePreviewAdapter(account),
    )
    account = _create_account(db_session, created_user)
    sync_account_symbols(account, "EURUSD", "XAUUSD")
    account.default_symbol = "EURUSD"
    account.default_side = "sell"
    account.default_risk_mode = "balance_percent"
    account.default_risk_value = 2.5
    account.default_rr_order_2 = 3
    db_session.add(account)
    db_session.commit()
    db_session.refresh(account)
    client.post("/api/auth/login", data={"email": created_user.email, "password": "password123"}, follow_redirects=False)

    response = client.get("/trade-setups/preview")

    assert response.status_code == 200
    assert '<option value="EURUSD" selected>' in response.text
    assert '<option value="sell" selected>' in response.text
    assert '<option value="balance_percent" selected>' in response.text
    assert 'name="risk_value" step="0.01" min="0.01" value="2.5"' in response.text
    assert 'name="rr_order2" step="0.1" min="0.1" value="3.0"' in response.text
    assert 'name="sl_price" step="0.0001" min="0" value=""' in response.text


def test_preview_submit_allows_user_to_override_account_defaults(
    client,
    db_session,
    created_user,
    monkeypatch,
    sync_account_symbols,
):
    monkeypatch.setattr(
        "app.services.execution.default_adapter_factory",
        lambda account: FakePreviewAdapter(account),
    )
    account = _create_account(db_session, created_user)
    sync_account_symbols(account, "EURUSD", "XAUUSD")
    account.default_symbol = "EURUSD"
    account.default_side = "sell"
    account.default_risk_mode = "balance_percent"
    account.default_risk_value = 2.5
    account.default_rr_order_2 = 3
    db_session.add(account)
    db_session.commit()
    db_session.refresh(account)
    client.post("/api/auth/login", data={"email": created_user.email, "password": "password123"}, follow_redirects=False)

    response = client.post(
        "/trade-setups/preview",
        data={
            "trading_account_id": str(account.id),
            "symbol": "XAUUSD",
            "side": "buy",
            "sl_price": "2319.2",
            "risk_mode": "fixed_money",
            "risk_value": "150",
            "rr_order2": "2",
        },
    )

    assert response.status_code == 200
    assert "Review Setup" in response.text
    assert "XAUUSD" in response.text
    assert "BUY" in response.text
    assert "2322.2" in response.text


def test_preview_page_keeps_primary_results_visible_and_moves_secondary_details_into_accordion(
    client,
    db_session,
    created_user,
    monkeypatch,
    sync_account_symbols,
):
    monkeypatch.setattr(
        "app.services.execution.default_adapter_factory",
        lambda account: FakePreviewAdapter(account),
    )
    account = _create_account(db_session, created_user, account_number="ACC-202")
    sync_account_symbols(account, "XAUUSD")
    client.post("/api/auth/login", data={"email": created_user.email, "password": "password123"}, follow_redirects=False)

    response = client.post(
        "/trade-setups/preview",
        data={
            "trading_account_id": str(account.id),
            "symbol": "XAUUSD",
            "side": "buy",
            "sl_price": "2319.2",
            "risk_mode": "fixed_money",
            "risk_value": "100",
            "rr_order2": "2",
        },
    )

    assert response.status_code == 200
    assert "Estimated entry" in response.text
    assert "Stop loss" in response.text
    assert "TP1" in response.text
    assert "TP2" in response.text
    assert "Order 1 volume" in response.text
    assert "Order 2 volume" in response.text
    assert "Total risk" in response.text
    assert "Advanced Details" in response.text
    assert "Risk / order" in response.text
    assert "Live bid" in response.text
    assert "Live ask" in response.text
    assert "Symbols come from the selected account&#39;s synced MT5 Market Watch list." not in response.text
    assert "Execute within 5 minutes or refresh the preview to avoid stale quote risk." not in response.text


def test_preview_uses_only_synced_symbols_for_selected_account(
    client,
    db_session,
    created_user,
    monkeypatch,
    sync_account_symbols,
):
    monkeypatch.setattr(
        "app.services.execution.default_adapter_factory",
        lambda account: FakePreviewAdapter(account),
    )
    account1 = _create_account(db_session, created_user)
    account2 = create_trading_account(
        db_session,
        created_user.id,
        TradingAccountCreate(
            broker_name="Demo Broker",
            account_number="ACC-200",
            server_name="demo-server",
            password="secret-pass",
        ),
    )
    sync_account_symbols(account1, "XAUUSD")
    sync_account_symbols(account2, "EURUSD")
    client.post("/api/auth/login", data={"email": created_user.email, "password": "password123"}, follow_redirects=False)

    response = client.get(f"/trading-accounts/id/{account2.id}/symbols")

    assert response.status_code == 200
    assert response.json()["symbols"] == [{"symbol_name": "EURUSD"}]


def test_invalid_default_symbol_falls_back_to_first_synced_symbol(
    client,
    db_session,
    created_user,
    monkeypatch,
    sync_account_symbols,
):
    monkeypatch.setattr(
        "app.services.execution.default_adapter_factory",
        lambda account: FakePreviewAdapter(account),
    )
    account = _create_account(db_session, created_user, account_number="ACC-201")
    sync_account_symbols(account, "EURUSD", "XAUUSD")
    account.default_symbol = "BTCUSD"
    account.default_side = "sell"
    account.default_risk_mode = "fixed_money"
    account.default_risk_value = 75
    account.default_rr_order_2 = 2.5
    db_session.add(account)
    db_session.commit()
    db_session.refresh(account)
    client.post("/api/auth/login", data={"email": created_user.email, "password": "password123"}, follow_redirects=False)

    response = client.get(f"/trading-accounts/id/{account.id}/symbols")

    assert response.status_code == 200
    assert response.json()["preview_defaults"]["symbol"] == "EURUSD"
    assert response.json()["message"] == "Saved default symbol is no longer synced for this account. Using the first available synced symbol instead."


def test_save_preview_defaults_updates_account_correctly(
    client,
    db_session,
    created_user,
    monkeypatch,
    sync_account_symbols,
):
    monkeypatch.setattr(
        "app.services.execution.default_adapter_factory",
        lambda account: FakePreviewAdapter(account),
    )
    account = _create_account(db_session, created_user, account_number="ACC-202")
    sync_account_symbols(account, "EURUSD", "XAUUSD")
    client.post("/api/auth/login", data={"email": created_user.email, "password": "password123"}, follow_redirects=False)

    response = client.post(
        "/trade-setups/preview/defaults",
        data={
            "trading_account_id": str(account.id),
            "symbol": "XAUUSD",
            "side": "sell",
            "risk_mode": "balance_percent",
            "risk_value": "1.75",
            "rr_order2": "2.8",
        },
    )

    assert response.status_code == 200
    db_session.refresh(account)
    assert account.default_symbol == "XAUUSD"
    assert account.default_side == "sell"
    assert account.default_risk_mode == "balance_percent"
    assert float(account.default_risk_value) == 1.75
    assert float(account.default_rr_order_2) == 2.8
    assert response.json()["preview_defaults"]["symbol"] == "XAUUSD"
