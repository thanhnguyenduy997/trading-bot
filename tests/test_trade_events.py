from app.models.trade_event import TradeEvent
from app.schemas.trade_setup import TradeSetupCreate
from app.schemas.trading_account import TradingAccountCreate
from app.schemas.user import UserCreate
from app.services.trade_events import list_trade_events
from app.services.trade_setups import create_trade_setup
from app.services.trading_accounts import create_trading_account
from app.services.users import create_user


def _create_account(db_session, user, account_number: str = "ACC-100"):
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


def _create_setup(db_session, user, account):
    return create_trade_setup(
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


def test_event_creation_on_setup_save(db_session, created_user):
    account = _create_account(db_session, created_user)
    setup = _create_setup(db_session, created_user, account)

    events = (
        db_session.query(TradeEvent)
        .filter(TradeEvent.setup_id == setup.id, TradeEvent.user_id == created_user.id)
        .order_by(TradeEvent.id.asc())
        .all()
    )

    assert len(events) == 1
    assert events[0].event_type == "setup_saved"
    assert events[0].message == "Trade setup saved as draft."


def test_event_visibility_only_for_correct_users_setup(db_session, created_user):
    own_account = _create_account(db_session, created_user, "ACC-101")
    own_setup = _create_setup(db_session, created_user, own_account)

    other_user = create_user(
        db_session,
        UserCreate(email="timeline-other@example.com", password="password123", full_name="Other User"),
    )
    other_account = _create_account(db_session, other_user, "ACC-202")
    _create_setup(db_session, other_user, other_account)

    visible_events = list_trade_events(db_session, own_setup.id, created_user.id)

    assert len(visible_events) == 1
    assert all(event.user_id == created_user.id for event in visible_events)
    assert all(event.setup_id == own_setup.id for event in visible_events)
