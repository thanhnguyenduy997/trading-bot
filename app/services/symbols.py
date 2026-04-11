from sqlalchemy.orm import Session

from app.execution.base import AdapterError
from app.models.allowed_symbol import AllowedSymbol
from app.schemas.allowed_symbol import AllowedSymbolCreate, AllowedSymbolUpdate
from app.services.execution import TradingAccountExecutionService
from app.services.trading_accounts import get_trading_account


def list_allowed_symbols(db: Session, active_only: bool = False) -> list[AllowedSymbol]:
    query = db.query(AllowedSymbol).order_by(AllowedSymbol.symbol_name.asc())
    if active_only:
        query = query.filter(AllowedSymbol.is_active.is_(True))
    return query.all()


def create_allowed_symbol(db: Session, payload: AllowedSymbolCreate) -> AllowedSymbol:
    existing = db.query(AllowedSymbol).filter(AllowedSymbol.symbol_name == payload.symbol_name).first()
    if existing:
        raise ValueError("Allowed symbol already exists.")
    symbol = AllowedSymbol(**payload.model_dump())
    db.add(symbol)
    db.commit()
    db.refresh(symbol)
    return symbol


def get_allowed_symbol(db: Session, symbol_id: int) -> AllowedSymbol | None:
    return db.query(AllowedSymbol).filter(AllowedSymbol.id == symbol_id).first()


def update_allowed_symbol(db: Session, symbol_id: int, payload: AllowedSymbolUpdate) -> AllowedSymbol | None:
    symbol = get_allowed_symbol(db, symbol_id)
    if not symbol:
        return None
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(symbol, field, value)
    db.add(symbol)
    db.commit()
    db.refresh(symbol)
    return symbol


class SymbolPolicyService:
    def __init__(self, db: Session, execution_service: TradingAccountExecutionService | None = None) -> None:
        self.db = db
        self.execution_service = execution_service or TradingAccountExecutionService(db)

    def list_selectable_symbols(self, *, account_id: int, user_id: int) -> list[dict[str, str]]:
        account = get_trading_account(self.db, account_id, user_id)
        if not account:
            raise LookupError("Trading account not found")

        allowed = {item.symbol_name: item for item in list_allowed_symbols(self.db, active_only=True)}
        available = {
            entry["symbol"]: entry
            for entry in self.execution_service.list_symbols(account_id, user_id)
            if entry.get("symbol") in allowed
        }

        result: list[dict[str, str]] = []
        for symbol_name in sorted(available):
            symbol = allowed[symbol_name]
            result.append(
                {
                    "symbol_name": symbol.symbol_name,
                    "display_name": symbol.display_name or symbol.symbol_name,
                }
            )
        return result

    def assert_symbol_allowed_for_account(self, *, account_id: int, user_id: int, symbol: str) -> None:
        normalized_symbol = symbol.upper()
        allowed = (
            self.db.query(AllowedSymbol)
            .filter(AllowedSymbol.symbol_name == normalized_symbol, AllowedSymbol.is_active.is_(True))
            .first()
        )
        if not allowed:
            raise ValueError("Symbol is not allowed by admin policy.")

        selectable = {item["symbol_name"] for item in self.list_selectable_symbols(account_id=account_id, user_id=user_id)}
        if normalized_symbol not in selectable:
            raise ValueError("Symbol is not available on the selected MT5 account.")
