from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User
from app.execution.base import AdapterError
from app.schemas.trading_account import (
    TradingAccountCreate,
    TradingAccountConnectionTestRead,
    TradingAccountQuoteRead,
    TradingAccountRead,
    TradingAccountTelegramTestRead,
    TradingAccountUpdate,
)
from app.services.account_symbols import TradingAccountSymbolService
from app.services.execution import TradingAccountExecutionService
from app.services.notifications import send_trading_account_test_notification
from app.services.trading_accounts import (
    DuplicateTradingAccountError,
    create_trading_account,
    delete_trading_account,
    get_trading_account,
    list_trading_accounts,
    update_trading_account,
)


router = APIRouter(prefix="/api/trading-accounts", tags=["trading-accounts"])


@router.get("/", response_model=List[TradingAccountRead])
def read_trading_accounts(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[TradingAccountRead]:
    accounts = list_trading_accounts(db, current_user.id)
    return [TradingAccountRead.model_validate(account) for account in accounts]


@router.post("/", response_model=TradingAccountRead, status_code=status.HTTP_201_CREATED)
def create_trading_account_endpoint(
    payload: TradingAccountCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TradingAccountRead:
    try:
        account = create_trading_account(db, current_user.id, payload)
    except DuplicateTradingAccountError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return TradingAccountRead.model_validate(account)


@router.get("/{account_id}", response_model=TradingAccountRead)
def read_trading_account(
    account_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TradingAccountRead:
    account = get_trading_account(db, account_id, current_user.id)
    if not account:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trading account not found")
    return TradingAccountRead.model_validate(account)


@router.put("/{account_id}", response_model=TradingAccountRead)
def update_trading_account_endpoint(
    account_id: int,
    payload: TradingAccountUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TradingAccountRead:
    try:
        account = update_trading_account(db, account_id, current_user.id, payload)
    except DuplicateTradingAccountError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if not account:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trading account not found")
    return TradingAccountRead.model_validate(account)


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_trading_account_endpoint(
    account_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    deleted = delete_trading_account(db, account_id, current_user.id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trading account not found")


@router.post("/{account_id}/test-connection", response_model=TradingAccountConnectionTestRead)
def test_trading_account_connection(
    account_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TradingAccountConnectionTestRead:
    service = TradingAccountExecutionService(db)
    try:
        result = service.test_connection(account_id, current_user.id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if not result.success:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                **(result.error or {}),
                "connection_status": result.connection_status,
                "last_heartbeat_at": result.last_heartbeat_at.isoformat() if result.last_heartbeat_at else None,
                "last_error": result.last_error,
            },
        )
    return result


@router.get("/{account_id}/quote", response_model=TradingAccountQuoteRead)
def get_trading_account_quote(
    account_id: int,
    symbol: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TradingAccountQuoteRead:
    service = TradingAccountExecutionService(db)
    try:
        TradingAccountSymbolService(db).assert_symbol_synced(
            account_id=account_id,
            user_id=current_user.id,
            symbol=symbol,
        )
        return service.fetch_quote(account_id, current_user.id, symbol)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except AdapterError as exc:
        account = get_trading_account(db, account_id, current_user.id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                **exc.to_dict(),
                "connection_status": account.connection_status if account else "error",
                "last_heartbeat_at": account.last_heartbeat_at.isoformat() if account and account.last_heartbeat_at else None,
                "last_error": account.last_error if account else exc.message,
            },
        ) from exc


@router.post("/{account_id}/test-telegram", response_model=TradingAccountTelegramTestRead)
def test_trading_account_telegram(
    account_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TradingAccountTelegramTestRead:
    account = get_trading_account(db, account_id, current_user.id)
    if not account:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trading account not found")
    success, message = send_trading_account_test_notification(account)
    if not success:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=message)
    return TradingAccountTelegramTestRead(success=True, message=message)
