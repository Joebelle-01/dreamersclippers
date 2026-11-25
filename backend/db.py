"""SQLite + SQLModel initialization and session factory management."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, event
from sqlalchemy.orm import sessionmaker
from sqlmodel import Session, SQLModel, create_engine

_ENGINE_CACHE: dict[Path, Engine] = {}


def _configure_sqlite_connection(dbapi_connection: Any, _connection_record: Any) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def get_engine(db_path: Path | str) -> Engine:
    """Return a cached SQLModel SQLite engine configured with WAL mode."""
    resolved = Path(db_path).resolve()
    engine = _ENGINE_CACHE.get(resolved)
    if engine is None:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(
            f"sqlite:///{resolved.as_posix()}",
            echo=False,
            connect_args={"check_same_thread": False, "timeout": 30.0},
        )
        event.listen(engine, "connect", _configure_sqlite_connection)
        _ENGINE_CACHE[resolved] = engine
    return engine


_CLIPITEM_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("captions_enabled", "ALTER TABLE clipitem ADD COLUMN captions_enabled BOOLEAN NOT NULL DEFAULT 1"),
    ("caption_style", "ALTER TABLE clipitem ADD COLUMN caption_style VARCHAR NOT NULL DEFAULT 'bold_pop'"),
    ("caption_position", "ALTER TABLE clipitem ADD COLUMN caption_position VARCHAR NOT NULL DEFAULT 'seam'"),
    ("captions_json", "ALTER TABLE clipitem ADD COLUMN captions_json VARCHAR NOT NULL DEFAULT '[]'"),
    ("export_srt_path", "ALTER TABLE clipitem ADD COLUMN export_srt_path VARCHAR"),
    ("export_thumb_path", "ALTER TABLE clipitem ADD COLUMN export_thumb_path VARCHAR"),
    ("thumb_time_sec", "ALTER TABLE clipitem ADD COLUMN thumb_time_sec FLOAT"),
    ("thumb_title_overlay", "ALTER TABLE clipitem ADD COLUMN thumb_title_overlay BOOLEAN NOT NULL DEFAULT 0"),
    ("hook_enabled", "ALTER TABLE clipitem ADD COLUMN hook_enabled BOOLEAN NOT NULL DEFAULT 0"),
    ("hook_text", "ALTER TABLE clipitem ADD COLUMN hook_text VARCHAR NOT NULL DEFAULT ''"),
    ("hook_style", "ALTER TABLE clipitem ADD COLUMN hook_style VARCHAR NOT NULL DEFAULT 'tiktok_white'"),
    (
        "hook_duration_mode",
        "ALTER TABLE clipitem ADD COLUMN hook_duration_mode VARCHAR NOT NULL DEFAULT 'full'",
    ),
)

_CHANNEL_MIGRATIONS: tuple[tuple[str, str], ...] = (
    (
        "auto_monitor_online",
        "ALTER TABLE channel ADD COLUMN auto_monitor_online BOOLEAN NOT NULL DEFAULT 0",
    ),
    ("broadcaster_id", "ALTER TABLE channel ADD COLUMN broadcaster_id VARCHAR"),
)


def _migrate_clipitem_columns(engine: Engine) -> None:
    """Idempotently add new ClipItem columns to existing SQLite databases."""
    with engine.begin() as conn:
        rows = conn.exec_driver_sql("PRAGMA table_info(clipitem)").fetchall()
        existing = {str(row[1]) for row in rows}
        for col_name, ddl in _CLIPITEM_MIGRATIONS:
            if col_name not in existing:
                conn.exec_driver_sql(ddl)


def _migrate_channel_columns(engine: Engine) -> None:
    """Idempotently add new Channel columns to existing SQLite databases."""
    with engine.begin() as conn:
        rows = conn.exec_driver_sql("PRAGMA table_info(channel)").fetchall()
        existing = {str(row[1]) for row in rows}
        for col_name, ddl in _CHANNEL_MIGRATIONS:
            if col_name not in existing:
                conn.exec_driver_sql(ddl)


def init_db(db_path: Path | str) -> Engine:
    """Initialize SQLite schema tables, apply idempotent column migrations, and return Engine."""
    import backend.models  # noqa: F401 - register SQLModel table metadata

    engine = get_engine(db_path)
    SQLModel.metadata.create_all(engine)
    _migrate_clipitem_columns(engine)
    _migrate_channel_columns(engine)
    return engine


def get_session_factory(db_path: Path | str) -> Callable[[], Session]:
    """Return a callable producing SQLModel Session instances with expire_on_commit=False."""
    engine = get_engine(db_path)
    return sessionmaker(bind=engine, class_=Session, expire_on_commit=False)


def dispose_engines() -> None:
    """Dispose all cached SQLite engines on application shutdown."""
    for engine in _ENGINE_CACHE.values():
        engine.dispose()
    _ENGINE_CACHE.clear()
