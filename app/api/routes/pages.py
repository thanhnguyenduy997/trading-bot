from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_user_from_cookie
from app.execution.base import AdapterError
from app.models.user import User
from app.schemas.trade_preview import TradePreviewRequest
from app.schemas.trade_setup import TradeSetupCreate
from app.schemas.trading_account import TradingAccountCreate, TradingAccountUpdate
from app.services.execution import TradingAccountExecutionService
from app.services.preview_service import PreviewService
from app.services.notifications import send_trading_account_test_notification
from app.services.risk_management import RiskManagementService
from app.services.symbols import SymbolPolicyService
from app.services.trade_events import list_trade_events
from app.services.trade_setup_execution import TradeSetupExecutionService
from app.services.trade_setup_monitoring import TradeSetupMonitoringService
from app.services.trade_setups import create_trade_setup, get_trade_setup, list_trade_setups, update_draft_trade_setup
from app.services.trading_accounts import create_trading_account, get_trading_account, list_trading_accounts, update_trading_account


router = APIRouter(tags=["pages"])
templates = Jinja2Templates(directory="app/templates")


def _render_trade_preview_page(
    request: Request,
    current_user: User,
    accounts: list,
    form_data: dict[str, object] | None = None,
    preview: object | None = None,
    setup: object | None = None,
    error: str | None = None,
    symbols: list[dict[str, str]] | None = None,
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    initial_symbols = symbols or []
    default_symbol = initial_symbols[0]["symbol_name"] if initial_symbols else ""
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
        },
        status_code=status_code,
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


def _render_trading_account_detail_page(
    request: Request,
    current_user: User,
    account: object,
    connection_result: object | None = None,
    quote_result: object | None = None,
    message: str | None = None,
    error: str | None = None,
    symbols: list[dict[str, str]] | None = None,
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
        },
        status_code=status_code,
    )


def _load_selectable_symbols(db: Session, current_user: User, account_id: int | None) -> list[dict[str, str]]:
    if account_id is None:
        return []
    try:
        return SymbolPolicyService(db).list_selectable_symbols(account_id=account_id, user_id=current_user.id)
    except (LookupError, AdapterError):
        return []


@router.get("/", response_class=HTMLResponse)
def root() -> RedirectResponse:
    return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "login.html", {"request": request})


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard_page(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie),
) -> HTMLResponse:
    daily_risk_state = RiskManagementService(db).get_daily_state(current_user.id)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"request": request, "user": current_user, "daily_risk_state": daily_risk_state},
    )


@router.get("/trade-setups/preview", response_class=HTMLResponse)
def trade_preview_page(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie),
) -> HTMLResponse:
    accounts = list_trading_accounts(db, current_user.id)
    selected_account_id = accounts[0].id if accounts else None
    symbols = _load_selectable_symbols(db, current_user, selected_account_id)
    return _render_trade_preview_page(request, current_user, accounts, symbols=symbols)


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

    symbols = _load_selectable_symbols(db, current_user, trading_account_id)

    try:
        payload = TradePreviewRequest(**form_data)
        preview = PreviewService(db).build_preview(current_user.id, payload)
    except ValidationError as exc:
        return _render_trade_preview_page(
            request,
            current_user,
            accounts,
            form_data=form_data,
            error=exc.errors()[0]["msg"],
            symbols=symbols,
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    except (LookupError, ValueError) as exc:
        return _render_trade_preview_page(
            request,
            current_user,
            accounts,
            form_data=form_data,
            error=str(exc),
            symbols=symbols,
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
            preview=preview,
            error=str(exc),
            symbols=symbols,
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    form_data["draft_setup_id"] = setup.id
    return _render_trade_preview_page(
        request,
        current_user,
        accounts,
        form_data=form_data,
        preview=preview,
        setup=setup,
        symbols=symbols,
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
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie),
) -> HTMLResponse:
    setup = get_trade_setup(db, setup_id, current_user.id)
    if not setup:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trade setup not found")
    events = list_trade_events(db, setup_id, current_user.id)
    return _render_trade_setup_detail_page(request, current_user, setup, events)


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
    symbols = _load_selectable_symbols(db, current_user, account.id)
    return _render_trading_account_detail_page(request, current_user, account, symbols=symbols)


@router.get("/trading-accounts/create", response_class=HTMLResponse)
def add_trading_account_page(
    request: Request,
    current_user: User = Depends(get_current_user_from_cookie),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "trading_account_form.html",
        {"request": request, "user": current_user},
    )


@router.post("/trading-accounts/create")
def create_trading_account_from_form(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie),
    broker_name: str = Form(...),
    account_number: str = Form(...),
    server_name: str = Form(...),
    terminal_path: str = Form(""),
    password: str = Form(...),
    telegram_enabled: bool = Form(False),
    telegram_chat_id: str = Form(""),
    telegram_bot_token: str = Form(""),
) -> RedirectResponse:
    create_trading_account(
        db,
        current_user.id,
        TradingAccountCreate(
            broker_name=broker_name,
            account_number=account_number,
            server_name=server_name,
            terminal_path=terminal_path or None,
            telegram_enabled=telegram_enabled,
            telegram_chat_id=telegram_chat_id or None,
            telegram_bot_token=telegram_bot_token or None,
            password=password,
        ),
    )
    return RedirectResponse(url="/trading-accounts", status_code=status.HTTP_303_SEE_OTHER)


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
    return _render_trading_account_detail_page(
        request,
        current_user,
        account,
        message="Telegram notification settings updated.",
        symbols=_load_selectable_symbols(db, current_user, account.id),
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
        SymbolPolicyService(db, execution_service=service).assert_symbol_allowed_for_account(
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
        symbols = SymbolPolicyService(db).list_selectable_symbols(account_id=account_id, user_id=current_user.id)
        return JSONResponse(status_code=status.HTTP_200_OK, content={"symbols": symbols})
    except AdapterError as exc:
        account = get_trading_account(db, account_id, current_user.id)
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "detail": {
                    **exc.to_dict(),
                    "message": exc.message,
                    "connection_status": account.connection_status if account else "error",
                    "last_heartbeat_at": account.last_heartbeat_at.isoformat() if account and account.last_heartbeat_at else None,
                    "last_error": account.last_error if account else str(exc),
                }
            },
        )
