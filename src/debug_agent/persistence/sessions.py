from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class SessionStore:
    connection: sqlite3.Connection
