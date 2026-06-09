from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.crypto import encrypt_value
from app.models.trading_account import TradingAccount
from app.models.trading_account_symbol import TradingAccountSymbol
from app.schemas.trading_account import TradingAccountCreate, TradingAccountUpdate


DUPLICATE_TRADING_ACCOUNT_MESSAGE = "This trading account already exists. Please edit the existing account instead."
INVALID_DEFAULT_SYMBOL_MESSAGE = "Default symbol must be one of this account's synced MT5 Market Watch symbols."


class DuplicateTradingAccountError(ValueError):
    def __init__(self, message: str = DUPLICATE_TRADING_ACCOUNT_MESSAGE) -> None:
        super().__init__(message)


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
    if _account_number_exists(db, user_id=user_id, account_number=payload.account_number):
        raise DuplicateTradingAccountError()
    if payload.default_symbol:
        raise ValueError(INVALID_DEFAULT_SYMBOL_MESSAGE)

    account = TradingAccount(
        user_id=user_id,
        broker_name=payload.broker_name,
        account_number=payload.account_number,
        server_name=payload.server_name,
        password_encrypted=encrypt_value(payload.password),
        platform=payload.platform,
        terminal_path=payload.terminal_path,
        telegram_enabled=payload.telegram_enabled,
        telegram_chat_id=payload.telegram_chat_id,
        telegram_bot_token_encrypted=encrypt_value(payload.telegram_bot_token) if payload.telegram_bot_token else None,
        max_total_setup_volume=payload.max_total_setup_volume,
        default_symbol=payload.default_symbol,
        default_side=payload.default_side,
        default_setup_mode=payload.default_setup_mode,
        default_risk_mode=payload.default_risk_mode,
        default_risk_value=payload.default_risk_value,
        default_rr_order_2=payload.default_rr_order_2,
        max_preview_drift_percent_override=payload.max_preview_drift_percent_override,
    )
    db.add(account)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if _is_duplicate_account_error(exc):
            raise DuplicateTradingAccountError() from exc
        raise
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
    if "account_number" in updates and _account_number_exists(
        db,
        user_id=user_id,
        account_number=updates["account_number"],
        exclude_account_id=account_id,
    ):
        raise DuplicateTradingAccountError()
    if "default_symbol" in updates and updates["default_symbol"] is not None:
        if not _account_symbol_exists(db, account_id=account_id, symbol=updates["default_symbol"]):
            raise ValueError(INVALID_DEFAULT_SYMBOL_MESSAGE)

    password = updates.pop("password", None)
    telegram_bot_token = updates.pop("telegram_bot_token", None)
    for field, value in updates.items():
        setattr(account, field, value)
    if password is not None:
        account.password_encrypted = encrypt_value(password)
    if telegram_bot_token is not None:
        account.telegram_bot_token_encrypted = encrypt_value(telegram_bot_token)

    db.add(account)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if _is_duplicate_account_error(exc):
            raise DuplicateTradingAccountError() from exc
        raise
    db.refresh(account)
    return account


def delete_trading_account(db: Session, account_id: int, user_id: int) -> bool:
    account = get_trading_account(db, account_id, user_id)
    if not account:
        return False
    db.delete(account)
    db.commit()
    return True


def _account_number_exists(
    db: Session,
    *,
    user_id: int,
    account_number: str,
    exclude_account_id: int | None = None,
) -> bool:
    query = db.query(TradingAccount).filter(
        TradingAccount.user_id == user_id,
        TradingAccount.account_number == account_number,
    )
    if exclude_account_id is not None:
        query = query.filter(TradingAccount.id != exclude_account_id)
    return db.query(query.exists()).scalar()


def _is_duplicate_account_error(exc: IntegrityError) -> bool:
    message = str(exc.orig).lower() if exc.orig is not None else str(exc).lower()
    return (
        "uq_user_account_number" in message
        or "trading_accounts.user_id, trading_accounts.account_number" in message
        or "user_id, account_number" in message
    )


def _account_symbol_exists(db: Session, *, account_id: int, symbol: str) -> bool:
    return (
        db.query(TradingAccountSymbol)
        .filter(
            TradingAccountSymbol.trading_account_id == account_id,
            TradingAccountSymbol.symbol_name == symbol.upper(),
        )
        .first()
        is not None
    )
