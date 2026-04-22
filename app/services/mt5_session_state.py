from __future__ import annotations

from datetime import datetime, timezone
import logging

from app.execution.base import AdapterError


logger = logging.getLogger(__name__)

MT5_SESSION_MISMATCH_CODES = {"mt5_session_mismatch"}
MT5_CONNECTED_ACTION_ERROR_CODES = {
    "mt5_symbol_not_found",
    "mt5_symbol_select_failed",
    "mt5_symbol_info_unavailable",
    "mt5_quote_unavailable",
}
MT5_SESSION_DISCONNECTED_CODES = {
    "mt5_terminal_path_missing",
    "mt5_terminal_path_not_found",
    "mt5_initialize_failed",
    "mt5_account_info_unavailable",
    "mt5_session_disconnected",
    "mt5_unavailable",
    "mt5_login_failed",
    "invalid_account_number",
}


def extract_current_mt5_login(error: AdapterError) -> str | None:
    if not error.details:
        return None
    login = error.details.get("current_login")
    return str(login) if login not in {None, ""} else None


def persist_session_matched(db, account, *, account_info: dict[str, object] | None) -> None:
    current_login = None
    if account_info and account_info.get("login") is not None:
        current_login = str(account_info["login"])
    account.connection_status = "connected"
    account.mt5_session_status = "matched"
    account.current_mt5_login = current_login
    account.last_heartbeat_at = datetime.now(timezone.utc)
    account.last_error = None
    db.add(account)
    db.commit()
    db.refresh(account)
    logger.info(
        "mt5_session_matched account_id=%s expected_account=%s current_login=%s",
        account.id,
        account.account_number,
        current_login,
    )


def persist_session_failure(db, account, *, error: AdapterError) -> None:
    current_login = extract_current_mt5_login(error)
    if error.code in MT5_SESSION_MISMATCH_CODES:
        connection_status = "connected"
        session_status = "mismatch"
        logger.warning(
            "mt5_session_mismatch account_id=%s expected_account=%s current_login=%s code=%s",
            account.id,
            account.account_number,
            current_login,
            error.code,
        )
    elif error.code in MT5_SESSION_DISCONNECTED_CODES:
        connection_status = "disconnected"
        session_status = "disconnected"
    elif error.code in MT5_CONNECTED_ACTION_ERROR_CODES:
        connection_status = "connected"
        session_status = account.mt5_session_status if account.mt5_session_status != "unknown" else "matched"
    else:
        connection_status = "error"
        session_status = "unknown"

    account.connection_status = connection_status
    account.mt5_session_status = session_status
    account.current_mt5_login = current_login
    account.last_heartbeat_at = datetime.now(timezone.utc)
    account.last_error = f"{error.code}: {error.message}"
    db.add(account)
    db.commit()
    db.refresh(account)
