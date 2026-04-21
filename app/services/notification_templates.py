from app.models.trade_event import TradeEvent
from app.models.trade_setup import TradeSetup


EVENT_TITLES = {
    "execute_completed": "✅ Thực thi thành công",
    "execute_failed": "❌ Thực thi thất bại",
    "tp1_hit": "🎯 Đã chạm TP1",
    "be_move_completed": "🔒 Đã dời SL về BE",
    "be_move_failed": "❌ Lỗi dời SL về BE",
    "preview_drift_reject": "⚠️ Từ chối do lệch preview",
    "risk_limit_execute_rejected": "⚠️ Từ chối do vượt giới hạn khối lượng",
    "daily_lock_execute_rejected": "⚠️ Từ chối do khóa giao dịch trong ngày",
}


def format_trade_event_notification(event: TradeEvent, setup: TradeSetup | None) -> str:
    title = EVENT_TITLES.get(event.event_type, f"⚠️ {event.event_type}")
    if setup is None:
        return "\n".join(
            line
            for line in [
                title,
                f"Setup: #{event.setup_id}",
                _short_reason(event.message),
            ]
            if line
        )

    account = getattr(setup, "trading_account", None)
    account_label = "-"
    if account is not None:
        account_label = account.account_number or account.broker_name or "-"

    lines = [
        title,
        f"Tài khoản: {account_label}",
        f"Setup: #{setup.id}",
        f"Mã lệnh: {setup.symbol}",
        f"Chiều: {setup.side.upper()}",
    ]

    outcome = _event_outcome_line(event, setup)
    if outcome:
        lines.append(outcome)

    reason = _short_reason(event.message)
    if reason:
        lines.append(f"Lý do: {reason}")

    tickets = _ticket_line(setup)
    if tickets:
        lines.append(tickets)

    return "\n".join(lines)


def _event_outcome_line(event: TradeEvent, setup: TradeSetup) -> str | None:
    if event.event_type == "execute_completed":
        return "Kết quả: Đã vào 2 lệnh"
    if event.event_type == "execute_failed":
        return "Kết quả: Không thể vào lệnh"
    if event.event_type == "tp1_hit":
        return "Kết quả: Lệnh 1 đã đạt TP1"
    if event.event_type == "be_move_completed":
        return "Kết quả: Lệnh 2 đã dời SL về BE"
    if event.event_type == "be_move_failed":
        return "Kết quả: Không dời được SL lệnh 2 về BE"
    if event.event_type == "preview_drift_reject":
        return "Kết quả: Bắt buộc preview lại trước khi Execute"
    if event.event_type == "risk_limit_execute_rejected":
        return "Kết quả: Từ chối Execute"
    if event.event_type == "daily_lock_execute_rejected":
        return "Kết quả: Từ chối Execute"
    return None


def _ticket_line(setup: TradeSetup) -> str | None:
    tickets = [str(ticket) for ticket in [setup.order1_ticket, setup.order2_ticket] if ticket]
    if not tickets:
        return None
    return f"Ticket: {' / '.join(tickets)}"


def _short_reason(message: str | None) -> str | None:
    if not message:
        return None
    message = " ".join(str(message).split())
    replacements = {
        "Trade setup executed.": "Đã vào lệnh.",
        "Order 1 appears to have closed at TP1.": "Lệnh 1 đóng tại TP1.",
        "Breakeven SL update requested": "Đã gửi yêu cầu dời SL về BE.",
        "Order 2 stop loss moved to breakeven": "SL lệnh 2 đã dời về BE.",
        "Execution requested for trade setup.": "Đã gửi yêu cầu Execute.",
        "Live quote has moved materially since preview. Refresh the preview before execution.": "Giá thay đổi nhiều, cần preview lại.",
    }
    for source, target in replacements.items():
        if source in message:
            return target
    return message
