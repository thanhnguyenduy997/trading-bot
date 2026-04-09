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
from app.schemas.trading_account import TradingAccountCreate
from app.services.execution import TradingAccountExecutionService
from app.services.preview_service import PreviewService
from app.services.trade_events import list_trade_events
from app.services.trade_setup_execution import TradeSetupExecutionService
from app.services.trade_setups import create_trade_setup, get_trade_setup, list_trade_setups
from app.services.trading_accounts import create_trading_account, get_trading_account, list_trading_accounts


router = APIRouter(tags=["pages"])
templates = Jinja2Templates(directory="app/templates")


def _render_trade_preview_page(
    request: Request,
    current_user: User,
    accounts: list,
    form_data: dict[str, object] | None = None,
    preview: object | None = None,
    error: str | None = None,
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "trade_preview.html",
        {
            "request": request,
            "user": current_user,
            "accounts": accounts,
            "form_data": form_data
            or {
                "symbol": "XAUUSD",
                "side": "buy",
                "risk_mode": "fixed_money",
                "risk_value": 100,
                "rr_order2": 2.0,
            },
            "preview": preview,
            "error": error,
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
    error: str | None = None,
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
            "error": error,
        },
        status_code=status_code,
    )


@router.get("/", response_class=HTMLResponse)
def root() -> RedirectResponse:
    return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "login.html", {"request": request})


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard_page(
    request: Request,
    current_user: User = Depends(get_current_user_from_cookie),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"request": request, "user": current_user},
    )


@router.get("/trade-setups/preview", response_class=HTMLResponse)
def trade_preview_page(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie),
) -> HTMLResponse:
    accounts = list_trading_accounts(db, current_user.id)
    return _render_trade_preview_page(request, current_user, accounts)


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
    }

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
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    except (LookupError, ValueError) as exc:
        return _render_trade_preview_page(
            request,
            current_user,
            accounts,
            form_data=form_data,
            error=str(exc),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    return _render_trade_preview_page(request, current_user, accounts, form_data=form_data, preview=preview)


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
            message="Execution completed.",
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
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
    return _render_trading_account_detail_page(request, current_user, account)


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
) -> RedirectResponse:
    create_trading_account(
        db,
        current_user.id,
        TradingAccountCreate(
            broker_name=broker_name,
            account_number=account_number,
            server_name=server_name,
            terminal_path=terminal_path or None,
            password=password,
        ),
    )
    return RedirectResponse(url="/trading-accounts", status_code=status.HTTP_303_SEE_OTHER)


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
        quote = service.fetch_quote(account_id, current_user.id, symbol)
        return JSONResponse(status_code=status.HTTP_200_OK, content=quote.model_dump(mode="json"))
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
