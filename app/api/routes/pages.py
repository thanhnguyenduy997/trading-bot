from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_user_from_cookie
from app.execution.base import AdapterError
from app.models.user import User
from app.schemas.trade_preview import TradePreviewRequest
from app.schemas.trade_setup import ManualTradeSetupCreate, TradeSetupCreate
from app.schemas.trading_account import TradingAccountCreate, TradingAccountUpdate
from app.services.account_symbols import (
    EMPTY_SYMBOL_MESSAGE,
    TradingAccountSymbolService,
)
from app.services.execution import TradingAccountExecutionService
from app.services.preview_service import PreviewService
from app.services.notifications import send_trading_account_test_notification
from app.services.mt5_trade_history import (
    DashboardAuthorizationError,
    DashboardFilters,
    DashboardService,
    MT5TradeHistorySyncService,
)
from app.services.manual_trade_setups import ManualTradeSetupService
from app.services.risk_management import RiskManagementService
from app.services.trade_events import list_trade_events
from app.services.trade_setup_execution import TradeSetupExecutionService
from app.services.trade_setup_monitoring import TradeSetupMonitoringService
from app.services.trade_setup_outcomes import TradeSetupOutcomeService
from app.services.trade_setups import create_trade_setup, get_trade_setup, list_trade_setups, update_draft_trade_setup
from app.services.trading_accounts import (
    DuplicateTradingAccountError,
    create_trading_account,
    get_trading_account,
    list_trading_accounts,
    update_trading_account,
)


router = APIRouter(tags=["pages"])
templates = Jinja2Templates(directory="app/templates")

INVALID_DEFAULT_SYMBOL_MESSAGE = (
    "Saved default symbol is no longer synced for this account. "
    "Using the first available synced symbol instead."
)
MISSING_DEFAULT_SYMBOL_MESSAGE = (
    "Saved default symbol is no longer synced for this account. "
    "No synced symbols are currently available."
)

TIME_RANGE_OPTIONS = (
    ("all_time", "All Time"),
    ("today", "Today"),
    ("yesterday", "Yesterday"),
    ("last_7_days", "Last 7 Days"),
    ("last_30_days", "Last 30 Days"),
    ("this_month", "This Month"),
    ("custom", "Custom Range"),
)
VALID_TIME_RANGE_KEYS = {value for value, _label in TIME_RANGE_OPTIONS}


