import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from app.ingest import IngestReport, ingest_file
from app.integrity import find_orphans
from scripts.make_sample_data import write
from tests.conftest import ROOT, count


@pytest.fixture(scope="module")
def sample(tmp_path_factory) -> tuple[Path, dict]:
    out = tmp_path_factory.mktemp("sample")
    return out, write(42, out)


def load(engine: Engine, sample: tuple[Path, dict], rel: str) -> IngestReport:
    out, manifest = sample
    meta = manifest["files"][rel]
    return ingest_file(
        engine,
        out / rel,
        source=meta["source"],
        expected_count=meta["expected_count"],
        kind=meta["kind"],
    )


def run_integrity(database_url: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "app.integrity"],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
    )


def test_same_seed_writes_identical_files(tmp_path: Path) -> None:
    write(42, tmp_path / "a")
    write(42, tmp_path / "b")
    write(7, tmp_path / "c")
    files = sorted(p.relative_to(tmp_path / "a") for p in (tmp_path / "a").rglob("*.*"))
    assert len(files) == 7
    for rel in files:
        assert (tmp_path / "a" / rel).read_bytes() == (tmp_path / "b" / rel).read_bytes()
    assert (tmp_path / "a" / "messy/organizations.csv").read_bytes() != (
        tmp_path / "c" / "messy/organizations.csv"
    ).read_bytes()


def test_every_sample_file_behaves_as_its_manifest_says(db, sample) -> None:
    _, manifest = sample
    # Organizations first, so events can resolve against them.
    for rel in sorted(manifest["files"], key=lambda r: ("events" in r, r)):
        expect = manifest["files"][rel]["expect"]
        report = load(db, sample, rel)
        assert report.status == expect["status"], rel
        assert report.inserted == expect["inserted"], rel
        if "duplicates" in expect:
            assert report.skipped_duplicates == expect["duplicates"], rel
            assert report.invalid == expect["invalid"], rel


def test_rejected_samples_leave_no_rows(db, sample) -> None:
    for rel in ("messy/organizations_short.csv", "messy/organizations_incomplete.csv"):
        assert load(db, sample, rel).status == "rejected"
    assert count(db, "organizations") == 0
    with db.connect() as conn:
        reasons = conn.execute(text("SELECT reason FROM batches ORDER BY id")).scalars().all()
    assert reasons[0].startswith("short batch: received 76 of 100")
    assert reasons[1].startswith("too many incomplete rows: 6 of 66")


def test_messy_sample_twice_adds_nothing(db, sample) -> None:
    load(db, sample, "messy/organizations.csv")
    load(db, sample, "messy/events.json")
    before = (count(db, "organizations"), count(db, "events"))
    second = load(db, sample, "messy/organizations.csv")
    assert (second.inserted, second.skipped_duplicates) == (0, 230)
    load(db, sample, "messy/events.json")
    assert (count(db, "organizations"), count(db, "events")) == before == (200, 312)


def test_integrity_fails_on_messy_sample(db, sample, test_database_url: str) -> None:
    load(db, sample, "messy/organizations.csv")
    load(db, sample, "messy/events.json")

    total, ids = find_orphans(db)
    assert total == 12
    assert len(ids) == 12

    result = run_integrity(test_database_url)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "12 event(s) with an org_ref that matches no organization" in result.stdout
    for event_id in ids:
        assert str(event_id) in result.stdout


def test_integrity_passes_on_clean_sample(db, sample, test_database_url: str) -> None:
    load(db, sample, "clean/organizations.csv")
    load(db, sample, "clean/events.json")

    assert find_orphans(db) == (0, [])
    result = run_integrity(test_database_url)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "integrity ok" in result.stdout


def test_integrity_prints_at_most_twenty_ids(db, test_database_url: str, tmp_path: Path) -> None:
    rows = [{"org_ref": f"nowhere-{i}", "title": "t", "starts_on": "2025"} for i in range(25)]
    path = tmp_path / "events.json"
    path.write_text(json.dumps(rows), encoding="utf-8")
    ingest_file(db, path, source="orphans", expected_count=25)

    result = run_integrity(test_database_url)
    assert result.returncode == 1
    assert "25 event(s)" in result.stdout
    assert "first 20 ids:" in result.stdout
    assert len([line for line in result.stdout.splitlines() if line.startswith("  ")]) == 20


def test_archived_organization_still_resolves(db, sample) -> None:
    load(db, sample, "clean/organizations.csv")
    load(db, sample, "clean/events.json")
    with db.begin() as conn:
        conn.execute(text("UPDATE organizations SET archived_at = now()"))
    assert find_orphans(db)[0] == 0
