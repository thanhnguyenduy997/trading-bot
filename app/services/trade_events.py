from sqlalchemy.orm import Session

from app.models.trade_event import TradeEvent


def create_trade_event(
    db: Session,
    user_id: int,
    setup_id: int,
    event_type: str,
    message: str | None = None,
) -> TradeEvent:
    event = TradeEvent(
        user_id=user_id,
        setup_id=setup_id,
        event_type=event_type,
        message=message,
    )
    db.add(event)
    db.flush()
    return event


def list_trade_events(db: Session, setup_id: int, user_id: int, limit: int = 10) -> list[TradeEvent]:
    return (
        db.query(TradeEvent)
        .filter(TradeEvent.setup_id == setup_id, TradeEvent.user_id == user_id)
        .order_by(TradeEvent.created_at.desc(), TradeEvent.id.desc())
        .limit(limit)
        .all()
    )
