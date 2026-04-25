from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.app_setting import AppSetting


def get_app_settings_record(db: Session) -> AppSetting:
    record = db.query(AppSetting).filter(AppSetting.id == 1).first()
    if record:
        return record
    record = AppSetting(id=1)
    db.add(record)
    db.flush()
    return record


def get_global_max_preview_drift_percent(db: Session) -> float:
    record = db.query(AppSetting).filter(AppSetting.id == 1).first()
    if record and record.max_preview_drift_percent is not None:
        return float(record.max_preview_drift_percent)
    return float(get_settings().max_preview_drift_percent)


def get_global_scratch_manual_threshold_r(db: Session) -> float:
    record = db.query(AppSetting).filter(AppSetting.id == 1).first()
    if record and record.scratch_manual_threshold_r is not None:
        return float(record.scratch_manual_threshold_r)
    return float(get_settings().scratch_manual_threshold_r)


def update_global_max_preview_drift_percent(db: Session, value: float) -> AppSetting:
    record = get_app_settings_record(db)
    record.max_preview_drift_percent = value
    db.add(record)
    db.flush()
    return record


def update_global_scratch_manual_threshold_r(db: Session, value: float) -> AppSetting:
    record = get_app_settings_record(db)
    record.scratch_manual_threshold_r = value
    db.add(record)
    db.flush()
    return record


def get_effective_max_preview_drift_percent(db: Session, account) -> float:
    if getattr(account, "max_preview_drift_percent_override", None) is not None:
        return float(account.max_preview_drift_percent_override)
    return get_global_max_preview_drift_percent(db)
