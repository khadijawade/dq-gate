"""Load a batch of organizations or events in one transaction.

    python -m app.ingest data/orgs.csv --source crm-export --expected-count 500

A batch file holds one kind of record. The kind is read from the columns: a file with a
``title`` or ``org_ref`` column holds events, otherwise organizations. CSV files have a header
row; JSON files are a list of objects, or an object with a ``records`` list.

Duplicates are skipped by ``INSERT ... ON CONFLICT DO NOTHING``: organizations conflict on the
partial unique index over live fingerprints, events on an id derived from their content. Loading
the same file twice therefore stores nothing new the second time.

Before anything is kept the batch is validated as a whole (see app.validation). A rejected batch
is still recorded in ``batches`` with its reason, but none of its rows are stored.
"""

import argparse
import csv
import json
import re
import sys
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, insert, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db import make_engine
from app.fingerprint import fingerprint, normalize_country
from app.models import Batch, Event, Organization
from app.validation import Kind, missing_required, validate_batch

# Fixed namespace so the same event content always gets the same id.
EVENT_ID_NAMESPACE = uuid.UUID("6f1c3c1e-2a7b-4d0e-9a51-8b0f4e2d7c10")

_COUNTRY = re.compile(r"^[A-Z]{2}$")
_DATE_FORMS = (
    (re.compile(r"^(\d{4})-(\d{2})-(\d{2})$"), "day"),
    (re.compile(r"^(\d{4})-(\d{2})$"), "month"),
    (re.compile(r"^(\d{4})$"), "year"),
)
_CHUNK = 1000


@dataclass
class IngestReport:
    batch_id: int
    kind: Kind
    status: str
    reason: str | None
    expected_count: int
    received_count: int
    inserted: int
    skipped_duplicates: int
    invalid: int


# --- reading -------------------------------------------------------------------------------


def clean_record(record: Mapping[str, Any]) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for key, value in record.items():
        if key is None:
            continue  # extra CSV cells with no header
        value = None if value is None else str(value).strip()
        out[str(key).strip()] = value or None
    return out


def read_records(path: Path) -> list[dict[str, str | None]]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with path.open(newline="", encoding="utf-8") as fh:
            return [clean_record(row) for row in csv.DictReader(fh)]
    if suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data = data.get("records", [])
        if not isinstance(data, list) or not all(isinstance(r, dict) for r in data):
            raise ValueError(f"{path}: expected a list of objects")
        return [clean_record(row) for row in data]
    raise ValueError(f"{path}: unsupported file type {suffix!r} (use .csv or .json)")


def detect_kind(records: Iterable[Mapping[str, Any]]) -> Kind:
    keys: set[str] = set()
    for record in records:
        keys.update(record)
    return "events" if keys & {"title", "org_ref"} else "organizations"


# --- shaping rows --------------------------------------------------------------------------


def parse_start(value: str) -> tuple[date, str] | None:
    """Parse YYYY-MM-DD, YYYY-MM or YYYY into a date and its precision."""
    for pattern, precision in _DATE_FORMS:
        match = pattern.match(value)
        if not match:
            continue
        parts = [int(p) for p in match.groups()] + [1, 1]
        try:
            return date(parts[0], parts[1], parts[2]), precision
        except ValueError:
            return None
    return None


def org_row(record: Mapping[str, str | None]) -> dict[str, Any] | None:
    if missing_required(record, "organizations"):
        return None
    country = normalize_country(record["country"])
    if not _COUNTRY.match(country):
        return None
    try:
        fp = fingerprint(record["name"], country, record.get("website"))
    except ValueError:
        return None
    return {
        "fingerprint": fp,
        "name": record["name"],
        "country": country,
        "website": record.get("website"),
    }


def event_row(record: Mapping[str, str | None]) -> dict[str, Any] | None:
    if missing_required(record, "events"):
        return None
    parsed = parse_start(record["starts_on"])
    if parsed is None:
        return None
    starts_on, precision = parsed
    declared = record.get("date_precision")
    if declared and declared != precision:
        return None
    key = "|".join((record["org_ref"], record["title"], starts_on.isoformat(), precision))
    return {
        "id": uuid.uuid5(EVENT_ID_NAMESPACE, key),
        "org_ref": record["org_ref"],
        "title": record["title"],
        "starts_on": starts_on,
        "date_precision": precision,
    }


# --- loading -------------------------------------------------------------------------------


def ingest_records(
    engine: Engine,
    records: list[dict[str, str | None]],
    *,
    source: str,
    expected_count: int,
    kind: Kind | None = None,
) -> IngestReport:
    if expected_count < 1:
        raise ValueError("expected_count must be at least 1")
    kind = kind or detect_kind(records)
    shape = org_row if kind == "organizations" else event_row
    rows = [shape(record) for record in records]
    valid = [row for row in rows if row is not None]
    invalid = len(rows) - len(valid)
    verdict = validate_batch(records, kind, expected_count)

    with engine.begin() as conn:
        batch_id = conn.execute(
            insert(Batch)
            .values(
                source=source,
                expected_count=expected_count,
                received_count=len(records),
                status=verdict.status,
                reason=verdict.reason,
            )
            .returning(Batch.id)
        ).scalar_one()

        inserted = 0
        if not verdict.accepted:
            valid = []  # the batch row is the only thing kept
        for start in range(0, len(valid), _CHUNK):
            chunk = [{**row, "batch_id": batch_id} for row in valid[start : start + _CHUNK]]
            if kind == "organizations":
                stmt = pg_insert(Organization).on_conflict_do_nothing(
                    index_elements=["fingerprint"], index_where=text("archived_at IS NULL")
                )
                returning = Organization.id
            else:
                stmt = pg_insert(Event).on_conflict_do_nothing(index_elements=["id"])
                returning = Event.id
            inserted += len(conn.execute(stmt.returning(returning), chunk).all())

        # Same transaction, so the counts can never disagree with the rows actually kept.
        conn.execute(
            update(Batch)
            .where(Batch.id == batch_id)
            .values(
                inserted_count=inserted,
                duplicate_count=len(valid) - inserted,
                invalid_count=invalid,
            )
        )

    return IngestReport(
        batch_id=batch_id,
        kind=kind,
        status=verdict.status,
        reason=verdict.reason,
        expected_count=expected_count,
        received_count=len(records),
        inserted=inserted,
        skipped_duplicates=len(valid) - inserted,
        invalid=invalid,
    )


def ingest_file(
    engine: Engine, path: Path, *, source: str, expected_count: int, kind: Kind | None = None
) -> IngestReport:
    return ingest_records(
        engine, read_records(path), source=source, expected_count=expected_count, kind=kind
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.ingest", description=__doc__.split("\n")[0]
    )
    parser.add_argument("file", type=Path, help="a .csv or .json batch file")
    parser.add_argument("--source", required=True, help="where the batch came from")
    parser.add_argument(
        "--expected-count", type=int, required=True, help="how many records the sender says it sent"
    )
    parser.add_argument(
        "--kind",
        choices=["organizations", "events"],
        help="override the kind read from the columns",
    )
    args = parser.parse_args(argv)
    if args.expected_count < 1:
        parser.error("--expected-count must be at least 1")

    engine = make_engine()
    try:
        report = ingest_file(
            engine,
            args.file,
            source=args.source,
            expected_count=args.expected_count,
            kind=args.kind,
        )
    finally:
        engine.dispose()
    print(json.dumps(asdict(report), indent=2))
    return 0 if report.status == "accepted" else 2


if __name__ == "__main__":
    sys.exit(main())
