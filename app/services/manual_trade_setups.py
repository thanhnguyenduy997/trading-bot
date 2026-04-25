from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import logging

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.execution.base import AdapterError
from app.models.mt5_trade_history import MT5TradeHistory
from app.models.trade_setup import TradeSetup
from app.schemas.trade_setup import ManualTradeSetupCreate
from app.services.execution import default_adapter_factory
from app.services.mt5_session_state import persist_session_failure, persist_session_matched
from app.services.risk_service import RiskService
from app.services.trade_events import create_trade_event
from app.services.trade_setups import get_trade_setup
from app.services.trading_accounts import get_trading_account


MANUAL_TICKET_TIME_WARNING_WINDOW = timedelta(minutes=15)
MANUAL_ENTRY_PRICE_WARNING_POINTS = Decimal("0.50")
logger = logging.getLogger(__name__)


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
    sl_price: float | None = None


class ManualTradeSetupService:
    def __init__(self, db: Session, adapter_factory=None) -> None:
        self.db = db
        self.adapter_factory = adapter_factory or default_adapter_factory
        self.risk_service = RiskService()

    def list_unlinked_manual_trades(self, *, user_id: int) -> list[MT5TradeHistory]:
        return (
            self.db.query(MT5TradeHistory)
            .filter(
                MT5TradeHistory.user_id == user_id,
                MT5TradeHistory.trade_source == "manual",
                MT5TradeHistory.linked_setup_id.is_(None),
            )
            .order_by(MT5TradeHistory.close_time.desc().nulls_last(), MT5TradeHistory.id.desc())
            .all()
        )

    def build_prefill_from_selected_trades(
        self,
        *,
        user_id: int,
        selected_trade_ids: list[int],
    ) -> dict[str, object]:
        if not selected_trade_ids:
            raise ValueError("Select 1 or 2 unlinked manual MT5 trades to auto-fill the setup.")
        if len(selected_trade_ids) > 2:
            raise ValueError("Manual setup auto-fill supports 1 or 2 selected MT5 trades.")

        trades = (
            self.db.query(MT5TradeHistory)
            .filter(
                MT5TradeHistory.user_id == user_id,
                MT5TradeHistory.id.in_(selected_trade_ids),
                MT5TradeHistory.trade_source == "manual",
            )
            .order_by(MT5TradeHistory.open_time.asc().nulls_last(), MT5TradeHistory.id.asc())
            .all()
        )
        if len(trades) != len(set(selected_trade_ids)):
            raise ValueError("One or more selected trades are unavailable for manual setup linking.")
        if any(trade.linked_setup_id for trade in trades):
            raise ValueError("Selected MT5 trade is already linked to a setup.")

        account_ids = {trade.trading_account_id for trade in trades}
        if len(account_ids) != 1:
            raise ValueError("Selected MT5 trades must belong to the same trading account.")
        symbols = {self._normalize_symbol(trade.symbol) for trade in trades}
        if len(symbols) != 1:
            raise ValueError("Selected MT5 trades must have the same symbol.")
        sides = {self._canonical_side(trade.side) for trade in trades}
        if len(sides) != 1:
            raise ValueError("Selected MT5 trades must have the same side.")

        account = get_trading_account(self.db, trades[0].trading_account_id, user_id)
        if not account:
            raise LookupError("Trading account not found")

        snapshots = self._load_ticket_snapshots(account=account, tickets=[int(trade.position_ticket) for trade in trades])
        self._assert_snapshots_match_selected_trades(trades=trades, snapshots=snapshots)

        total_volume = sum(Decimal(str(snapshot.volume)) for snapshot in snapshots)
        weighted_entry = self._weighted_entry_price(snapshots)
        rr_order2 = float(account.default_rr_order_2 or 2.0)
        derived_sl, warnings = self._derive_stop_loss(snapshots=snapshots, trades=trades)

        tp1_price = None
        tp2_price = None
        total_risk_money = None
        risk_message = None

        if weighted_entry is not None and derived_sl is not None:
            r_value = abs(weighted_entry - derived_sl)
            tp1_price = self._derive_target_price(
                side=trades[0].side,
                entry_price=weighted_entry,
                r_value=r_value,
                rr_multiple=Decimal("1"),
            )
            tp2_price = self._derive_target_price(
                side=trades[0].side,
                entry_price=weighted_entry,
                r_value=r_value,
                rr_multiple=Decimal(str(rr_order2)),
            )
            total_risk_money, risk_message = self._derive_total_risk_money(account=account, snapshots=snapshots, stop_loss=derived_sl)
        else:
            risk_message = "Stop loss is missing, so total risk and TP targets need manual review."

        if len(snapshots) == 2:
            warning = self._grouping_warning(snapshots)
            if warning:
                warnings.append(warning)

        messages = []
        if derived_sl is None:
            messages.append("Stop loss could not be derived from the selected MT5 trades. Enter it manually.")
        if risk_message:
            messages.append(risk_message)

        autofilled_fields = {
            "trading_account_id",
            "symbol",
            "side",
            "estimated_entry",
            "rr_order2",
            "order_count",
            "order1_ticket",
        }
        if len(trades) == 2:
            autofilled_fields.add("order2_ticket")
        if derived_sl is not None:
            autofilled_fields.add("sl_price")
        if total_risk_money is not None:
            autofilled_fields.add("total_risk_money")
        if tp1_price is not None:
            autofilled_fields.add("tp1_price")
        if tp2_price is not None:
            autofilled_fields.add("tp2_price")

        form_data = {
            "trading_account_id": account.id,
            "symbol": self._normalize_symbol(trades[0].symbol),
            "side": self._ui_side(trades[0].side),
            "estimated_entry": float(weighted_entry) if weighted_entry is not None else "",
            "sl_price": float(derived_sl) if derived_sl is not None else "",
            "total_risk_money": float(total_risk_money) if total_risk_money is not None else "",
            "rr_order2": rr_order2,
            "tp1_price": float(tp1_price) if tp1_price is not None else "",
            "tp2_price": float(tp2_price) if tp2_price is not None else "",
            "order_count": len(trades),
            "order1_ticket": int(trades[0].position_ticket),
            "order2_ticket": int(trades[1].position_ticket) if len(trades) == 2 else "",
        }
        return {
            "form_data": form_data,
            "selected_trades": trades,
            "autofilled_fields": autofilled_fields,
            "messages": messages,
            "warnings": warnings,
            "summary": {
                "weighted_entry_price": float(weighted_entry) if weighted_entry is not None else None,
                "derived_sl_price": float(derived_sl) if derived_sl is not None else None,
                "derived_total_risk_money": float(total_risk_money) if total_risk_money is not None else None,
                "derived_tp1_price": float(tp1_price) if tp1_price is not None else None,
                "derived_tp2_price": float(tp2_price) if tp2_price is not None else None,
                "rr_order_2": rr_order2,
            },
        }

    def derive_fields_from_form(
        self,
        *,
        user_id: int,
        trading_account_id: int | None,
        symbol: str | None,
        side: str | None,
        estimated_entry: float | None,
        sl_price: float | None,
        rr_order2: float | None,
        order_count: int | None,
        order1_ticket: int | None,
        order2_ticket: int | None,
    ) -> dict[str, object]:
        messages: list[str] = []
        warnings: list[str] = []
        if not trading_account_id:
            return self._empty_derivation("Select a trading account first.")
        account = get_trading_account(self.db, trading_account_id, user_id)
        if not account:
            raise LookupError("Trading account not found")

        if not order1_ticket:
            return self._empty_derivation("Select at least one manual MT5 trade first.")
        if not order_count:
            order_count = 1 if not order2_ticket else 2
        if order_count == 2 and not order2_ticket:
            return self._empty_derivation("Choose the second manual MT5 trade to derive combined risk.")

        tickets = [int(order1_ticket)] + ([int(order2_ticket)] if order_count == 2 and order2_ticket else [])
        snapshots = self._load_ticket_snapshots(account=account, tickets=tickets)
        if len(snapshots) != len(tickets):
            return self._empty_derivation("Selected MT5 tickets are unavailable for derivation.")

        if symbol:
            normalized_symbol = self._normalize_symbol(symbol)
            for snapshot in snapshots:
                if self._normalize_symbol(snapshot.symbol) != normalized_symbol:
                    raise ValueError(
                        f"Selected ticket {snapshot.ticket} symbol mismatch: form={normalized_symbol}, live={self._normalize_symbol(snapshot.symbol)}."
                    )
        if side:
            canonical_side = self._canonical_side(side)
            for snapshot in snapshots:
                snapshot_side = self._canonical_side(snapshot.side)
                if snapshot_side is not None and canonical_side is not None and snapshot_side != canonical_side:
                    raise ValueError(
                        f"Selected ticket {snapshot.ticket} side mismatch: form={canonical_side}, live={snapshot_side}."
                    )

        warning = self._grouping_warning(snapshots)
        if warning:
            warnings.append(warning)

        if estimated_entry is None or estimated_entry <= 0:
            return self._empty_derivation("Enter a valid Entry plan to derive Total risk, TP1, and TP2.", warnings=warnings)
        if sl_price is None or sl_price <= 0:
            return self._empty_derivation("Enter a valid Stop loss plan to derive Total risk, TP1, and TP2.", warnings=warnings)
        if rr_order2 is None or rr_order2 <= 0:
            return self._empty_derivation("Enter a valid RR order 2 value to derive TP2.", warnings=warnings)

        normalized_side = self._ui_side(side)
        entry = Decimal(str(estimated_entry))
        stop = Decimal(str(sl_price))
        r_value = abs(entry - stop)
        if r_value <= 0:
            return self._empty_derivation("Entry plan and Stop loss plan must produce a positive 1R distance.", warnings=warnings)
        if normalized_side == "buy" and stop >= entry:
            return self._empty_derivation("For buy setups, Stop loss plan must be below Entry plan.", warnings=warnings)
        if normalized_side == "sell" and stop <= entry:
            return self._empty_derivation("For sell setups, Stop loss plan must be above Entry plan.", warnings=warnings)

        tp1_price = self._derive_target_price(side=normalized_side, entry_price=entry, r_value=r_value, rr_multiple=Decimal("1"))
        tp2_price = self._derive_target_price(
            side=normalized_side,
            entry_price=entry,
            r_value=r_value,
            rr_multiple=Decimal(str(rr_order2)),
        )
        total_risk_money, risk_message = self._derive_total_risk_money_from_entry(
            account=account,
            snapshots=snapshots,
            entry_price=entry,
            stop_loss=stop,
        )
        if risk_message:
            messages.append(risk_message)
        else:
            messages.append("Derived values updated from current Entry, Stop loss, RR, and selected MT5 order volumes.")

        return {
            "total_risk_money": float(total_risk_money) if total_risk_money is not None else None,
            "tp1_price": float(tp1_price),
            "tp2_price": float(tp2_price),
            "messages": messages,
            "warnings": warnings,
            "autofilled_fields": ["total_risk_money", "tp1_price", "tp2_price"] if total_risk_money is not None else ["tp1_price", "tp2_price"],
        }

    def create_manual_setup(self, *, user_id: int, payload: ManualTradeSetupCreate) -> TradeSetup:
        account = get_trading_account(self.db, payload.trading_account_id, user_id)
        if not account:
            raise LookupError("Trading account not found")

        validated = self._validate_manual_payload(account=account, payload=payload, current_setup_id=None)
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
        self._link_trade_history_rows(setup=setup, previous_tickets=set())
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

        previous_tickets = {ticket for ticket in [setup.order1_ticket, setup.order2_ticket] if ticket}
        validated = self._validate_manual_payload(account=account, payload=payload, current_setup_id=setup.id)

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
        self._link_trade_history_rows(setup=setup, previous_tickets=previous_tickets)
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
        current_setup_id: int | None,
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

        tickets = [payload.order1_ticket] + ([payload.order2_ticket] if payload.order2_ticket is not None else [])
        synced_trades = self._load_synced_manual_trades(
            user_id=account.user_id,
            trading_account_id=account.id,
            tickets=tickets,
        )
        self._assert_synced_trade_grouping(
            synced_trades=synced_trades,
            expected_symbol=payload.symbol,
            expected_side=payload.side,
        )
        snapshots = self._load_ticket_snapshots(account=account, tickets=tickets)
        order1_snapshot = snapshots[0]
        order2_snapshot = snapshots[1] if len(snapshots) > 1 else None

        self._assert_ticket_not_already_linked(payload.order1_ticket, current_setup_id=current_setup_id)
        if payload.order2_ticket is not None:
            self._assert_ticket_not_already_linked(payload.order2_ticket, current_setup_id=current_setup_id)

        snapshot_map = {snapshot.ticket: snapshot for snapshot in snapshots}
        expected_symbol = self._normalize_symbol(payload.symbol)
        for trade in synced_trades:
            snapshot = snapshot_map.get(int(trade.position_ticket))
            raw_snapshot_symbol = self._normalize_symbol(snapshot.symbol) if snapshot is not None else None
            raw_snapshot_side = snapshot.side if snapshot is not None else None
            raw_snapshot_side_canonical = self._canonical_side(raw_snapshot_side) if snapshot is not None else None
            live_side_missing = raw_snapshot_side_canonical is None
            # Register-time grouping validation must use the synced normalized trade record.
            # Raw/live MT5 side can reflect the closing deal direction on historical trades,
            # so it is debug-only context and never a blocking validation source here.
            logger.info(
                "Manual setup register validation ticket=%s synced_side=%s raw_live_side=%s raw_live_side_canonical=%s live_side_missing=%s raw_side_from_closing_lookup=%s validation_source=%s synced_symbol=%s raw_live_symbol=%s rejection_reason=%s",
                trade.position_ticket,
                self._canonical_side(trade.side),
                raw_snapshot_side,
                raw_snapshot_side_canonical,
                live_side_missing,
                snapshot.status == "closed" if snapshot is not None else False,
                "synced_manual_trade_history_only",
                self._normalize_symbol(trade.symbol),
                raw_snapshot_symbol,
                None,
            )
            if raw_snapshot_symbol and raw_snapshot_symbol != expected_symbol:
                logger.warning(
                    "Manual setup register validation rejected ticket=%s reason=raw_symbol_mismatch synced_symbol=%s raw_live_symbol=%s expected_symbol=%s validation_source=%s",
                    trade.position_ticket,
                    self._normalize_symbol(trade.symbol),
                    raw_snapshot_symbol,
                    expected_symbol,
                    "synced_manual_trade_history_only",
                )
                raise ValueError(
                    f"Ticket {trade.position_ticket} belongs to symbol {raw_snapshot_symbol}, not {expected_symbol}."
                )

        tp1_price = (
            float(payload.tp1_price)
            if payload.tp1_price is not None
            else self._derive_target_price(side=payload.side, entry_price=Decimal(str(payload.estimated_entry)), r_value=Decimal(str(r_value)), rr_multiple=Decimal("1"))
        )
        tp2_price = (
            float(payload.tp2_price)
            if payload.tp2_price is not None
            else self._derive_target_price(
                side=payload.side,
                entry_price=Decimal(str(payload.estimated_entry)),
                r_value=Decimal(str(r_value)),
                rr_multiple=Decimal(str(payload.rr_order2)),
            )
        )
        executed_at = min([snapshot.open_time for snapshot in snapshots if snapshot.open_time is not None] or [datetime.now(timezone.utc)])
        risk_per_order = float(payload.total_risk_money) / float(payload.order_count)

        warnings = []
        warning = self._grouping_warning(snapshots)
        if warning:
            warnings.append(warning)

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
                "warnings": warnings,
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
                        "sl_price": snapshot.sl_price,
                    }
                    for snapshot in snapshots
                ],
            },
        }

    def _load_synced_manual_trades(
        self,
        *,
        user_id: int,
        trading_account_id: int,
        tickets: list[int],
    ) -> list[MT5TradeHistory]:
        trades = (
            self.db.query(MT5TradeHistory)
            .filter(
                MT5TradeHistory.user_id == user_id,
                MT5TradeHistory.trading_account_id == trading_account_id,
                MT5TradeHistory.trade_source == "manual",
                MT5TradeHistory.position_ticket.in_(tickets),
            )
            .order_by(MT5TradeHistory.open_time.asc().nulls_last(), MT5TradeHistory.id.asc())
            .all()
        )
        if len(trades) != len(set(tickets)):
            raise ValueError("One or more selected MT5 trades are unavailable in synced manual trade history.")
        return trades

    def _assert_synced_trade_grouping(
        self,
        *,
        synced_trades: list[MT5TradeHistory],
        expected_symbol: str,
        expected_side: str,
    ) -> None:
        canonical_expected_symbol = self._normalize_symbol(expected_symbol)
        canonical_expected_side = self._canonical_side(expected_side)
        account_ids = {trade.trading_account_id for trade in synced_trades}
        if len(account_ids) != 1:
            raise ValueError("Selected MT5 trades must belong to the same trading account.")
        symbols = {self._normalize_symbol(trade.symbol) for trade in synced_trades}
        if len(symbols) != 1:
            raise ValueError("Selected MT5 trades must have the same symbol.")
        sides = {self._canonical_side(trade.side) for trade in synced_trades}
        if len(sides) != 1:
            raise ValueError("Selected MT5 trades must have the same side.")
        for trade in synced_trades:
            synced_symbol = self._normalize_symbol(trade.symbol)
            synced_side = self._canonical_side(trade.side)
            if synced_symbol != canonical_expected_symbol:
                logger.warning(
                    "Manual setup register validation rejected ticket=%s reason=synced_symbol_mismatch synced_symbol=%s expected_symbol=%s",
                    trade.position_ticket,
                    synced_symbol,
                    canonical_expected_symbol,
                )
                raise ValueError(
                    f"Ticket {trade.position_ticket} belongs to symbol {synced_symbol}, not {canonical_expected_symbol}."
                )
            if synced_side != canonical_expected_side:
                logger.warning(
                    "Manual setup register validation rejected ticket=%s reason=synced_side_mismatch synced_side=%s expected_side=%s",
                    trade.position_ticket,
                    synced_side,
                    canonical_expected_side,
                )
                raise ValueError(
                    f"Ticket {trade.position_ticket} belongs to side {synced_side}, not {canonical_expected_side}."
                )

    def _load_ticket_snapshots(self, *, account, tickets: list[int]) -> list[ManualTicketSnapshot]:
        adapter = self.adapter_factory(account)
        try:
            adapter.connect()
            account_info = adapter.get_account_info()
            persist_session_matched(self.db, account, account_info=account_info)
            snapshots = [self._inspect_ticket(adapter, ticket) for ticket in tickets]
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
                sl_price=self._coerce_float(position.get("sl") or position.get("stop_loss")),
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
            sl_price=self._coerce_float(first_open.get("sl") or first_open.get("stop_loss") or last_close.get("sl") or last_close.get("stop_loss")),
        )

    def _assert_snapshots_match_selected_trades(self, *, trades: list[MT5TradeHistory], snapshots: list[ManualTicketSnapshot]) -> None:
        snapshot_map = {snapshot.ticket: snapshot for snapshot in snapshots}
        for trade in trades:
            snapshot = snapshot_map.get(int(trade.position_ticket))
            if snapshot is None:
                raise ValueError(f"Selected ticket {trade.position_ticket} is unavailable on the selected MT5 account.")
            db_symbol = self._normalize_symbol(trade.symbol)
            snapshot_symbol = self._normalize_symbol(snapshot.symbol)
            db_side = self._canonical_side(trade.side)
            snapshot_side = self._canonical_side(snapshot.side)
            live_side_missing = snapshot_side is None
            logger.info(
                "Manual setup trade selection validation ticket=%s payload_side=%s db_side=%s snapshot_side=%s live_side_missing=%s db_symbol=%s snapshot_symbol=%s account_id=%s snapshot_account_check=%s",
                trade.position_ticket,
                None,
                db_side,
                snapshot_side,
                live_side_missing,
                db_symbol,
                snapshot_symbol,
                trade.trading_account_id,
                True,
            )
            if snapshot_symbol != db_symbol:
                raise ValueError(
                    f"Selected ticket {trade.position_ticket} symbol mismatch: synced={db_symbol}, live={snapshot_symbol}."
                )
            if snapshot_side is not None and snapshot_side != db_side:
                raise ValueError(
                    f"Selected ticket {trade.position_ticket} side mismatch: synced={db_side}, live={snapshot_side}."
                )

    def _derive_stop_loss(
        self,
        *,
        snapshots: list[ManualTicketSnapshot],
        trades: list[MT5TradeHistory],
    ) -> tuple[Decimal | None, list[str]]:
        warnings: list[str] = []
        sl_values = []
        for snapshot in snapshots:
            if snapshot.sl_price is not None:
                sl_values.append(Decimal(str(snapshot.sl_price)))
        if not sl_values:
            return None, warnings
        if len(set(sl_values)) > 1:
            raise ValueError("Selected MT5 trades have conflicting stop loss values. Review them manually before creating one setup.")
        return sl_values[0], warnings

    def _derive_total_risk_money(
        self,
        *,
        account,
        snapshots: list[ManualTicketSnapshot],
        stop_loss: Decimal,
    ) -> tuple[Decimal | None, str | None]:
        entry_prices = [Decimal(str(snapshot.open_price)) for snapshot in snapshots if snapshot.open_price is not None]
        if len(entry_prices) != len(snapshots):
            return None, "Selected MT5 trades are missing open price data, so total risk needs manual review."
        return self._derive_total_risk_money_from_entry(
            account=account,
            snapshots=snapshots,
            entry_price=None,
            stop_loss=stop_loss,
        )

    def _derive_total_risk_money_from_entry(
        self,
        *,
        account,
        snapshots: list[ManualTicketSnapshot],
        entry_price: Decimal | None,
        stop_loss: Decimal,
    ) -> tuple[Decimal | None, str | None]:
        try:
            adapter = self.adapter_factory(account)
            adapter.connect()
            symbol_info = adapter.get_symbol_info(snapshots[0].symbol)
        except AdapterError as exc:
            persist_session_failure(self.db, account, error=exc)
            return None, "Total risk could not be derived safely from MT5 symbol specifications. Review it manually."
        finally:
            close = locals().get("adapter")
            close_fn = getattr(close, "close", None) if close is not None else None
            if callable(close_fn):
                close_fn()

        contract_size = symbol_info.get("trade_contract_size")
        if contract_size in (None, "", 0):
            return None, "Total risk could not be derived safely from MT5 symbol specifications. Review it manually."

        total_risk = Decimal("0")
        for snapshot in snapshots:
            if entry_price is None:
                if snapshot.open_price is None:
                    return None, "Selected MT5 trades are missing open price data, so total risk needs manual review."
                entry = Decimal(str(snapshot.open_price))
            else:
                entry = entry_price
            volume = Decimal(str(snapshot.volume))
            risk_distance = abs(entry - stop_loss)
            total_risk += risk_distance * Decimal(str(contract_size)) * volume
        return total_risk.quantize(Decimal("0.01")), None

    def _weighted_entry_price(self, snapshots: list[ManualTicketSnapshot]) -> Decimal | None:
        numerator = Decimal("0")
        denominator = Decimal("0")
        for snapshot in snapshots:
            if snapshot.open_price is None:
                return None
            volume = Decimal(str(snapshot.volume))
            numerator += Decimal(str(snapshot.open_price)) * volume
            denominator += volume
        if denominator <= 0:
            return None
        return numerator / denominator

    def _grouping_warning(self, snapshots: list[ManualTicketSnapshot]) -> str | None:
        if len(snapshots) < 2:
            return None
        first, second = snapshots[0], snapshots[1]
        if first.open_time and second.open_time and abs(first.open_time - second.open_time) > MANUAL_TICKET_TIME_WARNING_WINDOW:
            return "Selected MT5 trades were opened more than 15 minutes apart. Review whether they belong to one setup."
        if first.open_price is not None and second.open_price is not None:
            price_gap = abs(Decimal(str(first.open_price)) - Decimal(str(second.open_price)))
            if price_gap > MANUAL_ENTRY_PRICE_WARNING_POINTS:
                return "Selected MT5 trades have materially different entry prices. Review the combined setup before confirming."
        return None

    def _assert_ticket_not_already_linked(self, ticket: int, *, current_setup_id: int | None) -> None:
        query = self.db.query(TradeSetup).filter(or_(TradeSetup.order1_ticket == ticket, TradeSetup.order2_ticket == ticket))
        if current_setup_id is not None:
            query = query.filter(TradeSetup.id != current_setup_id)
        existing = query.first()
        if existing:
            raise ValueError(f"Ticket {ticket} is already linked to setup #{existing.id}.")

    def _link_trade_history_rows(self, *, setup: TradeSetup, previous_tickets: set[int]) -> None:
        current_tickets = {ticket for ticket in [setup.order1_ticket, setup.order2_ticket] if ticket}
        tickets_to_clear = previous_tickets - current_tickets
        if tickets_to_clear:
            (
                self.db.query(MT5TradeHistory)
                .filter(
                    MT5TradeHistory.user_id == setup.user_id,
                    MT5TradeHistory.position_ticket.in_(list(tickets_to_clear)),
                    MT5TradeHistory.linked_setup_id == setup.id,
                )
                .update({"linked_setup_id": None}, synchronize_session=False)
            )
        if current_tickets:
            (
                self.db.query(MT5TradeHistory)
                .filter(
                    MT5TradeHistory.user_id == setup.user_id,
                    MT5TradeHistory.trading_account_id == setup.trading_account_id,
                    MT5TradeHistory.position_ticket.in_(list(current_tickets)),
                )
                .update({"linked_setup_id": setup.id}, synchronize_session=False)
            )

    def _derive_target_price(self, *, side: str, entry_price: Decimal, r_value: Decimal, rr_multiple: Decimal) -> float:
        distance = r_value * rr_multiple
        if side == "buy":
            return float(entry_price + distance)
        return float(entry_price - distance)

    def _normalize_side(self, value: object) -> str:
        normalized = str(value or "").lower()
        if normalized in {"buy", "0"}:
            return "buy"
        if normalized in {"sell", "1"}:
            return "sell"
        return normalized

    def _canonical_side(self, value: object) -> str | None:
        normalized = self._normalize_side(value)
        if normalized == "buy":
            return "BUY"
        if normalized == "sell":
            return "SELL"
        raw = str(value or "").strip()
        if not raw:
            return None
        return raw.upper()

    def _ui_side(self, value: object) -> str:
        normalized = self._normalize_side(value)
        if normalized in {"buy", "sell"}:
            return normalized
        return str(value or "").lower()

    def _normalize_symbol(self, value: object) -> str:
        return str(value or "").upper()

    def _empty_derivation(self, message: str, *, warnings: list[str] | None = None) -> dict[str, object]:
        return {
            "total_risk_money": None,
            "tp1_price": None,
            "tp2_price": None,
            "messages": [message],
            "warnings": warnings or [],
            "autofilled_fields": [],
        }

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