def _render_trade_preview_page(
    request: Request,
    current_user: User,
    accounts: list,
    form_data: dict[str, object] | None = None,
    preview_defaults: dict[str, object] | None = None,
    preview: object | None = None,
    setup: object | None = None,
    error: str | None = None,
    symbols: list[dict[str, str]] | None = None,
    symbol_message: str | None = None,
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    initial_symbols = symbols or []
    resolved_preview_defaults = preview_defaults or {}
    return templates.TemplateResponse(
        request,
        "trade_preview.html",
        {
            "request": request,
            "user": current_user,
            "accounts": accounts,
            "form_data": form_data
            or {
                "symbol": default_symbol,
                "side": "buy",
                "risk_mode": "fixed_money",
                "risk_value": 100,
                "rr_order2": 2.0,
            },
            "preview": preview,
            "setup": setup,
            "error": error,
            "symbols": initial_symbols,
            "symbol_message": symbol_message,
            "preview_defaults": resolved_preview_defaults,
        },
        status_code=status_code,
    )


def _parse_dashboard_filters(
    *,
    range_key: str,
    start_date: str | None,
    end_date: str | None,
    account_id: str | None,
) -> DashboardFilters:
    normalized_range_key = range_key if range_key in VALID_TIME_RANGE_KEYS else "all_time"
    parsed_account_id = int(account_id) if account_id else None
    parsed_start = date.fromisoformat(start_date) if start_date else None
    parsed_end = date.fromisoformat(end_date) if end_date else None
    return DashboardFilters(
        range_key=normalized_range_key,
        start_date=parsed_start,
        end_date=parsed_end,
        trading_account_id=parsed_account_id,
    )


def _render_trade_setup_list_page(
    request: Request,
    current_user: User,
    setups: list,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "trade_setups.html",
        {"request": request, "user": current_user, "setups": setups},
    )


def _render_trade_setup_detail_page(
    request: Request,
    current_user: User,
    setup: object,
    events: list,
    message: str | None = None,
    error: str | None = None,
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "trade_setup_detail.html",
        {
            "request": request,
            "user": current_user,
            "setup": setup,
            "events": events,
            "message": message,
            "error": error,
        },
        status_code=status_code,
    )


def _render_manual_trade_setup_form_page(
    request: Request,
    current_user: User,
    accounts: list,
    *,
    form_action: str,
    heading: str,
    submit_label: str,
    form_data: dict[str, object] | None = None,
    setup: object | None = None,
    error: str | None = None,
    message: str | None = None,
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    values = form_data or {}
    return templates.TemplateResponse(
        request,
        "manual_trade_setup_form.html",
        {
            "request": request,
            "user": current_user,
            "accounts": accounts,
            "form_action": form_action,
            "heading": heading,
            "submit_label": submit_label,
            "form_data": values,
            "setup": setup,
            "error": error,
            "message": message,
        },
        status_code=status_code,
    )


def _render_trading_account_detail_page(
    request: Request,
    current_user: User,
    account: object,
    connection_result: object | None = None,
    quote_result: object | None = None,
    message: str | None = None,
    error: str | None = None,
    symbols: list[dict[str, str]] | None = None,
    symbol_message: str | None = None,
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "trading_account_detail.html",
        {
            "request": request,
            "user": current_user,
            "account": account,
            "connection_result": connection_result,
            "quote_result": quote_result,
            "message": message,
            "error": error,
            "symbols": symbols or [],
            "symbol_message": symbol_message,
        },
        status_code=status_code,
    )


def _render_trading_account_form_page(
    request: Request,
    current_user: User,
    *,
    form_action: str,
    heading: str,
    submit_label: str,
    form_data: dict[str, object] | None = None,
    account: object | None = None,
    error: str | None = None,
    message: str | None = None,
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    values = form_data or {}
    return templates.TemplateResponse(
        request,
        "trading_account_form.html",
        {
            "request": request,
            "user": current_user,
            "form_action": form_action,
            "heading": heading,
            "submit_label": submit_label,
            "form_data": values,
            "account": account,
            "error": error,
            "message": message,
        },
        status_code=status_code,
    )


def _load_synced_symbols(
    db: Session,
    current_user: User,
    account_id: int | None,
) -> tuple[list[dict[str, str]], str | None]:
    if account_id is None:
        return [], EMPTY_SYMBOL_MESSAGE
    try:
        service = TradingAccountSymbolService(db)
        symbols = service.list_synced_symbols(account_id=account_id, user_id=current_user.id)
        return symbols, service.get_symbol_message(account_id=account_id, user_id=current_user.id)
    except LookupError:
        return [], EMPTY_SYMBOL_MESSAGE


def _resolve_account_preview_defaults(
    account: object | None,
    symbols: list[dict[str, str]] | None,
    symbol_message: str | None,
) -> tuple[dict[str, object], str | None]:
    symbol_rows = symbols or []
    available_symbols = [item["symbol_name"] for item in symbol_rows]
    selected_symbol = ""
    resolved_message = symbol_message

    if getattr(account, "default_symbol", None) and account.default_symbol in available_symbols:
        selected_symbol = account.default_symbol
    elif available_symbols:
        selected_symbol = available_symbols[0]
        if getattr(account, "default_symbol", None):
            resolved_message = INVALID_DEFAULT_SYMBOL_MESSAGE
    elif getattr(account, "default_symbol", None):
        resolved_message = MISSING_DEFAULT_SYMBOL_MESSAGE

    defaults = {
        "trading_account_id": getattr(account, "id", None),
        "symbol": selected_symbol,
        "side": getattr(account, "default_side", None) or "buy",
        "risk_mode": getattr(account, "default_risk_mode", None) or "fixed_money",
        "risk_value": float(getattr(account, "default_risk_value", 100) or 100),
        "rr_order2": float(getattr(account, "default_rr_order_2", 2.0) or 2.0),
        "sl_price": "",
    }
    return defaults, resolved_message


def _get_preview_context(
    db: Session,
    current_user: User,
    account_id: int | None,
) -> tuple[object | None, list[dict[str, str]], str | None, dict[str, object]]:
    account = get_trading_account(db, account_id, current_user.id) if account_id else None
    symbols, symbol_message = _load_synced_symbols(db, current_user, account_id)
    defaults, resolved_message = _resolve_account_preview_defaults(account, symbols, symbol_message)
    return account, symbols, resolved_message, defaults


@router.get("/", response_class=HTMLResponse)
def root() -> RedirectResponse:
    return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)


@router.get("/login", response_class=HTMLResponse)
def login_page(
    request: Request,
    next: str | None = None,
    message: str | None = None,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "request": request,
            "next": next if next and next.startswith("/") and not next.startswith("//") else None,
            "message": message,
        },
    )


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard_page(
    request: Request,
    range_key: str = Query("all_time", alias="range"),
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
    account_id: str | None = Query(None),
    message: str | None = Query(None),
    error: str | None = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie),
) -> HTMLResponse:
    daily_risk_state = RiskManagementService(db).get_daily_state(current_user.id)
    service = DashboardService(db)
    try:
        filters = _parse_dashboard_filters(
            range_key=range_key,
            start_date=start_date,
            end_date=end_date,
            account_id=account_id,
        )
        selected_account = service.resolve_selected_account(
            actor=current_user,
            requested_account_id=filters.trading_account_id,
        )
        dashboard = (
            service.build_dashboard(actor=current_user, filters=filters, selected_account=selected_account)
            if selected_account is not None
            else {"filters": filters, "accounts": [], "selected_account": None}
        )
    except (ValueError, DashboardAuthorizationError) as exc:
        filters = DashboardFilters()
        selected_account = service.resolve_selected_account(actor=current_user, requested_account_id=None)
        dashboard = (
            service.build_dashboard(actor=current_user, filters=filters, selected_account=selected_account)
            if selected_account is not None
            else {"filters": filters, "accounts": [], "selected_account": None}
        )
        error = str(exc)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "request": request,
            "user": current_user,
            "daily_risk_state": daily_risk_state,
            "dashboard": dashboard,
            "time_range_options": TIME_RANGE_OPTIONS,
            "message": message,
            "error": error,
        },
    )


@router.post("/dashboard/sync", response_class=HTMLResponse)
def dashboard_sync_page(
    request: Request,
    range_key: str = Form("all_time", alias="range"),
    start_date: str = Form(""),
    end_date: str = Form(""),
    account_id: str = Form(""),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie),
) -> HTMLResponse:
    daily_risk_state = RiskManagementService(db).get_daily_state(current_user.id)
    message = None
    error = None
    service = DashboardService(db)
    try:
        filters = _parse_dashboard_filters(
            range_key=range_key,
            start_date=start_date or None,
            end_date=end_date or None,
            account_id=account_id or None,
        )
        selected_account = service.resolve_selected_account(
            actor=current_user,
            requested_account_id=filters.trading_account_id,
        )
        if selected_account is None:
            dashboard = {"filters": filters, "accounts": [], "selected_account": None}
        else:
            range_start, range_end = service.resolve_time_range(filters)
            result = MT5TradeHistorySyncService(db).sync_account_history(
                account_id=selected_account.id,
                user_id=current_user.id,
                range_start=range_start,
                range_end=range_end,
            )
            dashboard = service.build_dashboard(actor=current_user, filters=filters, selected_account=selected_account)
            message = (
                f"Synced {result['synced_count']} MT5 trades for account {selected_account.account_number}."
            )
    except (ValueError, DashboardAuthorizationError, LookupError, AdapterError) as exc:
        filters = DashboardFilters()
        selected_account = service.resolve_selected_account(actor=current_user, requested_account_id=None)
        dashboard = (
            service.build_dashboard(actor=current_user, filters=filters, selected_account=selected_account)
            if selected_account is not None
            else {"filters": filters, "accounts": [], "selected_account": None}
        )
        error = str(exc)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "request": request,
            "user": current_user,
            "daily_risk_state": daily_risk_state,
            "dashboard": dashboard,
            "time_range_options": TIME_RANGE_OPTIONS,
            "message": message,
            "error": error,
        },
    )


