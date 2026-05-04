from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import logging

from jose import jwt
from passlib.context import CryptContext
from starlette.responses import Response

from app.core.config import get_settings


settings = get_settings()
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
SESSION_LIFETIME_MINUTES = 3 * 24 * 60
SESSION_REFRESH_THRESHOLD_SECONDS = 24 * 60 * 60
logger = logging.getLogger(__name__)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


def effective_access_token_expire_minutes() -> int:
    configured_minutes = settings.access_token_expire_minutes
    if configured_minutes != SESSION_LIFETIME_MINUTES:
        logger.debug(
            "auth_session_lifetime_configured minutes=%s target_minutes=%s",
            configured_minutes,
            SESSION_LIFETIME_MINUTES,
        )
    return configured_minutes


def access_token_max_age_seconds() -> int:
    return effective_access_token_expire_minutes() * 60


def password_session_fingerprint(hashed_password: str) -> str:
    return hmac.new(
        settings.secret_key.encode("utf-8"),
        hashed_password.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def create_access_token(subject: str, *, hashed_password: str | None = None) -> str:
    expires_delta = timedelta(minutes=effective_access_token_expire_minutes())
    expire = datetime.now(timezone.utc) + expires_delta
    payload = {"sub": subject, "exp": expire}
    if hashed_password is not None:
        payload["pwd"] = password_session_fingerprint(hashed_password)
    logger.debug("auth_token_issued user_id=%s expires_at=%s", subject, expire.isoformat())
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict:
    return jwt.decode(token, settings.secret_key, algorithms=[settings.jwt_algorithm])


def token_expires_soon(payload: dict, *, threshold_seconds: int = SESSION_REFRESH_THRESHOLD_SECONDS) -> bool:
    exp = payload.get("exp")
    if exp is None:
        return False
    try:
        expires_at = datetime.fromtimestamp(float(exp), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return False
    return expires_at - datetime.now(timezone.utc) <= timedelta(seconds=threshold_seconds)


def set_auth_cookie(response: Response, key: str, token: str) -> None:
    max_age = access_token_max_age_seconds()
    response.set_cookie(
        key=key,
        value=token,
        max_age=max_age,
        httponly=True,
        samesite="lax",
        secure=False,
    )
    logger.debug("auth_cookie_set cookie=%s max_age_seconds=%s", key, max_age)
