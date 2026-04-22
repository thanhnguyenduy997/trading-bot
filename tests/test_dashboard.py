from datetime import date, datetime, timedelta, timezone

from app.models.mt5_trade_history import MT5TradeHistory
from app.models.trade_setup import TradeSetup
from app.schemas.trade_setup import TradeSetupCreate
from app.schemas.trading_account import TradingAccountCreate
from app.schemas.user import UserCreate
from app.services.mt5_trade_history import DashboardFilters, DashboardService, MT5TradeHistorySyncService
from app.services.trade_setups import create_trade_setup
from app.services.trading_accounts import create_trading_account
from app.services.users import create_user


def _create_account(db_session, user, account_number: str = "DASH-100"):
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


def _create_setup(db_session, user, account, *, order1_ticket: int, order2_ticket: int, setup_outcome: str = "tp2_hit"):
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
    setup.order1_ticket = order1_ticket
    setup.order2_ticket = order2_ticket
    setup.order1_outcome = "tp_hit"
    setup.order2_outcome = "closed_at_be" if setup_outcome == "breakeven" else "tp_hit"
    setup.setup_outcome = setup_outcome
    setup.executed_at = datetime.now(timezone.utc)
    db_session.add(setup)
    db_session.commit()
    db_session.refresh(setup)
    return setup


class DashboardHistoryAdapter:
    def __init__(self, account, history_map):
        self.account = account
        self.history_map = history_map

    def connect(self):
        return None

    def get_account_info(self):
        return {"login": self.account.account_number}

    def get_quote(self, symbol: str):
        return {}

    def get_symbol_info(self, symbol: str):
        return {}

    def list_symbols(self):
        return []

    def execute_setup(self, setup):
        return {}

    def place_market_order(self, **kwargs):
        return {}

    def close_position(self, **kwargs):
        return {}

    def get_position(self, *, position_ticket: int):
        return None

    def get_position_history(self, *, position_ticket: int):
        return []

    def get_trade_history(self, *, date_from: datetime, date_to: datetime):
        return self.history_map.get(self.account.id, [])

    def modify_position_sl(self, **kwargs):
        return {}

    def close(self):
        return None


def _deal(
    *,
    ticket: int,
    position_id: int,
    entry: str,
    deal_type: str,
    price: float,
    profit: float,
    when: datetime,
    comment: str,
    volume: float = 0.5,
    symbol: str = "XAUUSD",
):
    return {
        "ticket": ticket,
        "position_id": position_id,
        "entry": entry,
        "type": deal_type,
        "symbol": symbol,
        "volume": volume,
        "price": price,
        "profit": profit,
        "commission": 0.0,
        "swap": 0.0,
        "time": int(when.timestamp()),
        "comment": comment,
    }


def _login(client, email: str, password: str = "password123"):
    return client.post("/api/auth/login", data={"email": email, "password": password}, follow_redirects=False)


def test_sync_classifies_system_manual_and_unknown_trades(db_session, created_user):
    account = _create_account(db_session, created_user, "DASH-101")
    setup_one = _create_setup(db_session, created_user, account, order1_ticket=70001, order2_ticket=70002, setup_outcome="tp2_hit")
    setup_two = _create_setup(db_session, created_user, account, order1_ticket=71001, order2_ticket=71002, setup_outcome="stoploss")
    now = datetime.now(timezone.utc)
    history_map = {
        account.id: [
            _deal(ticket=11, position_id=70001, entry="in", deal_type="buy", price=2320.2, profit=0.0, when=now - timedelta(hours=2), comment=f"setup-{setup_one.id}-o1"),
            _deal(ticket=12, position_id=70001, entry="out", deal_type="sell", price=2321.2, profit=50.0, when=now - timedelta(hours=1), comment=f"setup-{setup_one.id}-o1"),
            _deal(ticket=21, position_id=90001, entry="in", deal_type="buy", price=2300.0, profit=0.0, when=now - timedelta(hours=4), comment="manual scalp"),
            _deal(ticket=22, position_id=90001, entry="out", deal_type="sell", price=2302.0, profit=40.0, when=now - timedelta(hours=3), comment="manual scalp"),
            _deal(ticket=31, position_id=91001, entry="in", deal_type="buy", price=2290.0, profit=0.0, when=now - timedelta(hours=6), comment=f"setup-{setup_one.id}-o1"),
            _deal(ticket=32, position_id=91001, entry="out", deal_type="sell", price=2288.0, profit=-20.0, when=now - timedelta(hours=5), comment=f"setup-{setup_two.id}-o1"),
        ]
    }
    service = MT5TradeHistorySyncService(
        db_session,
        adapter_factory=lambda current_account: DashboardHistoryAdapter(current_account, history_map),
    )

    result = service.sync_account_history(
        account_id=account.id,
        user_id=created_user.id,
        range_start=now - timedelta(days=1),
        range_end=now,
    )

    assert result["synced_count"] == 3
    rows = (
        db_session.query(MT5TradeHistory)
        .filter(MT5TradeHistory.trading_account_id == account.id)
        .order_by(MT5TradeHistory.position_ticket.asc())
        .all()
    )
    assert [row.trade_source for row in rows] == ["system", "manual", "unknown"]
    assert rows[0].linked_setup_id == setup_one.id
    assert rows[1].linked_setup_id is None
    assert rows[2].linked_setup_id is None


