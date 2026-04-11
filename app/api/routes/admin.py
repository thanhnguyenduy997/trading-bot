from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_admin_user_from_cookie, get_current_user_from_cookie
from app.core.security import create_access_token
from app.models.user import User
from app.schemas.trading_account import TradingAccountCreate, TradingAccountUpdate
from app.schemas.user import UserCreate, UserUpdate
from app.services.admin_audit import log_admin_action
from app.services.risk_management import RiskManagementService
from app.services.trading_accounts import (
    create_trading_account,
    delete_trading_account,
    get_trading_account,
    list_trading_accounts,
    update_trading_account,
)
from app.services.users import create_user, get_user, list_users, set_user_password, update_user


router = APIRouter(prefix="/admin", tags=["admin"])
templates = Jinja2Templates(directory="app/templates")


@router.get("", response_class=HTMLResponse)
def admin_home() -> RedirectResponse:
    return RedirectResponse(url="/admin/users", status_code=status.HTTP_302_FOUND)


@router.get("/users", response_class=HTMLResponse)
def admin_user_list_page(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie),
    admin_user: User = Depends(get_current_admin_user_from_cookie),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "admin_users.html",
        {"request": request, "user": current_user, "admin_user": admin_user, "users": list_users(db)},
    )


@router.get("/users/new", response_class=HTMLResponse)
def admin_create_user_page(
    request: Request,
    current_user: User = Depends(get_current_user_from_cookie),
    admin_user: User = Depends(get_current_admin_user_from_cookie),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "admin_user_form.html",
        {"request": request, "user": current_user, "admin_user": admin_user},
    )


@router.post("/users/new")
def admin_create_user(
    db: Session = Depends(get_db),
    admin_user: User = Depends(get_current_admin_user_from_cookie),
    email: str = Form(...),
    full_name: str = Form(""),
    role: str = Form("user"),
    password: str = Form(...),
    is_active: bool = Form(False),
) -> RedirectResponse:
    created = create_user(
        db,
        UserCreate(
            email=email,
            full_name=full_name or None,
            role=role,
            password=password,
            is_active=is_active,
        ),
    )
    log_admin_action(
        db,
        admin_user_id=admin_user.id,
        target_user_id=created.id,
        action="user_created",
        message=f"Created user {created.email}.",
    )
    db.commit()
    return RedirectResponse(url=f"/admin/users/{created.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/users/{user_id}", response_class=HTMLResponse)
def admin_user_detail_page(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie),
    admin_user: User = Depends(get_current_admin_user_from_cookie),
) -> HTMLResponse:
    target = get_user(db, user_id)
    if not target:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    accounts = list_trading_accounts(db, target.id)
    daily_risk_state = RiskManagementService(db).get_daily_state(target.id)
    return templates.TemplateResponse(
        request,
        "admin_user_detail.html",
        {
            "request": request,
            "user": current_user,
            "admin_user": admin_user,
            "target": target,
            "accounts": accounts,
            "daily_risk_state": daily_risk_state,
        },
    )


@router.post("/users/{user_id}/edit")
def admin_update_user(
    user_id: int,
    db: Session = Depends(get_db),
    admin_user: User = Depends(get_current_admin_user_from_cookie),
    email: str = Form(...),
    full_name: str = Form(""),
    role: str = Form("user"),
    is_active: bool = Form(False),
) -> RedirectResponse:
    target = update_user(
        db,
        user_id,
        UserUpdate(email=email, full_name=full_name or None, role=role, is_active=is_active),
    )
    if not target:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    log_admin_action(
        db,
        admin_user_id=admin_user.id,
        target_user_id=target.id,
        action="user_updated" if target.is_active else "user_disabled",
        message=f"Updated user {target.email}.",
    )
    db.commit()
    return RedirectResponse(url=f"/admin/users/{target.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/users/{user_id}/password")
def admin_reset_password(
    user_id: int,
    db: Session = Depends(get_db),
    admin_user: User = Depends(get_current_admin_user_from_cookie),
    password: str = Form(...),
) -> RedirectResponse:
    target = set_user_password(db, user_id, password)
    if not target:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    log_admin_action(
        db,
        admin_user_id=admin_user.id,
        target_user_id=target.id,
        action="password_reset",
        message=f"Password reset for {target.email}.",
    )
    db.commit()
    return RedirectResponse(url=f"/admin/users/{target.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/users/{user_id}/impersonate")
