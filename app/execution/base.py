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
