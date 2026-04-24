from __future__ import annotations

from datetime import datetime, timedelta, timezone
from threading import Lock


_recent_view_lock = Lock()
_recent_account_views: dict[int, datetime] = {}


def mark_trading_account_viewed(account_id: int, viewed_at: datetime | None = None) -> None:
    timestamp = viewed_at or datetime.now(timezone.utc)
    with _recent_view_lock:
        _recent_account_views[int(account_id)] = timestamp


def get_recent_account_views(*, since: datetime | None = None) -> dict[int, datetime]:
    threshold = since or (datetime.now(timezone.utc) - timedelta(hours=6))
    with _recent_view_lock:
        stale_ids = [account_id for account_id, seen_at in _recent_account_views.items() if seen_at < threshold]
        for account_id in stale_ids:
            _recent_account_views.pop(account_id, None)
        return dict(_recent_account_views)
