from app.models.trade_setup import TradeSetup
from app.schemas.trade_setup import TradeSetupCreate
from sqlalchemy.orm import Session

from app.services.trade_events import create_trade_event
from app.services.trading_accounts import get_trading_account


def create_trade_setup(db: Session, user_id: int, payload: TradeSetupCreate) -> TradeSetup:
    account = get_trading_account(db, payload.trading_account_id, user_id)
    if not account:
        raise LookupError("Trading account not found")

    setup = TradeSetup(user_id=user_id, **payload.model_dump())
    db.add(setup)
    db.flush()
    create_trade_event(db, user_id, setup.id, "setup_saved", "Trade setup saved as draft.")
    db.commit()
    db.refresh(setup)
    return setup


def update_draft_trade_setup(db: Session, setup_id: int, user_id: int, payload: TradeSetupCreate) -> TradeSetup:
    account = get_trading_account(db, payload.trading_account_id, user_id)
    if not account:
        raise LookupError("Trading account not found")

    setup = get_trade_setup(db, setup_id, user_id)
    if not setup:
        raise LookupError("Trade setup not found")
    if setup.status != "draft":
        raise ValueError("Only draft trade setups can be updated from preview.")

    for field, value in payload.model_dump().items():
        setattr(setup, field, value)
    setup.execution_error = None
    setup.execution_details = None
    setup.monitoring_status = None
    setup.order2_be_moved_at = None
    setup.order2_be_move_error = None
    db.add(setup)
    db.flush()
    create_trade_event(db, user_id, setup.id, "setup_saved", "Trade setup draft updated from preview.")
    db.commit()
    db.refresh(setup)
    return setup


def list_trade_setups(db: Session, user_id: int) -> list[TradeSetup]:
    return (
        db.query(TradeSetup)
        .filter(TradeSetup.user_id == user_id)
        .order_by(TradeSetup.created_at.desc(), TradeSetup.id.desc())
        .all()
    )


def get_trade_setup(db: Session, setup_id: int, user_id: int) -> TradeSetup | None:
    return (
        db.query(TradeSetup)
        .filter(TradeSetup.id == setup_id, TradeSetup.user_id == user_id)
        .first()
    )
