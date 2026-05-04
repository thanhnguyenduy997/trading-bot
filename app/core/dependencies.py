from urllib.parse import quote
import logging

from fastapi import Cookie, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import (
    decode_access_token,
    password_session_fingerprint,
)
from app.models.user import User


SESSION_EXPIRED_MESSAGE = "Phiên đăng nhập đã hết hạn, vui lòng đăng nhập lại."
REAUTH_MESSAGE = "Vui lòng đăng nhập lại để tiếp tục."


class SessionExpiredError(Exception):
    def __init__(self, message: str = SESSION_EXPIRED_MESSAGE) -> None:
        super().__init__(message)
        self.message = message


oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/token", auto_error=False)
logger = logging.getLogger(__name__)


def build_next_value(request: Request) -> str:
    next_value = request.url.path or "/dashboard"
    if request.url.query:
        next_value = f"{next_value}?{request.url.query}"
    return next_value


def sanitize_next_value(next_value: str | None) -> str | None:
    if not next_value:
        return None
    if not next_value.startswith("/") or next_value.startswith("//"):
        return None
    return next_value


def build_login_redirect_url(request: Request, message: str = SESSION_EXPIRED_MESSAGE) -> str:
    next_value = sanitize_next_value(build_next_value(request)) or "/dashboard"
    return f"/login?message={quote(message)}&next={quote(next_value, safe='/?=&')}"


def is_api_like_request(request: Request) -> bool:
    if request.url.path.startswith("/api/"):
        return True
    return request.headers.get("x-requested-with") == "XMLHttpRequest"


def _payload_from_token(token: str | None) -> dict:
    if not token:
        raise SessionExpiredError(REAUTH_MESSAGE)
    try:
        return decode_access_token(token)
    except (JWTError, KeyError, ValueError):
        raise SessionExpiredError() from None


def _user_id_from_payload(payload: dict) -> int:
    try:
        return int(payload["sub"])
    except (KeyError, TypeError, ValueError):
        raise SessionExpiredError() from None


def _resolve_user_with_payload(db: Session, token: str | None) -> tuple[User, dict]:
    payload = _payload_from_token(token)
    user_id = _user_id_from_payload(payload)
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise SessionExpiredError()
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User is inactive")
    password_fingerprint = payload.get("pwd")
    if password_fingerprint is not None and password_fingerprint != password_session_fingerprint(user.hashed_password):
        logger.debug("auth_user_load_failed_password_fingerprint user_id=%s", user.id)
        raise SessionExpiredError()
    logger.debug("auth_user_loaded user_id=%s token_exp=%s", user.id, payload.get("exp"))
    return user, payload


def _resolve_user(db: Session, token: str | None) -> User:
    user, _payload = _resolve_user_with_payload(db, token)
    return user


def _remember_cookie_session(request: Request, cookie_name: str, user: User, payload: dict) -> None:
    sessions = getattr(request.state, "auth_cookie_sessions", None)
    if sessions is None:
        sessions = []
        request.state.auth_cookie_sessions = sessions
    sessions.append(
        {
            "cookie_name": cookie_name,
            "user_id": user.id,
            "hashed_password": user.hashed_password,
            "payload": payload,
        }
    )


def get_current_user(token: str | None = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> User:
    return _resolve_user(db, token)


def get_current_user_from_cookie(
    request: Request,
    access_token: str | None = Cookie(default=None),
    admin_access_token: str | None = Cookie(default=None),
    db: Session = Depends(get_db),
) -> User:
    user, user_payload = _resolve_user_with_payload(db, access_token)
    _remember_cookie_session(request, "access_token", user, user_payload)
    if admin_access_token:
        admin, admin_payload = _resolve_user_with_payload(db, admin_access_token)
        if admin.role == "admin" and admin.id != user.id:
            setattr(user, "impersonator", admin)
            _remember_cookie_session(request, "admin_access_token", admin, admin_payload)
            logger.debug(
                "auth_impersonation_user_loaded authenticated_user_id=%s impersonator_user_id=%s",
                user.id,
                admin.id,
            )
        elif admin.role != "admin":
            logger.debug(
                "auth_impersonation_ignored_admin_cookie_not_admin authenticated_user_id=%s admin_cookie_user_id=%s",
                user.id,
                admin.id,
            )
    return user


def get_current_admin_user_from_cookie(
    current_user: User = Depends(get_current_user_from_cookie),
) -> User:
    admin = getattr(current_user, "impersonator", None) or current_user
    if admin.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return admin


def get_current_admin_user(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return current_user
