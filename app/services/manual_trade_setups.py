from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.execution.base import AdapterError
from app.models.trade_setup import TradeSetup
from app.schemas.trade_setup import ManualTradeSetupCreate
from app.services.execution import default_adapter_factory
from app.services.mt5_session_state import persist_session_failure, persist_session_matched
from app.services.trade_events import create_trade_event
from app.services.trade_setups import get_trade_setup
from app.services.trading_accounts import get_trading_account


MANUAL_TICKET_TIME_WINDOW = timedelta(minutes=15)


@dataclass
class ManualTicketSnapshot:
    ticket: int
    symbol: str
    side: str
    volume: float
    open_price: float | None
    open_time: datetime | None
    close_price: float | None
    close_time: datetime | None
    status: str


class ManualTradeSetupService:
    def __init__(self, db: Session, adapter_factory=None) -> None:
        self.db = db
        self.adapter_factory = adapter_factory or default_adapter_factory

    def create_manual_setup(self, *, user_id: int, payload: ManualTradeSetupCreate) -> TradeSetup:
        account = get_trading_account(self.db, payload.trading_account_id, user_id)
        if not account:
            raise LookupError("Trading account not found")

        validated = self._validate_manual_payload(account=account, payload=payload, user_id=user_id)
        setup = TradeSetup(
            user_id=user_id,
            trading_account_id=account.id,
            setup_source="manual",
            order_count=payload.order_count,
            symbol=payload.symbol,
            side=payload.side,
            sl_price=payload.sl_price,
            risk_mode="fixed_money",
            risk_value=payload.total_risk_money,
            rr_order2=payload.rr_order2,
            estimated_entry=payload.estimated_entry,
            r_value=validated["r_value"],
            tp1_price=validated["tp1_price"],
            tp2_price=validated["tp2_price"],
            total_risk_money=payload.total_risk_money,
            risk_per_order=validated["risk_per_order"],
            order1_volume=validated["order1_volume"],
            order2_volume=validated["order2_volume"],
            order1_ticket=payload.order1_ticket,
            order2_ticket=payload.order2_ticket,
            status="executed",
            manual_confirmed_at=datetime.now(timezone.utc),
            executed_at=validated["executed_at"],
            monitoring_status="waiting_tp1" if payload.order_count == 2 and payload.order2_ticket else None,
        )
        self.db.add(setup)
        self.db.flush()
        create_trade_event(
            self.db,
            user_id,
            setup.id,
            "manual_setup_registered",
            "Manual MT5 trade setup registered and confirmed for risk logic.",
            details=json.dumps(validated["summary"], indent=2, sort_keys=True, default=str),
        )
        self.db.commit()
        self.db.refresh(setup)
        return setup

    def update_manual_setup(self, *, setup_id: int, user_id: int, payload: ManualTradeSetupCreate) -> TradeSetup:
        setup = get_trade_setup(self.db, setup_id, user_id)
        if not setup:
            raise LookupError("Trade setup not found")
        if setup.setup_source != "manual":
            raise ValueError("Only manual trade setups can be edited from this form.")
        if setup.result_status is not None:
            raise ValueError("Manual setup can no longer be edited after it has been counted in risk logic.")

        account = get_trading_account(self.db, payload.trading_account_id, user_id)
        if not account:
            raise LookupError("Trading account not found")

        validated = self._validate_manual_payload(
            account=account,
            payload=payload,
            user_id=user_id,
            current_setup_id=setup.id,
        )

        setup.trading_account_id = account.id
        setup.setup_source = "manual"
        setup.order_count = payload.order_count
        setup.symbol = payload.symbol
        setup.side = payload.side
        setup.sl_price = payload.sl_price
        setup.risk_mode = "fixed_money"
        setup.risk_value = payload.total_risk_money
        setup.rr_order2 = payload.rr_order2
        setup.estimated_entry = payload.estimated_entry
        setup.r_value = validated["r_value"]
        setup.tp1_price = validated["tp1_price"]
        setup.tp2_price = validated["tp2_price"]
        setup.total_risk_money = payload.total_risk_money
        setup.risk_per_order = validated["risk_per_order"]
        setup.order1_volume = validated["order1_volume"]
        setup.order2_volume = validated["order2_volume"]
        setup.order1_ticket = payload.order1_ticket
        setup.order2_ticket = payload.order2_ticket
        setup.status = "executed"
        setup.executed_at = validated["executed_at"]
        setup.manual_confirmed_at = setup.manual_confirmed_at or datetime.now(timezone.utc)
        setup.monitoring_status = "waiting_tp1" if payload.order_count == 2 and payload.order2_ticket else None
        setup.execution_error = None
        setup.execution_details = None
        setup.order2_be_moved_at = None
        setup.order2_be_move_error = None
        setup.order1_outcome = None
        setup.order2_outcome = None
        setup.order1_closed_at = None
        setup.order2_closed_at = None
        setup.order1_close_price = None
        setup.order2_close_price = None
        setup.order1_realized_pnl = None
        setup.order2_realized_pnl = None
        setup.setup_outcome = None
        setup.setup_outcome_recorded_at = None
        self.db.add(setup)
        self.db.flush()
        create_trade_event(
            self.db,
            user_id,
            setup.id,
            "manual_setup_updated",
            "Manual MT5 trade setup updated.",
            details=json.dumps(validated["summary"], indent=2, sort_keys=True, default=str),
        )
        self.db.commit()
        self.db.refresh(setup)
        return setup

    def _validate_manual_payload(
        self,
        *,
        account,
        payload: ManualTradeSetupCreate,
        user_id: int,
        current_setup_id: int | None = None,
    ) -> dict[str, object]:
        if payload.order_count == 1 and payload.order2_ticket is not None:
            raise ValueError("Single-order manual setups cannot include Order 2 ticket.")
        if payload.order_count == 2 and payload.order2_ticket is None:
            raise ValueError("Two-order manual setups require both MT5 tickets.")
        if payload.order2_ticket is not None and payload.order1_ticket == payload.order2_ticket:
            raise ValueError("Order 1 and Order 2 tickets must be different.")

        r_value = abs(float(payload.estimated_entry) - float(payload.sl_price))
        if r_value <= 0:
            raise ValueError("Entry plan and stop loss plan must produce a positive 1R distance.")
        if payload.side == "buy" and float(payload.sl_price) >= float(payload.estimated_entry):
            raise ValueError("For buy manual setups, stop loss plan must be below entry plan.")
        if payload.side == "sell" and float(payload.sl_price) <= float(payload.estimated_entry):
            raise ValueError("For sell manual setups, stop loss plan must be above entry plan.")

        snapshots = self._load_ticket_snapshots(account=account, payload=payload)
        order1_snapshot = snapshots[0]
        order2_snapshot = snapshots[1] if len(snapshots) > 1 else None

        self._assert_ticket_not_already_linked(payload.order1_ticket, current_setup_id=current_setup_id)
        if payload.order2_ticket is not None:
            self._assert_ticket_not_already_linked(payload.order2_ticket, current_setup_id=current_setup_id)

        for snapshot in snapshots:
            if snapshot.symbol != payload.symbol:
                raise ValueError(
                    f"Ticket {snapshot.ticket} belongs to symbol {snapshot.symbol}, not {payload.symbol}."
                )
            if snapshot.side != payload.side:
                raise ValueError(
                    f"Ticket {snapshot.ticket} belongs to side {snapshot.side.upper()}, not {payload.side.upper()}."
                )

        if order2_snapshot and order1_snapshot.open_time and order2_snapshot.open_time:
            if abs(order1_snapshot.open_time - order2_snapshot.open_time) > MANUAL_TICKET_TIME_WINDOW:
                raise ValueError("Order 1 and Order 2 tickets are too far apart in time to register as one setup.")

        tp1_price = (
            float(payload.tp1_price)
            if payload.tp1_price is not None
            else self._derive_target_price(
                side=payload.side,
                entry_price=float(payload.estimated_entry),
                r_value=r_value,
                rr_multiple=1.0,
            )
        )
        tp2_price = (
            float(payload.tp2_price)
            if payload.tp2_price is not None
            else self._derive_target_price(
                side=payload.side,
                entry_price=float(payload.estimated_entry),
                r_value=r_value,
                rr_multiple=float(payload.rr_order2),
            )
        )
        executed_at = min(
            [snapshot.open_time for snapshot in snapshots if snapshot.open_time is not None] or [datetime.now(timezone.utc)]
        )
        risk_per_order = float(payload.total_risk_money) / float(payload.order_count)

        return {
            "r_value": r_value,
            "tp1_price": tp1_price,
            "tp2_price": tp2_price,
            "risk_per_order": risk_per_order,
            "order1_volume": order1_snapshot.volume,
            "order2_volume": order2_snapshot.volume if order2_snapshot is not None else 0.0,
            "executed_at": executed_at,
            "summary": {
                "order_count": payload.order_count,
                "order1_ticket": payload.order1_ticket,
                "order2_ticket": payload.order2_ticket,
                "symbol": payload.symbol,
                "side": payload.side,
                "executed_at": executed_at,
                "ticket_snapshots": [
                    {
                        "ticket": snapshot.ticket,
                        "status": snapshot.status,
                        "symbol": snapshot.symbol,
                        "side": snapshot.side,
                        "volume": snapshot.volume,
                        "open_price": snapshot.open_price,
                        "open_time": snapshot.open_time,
                        "close_price": snapshot.close_price,
                        "close_time": snapshot.close_time,
                    }
                    for snapshot in snapshots
                ],
            },
        }

    def _load_ticket_snapshots(self, *, account, payload: ManualTradeSetupCreate) -> list[ManualTicketSnapshot]:
        adapter = self.adapter_factory(account)
        try:
            adapter.connect()
            account_info = adapter.get_account_info()
            persist_session_matched(self.db, account, account_info=account_info)
            snapshots = [self._inspect_ticket(adapter, payload.order1_ticket)]
            if payload.order2_ticket is not None:
                snapshots.append(self._inspect_ticket(adapter, payload.order2_ticket))
            return snapshots
        except AdapterError as exc:
            persist_session_failure(self.db, account, error=exc)
            raise
        finally:
            close = getattr(adapter, "close", None)
            if callable(close):
                close()

    def _inspect_ticket(self, adapter, ticket: int) -> ManualTicketSnapshot:
        position = adapter.get_position(position_ticket=int(ticket))
        if position is not None:
            return ManualTicketSnapshot(
                ticket=int(ticket),
                symbol=str(position.get("symbol") or "").upper(),
                side=self._normalize_side(position.get("side") or position.get("type")),
                volume=float(position.get("volume") or 0.0),
                open_price=self._coerce_float(position.get("price_open")),
                open_time=self._coerce_datetime(position.get("time")),
                close_price=None,
                close_time=None,
                status="open",
            )

        history = adapter.get_position_history(position_ticket=int(ticket))
        filtered = self._filter_history_for_ticket(history, ticket=ticket)
        if not filtered:
            raise ValueError(f"Ticket {ticket} was not found on the selected MT5 account.")

        open_deals = [deal for deal in filtered if deal.get("entry") in {0, "in", 2, "in_by", "entry"}]
        close_deals = [deal for deal in filtered if deal.get("entry") in {1, 3, "out", "out_by", "out_by_reverse", "close"}]
        first_open = open_deals[0] if open_deals else filtered[0]
        last_close = close_deals[-1] if close_deals else filtered[-1]
        symbol = str(first_open.get("symbol") or last_close.get("symbol") or "").upper()
        side = self._normalize_side(first_open.get("type") or last_close.get("type"))
        volume = self._coerce_float(first_open.get("volume")) or self._coerce_float(last_close.get("volume")) or 0.0
        return ManualTicketSnapshot(
            ticket=int(ticket),
            symbol=symbol,
            side=side,
            volume=volume,
            open_price=self._coerce_float(first_open.get("price")),
            open_time=self._coerce_datetime(first_open.get("time")),
            close_price=self._coerce_float(last_close.get("price")),
            close_time=self._coerce_datetime(last_close.get("time")),
            status="closed",
        )

    def _assert_ticket_not_already_linked(self, ticket: int, *, current_setup_id: int | None) -> None:
        query = self.db.query(TradeSetup).filter(or_(TradeSetup.order1_ticket == ticket, TradeSetup.order2_ticket == ticket))
        if current_setup_id is not None:
            query = query.filter(TradeSetup.id != current_setup_id)
        existing = query.first()
        if existing:
            raise ValueError(f"Ticket {ticket} is already linked to setup #{existing.id}.")

    def _derive_target_price(self, *, side: str, entry_price: float, r_value: float, rr_multiple: float) -> float:
        distance = r_value * rr_multiple
        if side == "buy":
            return entry_price + distance
        return entry_price - distance

    def _normalize_side(self, value: object) -> str:
        normalized = str(value or "").lower()
        if normalized in {"buy", "0"}:
            return "buy"
        if normalized in {"sell", "1"}:
            return "sell"
        return normalized

    def _coerce_float(self, value: object) -> float | None:
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _coerce_datetime(self, value: object) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        return None

    def _filter_history_for_ticket(self, history: list[dict[str, object]], *, ticket: int) -> list[dict[str, object]]:
        matching = [
            deal
            for deal in history
            if deal.get("position_id") in {None, ticket}
            and deal.get("position") in {None, ticket}
        ]
        return matching or history
