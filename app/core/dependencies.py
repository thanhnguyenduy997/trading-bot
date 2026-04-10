from fastapi import Cookie, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import decode_access_token
from app.models.user import User


oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/token")


def _user_id_from_token(token: str | None) -> int:
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    try:
        payload = decode_access_token(token)
        return int(payload["sub"])
    except (JWTError, KeyError, ValueError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from None


def _resolve_user(db: Session, token: str | None) -> User:
    user_id = _user_id_from_token(token)
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User is inactive")
    return user


def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> User:
    return _resolve_user(db, token)


def get_current_user_from_cookie(
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