@router.get("/trade-setups/preview", response_class=HTMLResponse)
def trade_preview_page(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie),
) -> HTMLResponse:
    accounts = list_trading_accounts(db, current_user.id)
    selected_account_id = accounts[0].id if accounts else None
    account, symbols, symbol_message, defaults = _get_preview_context(db, current_user, selected_account_id)
    return _render_trade_preview_page(
        request,
        current_user,
        accounts,
        form_data=defaults if account else None,
        preview_defaults=defaults if account else None,
        symbols=symbols,
        symbol_message=symbol_message,
    )


@router.post("/trade-setups/preview", response_class=HTMLResponse)
def trade_preview_page_submit(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie),
    trading_account_id: int = Form(...),
    symbol: str = Form(...),
    side: str = Form(...),
    sl_price: float = Form(...),
    risk_mode: str = Form(...),
    risk_value: float = Form(...),
    rr_order2: float = Form(...),
    draft_setup_id: int | None = Form(None),
) -> HTMLResponse:
    accounts = list_trading_accounts(db, current_user.id)
    form_data = {
        "trading_account_id": trading_account_id,
        "symbol": symbol,
        "side": side,
        "sl_price": sl_price,
        "risk_mode": risk_mode,
        "risk_value": risk_value,
        "rr_order2": rr_order2,
        "draft_setup_id": draft_setup_id,
    }

    _, symbols, symbol_message, preview_defaults = _get_preview_context(db, current_user, trading_account_id)

    try:
        payload = TradePreviewRequest(**form_data)
        preview = PreviewService(db).build_preview(current_user.id, payload)
    except ValidationError as exc:
        return _render_trade_preview_page(
            request,
            current_user,
            accounts,
            form_data=form_data,
            preview_defaults=preview_defaults,
            error=exc.errors()[0]["msg"],
            symbols=symbols,
            symbol_message=symbol_message,
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    except (LookupError, ValueError) as exc:
        return _render_trade_preview_page(
            request,
            current_user,
            accounts,
            form_data=form_data,
            preview_defaults=preview_defaults,
            error=str(exc),
            symbols=symbols,
            symbol_message=symbol_message,
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    setup_payload = TradeSetupCreate(
        trading_account_id=trading_account_id,
        symbol=preview.symbol,
        side=preview.side,
        sl_price=preview.sl_price,
        risk_mode=risk_mode,
        risk_value=risk_value,
        rr_order2=rr_order2,
        estimated_entry=preview.estimated_entry,
        r_value=preview.r_value,
        tp1_price=preview.tp1_price,
        tp2_price=preview.tp2_price,
        total_risk_money=preview.total_risk_money,
        risk_per_order=preview.risk_per_order,
        order1_volume=preview.order1_volume,
        order2_volume=preview.order2_volume,
        status="draft",
    )
    try:
        if draft_setup_id:
            setup = update_draft_trade_setup(db, draft_setup_id, current_user.id, setup_payload)
        else:
            setup = create_trade_setup(db, current_user.id, setup_payload)
    except (LookupError, ValueError) as exc:
        return _render_trade_preview_page(
            request,
            current_user,
            accounts,
            form_data=form_data,
            preview_defaults=preview_defaults,
            preview=preview,
            error=str(exc),
            symbols=symbols,
            symbol_message=symbol_message,
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    form_data["draft_setup_id"] = setup.id
    return _render_trade_preview_page(
        request,
        current_user,
        accounts,
        form_data=form_data,
        preview_defaults=preview_defaults,
        preview=preview,
        setup=setup,
        symbols=symbols,
        symbol_message=symbol_message,
    )


@router.post("/trade-setups/save")
def save_trade_setup_from_preview(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie),
    trading_account_id: int = Form(...),
    symbol: str = Form(...),
    side: str = Form(...),
    sl_price: float = Form(...),
    risk_mode: str = Form(...),
    risk_value: float = Form(...),
    rr_order2: float = Form(...),
    estimated_entry: float = Form(...),
    r_value: float = Form(...),
    tp1_price: float = Form(...),
    tp2_price: float = Form(...),
    total_risk_money: float = Form(...),
    risk_per_order: float = Form(...),
    order1_volume: float = Form(...),
    order2_volume: float = Form(...),
) -> RedirectResponse:
    payload = TradeSetupCreate(
        trading_account_id=trading_account_id,
        symbol=symbol,
        side=side,
        sl_price=sl_price,
        risk_mode=risk_mode,
        risk_value=risk_value,
        rr_order2=rr_order2,
        estimated_entry=estimated_entry,
        r_value=r_value,
        tp1_price=tp1_price,
        tp2_price=tp2_price,
        total_risk_money=total_risk_money,
        risk_per_order=risk_per_order,
        order1_volume=order1_volume,
        order2_volume=order2_volume,
        status="draft",
    )
    try:
        setup = create_trade_setup(db, current_user.id, payload)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return RedirectResponse(url=f"/trade-setups/{setup.id}", status_code=status.HTTP_303_SEE_OTHER)


def _manual_setup_form_defaults(accounts: list, setup: object | None = None) -> dict[str, object]:
    if setup is not None:
        return {
            "trading_account_id": setup.trading_account_id,
            "symbol": setup.symbol,
            "side": setup.side,
            "estimated_entry": float(setup.estimated_entry),
            "sl_price": float(setup.sl_price),
            "total_risk_money": float(setup.total_risk_money),
            "rr_order2": float(setup.rr_order2),
            "tp1_price": float(setup.tp1_price),
            "tp2_price": float(setup.tp2_price),
            "order_count": setup.order_count,
            "order1_ticket": setup.order1_ticket or "",
            "order2_ticket": setup.order2_ticket or "",
        }
    return {
        "trading_account_id": accounts[0].id if accounts else None,
        "symbol": "",
        "side": "buy",
        "estimated_entry": "",
        "sl_price": "",
        "total_risk_money": "",
        "rr_order2": 2.0,
        "tp1_price": "",
        "tp2_price": "",
        "order_count": 2,
        "order1_ticket": "",
        "order2_ticket": "",
    }


@router.get("/trade-setups/manual/create", response_class=HTMLResponse)
def create_manual_trade_setup_page(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie),
) -> HTMLResponse:
    accounts = list_trading_accounts(db, current_user.id)
    return _render_manual_trade_setup_form_page(
        request,
        current_user,
        accounts,
        form_action="/trade-setups/manual/create",
        heading="Create Manual Setup",
        submit_label="Register Manual Setup",
        form_data=_manual_setup_form_defaults(accounts),
    )


@router.post("/trade-setups/manual/create", response_class=HTMLResponse)
def create_manual_trade_setup_page_submit(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie),
    trading_account_id: int = Form(...),
    symbol: str = Form(...),
    side: str = Form(...),
    estimated_entry: float = Form(...),
    sl_price: float = Form(...),
    total_risk_money: float = Form(...),
    rr_order2: float = Form(...),
    tp1_price: str = Form(""),
    tp2_price: str = Form(""),
    order_count: int = Form(...),
    order1_ticket: int = Form(...),
    order2_ticket: str = Form(""),
) -> Response:
    accounts = list_trading_accounts(db, current_user.id)
    form_data = {
        "trading_account_id": trading_account_id,
        "symbol": symbol,
        "side": side,
        "estimated_entry": estimated_entry,
        "sl_price": sl_price,
        "total_risk_money": total_risk_money,
        "rr_order2": rr_order2,
        "tp1_price": tp1_price,
        "tp2_price": tp2_price,
        "order_count": order_count,
        "order1_ticket": order1_ticket,
        "order2_ticket": order2_ticket,
    }
    try:
        payload = ManualTradeSetupCreate(
            trading_account_id=trading_account_id,
            symbol=symbol,
            side=side,
            estimated_entry=estimated_entry,
            sl_price=sl_price,
            total_risk_money=total_risk_money,
            rr_order2=rr_order2,
            tp1_price=float(tp1_price) if tp1_price else None,
            tp2_price=float(tp2_price) if tp2_price else None,
            order_count=order_count,
            order1_ticket=order1_ticket,
            order2_ticket=int(order2_ticket) if order2_ticket else None,
        )
        setup = ManualTradeSetupService(db).create_manual_setup(user_id=current_user.id, payload=payload)
        result = TradeSetupOutcomeService(db).reconcile_setup(setup.id, current_user.id)
        setup = get_trade_setup(db, setup.id, current_user.id)
        return _render_trade_setup_detail_page(
            request,
            current_user,
            setup,
            list_trade_events(db, setup.id, current_user.id),
            message=f"Manual setup registered. Outcome status: {result['setup_outcome']}.",
        )
    except (ValidationError, ValueError, LookupError, AdapterError) as exc:
        error = exc.errors()[0]["msg"] if isinstance(exc, ValidationError) else str(exc)
        return _render_manual_trade_setup_form_page(
            request,
            current_user,
            accounts,
            form_action="/trade-setups/manual/create",
            heading="Create Manual Setup",
            submit_label="Register Manual Setup",
            form_data=form_data,
            error=error,
            status_code=status.HTTP_400_BAD_REQUEST,
        )


