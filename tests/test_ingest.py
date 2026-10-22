import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import text

from app.fingerprint import fingerprint
from app.ingest import ingest_file, ingest_records, parse_start, read_records
from tests.conftest import ROOT, count

ORGS = [
    {"name": "Northwind Traders", "country": "GB", "website": "northwind.example"},
    {"name": "Bluefin Analytics", "country": "US", "website": "https://bluefin.example"},
    {"name": "Kestrel Works", "country": "DE", "website": None},
]
EVENTS = [
    {"org_ref": "a" * 32, "title": "Spring summit", "starts_on": "2025-04-12"},
    {"org_ref": "b" * 32, "title": "Annual report", "starts_on": "2024"},
    {"org_ref": "a" * 32, "title": "Product launch", "starts_on": "2025-09"},
]


def many_orgs(n: int) -> list[dict[str, str | None]]:
    return [
        {"name": f"Sample Org {i:03d}", "country": "NL", "website": f"org{i}.example"}
        for i in range(n)
    ]


def test_same_org_batch_twice_leaves_same_row_count(db) -> None:
    first = ingest_records(db, ORGS, source="test", expected_count=3)
    assert (first.inserted, first.skipped_duplicates, first.invalid) == (3, 0, 0)
    assert count(db, "organizations") == 3

    second = ingest_records(db, ORGS, source="test", expected_count=3)
    assert (second.inserted, second.skipped_duplicates, second.invalid) == (0, 3, 0)
    assert count(db, "organizations") == 3
    assert count(db, "batches") == 2  # both loads are recorded


def test_same_event_batch_twice_leaves_same_row_count(db) -> None:
    first = ingest_records(db, EVENTS, source="test", expected_count=3)
    assert first.kind == "events"
    assert first.inserted == 3
    second = ingest_records(db, EVENTS, source="test", expected_count=3)
    assert (second.inserted, second.skipped_duplicates) == (0, 3)
    assert count(db, "events") == 3


def test_spelling_variants_collapse_within_one_batch(db) -> None:
    records = [
        {"name": "Northwind Traders", "country": "GB", "website": "northwind.example"},
        {"name": "NORTHWIND TRADERS LTD", "country": "gb", "website": "www.northwind.example"},
        {
            "name": "Northwind Traders, Inc.",
            "country": "GB",
            "website": "https://northwind.example/",
        },
    ]
    report = ingest_records(db, records, source="test", expected_count=3)
    assert (report.inserted, report.skipped_duplicates) == (1, 2)
    with db.connect() as conn:
        name = conn.execute(text("SELECT name FROM organizations")).scalar_one()
    assert name == "Northwind Traders"  # first spelling wins


def test_invalid_rows_are_counted_and_skipped(db) -> None:
    # 2 of 43 rows (4.7%) miss a required field: under the 5% limit, so the batch is kept.
    records = [
        *many_orgs(40),
        {"name": None, "country": "GB", "website": None},  # missing name
        {"name": "Lone Org", "country": None, "website": None},  # missing country
        {"name": "Wrong Country", "country": "Britain", "website": None},  # not a 2-letter code
    ]
    report = ingest_records(db, records, source="test", expected_count=43)
    assert report.status == "accepted"
    assert (report.inserted, report.skipped_duplicates, report.invalid) == (40, 0, 3)
    assert report.received_count == 43


def test_partial_index_allows_reinsert_after_archive(db) -> None:
    ingest_records(db, ORGS[:1], source="test", expected_count=1)
    with db.begin() as conn:
        conn.execute(text("UPDATE organizations SET archived_at = now()"))

    report = ingest_records(db, ORGS[:1], source="test", expected_count=1)
    assert report.inserted == 1

    fp = fingerprint(**ORGS[0])
    with db.connect() as conn:
        rows = (
            conn.execute(
                text("SELECT archived_at IS NULL FROM organizations WHERE fingerprint = :fp"),
                {"fp": fp},
            )
            .scalars()
            .all()
        )
    assert sorted(rows) == [False, True]  # one archived, one live

    # A third load conflicts with the live row again.
    assert ingest_records(db, ORGS[:1], source="test", expected_count=1).inserted == 0


def test_partial_index_rejects_two_live_rows(db) -> None:
    ingest_records(db, ORGS[:1], source="test", expected_count=1)
    with (
        db.connect() as conn,
        pytest.raises(Exception, match="organizations_fingerprint_live_uniq"),
    ):
        conn.execute(
            text(
                "INSERT INTO organizations (fingerprint, name, country, batch_id) "
                "SELECT fingerprint, name, country, batch_id FROM organizations"
            )
        )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2025-04-12", (date(2025, 4, 12), "day")),
        ("2025-04", (date(2025, 4, 1), "month")),
        ("2025", (date(2025, 1, 1), "year")),
        ("2025-02-30", None),
        ("12/04/2025", None),
        ("soon", None),
    ],
)
def test_parse_start(raw: str, expected) -> None:
    assert parse_start(raw) == expected


def test_declared_precision_must_match(db) -> None:
    records = [
        {"org_ref": "x", "title": "t1", "starts_on": "2025-04", "date_precision": "month"},
        {"org_ref": "x", "title": "t2", "starts_on": "2025-04", "date_precision": "day"},
    ]
    report = ingest_records(db, records, source="test", expected_count=2)
    assert (report.inserted, report.invalid) == (1, 1)


def test_read_csv_and_json_agree(tmp_path: Path) -> None:
    csv_path = tmp_path / "orgs.csv"
    csv_path.write_text(
        "name,country,website\nNorthwind Traders,GB,northwind.example\n Kestrel Works ,DE,\n",
        encoding="utf-8",
    )
    json_path = tmp_path / "orgs.json"
    json_path.write_text(
        json.dumps(
            {
                "records": [
                    {"name": "Northwind Traders", "country": "GB", "website": "northwind.example"},
                    {"name": "Kestrel Works", "country": "DE", "website": ""},
                ]
            }
        ),
        encoding="utf-8",
    )
    assert read_records(csv_path) == read_records(json_path)
    assert read_records(csv_path)[1] == {"name": "Kestrel Works", "country": "DE", "website": None}


def test_ingest_file(db, tmp_path: Path) -> None:
    path = tmp_path / "orgs.json"
    path.write_text(json.dumps(ORGS), encoding="utf-8")
    assert ingest_file(db, path, source="file", expected_count=3).inserted == 3


def test_cli_reports_counts_and_is_idempotent(db, tmp_path: Path, test_database_url: str) -> None:
    path = tmp_path / "orgs.csv"
    # 20 organizations, one rewritten duplicate, one row with no name (1 of 22 = 4.5%).
    lines = ["name,country,website"]
    lines += [f"{o['name']},{o['country']},{o['website']}" for o in many_orgs(20)]
    lines += ["SAMPLE ORG 000 Ltd,nl,www.org0.example", ",GB,"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    env = {**os.environ, "DATABASE_URL": test_database_url}
    cmd = [
        sys.executable,
        "-m",
        "app.ingest",
        str(path),
        "--source",
        "cli",
        "--expected-count",
        "22",
    ]

    reports = []
    for _ in range(2):
        result = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, check=True)
        reports.append(json.loads(result.stdout))

    assert [(r["inserted"], r["skipped_duplicates"], r["invalid"]) for r in reports] == [
        (20, 1, 1),
        (0, 21, 1),
    ]
    assert count(db, "organizations") == 20
