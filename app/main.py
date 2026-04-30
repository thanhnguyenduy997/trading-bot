from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.api.router import api_router
from app.core.config import get_settings
from app.core.dependencies import (
    SESSION_EXPIRED_MESSAGE,
    SessionExpiredError,
    build_login_redirect_url,
    is_api_like_request,
)
from app.core.security import (
    create_access_token,
    set_auth_cookie,
    token_expires_soon,
)
from app.services.background_monitor import TradeMonitorRunner


settings = get_settings()
trade_monitor = TradeMonitorRunner(settings)

app = FastAPI(title=settings.app_name, debug=settings.debug)
app.include_router(api_router)
app.mount("/static", StaticFiles(directory="app/templates/static"), name="static")


@app.middleware("http")
async def refresh_expiring_cookie_session(request: Request, call_next):
    response = await call_next(request)
    for session in getattr(request.state, "auth_cookie_sessions", []):
        payload = session["payload"]
        if not token_expires_soon(payload):
            continue
        refreshed_token = create_access_token(
            str(session["user_id"]),
            hashed_password=session["hashed_password"],
        )
        set_auth_cookie(response, session["cookie_name"], refreshed_token)
    return response


@app.exception_handler(SessionExpiredError)
async def handle_session_expired(request: Request, exc: SessionExpiredError):
    if is_api_like_request(request):
        response = JSONResponse(
            status_code=401,
            content={
                "detail": exc.message,
                "login_url": build_login_redirect_url(request, SESSION_EXPIRED_MESSAGE),
            },
        )
    else:
        response = RedirectResponse(
            url=build_login_redirect_url(request, exc.message),
            status_code=303,
        )
    response.delete_cookie("access_token")
    response.delete_cookie("admin_access_token")
    return response


@app.on_event("startup")
async def start_trade_monitor() -> None:
    trade_monitor.start()


@app.on_event("shutdown")
async def stop_trade_monitor() -> None:
    await trade_monitor.stop()