@router.get("/trade-setups/{setup_id}/manual/edit", response_class=HTMLResponse)
def edit_manual_trade_setup_page(
    setup_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie),
) -> HTMLResponse:
    setup = get_trade_setup(db, setup_id, current_user.id)
    if not setup:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trade setup not found")
    if setup.setup_source != "manual":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Trade setup is not a manual setup")
    accounts = list_trading_accounts(db, current_user.id)
    return _render_manual_trade_setup_form_page(
        request,
        current_user,
        accounts,
        form_action=f"/trade-setups/{setup_id}/manual/edit",
        heading=f"Edit Manual Setup #{setup.id}",
        submit_label="Update Manual Setup",
        form_data=_manual_setup_form_defaults(accounts, setup),
        setup=setup,
    )


@router.post("/trade-setups/{setup_id}/manual/edit", response_class=HTMLResponse)
def edit_manual_trade_setup_page_submit(
    setup_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie),
    trading_account_id: int = Form(...),
    symbol: str = Form(...),
    side: str = Form(...),
    estimated_entry: float = Form(...),
    sl_price: float = Form(...),
    total_risk_money: float = Form(...),
    rr_order2: float = Form(...),
    tp1_price: str = Form(""),
    tp2_price: str = Form(""),
    order_count: int = Form(...),
    order1_ticket: int = Form(...),
    order2_ticket: str = Form(""),
) -> HTMLResponse:
    accounts = list_trading_accounts(db, current_user.id)
    setup = get_trade_setup(db, setup_id, current_user.id)
    if not setup:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trade setup not found")
    form_data = {
        "trading_account_id": trading_account_id,
        "symbol": symbol,
        "side": side,
        "estimated_entry": estimated_entry,
        "sl_price": sl_price,
        "total_risk_money": total_risk_money,
        "rr_order2": rr_order2,
        "tp1_price": tp1_price,
        "tp2_price": tp2_price,
        "order_count": order_count,
        "order1_ticket": order1_ticket,
        "order2_ticket": order2_ticket,
    }
    try:
        payload = ManualTradeSetupCreate(
            trading_account_id=trading_account_id,
            symbol=symbol,
            side=side,
            estimated_entry=estimated_entry,
            sl_price=sl_price,
            total_risk_money=total_risk_money,
            rr_order2=rr_order2,
            tp1_price=float(tp1_price) if tp1_price else None,
            tp2_price=float(tp2_price) if tp2_price else None,
            order_count=order_count,
            order1_ticket=order1_ticket,
            order2_ticket=int(order2_ticket) if order2_ticket else None,
        )
        updated = ManualTradeSetupService(db).update_manual_setup(
            setup_id=setup_id,
            user_id=current_user.id,
            payload=payload,
        )
        result = TradeSetupOutcomeService(db).reconcile_setup(updated.id, current_user.id)
        updated = get_trade_setup(db, updated.id, current_user.id)
        return _render_trade_setup_detail_page(
            request,
            current_user,
            updated,
            list_trade_events(db, updated.id, current_user.id),
            message=f"Manual setup updated. Outcome status: {result['setup_outcome']}.",
        )
    except (ValidationError, ValueError, LookupError, AdapterError) as exc:
        error = exc.errors()[0]["msg"] if isinstance(exc, ValidationError) else str(exc)
        return _render_manual_trade_setup_form_page(
            request,
            current_user,
            accounts,
            form_action=f"/trade-setups/{setup_id}/manual/edit",
            heading=f"Edit Manual Setup #{setup.id}",
            submit_label="Update Manual Setup",
            form_data=form_data,
            setup=setup,
            error=error,
            status_code=status.HTTP_400_BAD_REQUEST,
        )


