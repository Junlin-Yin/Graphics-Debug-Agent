from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from debug_agent.persistence.settings import (
    LEGACY_SCHEMA_USER_VERSIONS,
    PHASE_2_SCHEMA_USER_VERSION,
    PHASE_3_SCHEMA_USER_VERSION,
    READ_ONLY_SCHEMA_FAILURE_GUIDANCE,
    SQLITE_SCHEMA,
    STARTUP_LEGACY_RESET_GUIDANCE,
    UNSUPPORTED_PHASE_2_DATABASE_MESSAGE,
)
from debug_agent.runtime.errors import NormalizedError


class RuntimeBootstrapError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        error_class: str = "config_error",
        reason: str = "startup_schema_validation_failed",
        source: str = "persistence",
        recoverable: bool = True,
        normalized_error: NormalizedError | None = None,
    ) -> None:
        super().__init__(message)
        self.error_class = error_class
        self.reason = reason
        self.source = source
        self.recoverable = recoverable
        self.normalized_error = normalized_error or NormalizedError.create(
            error_class,
            reason,
            message=message,
            scope="persistence",
        )


@dataclass
class RuntimeDatabase:
    path: Path
    connection: sqlite3.Connection
    startup_messages: tuple[str, ...] = ()

    @classmethod
    def bootstrap(cls, workspace_root: str | Path) -> Self:
        sessions_root = Path(workspace_root).resolve() / ".sessions"
        try:
            sessions_root.mkdir(parents=True, exist_ok=True)
            db_path = sessions_root / "runtime.db"
            existed = db_path.exists()
            startup_messages: list[str] = []
            if existed:
                user_version = _read_user_version(db_path)
                if user_version in LEGACY_SCHEMA_USER_VERSIONS:
                    db_path.unlink()
                    existed = False
                    startup_messages.append(STARTUP_LEGACY_RESET_GUIDANCE)
                elif user_version != PHASE_3_SCHEMA_USER_VERSION:
                    raise _schema_error(user_version, startup=True)
            connection = sqlite3.connect(db_path, check_same_thread=False)
            connection.execute("PRAGMA foreign_keys = ON")
            if existed:
                _validate_open_connection_user_version(connection, startup=True)
            connection.executescript(SQLITE_SCHEMA)
            connection.execute(f"PRAGMA user_version = {PHASE_3_SCHEMA_USER_VERSION}")
            connection.commit()
        except RuntimeBootstrapError:
            raise
        except (OSError, sqlite3.DatabaseError) as exc:
            raise RuntimeBootstrapError(
                f"Runtime database bootstrap failed: {exc}"
            ) from exc
        return cls(path=db_path, connection=connection, startup_messages=tuple(startup_messages))

    @classmethod
    def bootstrap_read_only(cls, workspace_root: str | Path) -> Self | None:
        sessions_root = Path(workspace_root).resolve() / ".sessions"
        db_path = sessions_root / "runtime.db"
        if not db_path.exists():
            return None
        try:
            connection = sqlite3.connect(
                f"file:{db_path.as_posix()}?mode=ro",
                uri=True,
                check_same_thread=False,
            )
            connection.execute("PRAGMA foreign_keys = ON")
            try:
                _validate_open_connection_user_version(connection, startup=False)
            except RuntimeBootstrapError:
                connection.close()
                raise
        except RuntimeBootstrapError:
            raise
        except (OSError, sqlite3.DatabaseError) as exc:
            raise RuntimeBootstrapError(
                f"Runtime database read-only bootstrap failed: {exc}",
                error_class="persistence_error",
                reason="persistence_read_failed",
            ) from exc
        return cls(path=db_path, connection=connection)

    def close(self) -> None:
        self.connection.close()


def _read_user_version(db_path: Path) -> int:
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
        row = connection.execute("PRAGMA user_version").fetchone()
    except sqlite3.DatabaseError as exc:
        raise RuntimeBootstrapError(
            f"Runtime database schema validation failed: {exc}",
            error_class="persistence_error",
            reason="persistence_read_failed",
        ) from exc
    finally:
        if connection is not None:
            connection.close()
    return int(row[0]) if row is not None else 0


def _validate_open_connection_user_version(
    connection: sqlite3.Connection, *, startup: bool
) -> None:
    row = connection.execute("PRAGMA user_version").fetchone()
    user_version = int(row[0]) if row is not None else 0
    if user_version != PHASE_3_SCHEMA_USER_VERSION:
        raise _schema_error(user_version, startup=startup)


def _schema_error(user_version: int, *, startup: bool) -> RuntimeBootstrapError:
    if user_version == 0:
        reason = "schema_version_missing"
    elif user_version in LEGACY_SCHEMA_USER_VERSIONS:
        reason = "legacy_schema_version"
    else:
        reason = "unknown_schema_version"
    guidance = STARTUP_LEGACY_RESET_GUIDANCE if startup else READ_ONLY_SCHEMA_FAILURE_GUIDANCE
    if reason == "unknown_schema_version":
        guidance = READ_ONLY_SCHEMA_FAILURE_GUIDANCE
    message = f"{guidance} Found user_version={user_version}."
    return RuntimeBootstrapError(
        message,
        error_class="config_error",
        reason=reason,
        recoverable=False,
    )
