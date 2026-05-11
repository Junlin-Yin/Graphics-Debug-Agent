from __future__ import annotations

from dataclasses import dataclass


@dataclass
class StoreError(Exception):
    error_class: str
    message: str
    source: str = "persistence"
    recoverable: bool = False

    def __str__(self) -> str:
        return self.message