@router.get("/trade-setups", response_class=HTMLResponse)
def trade_setup_list_page(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie),
) -> HTMLResponse:
    setups = list_trade_setups(db, current_user.id)
    return _render_trade_setup_list_page(request, current_user, setups)


@router.get("/trade-setups/{setup_id}", response_class=HTMLResponse)
def trade_setup_detail_page(
    setup_id: int,
    request: Request,
    message: str | None = Query(None),
    error: str | None = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie),
) -> HTMLResponse:
    setup = get_trade_setup(db, setup_id, current_user.id)
    if not setup:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trade setup not found")
    events = list_trade_events(db, setup_id, current_user.id)
    return _render_trade_setup_detail_page(request, current_user, setup, events, message=message, error=error)


@router.post("/trade-setups/{setup_id}/execute", response_class=HTMLResponse)
def execute_trade_setup_page(
    setup_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie),
) -> HTMLResponse:
    service = TradeSetupExecutionService(db)
    try:
        setup = service.execute_setup(setup_id, current_user.id)
        events = list_trade_events(db, setup_id, current_user.id)
        return _render_trade_setup_detail_page(
            request,
            current_user,
            setup,
            events,
            message=f"Execution completed. Tickets: {setup.order1_ticket}, {setup.order2_ticket}.",
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        setup = get_trade_setup(db, setup_id, current_user.id)
        if not setup:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trade setup not found") from exc
        events = list_trade_events(db, setup_id, current_user.id)
        return _render_trade_setup_detail_page(
            request,
            current_user,
            setup,
            events,
            error=str(exc),
            status_code=status.HTTP_409_CONFLICT,
        )
    except AdapterError as exc:
        setup = get_trade_setup(db, setup_id, current_user.id)
        if not setup:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trade setup not found") from exc
        events = list_trade_events(db, setup_id, current_user.id)
        return _render_trade_setup_detail_page(
            request,
            current_user,
            setup,
            events,
            error=setup.execution_error or exc.message,
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )


@router.post("/trade-setups/{setup_id}/monitor", response_class=HTMLResponse)
def monitor_trade_setup_page(
    setup_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie),
) -> HTMLResponse:
    service = TradeSetupMonitoringService(db)
    try:
        result = service.process_setup(setup_id, current_user.id)
        setup = get_trade_setup(db, setup_id, current_user.id)
        if not setup:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trade setup not found")
        events = list_trade_events(db, setup_id, current_user.id)
        message = {
            "be_moved": "Breakeven stop was moved for order 2.",
            "be_already_moved": "Breakeven stop was already moved earlier.",
            "be_already_set": "Order 2 stop loss is already at or better than breakeven.",
            "waiting_tp1": "Order 1 is still open. No breakeven change yet.",
            "order1_closed_not_tp1": "Order 1 is closed, but the close does not look like a TP1 hit.",
            "order2_closed": "Order 2 is already closed. No breakeven change needed.",
        }.get(result["monitoring_status"], f"Monitoring finished with status: {result['monitoring_status']}.")
        return _render_trade_setup_detail_page(request, current_user, setup, events, message=message)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        setup = get_trade_setup(db, setup_id, current_user.id)
        if not setup:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trade setup not found") from exc
        events = list_trade_events(db, setup_id, current_user.id)
        return _render_trade_setup_detail_page(
            request,
            current_user,
            setup,
            events,
            error=str(exc),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    except AdapterError as exc:
        setup = get_trade_setup(db, setup_id, current_user.id)
        if not setup:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trade setup not found") from exc
        events = list_trade_events(db, setup_id, current_user.id)
        return _render_trade_setup_detail_page(
            request,
            current_user,
            setup,
            events,
            error=setup.order2_be_move_error or exc.message,
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )


@router.post("/trade-setups/{setup_id}/reconcile", response_class=HTMLResponse)
def reconcile_trade_setup_page(
    setup_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie),
) -> HTMLResponse:
    service = TradeSetupOutcomeService(db)
    try:
        result = service.reconcile_setup(setup_id, current_user.id)
        setup = get_trade_setup(db, setup_id, current_user.id)
        if not setup:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trade setup not found")
        events = list_trade_events(db, setup_id, current_user.id)
        return _render_trade_setup_detail_page(
            request,
            current_user,
            setup,
            events,
            message=f"Outcome reconciled: {result['setup_outcome']}.",
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        setup = get_trade_setup(db, setup_id, current_user.id)
        if not setup:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trade setup not found") from exc
        events = list_trade_events(db, setup_id, current_user.id)
        return _render_trade_setup_detail_page(
            request,
            current_user,
            setup,
            events,
            error=str(exc),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    except AdapterError as exc:
        setup = get_trade_setup(db, setup_id, current_user.id)
        if not setup:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trade setup not found") from exc
        events = list_trade_events(db, setup_id, current_user.id)
        return _render_trade_setup_detail_page(
            request,
            current_user,
            setup,
            events,
            error=exc.message,
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )


@router.get("/trading-accounts", response_class=HTMLResponse)
def trading_accounts_page(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie),
) -> HTMLResponse:
    accounts = list_trading_accounts(db, current_user.id)
    return templates.TemplateResponse(
        request,
        "trading_accounts.html",
        {"request": request, "user": current_user, "accounts": accounts},
    )


@router.get("/trading-accounts/id/{account_id}", response_class=HTMLResponse)
def trading_account_detail_page(
    account_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie),
) -> HTMLResponse:
    account = get_trading_account(db, account_id, current_user.id)
    if not account:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trading account not found")
    symbols, symbol_message = _load_synced_symbols(db, current_user, account.id)
    return _render_trading_account_detail_page(
        request,
        current_user,
        account,
        symbols=symbols,
        symbol_message=symbol_message,
    )


