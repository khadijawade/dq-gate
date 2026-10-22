"""Tests run against a real Postgres database, created and migrated once per session.

Point TEST_DATABASE_URL at any server you can create databases on. The default matches
docker-compose.yml. The test database is separate from the one the CLI uses by default.
"""

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, make_url, text

ROOT = Path(__file__).resolve().parent.parent
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://dq:dq@localhost:5433/dq_gate_test"
)


def _create_database_if_missing(url: str) -> None:
    parsed = make_url(url)
    admin = create_engine(parsed.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": parsed.database}
            ).scalar()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{parsed.database}"'))
    finally:
        admin.dispose()


@pytest.fixture(scope="session")
def test_database_url() -> str:
    return TEST_DATABASE_URL


@pytest.fixture(scope="session")
def engine(test_database_url: str) -> Engine:
    _create_database_if_missing(test_database_url)
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", test_database_url)
    command.upgrade(cfg, "head")
    eng = create_engine(test_database_url)
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine: Engine) -> Engine:
    """The migrated engine, with every table emptied before the test."""
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE events, organizations, batches RESTART IDENTITY CASCADE"))
    return engine


def count(engine: Engine, table: str) -> int:
    with engine.connect() as conn:
        return conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
