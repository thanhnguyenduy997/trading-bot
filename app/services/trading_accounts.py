from sqlalchemy.orm import Session

from app.core.crypto import encrypt_value
from app.models.trading_account import TradingAccount
from app.schemas.trading_account import TradingAccountCreate, TradingAccountUpdate


def list_trading_accounts(db: Session, user_id: int) -> list[TradingAccount]:
    return (
        db.query(TradingAccount)
        .filter(TradingAccount.user_id == user_id)
        .order_by(TradingAccount.created_at.desc())
        .all()
    )


def get_trading_account(db: Session, account_id: int, user_id: int) -> TradingAccount | None:
    return (
        db.query(TradingAccount)
        .filter(TradingAccount.id == account_id, TradingAccount.user_id == user_id)
        .first()
    )


def create_trading_account(db: Session, user_id: int, payload: TradingAccountCreate) -> TradingAccount:
    account = TradingAccount(
        user_id=user_id,
        broker_name=payload.broker_name,
        account_number=payload.account_number,
        server_name=payload.server_name,
        password_encrypted=encrypt_value(payload.password),
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


def update_trading_account(
    db: Session,
    account_id: int,
    user_id: int,
    payload: TradingAccountUpdate,
) -> TradingAccount | None:
    account = get_trading_account(db, account_id, user_id)
    if not account:
        return None

    updates = payload.model_dump(exclude_unset=True)
    password = updates.pop("password", None)
    for field, value in updates.items():
        setattr(account, field, value)
    if password is not None:
        account.password_encrypted = encrypt_value(password)

    db.add(account)
    db.commit()
    db.refresh(account)
    return account


def delete_trading_account(db: Session, account_id: int, user_id: int) -> bool:
    account = get_trading_account(db, account_id, user_id)
    if not account:
        return False
    db.delete(account)
    db.commit()
    return True
