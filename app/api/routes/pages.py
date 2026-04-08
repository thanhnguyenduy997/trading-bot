from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_user_from_cookie
from app.models.user import User
from app.schemas.trading_account import TradingAccountCreate
from app.services.trading_accounts import create_trading_account, list_trading_accounts


router = APIRouter(tags=["pages"])
templates = Jinja2Templates(directory="app/templates")


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