@router.get("/trading-accounts/create", response_class=HTMLResponse)
def add_trading_account_page(
    request: Request,
    current_user: User = Depends(get_current_user_from_cookie),
) -> HTMLResponse:
    return _render_trading_account_form_page(
        request,
        current_user,
        form_action="/trading-accounts/create",
        heading="Add Trading Account",
        submit_label="Save Account",
    )


@router.post("/trading-accounts/create")
def create_trading_account_from_form(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie),
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
    default_symbol: str = Form(""),
    default_side: str = Form("buy"),
    default_risk_mode: str = Form("fixed_money"),
    default_risk_value: str = Form("100"),
    default_rr_order_2: str = Form("2"),
) -> Response:
    form_data = {
        "broker_name": broker_name,
        "account_number": account_number,
        "server_name": server_name,
        "platform": platform,
        "terminal_path": terminal_path,
        "telegram_enabled": telegram_enabled,
        "telegram_chat_id": telegram_chat_id,
        "max_total_setup_volume": max_total_setup_volume,
        "default_symbol": default_symbol,
        "default_side": default_side,
        "default_risk_mode": default_risk_mode,
        "default_risk_value": default_risk_value,
        "default_rr_order_2": default_rr_order_2,
    }
    try:
        payload = TradingAccountCreate(
            broker_name=broker_name,
            account_number=account_number,
            server_name=server_name,
            platform=platform,
            terminal_path=terminal_path or None,
            telegram_enabled=telegram_enabled,
            telegram_chat_id=telegram_chat_id or None,
            telegram_bot_token=telegram_bot_token or None,
            max_total_setup_volume=float(max_total_setup_volume) if max_total_setup_volume else None,
            default_symbol=default_symbol or None,
            default_side=default_side or None,
            default_risk_mode=default_risk_mode or None,
            default_risk_value=float(default_risk_value) if default_risk_value else None,
            default_rr_order_2=float(default_rr_order_2) if default_rr_order_2 else None,
            password=password,
        )
        create_trading_account(db, current_user.id, payload)
    except ValidationError as exc:
        return _render_trading_account_form_page(
            request,
            current_user,
            form_action="/trading-accounts/create",
            heading="Add Trading Account",
            submit_label="Save Account",
            form_data=form_data,
            error=exc.errors()[0]["msg"],
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    except DuplicateTradingAccountError as exc:
        return _render_trading_account_form_page(
            request,
            current_user,
            form_action="/trading-accounts/create",
            heading="Add Trading Account",
            submit_label="Save Account",
            form_data=form_data,
            error=str(exc),
            status_code=status.HTTP_409_CONFLICT,
        )
    except ValueError as exc:
        return _render_trading_account_form_page(
            request,
            current_user,
            form_action="/trading-accounts/create",
            heading="Add Trading Account",
            submit_label="Save Account",
            form_data=form_data,
            error=str(exc),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    return RedirectResponse(url="/trading-accounts", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/trading-accounts/id/{account_id}/edit", response_class=HTMLResponse)
def edit_trading_account_page(
    account_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie),
) -> HTMLResponse:
    account = get_trading_account(db, account_id, current_user.id)
    if not account:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trading account not found")
    return _render_trading_account_form_page(
        request,
        current_user,
        form_action=f"/trading-accounts/id/{account.id}/edit",
        heading=f"Edit Trading Account {account.account_number}",
        submit_label="Update Account",
        form_data={
            "broker_name": account.broker_name,
            "account_number": account.account_number,
            "server_name": account.server_name,
            "platform": account.platform,
            "terminal_path": account.terminal_path or "",
            "telegram_enabled": account.telegram_enabled,
            "telegram_chat_id": account.telegram_chat_id or "",
            "max_total_setup_volume": account.max_total_setup_volume or "",
            "default_symbol": account.default_symbol or "",
            "default_side": account.default_side or "buy",
            "default_risk_mode": account.default_risk_mode or "fixed_money",
            "default_risk_value": account.default_risk_value or 100,
            "default_rr_order_2": account.default_rr_order_2 or 2,
        },
        account=account,
    )


@router.post("/trading-accounts/id/{account_id}/edit", response_class=HTMLResponse)
def update_trading_account_from_form(
    account_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie),
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
    default_symbol: str = Form(""),
    default_side: str = Form("buy"),
    default_risk_mode: str = Form("fixed_money"),
    default_risk_value: str = Form("100"),
    default_rr_order_2: str = Form("2"),
) -> HTMLResponse:
    account = get_trading_account(db, account_id, current_user.id)
    if not account:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trading account not found")

    form_data = {
        "broker_name": broker_name,
        "account_number": account_number,
        "server_name": server_name,
        "platform": platform,
        "terminal_path": terminal_path,
        "telegram_enabled": telegram_enabled,
        "telegram_chat_id": telegram_chat_id,
        "max_total_setup_volume": max_total_setup_volume,
        "default_symbol": default_symbol,
        "default_side": default_side,
        "default_risk_mode": default_risk_mode,
        "default_risk_value": default_risk_value,
        "default_rr_order_2": default_rr_order_2,
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
            "default_symbol": default_symbol or None,
            "default_side": default_side or None,
            "default_risk_mode": default_risk_mode or None,
            "default_risk_value": float(default_risk_value) if default_risk_value else None,
            "default_rr_order_2": float(default_rr_order_2) if default_rr_order_2 else None,
        }
        if password:
            payload_kwargs["password"] = password
        if telegram_bot_token:
            payload_kwargs["telegram_bot_token"] = telegram_bot_token
        account = update_trading_account(db, account_id, current_user.id, TradingAccountUpdate(**payload_kwargs))
    except ValidationError as exc:
        return _render_trading_account_form_page(
            request,
            current_user,
            form_action=f"/trading-accounts/id/{account_id}/edit",
            heading=f"Edit Trading Account {account.account_number}",
            submit_label="Update Account",
            form_data=form_data,
            account=account,
            error=exc.errors()[0]["msg"],
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    except DuplicateTradingAccountError as exc:
        return _render_trading_account_form_page(
            request,
            current_user,
            form_action=f"/trading-accounts/id/{account_id}/edit",
            heading=f"Edit Trading Account {account.account_number}",
            submit_label="Update Account",
            form_data=form_data,
            account=account,
            error=str(exc),
            status_code=status.HTTP_409_CONFLICT,
        )
    except ValueError as exc:
        return _render_trading_account_form_page(
            request,
            current_user,
            form_action=f"/trading-accounts/id/{account_id}/edit",
            heading=f"Edit Trading Account {account.account_number}",
            submit_label="Update Account",
            form_data=form_data,
            account=account,
            error=str(exc),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    if not account:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trading account not found")
    symbols, symbol_message = _load_synced_symbols(db, current_user, account_id)
    return _render_trading_account_detail_page(
        request,
        current_user,
        account,
        message="Trading account updated.",
        symbols=symbols,
        symbol_message=symbol_message,
    )


@router.post("/trading-accounts/id/{account_id}/telegram", response_class=HTMLResponse)
def update_trading_account_telegram_page(
    account_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie),
    telegram_enabled: bool = Form(False),
    telegram_chat_id: str = Form(""),
    telegram_bot_token: str = Form(""),
) -> HTMLResponse:
    payload = TradingAccountUpdate(
        telegram_enabled=telegram_enabled,
        telegram_chat_id=telegram_chat_id or None,
        **({"telegram_bot_token": telegram_bot_token} if telegram_bot_token else {}),
    )
    account = update_trading_account(db, account_id, current_user.id, payload)
    if not account:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trading account not found")
    symbols, symbol_message = _load_synced_symbols(db, current_user, account.id)
    return _render_trading_account_detail_page(
        request,
        current_user,
        account,
        message="Telegram notification settings updated.",
        symbols=symbols,
        symbol_message=symbol_message,
    )


@router.post("/trading-accounts/id/{account_id}/test-telegram")
def test_trading_account_telegram_page(
    account_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie),
) -> JSONResponse:
    account = get_trading_account(db, account_id, current_user.id)
    if not account:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trading account not found")
    success, message = send_trading_account_test_notification(account)
    status_code = status.HTTP_200_OK if success else status.HTTP_400_BAD_REQUEST
    return JSONResponse(status_code=status_code, content={"success": success, "message": message})


@router.post("/trading-accounts/id/{account_id}/test-connection")
def test_trading_account_connection_page(
    account_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie),
) -> JSONResponse:
    account = get_trading_account(db, account_id, current_user.id)
    if not account:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trading account not found")

    service = TradingAccountExecutionService(db)
    result = service.test_connection(account_id, current_user.id)
    if not result.success:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "detail": {
                    **(result.error or {}),
                    "connection_status": result.connection_status,
                    "mt5_session_status": result.mt5_session_status,
                    "current_mt5_login": result.current_mt5_login,
                    "last_heartbeat_at": result.last_heartbeat_at.isoformat() if result.last_heartbeat_at else None,
                    "last_error": result.last_error,
                }
            },
        )
    return JSONResponse(status_code=status.HTTP_200_OK, content=result.model_dump(mode="json"))


