from __future__ import annotations

import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path

from flask import Flask, current_app, g


def connect(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.create_function(
        "unicode_casefold",
        1,
        lambda value: str(value or "").casefold(),
        deterministic=True,
    )
    connection.create_collation(
        "UNICODE_NOCASE",
        lambda left, right: (str(left).casefold() > str(right).casefold())
        - (str(left).casefold() < str(right).casefold()),
    )
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = NORMAL")
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


@contextmanager
def database_connection(path: str | Path) -> Iterator[sqlite3.Connection]:
    connection = connect(path)
    try:
        yield connection
    finally:
        connection.close()


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(_error: BaseException | None = None) -> None:
    connection = g.pop("db", None)
    if connection is not None:
        connection.close()


def run_migrations(path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with database_connection(path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            )
            """
        )
        applied = {
            row["version"] for row in connection.execute("SELECT version FROM schema_migrations")
        }
        migration_dir = resources.files("arxiv_cortex").joinpath("migrations")
        migrations = sorted(item for item in migration_dir.iterdir() if item.name.endswith(".sql"))
        for migration in migrations:
            version = migration.name.split("_", 1)[0]
            if version in applied:
                continue
            script = migration.read_text(encoding="utf-8")
            connection.executescript(script)
            connection.execute(
                "INSERT INTO schema_migrations(version) VALUES (?)",
                (version,),
            )


def load_or_create_secret(path: str | Path) -> str:
    secret_path = Path(path)
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    if secret_path.exists():
        return secret_path.read_text(encoding="utf-8").strip()
    value = secrets.token_urlsafe(48)
    secret_path.write_text(value, encoding="utf-8")
    secret_path.chmod(0o600)
    return value


def init_app(app: Flask) -> None:
    run_migrations(app.config["DATABASE"])
    app.teardown_appcontext(close_db)
