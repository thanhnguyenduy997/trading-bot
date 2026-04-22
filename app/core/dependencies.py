from urllib.parse import quote

from fastapi import Cookie, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import decode_access_token
from app.models.user import User


SESSION_EXPIRED_MESSAGE = "Phiên đăng nhập đã hết hạn, vui lòng đăng nhập lại."
REAUTH_MESSAGE = "Vui lòng đăng nhập lại để tiếp tục."


class SessionExpiredError(Exception):
    def __init__(self, message: str = SESSION_EXPIRED_MESSAGE) -> None:
        super().__init__(message)
        self.message = message


oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/token", auto_error=False)


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


def _user_id_from_token(token: str | None) -> int:
    if not token:
        raise SessionExpiredError(REAUTH_MESSAGE)
    try:
        payload = decode_access_token(token)
        return int(payload["sub"])
    except (JWTError, KeyError, ValueError):
        raise SessionExpiredError() from None


def _resolve_user(db: Session, token: str | None) -> User:
    user_id = _user_id_from_token(token)
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise SessionExpiredError()
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User is inactive")
    return user


def get_current_user(token: str | None = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> User:
    return _resolve_user(db, token)


def get_current_user_from_cookie(
    request: Request,
    access_token: str | None = Cookie(default=None),
    admin_access_token: str | None = Cookie(default=None),
    db: Session = Depends(get_db),
) -> User:
    user = _resolve_user(db, access_token)
    if admin_access_token:
        admin = _resolve_user(db, admin_access_token)
        if admin.role == "admin" and admin.id != user.id:
            setattr(user, "impersonator", admin)
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