def admin_start_impersonation(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie),
    admin_user: User = Depends(get_current_admin_user_from_cookie),
) -> RedirectResponse:
    target = get_user(db, user_id)
    if not target or not target.is_active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Active user not found")
    if target.id == admin_user.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot impersonate yourself")
    response = RedirectResponse(url="/dashboard", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie("access_token", create_access_token(str(target.id)), httponly=True, samesite="lax", secure=False)
    if not getattr(current_user, "impersonator", None):
        response.set_cookie(
            "admin_access_token",
            create_access_token(str(admin_user.id)),
            httponly=True,
            samesite="lax",
            secure=False,
        )
    log_admin_action(
        db,
        admin_user_id=admin_user.id,
        target_user_id=target.id,
        action="impersonation_started",
        message=f"Admin {admin_user.email} started impersonating {target.email}.",
    )
    db.commit()
    return response


@router.post("/impersonation/stop")
def admin_stop_impersonation(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie),
    admin_user: User = Depends(get_current_admin_user_from_cookie),
) -> RedirectResponse:
    response = RedirectResponse(url="/admin/users", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie("access_token", create_access_token(str(admin_user.id)), httponly=True, samesite="lax", secure=False)
    response.delete_cookie("admin_access_token")
    log_admin_action(
        db,
        admin_user_id=admin_user.id,
        target_user_id=current_user.id,
        action="impersonation_stopped",
        message=f"Admin {admin_user.email} stopped impersonating {current_user.email}.",
    )
    db.commit()
    return response


@router.post("/users/{user_id}/trading-accounts")
def admin_create_trading_account(
    user_id: int,
    db: Session = Depends(get_db),
    admin_user: User = Depends(get_current_admin_user_from_cookie),
    broker_name: str = Form(...),
    account_number: str = Form(...),
    server_name: str = Form(...),
    terminal_path: str = Form(""),
    password: str = Form(...),
    max_total_setup_volume: str = Form(""),
) -> RedirectResponse:
    target = get_user(db, user_id)
    if not target:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    account = create_trading_account(
        db,
        target.id,
        TradingAccountCreate(
            broker_name=broker_name,
            account_number=account_number,
            server_name=server_name,
            terminal_path=terminal_path or None,
            max_total_setup_volume=float(max_total_setup_volume) if max_total_setup_volume else None,
            password=password,
        ),
    )
    log_admin_action(
        db,
        admin_user_id=admin_user.id,
        target_user_id=target.id,
        action="trading_account_created",
        message=f"Created trading account {account.account_number}.",
    )
    db.commit()
    return RedirectResponse(url=f"/admin/users/{target.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/users/{user_id}/trading-accounts/{account_id}/edit")
def admin_update_trading_account(
    user_id: int,
    account_id: int,
    db: Session = Depends(get_db),
    admin_user: User = Depends(get_current_admin_user_from_cookie),
    broker_name: str = Form(...),
    account_number: str = Form(...),
    server_name: str = Form(...),
    terminal_path: str = Form(""),
    password: str = Form(""),
    max_total_setup_volume: str = Form(""),
) -> RedirectResponse:
    payload = TradingAccountUpdate(
        broker_name=broker_name,
        account_number=account_number,
        server_name=server_name,
        terminal_path=terminal_path or None,
        max_total_setup_volume=float(max_total_setup_volume) if max_total_setup_volume else None,
        **({"password": password} if password else {}),
    )
    account = update_trading_account(db, account_id, user_id, payload)
    if not account:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trading account not found")
    log_admin_action(
        db,
        admin_user_id=admin_user.id,
        target_user_id=user_id,
        action="trading_account_updated",
        message=f"Updated trading account {account.account_number}.",
    )
    db.commit()
    return RedirectResponse(url=f"/admin/users/{user_id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/users/{user_id}/trading-accounts/{account_id}/delete")
def admin_delete_trading_account(
    user_id: int,
    account_id: int,
    db: Session = Depends(get_db),
    admin_user: User = Depends(get_current_admin_user_from_cookie),
) -> RedirectResponse:
    account = get_trading_account(db, account_id, user_id)
    deleted = delete_trading_account(db, account_id, user_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trading account not found")
    log_admin_action(
        db,
        admin_user_id=admin_user.id,
        target_user_id=user_id,
        action="trading_account_updated",
        message=f"Deleted trading account {account.account_number if account else account_id}.",
    )
    db.commit()
    return RedirectResponse(url=f"/admin/users/{user_id}", status_code=status.HTTP_303_SEE_OTHER)