def test_dashboard_time_range_filters(db_session, created_user):
    account = _create_account(db_session, created_user, "DASH-102")
    db_session.add_all(
        [
            MT5TradeHistory(
                user_id=created_user.id,
                trading_account_id=account.id,
                position_ticket=1001,
                symbol="XAUUSD",
                side="buy",
                trade_source="manual",
                outcome="take_profit",
                volume=0.5,
                open_price=2320.0,
                close_price=2322.0,
                realized_pnl=45.0,
                open_time=datetime(2026, 4, 21, 9, tzinfo=timezone.utc),
                close_time=datetime(2026, 4, 21, 10, tzinfo=timezone.utc),
            ),
            MT5TradeHistory(
                user_id=created_user.id,
                trading_account_id=account.id,
                position_ticket=1002,
                symbol="XAUUSD",
                side="buy",
                trade_source="manual",
                outcome="stoploss",
                volume=0.5,
                open_price=2320.0,
                close_price=2318.0,
                realized_pnl=-30.0,
                open_time=datetime(2026, 4, 10, 9, tzinfo=timezone.utc),
                close_time=datetime(2026, 4, 10, 10, tzinfo=timezone.utc),
            ),
        ]
    )
    db_session.commit()
    dashboard = DashboardService(
        db_session,
        now_provider=lambda: datetime(2026, 4, 22, 8, tzinfo=timezone.utc),
    ).build_dashboard(
        actor=created_user,
        filters=DashboardFilters(range_key="last_7_days", user_id=created_user.id),
    )

    assert dashboard["summary"]["total_trades"] == 1
    assert dashboard["table_rows"][0]["position_ticket"] == 1001


def test_dashboard_summaries_include_mixed_trade_sources(db_session, created_user):
    account = _create_account(db_session, created_user, "DASH-103")
    setup = _create_setup(db_session, created_user, account, order1_ticket=80001, order2_ticket=80002, setup_outcome="breakeven")
    db_session.add_all(
        [
            MT5TradeHistory(
                user_id=created_user.id,
                trading_account_id=account.id,
                linked_setup_id=setup.id,
                position_ticket=80001,
                symbol="XAUUSD",
                side="buy",
                trade_source="system",
                outcome="tp_hit",
                volume=0.5,
                open_price=2320.0,
                close_price=2321.0,
                realized_pnl=50.0,
                open_time=datetime(2026, 4, 22, 1, tzinfo=timezone.utc),
                close_time=datetime(2026, 4, 22, 2, tzinfo=timezone.utc),
            ),
            MT5TradeHistory(
                user_id=created_user.id,
                trading_account_id=account.id,
                position_ticket=80003,
                symbol="EURUSD",
                side="sell",
                trade_source="manual",
                outcome="take_profit",
                volume=0.3,
                open_price=1.1,
                close_price=1.09,
                realized_pnl=25.0,
                open_time=datetime(2026, 4, 22, 3, tzinfo=timezone.utc),
                close_time=datetime(2026, 4, 22, 4, tzinfo=timezone.utc),
            ),
            MT5TradeHistory(
                user_id=created_user.id,
                trading_account_id=account.id,
                position_ticket=80004,
                symbol="GBPUSD",
                side="buy",
                trade_source="manual",
                outcome="stoploss",
                volume=0.2,
                open_price=1.3,
                close_price=1.29,
                realized_pnl=-15.0,
                open_time=datetime(2026, 4, 22, 5, tzinfo=timezone.utc),
                close_time=datetime(2026, 4, 22, 6, tzinfo=timezone.utc),
            ),
        ]
    )
    db_session.commit()

    dashboard = DashboardService(
        db_session,
        now_provider=lambda: datetime(2026, 4, 22, 12, tzinfo=timezone.utc),
    ).build_dashboard(
        actor=created_user,
        filters=DashboardFilters(range_key="today", user_id=created_user.id),
    )

    assert dashboard["summary"]["total_trades"] == 3
    assert dashboard["summary"]["total_setups"] == 1
    assert dashboard["summary"]["system_trades"] == 1
    assert dashboard["summary"]["manual_trades"] == 2
    assert dashboard["summary"]["breakeven_count"] == 1
    assert dashboard["summary"]["stoploss_count"] == 1
    assert dashboard["summary"]["take_profit_count"] == 1
    assert round(dashboard["summary"]["total_realized_pnl"], 2) == 60.0


