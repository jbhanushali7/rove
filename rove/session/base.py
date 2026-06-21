from abc import ABC, abstractmethod


class SessionStore(ABC):
    @abstractmethod
    def load(self) -> dict | None: ...

    @abstractmethod
    def save_state(self, storage_state: dict) -> None: ...
