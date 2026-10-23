from alembic import command
from sqlalchemy import text

from tests.conftest import count


def test_downgrade_and_upgrade_round_trip(db, alembic_config) -> None:
    command.downgrade(alembic_config, "base")
    try:
        with db.connect() as conn:
            tables = conn.execute(
                text(
                    "SELECT count(*) FROM pg_tables "
                    "WHERE tablename IN ('batches', 'organizations', 'events')"
                )
            ).scalar_one()
        assert tables == 0
    finally:
        command.upgrade(alembic_config, "head")  # leave the schema as the other tests expect
    assert count(db, "batches") == 0


def test_batches_from_before_0002_get_null_counts(db, alembic_config) -> None:
    command.downgrade(alembic_config, "0001")
    try:
        with db.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO batches (source, expected_count, received_count, status) "
                    "VALUES ('old', 1, 1, 'accepted')"
                )
            )
    finally:
        command.upgrade(alembic_config, "head")
    with db.connect() as conn:
        row = conn.execute(
            text("SELECT inserted_count, duplicate_count, invalid_count FROM batches")
        ).one()
    assert tuple(row) == (None, None, None)  # unknown, not a false zero
