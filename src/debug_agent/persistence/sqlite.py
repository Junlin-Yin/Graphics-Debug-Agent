from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from debug_agent.persistence.settings import (
    LEGACY_SCHEMA_USER_VERSIONS,
    PHASE_3_5_LEGACY_SCHEMA_USER_VERSIONS,
    PHASE_3_5_READ_ONLY_SCHEMA_FAILURE_GUIDANCE,
    PHASE_3_5_SCHEMA_USER_VERSION,
    PHASE_3_5_STARTUP_LEGACY_RESET_GUIDANCE,
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
        return cls._bootstrap(
            workspace_root,
            expected_user_version=PHASE_3_SCHEMA_USER_VERSION,
            legacy_user_versions=LEGACY_SCHEMA_USER_VERSIONS,
            delete_sidecars=False,
            startup_reset_guidance=STARTUP_LEGACY_RESET_GUIDANCE,
            read_only_guidance=READ_ONLY_SCHEMA_FAILURE_GUIDANCE,
        )

    @classmethod
    def bootstrap_phase_3_5_internal(cls, workspace_root: str | Path) -> Self:
        return cls._bootstrap(
            workspace_root,
            expected_user_version=PHASE_3_5_SCHEMA_USER_VERSION,
            legacy_user_versions=PHASE_3_5_LEGACY_SCHEMA_USER_VERSIONS,
            delete_sidecars=True,
            startup_reset_guidance=PHASE_3_5_STARTUP_LEGACY_RESET_GUIDANCE,
            read_only_guidance=PHASE_3_5_READ_ONLY_SCHEMA_FAILURE_GUIDANCE,
        )

    @classmethod
    def _bootstrap(
        cls,
        workspace_root: str | Path,
        *,
        expected_user_version: int,
        legacy_user_versions: frozenset[int],
        delete_sidecars: bool,
        startup_reset_guidance: str,
        read_only_guidance: str,
    ) -> Self:
        sessions_root = Path(workspace_root).resolve() / ".sessions"
        try:
            sessions_root.mkdir(parents=True, exist_ok=True)
            db_path = sessions_root / "runtime.db"
            existed = db_path.exists()
            startup_messages: list[str] = []
            if existed:
                user_version = _read_user_version(db_path)
                if user_version in legacy_user_versions:
                    _delete_runtime_database_files(db_path, include_sidecars=delete_sidecars)
                    existed = False
                    startup_messages.append(startup_reset_guidance)
                elif user_version != expected_user_version:
                    raise _schema_error(
                        user_version,
                        startup=True,
                        read_only_guidance=read_only_guidance,
                    )
            connection = sqlite3.connect(db_path, check_same_thread=False)
            connection.execute("PRAGMA foreign_keys = ON")
            if existed:
                _validate_open_connection_user_version(
                    connection,
                    startup=True,
                    expected_user_version=expected_user_version,
                    read_only_guidance=read_only_guidance,
                )
            connection.executescript(SQLITE_SCHEMA)
            connection.execute(f"PRAGMA user_version = {expected_user_version}")
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
        return cls._bootstrap_read_only(
            workspace_root,
            expected_user_version=PHASE_3_SCHEMA_USER_VERSION,
            legacy_user_versions=LEGACY_SCHEMA_USER_VERSIONS,
            read_only_guidance=READ_ONLY_SCHEMA_FAILURE_GUIDANCE,
        )

    @classmethod
    def bootstrap_phase_3_5_read_only_internal(
        cls, workspace_root: str | Path
    ) -> Self | None:
        return cls._bootstrap_read_only(
            workspace_root,
            expected_user_version=PHASE_3_5_SCHEMA_USER_VERSION,
            legacy_user_versions=PHASE_3_5_LEGACY_SCHEMA_USER_VERSIONS,
            read_only_guidance=PHASE_3_5_READ_ONLY_SCHEMA_FAILURE_GUIDANCE,
        )

    @classmethod
    def open_phase_3_5_existing_read_write(cls, workspace_root: str | Path) -> Self | None:
        return cls._open_existing_read_write(
            workspace_root,
            expected_user_version=PHASE_3_5_SCHEMA_USER_VERSION,
            legacy_user_versions=PHASE_3_5_LEGACY_SCHEMA_USER_VERSIONS,
            read_only_guidance=PHASE_3_5_READ_ONLY_SCHEMA_FAILURE_GUIDANCE,
        )

    @classmethod
    def _bootstrap_read_only(
        cls,
        workspace_root: str | Path,
        *,
        expected_user_version: int,
        legacy_user_versions: frozenset[int],
        read_only_guidance: str,
    ) -> Self | None:
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
                _validate_open_connection_user_version(
                    connection,
                    startup=False,
                    expected_user_version=expected_user_version,
                    legacy_user_versions=legacy_user_versions,
                    read_only_guidance=read_only_guidance,
                )
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

    @classmethod
    def _open_existing_read_write(
        cls,
        workspace_root: str | Path,
        *,
        expected_user_version: int,
        legacy_user_versions: frozenset[int],
        read_only_guidance: str,
    ) -> Self | None:
        sessions_root = Path(workspace_root).resolve() / ".sessions"
        db_path = sessions_root / "runtime.db"
        if not db_path.exists():
            return None
        try:
            connection = sqlite3.connect(
                f"file:{db_path.as_posix()}?mode=rw",
                uri=True,
                check_same_thread=False,
            )
            connection.execute("PRAGMA foreign_keys = ON")
            try:
                _validate_open_connection_user_version(
                    connection,
                    startup=False,
                    expected_user_version=expected_user_version,
                    legacy_user_versions=legacy_user_versions,
                    read_only_guidance=read_only_guidance,
                )
            except RuntimeBootstrapError:
                connection.close()
                raise
        except RuntimeBootstrapError:
            raise
        except (OSError, sqlite3.DatabaseError) as exc:
            raise RuntimeBootstrapError(
                f"Runtime database read-write open failed: {exc}",
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
    connection: sqlite3.Connection,
    *,
    startup: bool,
    expected_user_version: int,
    legacy_user_versions: frozenset[int] | None = None,
    read_only_guidance: str = READ_ONLY_SCHEMA_FAILURE_GUIDANCE,
) -> None:
    row = connection.execute("PRAGMA user_version").fetchone()
    user_version = int(row[0]) if row is not None else 0
    if user_version != expected_user_version:
        raise _schema_error(
            user_version,
            startup=startup,
            legacy_user_versions=legacy_user_versions,
            read_only_guidance=read_only_guidance,
        )


def _schema_error(
    user_version: int,
    *,
    startup: bool,
    legacy_user_versions: frozenset[int] | None = None,
    read_only_guidance: str = READ_ONLY_SCHEMA_FAILURE_GUIDANCE,
) -> RuntimeBootstrapError:
    legacy_versions = (
        LEGACY_SCHEMA_USER_VERSIONS
        if legacy_user_versions is None
        else legacy_user_versions
    )
    if user_version == 0:
        reason = "schema_version_missing"
    elif user_version in legacy_versions:
        reason = "legacy_schema_version"
    else:
        reason = "unknown_schema_version"
    guidance = STARTUP_LEGACY_RESET_GUIDANCE if startup else read_only_guidance
    if reason == "unknown_schema_version":
        guidance = read_only_guidance
    message = f"{guidance} Found user_version={user_version}."
    return RuntimeBootstrapError(
        message,
        error_class="config_error",
        reason=reason,
        recoverable=False,
    )


def _delete_runtime_database_files(db_path: Path, *, include_sidecars: bool) -> None:
    try:
        db_path.unlink()
        if not include_sidecars:
            return
        for suffix in ("-wal", "-shm"):
            sidecar = db_path.with_name(f"{db_path.name}{suffix}")
            try:
                sidecar.unlink()
            except FileNotFoundError:
                pass
    except OSError as exc:
        raise RuntimeBootstrapError(
            f"Runtime database legacy reset failed: {exc}",
            error_class="persistence_error",
            reason="persistence_write_failed",
            recoverable=False,
        ) from exc


def validate_phase_3_5_fresh_session_paths(
    workspace_root: str | Path, session_id: str
) -> None:
    sessions_root = Path(workspace_root).resolve() / ".sessions"
    session_root = sessions_root / session_id
    candidate_paths = (
        session_root,
        session_root / "logs",
        session_root / "artifacts",
        session_root / "checkpoint-payloads",
        session_root / "tmp",
    )
    collided = next((path for path in candidate_paths if path.exists()), None)
    if collided is None:
        return
    raise RuntimeBootstrapError(
        (
            "Fresh Phase 3.5 runtime path allocation collided with an existing "
            f"legacy .sessions/ path: {collided}. Legacy files may remain but "
            "are not reused or interpreted."
        ),
        error_class="persistence_error",
        reason="persistence_write_failed",
        recoverable=False,
    )