def test_dashboard_page_shows_linked_setup_trade(client, db_session, created_user):
    account = _create_account(db_session, created_user, "DASH-104")
    setup = _create_setup(db_session, created_user, account, order1_ticket=82001, order2_ticket=82002, setup_outcome="tp2_hit")
    db_session.add(
        MT5TradeHistory(
            user_id=created_user.id,
            trading_account_id=account.id,
            linked_setup_id=setup.id,
            position_ticket=82001,
            symbol="XAUUSD",
            side="buy",
            trade_source="system",
            outcome="tp_hit",
            volume=0.5,
            open_price=2320.0,
            close_price=2321.0,
            realized_pnl=50.0,
            open_time=datetime.now(timezone.utc) - timedelta(hours=2),
            close_time=datetime.now(timezone.utc) - timedelta(hours=1),
            comment=f"setup-{setup.id}-o1",
        )
    )
    db_session.commit()
    _login(client, created_user.email)

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert "Trading Dashboard" in response.text
    assert f"/trade-setups/{setup.id}" in response.text
    assert "system" in response.text


def test_dashboard_unmatched_trades_still_appear_as_manual_or_unknown(db_session, created_user):
    account = _create_account(db_session, created_user, "DASH-105")
    db_session.add_all(
        [
            MT5TradeHistory(
                user_id=created_user.id,
                trading_account_id=account.id,
                position_ticket=83001,
                symbol="XAUUSD",
                side="buy",
                trade_source="manual",
                outcome="take_profit",
                volume=0.2,
                open_price=2320.0,
                close_price=2321.0,
                realized_pnl=10.0,
                open_time=datetime(2026, 4, 22, 1, tzinfo=timezone.utc),
                close_time=datetime(2026, 4, 22, 2, tzinfo=timezone.utc),
                comment="manual trade",
            ),
            MT5TradeHistory(
                user_id=created_user.id,
                trading_account_id=account.id,
                position_ticket=83002,
                symbol="EURUSD",
                side="sell",
                trade_source="unknown",
                outcome="stoploss",
                volume=0.2,
                open_price=1.1,
                close_price=1.11,
                realized_pnl=-12.0,
                open_time=datetime(2026, 4, 22, 3, tzinfo=timezone.utc),
                close_time=datetime(2026, 4, 22, 4, tzinfo=timezone.utc),
                comment="ambiguous history",
            ),
        ]
    )
    db_session.commit()

    dashboard = DashboardService(
        db_session,
        now_provider=lambda: datetime(2026, 4, 22, 12, tzinfo=timezone.utc),
    ).build_dashboard(
        actor=created_user,
        filters=DashboardFilters(range_key="today", user_id=created_user.id),
    )

    sources = {row["position_ticket"]: row["trade_source"] for row in dashboard["table_rows"]}
    assert sources[83001] == "manual"
    assert sources[83002] == "unknown"
