from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_admin_user_from_cookie, get_current_user_from_cookie
from app.core.security import create_access_token
from app.models.user import User
from app.schemas.trading_account import TradingAccountCreate, TradingAccountUpdate
from app.schemas.user import UserCreate, UserUpdate
from app.services.admin_audit import log_admin_action
from app.services.app_settings import (
    get_app_settings_record,
    get_global_max_preview_drift_percent,
    get_global_scratch_manual_threshold_r,
    update_global_max_preview_drift_percent,
    update_global_scratch_manual_threshold_r,
)
from app.services.risk_management import RiskManagementService
from app.services.trade_setup_outcomes import TradeSetupOutcomeService
from app.services.trading_accounts import (
    DuplicateTradingAccountError,
    create_trading_account,
    delete_trading_account,
    get_trading_account,
    list_trading_accounts,
    update_trading_account,
)
from app.services.users import create_user, get_user, list_users, set_user_password, update_user


router = APIRouter(prefix="/admin", tags=["admin"])
templates = Jinja2Templates(directory="app/templates")


def _render_admin_user_detail_page(
    request: Request,
    *,
    current_user: User,
    admin_user: User,
    target: User,
    accounts: list,
    daily_risk_state: object,
    message: str | None = None,
    error: str | None = None,
    create_account_form: dict[str, object] | None = None,
    edit_account_forms: dict[int, dict[str, object]] | None = None,
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
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
            "message": message,
            "error": error,
            "create_account_form": create_account_form or {},
            "edit_account_forms": edit_account_forms or {},
        },
        status_code=status_code,
    )


@router.get("", response_class=HTMLResponse)
def admin_home() -> RedirectResponse:
    return RedirectResponse(url="/admin/users", status_code=status.HTTP_302_FOUND)


@router.get("/settings", response_class=HTMLResponse)
def admin_settings_page(
    request: Request,
    message: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie),
    admin_user: User = Depends(get_current_admin_user_from_cookie),
) -> HTMLResponse:
    record = get_app_settings_record(db)
    return templates.TemplateResponse(
        request,
        "admin_settings.html",
        {
            "request": request,
            "user": current_user,
            "admin_user": admin_user,
            "settings_record": record,
            "effective_max_preview_drift_percent": get_global_max_preview_drift_percent(db),
            "effective_scratch_manual_threshold_r": get_global_scratch_manual_threshold_r(db),
            "message": message,
            "error": error,
        },
    )