@router.get("/trading-accounts/id/{account_id}/quote")
def get_trading_account_quote_page(
    account_id: int,
    symbol: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie),
) -> JSONResponse:
    account = get_trading_account(db, account_id, current_user.id)
    if not account:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trading account not found")

    service = TradingAccountExecutionService(db)
    try:
        TradingAccountSymbolService(db).assert_symbol_synced(
            account_id=account_id,
            user_id=current_user.id,
            symbol=symbol,
        )
        quote = service.fetch_quote(account_id, current_user.id, symbol)
        return JSONResponse(status_code=status.HTTP_200_OK, content=quote.model_dump(mode="json"))
    except ValueError as exc:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "detail": {
                    "message": str(exc),
                    "connection_status": account.connection_status,
                    "mt5_session_status": account.mt5_session_status,
                    "current_mt5_login": account.current_mt5_login,
                    "last_heartbeat_at": account.last_heartbeat_at.isoformat() if account.last_heartbeat_at else None,
                    "last_error": account.last_error,
                }
            },
        )
    except AdapterError as exc:
        account = get_trading_account(db, account_id, current_user.id)
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "detail": {
                    **exc.to_dict(),
                    "connection_status": account.connection_status if account else "error",
                    "mt5_session_status": account.mt5_session_status if account else "unknown",
                    "current_mt5_login": account.current_mt5_login if account else None,
                    "last_heartbeat_at": account.last_heartbeat_at.isoformat() if account and account.last_heartbeat_at else None,
                    "last_error": account.last_error if account else str(exc),
                }
            },
        )


