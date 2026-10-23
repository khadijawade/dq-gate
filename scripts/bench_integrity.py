"""Time the integrity check on a large synthetic table, with and without a plain fingerprint index.

    python -m scripts.bench_integrity --orgs 500000 --events 2000000

It creates (or recreates) a separate database, ``dq_gate_bench`` by default, on the server in
DATABASE_URL, migrates it, fills it with generated rows, and prints the median of several timed
runs of the same query ``app.integrity`` uses. One event in every 1,000 points nowhere, and one
organization in ten is archived. The database is dropped afterwards unless ``--keep`` is given.

The question it answers: does a non-partial index on ``organizations.fingerprint`` speed up the
check? The partial unique index cannot serve it, because the check also matches archived rows.
"""

import argparse
import statistics
import time

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, make_url, text

from app.db import database_url
from app.integrity import ORPHAN_FILTER

QUERY = f"SELECT count(*) {ORPHAN_FILTER}"


def _admin(url):
    return create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")


def _timed(conn, runs: int) -> tuple[float, int]:
    conn.execute(text(QUERY))  # warm the cache
    times, result = [], 0
    for _ in range(runs):
        start = time.perf_counter()
        result = conn.execute(text(QUERY)).scalar_one()
        times.append((time.perf_counter() - start) * 1000)
    return statistics.median(times), result


def _plan(conn) -> str:
    rows = conn.execute(text(f"EXPLAIN (COSTS OFF) {QUERY}")).scalars()
    return " / ".join(line.strip().removeprefix("->  ") for line in rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.bench_integrity")
    parser.add_argument("--orgs", type=int, default=500_000)
    parser.add_argument("--events", type=int, default=2_000_000)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--database", default="dq_gate_bench")
    parser.add_argument("--keep", action="store_true", help="leave the database in place")
    args = parser.parse_args(argv)

    url = make_url(database_url()).set(database=args.database)
    admin = _admin(url)
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{args.database}"'))
        conn.execute(text(f'CREATE DATABASE "{args.database}"'))

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url.render_as_string(hide_password=False))
    command.upgrade(cfg, "head")

    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO batches (source, expected_count, received_count, status) "
                    "VALUES ('bench', 1, 1, 'accepted')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO organizations (fingerprint, name, country, batch_id, archived_at) "
                    "SELECT md5(g::text), 'org ' || g, 'GB', 1, "
                    "       CASE WHEN g % 10 = 0 THEN now() END "
                    "FROM generate_series(1, :n) g"
                ),
                {"n": args.orgs},
            )
            conn.execute(
                text(
                    "INSERT INTO events (id, org_ref, title, starts_on, date_precision, batch_id) "
                    "SELECT gen_random_uuid(), "
                    "       md5((CASE WHEN g % 1000 = 0 THEN -g ELSE g % :orgs + 1 END)::text), "
                    "       't', DATE '2025-01-01', 'day', 1 "
                    "FROM generate_series(1, :n) g"
                ),
                {"n": args.events, "orgs": args.orgs},
            )
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(text("VACUUM ANALYZE"))

            print(f"{args.orgs:,} organizations, {args.events:,} events, median of {args.runs}")
            ms, orphans = _timed(conn, args.runs)
            print(f"partial unique index only:    {ms:7.1f} ms  orphans={orphans:,}")
            print(f"  plan: {_plan(conn)}")

            conn.execute(text("CREATE INDEX bench_fingerprint_idx ON organizations (fingerprint)"))
            conn.execute(text("ANALYZE organizations"))
            ms, orphans = _timed(conn, args.runs)
            print(f"plus plain fingerprint index: {ms:7.1f} ms  orphans={orphans:,}")
            print(f"  plan: {_plan(conn)}")
    finally:
        engine.dispose()
        if not args.keep:
            with admin.connect() as conn:
                conn.execute(text(f'DROP DATABASE IF EXISTS "{args.database}"'))
        admin.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