@router.post("/settings")
def admin_update_settings(
    db: Session = Depends(get_db),
    admin_user: User = Depends(get_current_admin_user_from_cookie),
    max_preview_drift_percent: float = Form(...),
    scratch_manual_threshold_r: float = Form(...),
) -> RedirectResponse:
    update_global_max_preview_drift_percent(db, max_preview_drift_percent)
    update_global_scratch_manual_threshold_r(db, scratch_manual_threshold_r)
    log_admin_action(
        db,
        admin_user_id=admin_user.id,
        target_user_id=None,
        action="settings_updated",
        message=(
            f"Updated global max preview drift percent to {max_preview_drift_percent:.2f}% "
            f"and scratch manual threshold to {scratch_manual_threshold_r:.2f}R."
        ),
    )
    db.commit()
    return RedirectResponse(url="/admin/settings", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/setup-outcomes/recalculate")
def admin_recalculate_setup_outcomes(
    db: Session = Depends(get_db),
    admin_user: User = Depends(get_current_admin_user_from_cookie),
    trading_account_id: str = Form(""),
) -> RedirectResponse:
    try:
        parsed_account_id = int(trading_account_id) if trading_account_id.strip() else None
    except ValueError as exc:
        return RedirectResponse(
            url="/admin/settings?error=Trading+account+id+must+be+a+number.",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    result = TradeSetupOutcomeService(db).backfill_setup_outcomes_from_stored_data(
        trading_account_id=parsed_account_id,
    )
    scope_label = f"account {parsed_account_id}" if parsed_account_id is not None else "all accounts"
    log_admin_action(
        db,
        admin_user_id=admin_user.id,
        target_user_id=None,
        action="setup_outcomes_recalculated",
        message=(
            f"Recalculated setup outcomes for {scope_label}. "
            f"Examined {result['examined']} setups, updated {result['updated']}."
        ),
    )
    db.commit()
    return RedirectResponse(
        url=(
            "/admin/settings?"
            f"message=Recalculated+setup+outcomes+for+{scope_label.replace(' ', '+')}."
            f"+Examined+{result['examined']}+setups,+updated+{result['updated']}."
        ),
        status_code=status.HTTP_303_SEE_OTHER,
    )


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
    return _render_admin_user_detail_page(
        request,
        current_user=current_user,
        admin_user=admin_user,
        target=target,
        accounts=accounts,
        daily_risk_state=daily_risk_state,
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
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie),
    admin_user: User = Depends(get_current_admin_user_from_cookie),
    broker_name: str = Form(...),
    account_number: str = Form(...),
    server_name: str = Form(...),
    platform: str = Form("mt5"),
    terminal_path: str = Form(""),
    password: str = Form(...),
    telegram_enabled: bool = Form(False),
    telegram_chat_id: str = Form(""),
    telegram_bot_token: str = Form(""),
    max_total_setup_volume: str = Form(""),
    max_preview_drift_percent_override: str = Form(""),
) -> Response:
    target = get_user(db, user_id)
    if not target:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    form_data = {
        "broker_name": broker_name,
        "account_number": account_number,
        "server_name": server_name,
        "platform": platform,
        "terminal_path": terminal_path,
        "telegram_enabled": telegram_enabled,
        "telegram_chat_id": telegram_chat_id,
        "max_total_setup_volume": max_total_setup_volume,
        "max_preview_drift_percent_override": max_preview_drift_percent_override,
    }
    try:
        account = create_trading_account(
            db,
            target.id,
            TradingAccountCreate(
                broker_name=broker_name,
                account_number=account_number,
                server_name=server_name,
                platform=platform,
                terminal_path=terminal_path or None,
                telegram_enabled=telegram_enabled,
                telegram_chat_id=telegram_chat_id or None,
                telegram_bot_token=telegram_bot_token or None,
                max_total_setup_volume=float(max_total_setup_volume) if max_total_setup_volume else None,
                max_preview_drift_percent_override=float(max_preview_drift_percent_override) if max_preview_drift_percent_override else None,
                password=password,
            ),
        )
    except (ValidationError, DuplicateTradingAccountError, ValueError) as exc:
        return _render_admin_user_detail_page(
            request,
            current_user=current_user,
            admin_user=admin_user,
            target=target,
            accounts=list_trading_accounts(db, target.id),
            daily_risk_state=RiskManagementService(db).get_daily_state(target.id),
            error=exc.errors()[0]["msg"] if isinstance(exc, ValidationError) else str(exc),
            create_account_form=form_data,
            status_code=status.HTTP_409_CONFLICT if isinstance(exc, DuplicateTradingAccountError) else status.HTTP_400_BAD_REQUEST,
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
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie),
    admin_user: User = Depends(get_current_admin_user_from_cookie),
    broker_name: str = Form(...),
    account_number: str = Form(...),
    server_name: str = Form(...),
    platform: str = Form("mt5"),
    terminal_path: str = Form(""),
    password: str = Form(""),
    telegram_enabled: bool = Form(False),
    telegram_chat_id: str = Form(""),
    telegram_bot_token: str = Form(""),
    max_total_setup_volume: str = Form(""),
    max_preview_drift_percent_override: str = Form(""),
) -> Response:
    target = get_user(db, user_id)
    if not target:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    existing_account = get_trading_account(db, account_id, user_id)
    if not existing_account:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trading account not found")

    edit_form = {
        "broker_name": broker_name,
        "account_number": account_number,
        "server_name": server_name,
        "platform": platform,
        "terminal_path": terminal_path,
        "telegram_enabled": telegram_enabled,
        "telegram_chat_id": telegram_chat_id,
        "max_total_setup_volume": max_total_setup_volume,
        "max_preview_drift_percent_override": max_preview_drift_percent_override,
    }
    try:
        payload_kwargs = {
            "broker_name": broker_name,
            "account_number": account_number,
            "server_name": server_name,
            "platform": platform,
            "terminal_path": terminal_path or None,
            "telegram_enabled": telegram_enabled,
            "telegram_chat_id": telegram_chat_id or None,
            "max_total_setup_volume": float(max_total_setup_volume) if max_total_setup_volume else None,
            "max_preview_drift_percent_override": float(max_preview_drift_percent_override) if max_preview_drift_percent_override else None,
        }
        if password:
            payload_kwargs["password"] = password
        if telegram_bot_token:
            payload_kwargs["telegram_bot_token"] = telegram_bot_token
        payload = TradingAccountUpdate(**payload_kwargs)
        account = update_trading_account(db, account_id, user_id, payload)
    except (ValidationError, DuplicateTradingAccountError, ValueError) as exc:
        edit_account_forms = {account_id: edit_form}
        return _render_admin_user_detail_page(
            request,
            current_user=current_user,
            admin_user=admin_user,
            target=target,
            accounts=list_trading_accounts(db, target.id),
            daily_risk_state=RiskManagementService(db).get_daily_state(target.id),
            error=exc.errors()[0]["msg"] if isinstance(exc, ValidationError) else str(exc),
            edit_account_forms=edit_account_forms,
            status_code=status.HTTP_409_CONFLICT if isinstance(exc, DuplicateTradingAccountError) else status.HTTP_400_BAD_REQUEST,
        )
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