@router.get("/trading-accounts/id/{account_id}/symbols")
def get_trading_account_symbols_page(
    account_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie),
) -> JSONResponse:
    account = get_trading_account(db, account_id, current_user.id)
    if not account:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trading account not found")

    try:
        service = TradingAccountSymbolService(db)
        symbols = service.list_synced_symbols(account_id=account_id, user_id=current_user.id)
        defaults, message = _resolve_account_preview_defaults(
            account,
            symbols,
            service.get_symbol_message(account_id=account_id, user_id=current_user.id),
        )
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "symbols": symbols,
                "message": message,
                "preview_defaults": defaults,
            },
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post("/trade-setups/preview/defaults")
def save_trade_preview_defaults(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie),
    trading_account_id: int = Form(...),
    symbol: str = Form(""),
    side: str = Form(...),
    risk_mode: str = Form(...),
    risk_value: float = Form(...),
    rr_order2: float = Form(...),
) -> JSONResponse:
    account = get_trading_account(db, trading_account_id, current_user.id)
    if not account:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trading account not found")

    symbols_service = TradingAccountSymbolService(db)
    if symbol:
        try:
            symbols_service.assert_symbol_synced(account_id=trading_account_id, user_id=current_user.id, symbol=symbol)
        except ValueError as exc:
            return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={"detail": {"message": str(exc)}})

    try:
        updated = update_trading_account(
            db,
            trading_account_id,
            current_user.id,
            TradingAccountUpdate(
                default_symbol=symbol or None,
                default_side=side,
                default_risk_mode=risk_mode,
                default_risk_value=risk_value,
                default_rr_order_2=rr_order2,
            ),
        )
    except ValidationError as exc:
        return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={"detail": {"message": exc.errors()[0]["msg"]}})

    if not updated:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trading account not found")

    symbols = symbols_service.list_synced_symbols(account_id=trading_account_id, user_id=current_user.id)
    defaults, message = _resolve_account_preview_defaults(
        updated,
        symbols,
        symbols_service.get_symbol_message(account_id=trading_account_id, user_id=current_user.id),
    )
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "success": True,
            "message": "Trade Preview defaults saved for this account.",
            "preview_defaults": defaults,
            "symbols": symbols,
            "symbol_message": message,
        },
    )


@router.post("/trading-accounts/id/{account_id}/refresh-symbols")
def refresh_trading_account_symbols_page(
    account_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie),
) -> JSONResponse:
    account = get_trading_account(db, account_id, current_user.id)
    if not account:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trading account not found")

    service = TradingAccountSymbolService(db)
    try:
        symbols = service.sync_symbols(account_id=account_id, user_id=current_user.id)
        message = (
            f"Refreshed {len(symbols)} symbols from MT5 Market Watch."
            if symbols
            else EMPTY_SYMBOL_MESSAGE
        )
        refreshed_account = get_trading_account(db, account_id, current_user.id)
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "symbols": symbols,
                "message": message,
                "symbols_last_synced_at": refreshed_account.symbols_last_synced_at.isoformat()
                if refreshed_account and refreshed_account.symbols_last_synced_at
                else None,
                "symbols_sync_status": refreshed_account.symbols_sync_status if refreshed_account else "unknown",
                "symbols_sync_error": refreshed_account.symbols_sync_error if refreshed_account else None,
                "connection_status": refreshed_account.connection_status if refreshed_account else "unknown",
                "mt5_session_status": refreshed_account.mt5_session_status if refreshed_account else "unknown",
                "current_mt5_login": refreshed_account.current_mt5_login if refreshed_account else None,
            },
        )
    except ValueError as exc:
        refreshed_account = get_trading_account(db, account_id, current_user.id)
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "detail": {
                    "message": str(exc),
                    "symbols_last_synced_at": refreshed_account.symbols_last_synced_at.isoformat()
                    if refreshed_account and refreshed_account.symbols_last_synced_at
                    else None,
                    "symbols_sync_status": refreshed_account.symbols_sync_status if refreshed_account else "failed",
                    "symbols_sync_error": refreshed_account.symbols_sync_error if refreshed_account else str(exc),
                    "connection_status": refreshed_account.connection_status if refreshed_account else "unknown",
                    "mt5_session_status": refreshed_account.mt5_session_status if refreshed_account else "unknown",
                    "current_mt5_login": refreshed_account.current_mt5_login if refreshed_account else None,
                }
            },
        )
    except AdapterError as exc:
        refreshed_account = get_trading_account(db, account_id, current_user.id)
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "detail": {
                    **exc.to_dict(),
                    "message": refreshed_account.symbols_sync_error if refreshed_account else exc.message,
                    "symbols_last_synced_at": refreshed_account.symbols_last_synced_at.isoformat()
                    if refreshed_account and refreshed_account.symbols_last_synced_at
                    else None,
                    "symbols_sync_status": refreshed_account.symbols_sync_status if refreshed_account else "failed",
                    "symbols_sync_error": refreshed_account.symbols_sync_error if refreshed_account else exc.message,
                    "connection_status": refreshed_account.connection_status if refreshed_account else "unknown",
                    "mt5_session_status": refreshed_account.mt5_session_status if refreshed_account else "unknown",
                    "current_mt5_login": refreshed_account.current_mt5_login if refreshed_account else None,
                }
            },
        )
