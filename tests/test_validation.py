import pytest
from sqlalchemy import text

from app.ingest import ingest_records
from app.validation import validate_batch
from tests.conftest import count


def orgs(n: int, missing: int = 0) -> list[dict[str, str | None]]:
    rows = [
        {"name": f"Valid Org {i:03d}", "country": "SE", "website": f"valid{i}.example"}
        for i in range(n - missing)
    ]
    rows += [{"name": None, "country": "SE", "website": None} for _ in range(missing)]
    return rows


@pytest.mark.parametrize(
    ("received", "expected", "accepted"),
    [
        (100, 100, True),
        (95, 100, True),  # exactly at the threshold
        (94, 100, False),
        (19, 20, True),
        (18, 20, False),
        (0, 10, False),
        (120, 100, True),  # more than promised is not a short batch
    ],
)
def test_received_ratio(received: int, expected: int, accepted: bool) -> None:
    verdict = validate_batch(orgs(received), "organizations", expected)
    assert verdict.accepted is accepted
    if not accepted:
        assert verdict.reason.startswith("short batch")


@pytest.mark.parametrize(
    ("missing", "accepted"),
    [(0, True), (5, True), (6, False), (100, False)],  # out of 100 rows; 5% is the limit
)
def test_missing_required_ratio(missing: int, accepted: bool) -> None:
    verdict = validate_batch(orgs(100, missing=missing), "organizations", 100)
    assert verdict.accepted is accepted
    if not accepted:
        assert verdict.reason.startswith("too many incomplete rows")


def test_malformed_but_present_fields_do_not_reject() -> None:
    rows = orgs(10) + [{"name": "Odd Country", "country": "Nowhere", "website": None}] * 10
    assert validate_batch(rows, "organizations", 20).accepted


def test_event_required_fields() -> None:
    rows = [{"org_ref": "r", "title": "t", "starts_on": "2025"}] * 9 + [
        {"org_ref": "r", "title": None, "starts_on": "2025"}
    ]
    verdict = validate_batch(rows, "events", 10)
    assert not verdict.accepted
    assert "1 of 10" in verdict.reason


def test_short_batch_is_rejected_and_nothing_is_stored(db) -> None:
    report = ingest_records(db, orgs(76), source="short", expected_count=100)

    assert report.status == "rejected"
    assert report.reason == "short batch: received 76 of 100 expected (76.0%, minimum 95%)"
    assert (report.inserted, report.skipped_duplicates) == (0, 0)
    assert count(db, "organizations") == 0
    with db.connect() as conn:
        batch = conn.execute(text("SELECT * FROM batches")).mappings().one()
    assert (batch["status"], batch["expected_count"], batch["received_count"]) == (
        "rejected",
        100,
        76,
    )
    assert batch["reason"] == report.reason


def test_incomplete_batch_is_rejected_and_nothing_is_stored(db) -> None:
    report = ingest_records(db, orgs(66, missing=6), source="incomplete", expected_count=66)
    assert report.status == "rejected"
    assert report.reason.startswith("too many incomplete rows: 6 of 66")
    assert count(db, "organizations") == 0
    assert count(db, "batches") == 1


def test_rejected_event_batch_stores_no_events(db) -> None:
    rows = [{"org_ref": "r", "title": f"t{i}", "starts_on": "2025"} for i in range(10)]
    report = ingest_records(db, rows, source="events", expected_count=20)
    assert report.status == "rejected"
    assert count(db, "events") == 0


def test_good_batch_is_accepted(db) -> None:
    report = ingest_records(db, orgs(100), source="good", expected_count=100)
    assert (report.status, report.reason, report.inserted) == ("accepted", None, 100)
    assert count(db, "organizations") == 100
    with db.connect() as conn:
        status = conn.execute(text("SELECT status FROM batches")).scalar_one()
    assert status == "accepted"


def test_rejection_leaves_earlier_batches_alone(db) -> None:
    ingest_records(db, orgs(100), source="good", expected_count=100)
    ingest_records(db, orgs(50), source="short", expected_count=100)
    assert count(db, "organizations") == 100
    with db.connect() as conn:
        statuses = conn.execute(text("SELECT status FROM batches ORDER BY id")).scalars().all()
    assert statuses == ["accepted", "rejected"]


def test_expected_count_must_be_positive(db) -> None:
    with pytest.raises(ValueError):
        ingest_records(db, orgs(1), source="x", expected_count=0)
