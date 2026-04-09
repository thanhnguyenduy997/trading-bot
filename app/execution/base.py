from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class AdapterError(Exception):
    code: str
    message: str
    details: dict[str, object] | None = None

    def __str__(self) -> str:
        return self.message

    def to_dict(self) -> dict[str, object]:
        return {"code": self.code, "message": self.message, "details": self.details}


class ExecutionAdapter(ABC):
    @abstractmethod
    def connect(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def get_account_info(self) -> dict[str, str | int | float | None]:
        raise NotImplementedError

    @abstractmethod
    def get_quote(self, symbol: str) -> dict[str, str | int | float | None]:
        raise NotImplementedError

    @abstractmethod
    def get_symbol_info(self, symbol: str) -> dict[str, str | int | float | None]:
        raise NotImplementedError

    @abstractmethod
    def execute_setup(self, setup: object) -> dict[str, str | int | float | None]:
        raise NotImplementedError

    @abstractmethod
    def place_market_order(
        self,
        *,
        symbol: str,
        side: str,
        volume: float,
        sl: float,
        tp: float,
        comment: str,
    ) -> dict[str, object]:
        raise NotImplementedError

    @abstractmethod
    def close_position(
        self,
        *,
        symbol: str,
        side: str,
        volume: float,
        position_ticket: int,
        comment: str,
    ) -> dict[str, object]:
        raise NotImplementedError
