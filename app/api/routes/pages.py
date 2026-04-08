from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_user_from_cookie
from app.models.user import User
from app.schemas.trade_preview import TradePreviewRequest
from app.schemas.trading_account import TradingAccountCreate
from app.services.preview_service import PreviewService
from app.services.trading_accounts import create_trading_account, list_trading_accounts


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


@router.get("/trading-accounts/new", response_class=HTMLResponse)
def add_trading_account_page(
    request: Request,
    current_user: User = Depends(get_current_user_from_cookie),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "trading_account_form.html",
        {"request": request, "user": current_user},
    )


@router.post("/trading-accounts/new")
def create_trading_account_from_form(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie),
    broker_name: str = Form(...),
    account_number: str = Form(...),
    server_name: str = Form(...),
    password: str = Form(...),
) -> RedirectResponse:
    create_trading_account(
        db,
        current_user.id,
        TradingAccountCreate(
            broker_name=broker_name,
            account_number=account_number,
            server_name=server_name,
            password=password,
        ),
    )
    return RedirectResponse(url="/trading-accounts", status_code=status.HTTP_303_SEE_OTHER)
