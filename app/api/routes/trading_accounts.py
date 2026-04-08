from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User
from app.schemas.trading_account import (
    TradingAccountCreate,
    TradingAccountRead,
    TradingAccountUpdate,
)
from app.services.trading_accounts import (
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
    account = create_trading_account(db, current_user.id, payload)
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
    account = update_trading_account(db, account_id, current_user.id, payload)
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
