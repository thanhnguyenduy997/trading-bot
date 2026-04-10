from sqlalchemy.orm import Session

from app.models.admin_audit_log import AdminAuditLog


def log_admin_action(
    db: Session,
    *,
    admin_user_id: int | None,
    target_user_id: int | None,
    action: str,
    message: str | None = None,
) -> AdminAuditLog:
    log = AdminAuditLog(
        admin_user_id=admin_user_id,
        target_user_id=target_user_id,
        action=action,
        message=message,
    )
    db.add(log)
    db.flush()
    return log
