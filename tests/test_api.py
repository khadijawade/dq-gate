import base64

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api import MAX_BATCH_RECORDS, BatchIn, create_app, encode_cursor
from app.ingest import ingest_records
from tests.conftest import count


@pytest.fixture
def client(db):
    with TestClient(create_app(db)) as c:
        yield c


def orgs(prefix: str, n: int) -> list[dict[str, str | None]]:
    return [
        {"name": f"{prefix} Org {i:03d}", "country": "FR", "website": f"{prefix}{i}.example"}
        for i in range(n)
    ]


def walk(client: TestClient, path: str, limit: int, **params) -> tuple[list[dict], int]:
    items, pages, cursor = [], 0, None
    while True:
        query = {"limit": limit, **params, **({"cursor": cursor} if cursor else {})}
        response = client.get(path, params=query)
        assert response.status_code == 200, response.text
        body = response.json()
        assert len(body["items"]) <= limit
        items += body["items"]
        pages += 1
        cursor = body["next_cursor"]
        if cursor is None:
            return items, pages


def db_ids(db, sql: str) -> list[str]:
    with db.connect() as conn:
        return [str(row) for row in conn.execute(text(sql)).scalars()]


def test_health(client) -> None:
    assert client.get("/health").json() == {"status": "ok"}


@pytest.mark.parametrize("limit", [1, 7, 50, 137, 500])
def test_walking_organizations_returns_every_row_once_in_order(db, client, limit) -> None:
    # Three batches, so rows have three different created_at values and many ties within each.
    for prefix, n in (("Alpha", 60), ("Beta", 45), ("Gamma", 32)):
        ingest_records(db, orgs(prefix, n), source=prefix, expected_count=n)

    items, pages = walk(client, "/organizations", limit)
    ids = [item["id"] for item in items]

    assert len(ids) == 137 == len(set(ids))
    assert ids == db_ids(db, "SELECT id FROM organizations ORDER BY created_at, id")
    assert pages == -(-137 // limit)


def test_walking_events_returns_every_row_once_in_order(db, client) -> None:
    events = [
        {"org_ref": f"ref-{i % 9}", "title": f"Event {i}", "starts_on": f"2024-{i % 12 + 1:02d}"}
        for i in range(83)
    ]
    ingest_records(db, events[:40], source="e1", expected_count=40)
    ingest_records(db, events[40:], source="e2", expected_count=43)

    items, _ = walk(client, "/events", 10)
    ids = [item["id"] for item in items]
    assert len(ids) == 83 == len(set(ids))
    assert ids == db_ids(db, "SELECT id FROM events ORDER BY created_at, id")
    assert items[0]["date_precision"] == "month"


def test_rows_added_mid_walk_do_not_disturb_earlier_pages(db, client) -> None:
    ingest_records(db, orgs("Early", 20), source="early", expected_count=20)
    first = client.get("/organizations", params={"limit": 10}).json()

    ingest_records(db, orgs("Late", 5), source="late", expected_count=5)
    rest, _ = walk(client, "/organizations", 10, cursor=first["next_cursor"])

    ids = [item["id"] for item in first["items"] + rest]
    assert len(ids) == 25 == len(set(ids))
    # The late batch sorts after everything that was there before, so it all lands at the end.
    assert {item["name"] for item in rest[-5:]} == {f"Late Org {i:03d}" for i in range(5)}


def test_archived_organizations_are_hidden_unless_asked_for(db, client) -> None:
    ingest_records(db, orgs("Kept", 4), source="k", expected_count=4)
    with db.begin() as conn:
        conn.execute(text("UPDATE organizations SET archived_at = now() WHERE name LIKE '%000'"))
    assert len(walk(client, "/organizations", 2)[0]) == 3
    assert len(walk(client, "/organizations", 2, include_archived="true")[0]) == 4


def test_batches_are_listed_in_id_order(db, client) -> None:
    for i in range(5):
        ingest_records(db, orgs(f"B{i}", 1), source=f"s{i}", expected_count=1)
    ingest_records(db, orgs("Short", 1), source="short", expected_count=10)

    items, pages = walk(client, "/batches", 2)
    assert [item["id"] for item in items] == [1, 2, 3, 4, 5, 6]
    assert pages == 3
    assert items[-1]["status"] == "rejected"
    assert items[-1]["reason"].startswith("short batch")
    assert [item["inserted_count"] for item in items] == [1, 1, 1, 1, 1, 0]


def test_empty_table_has_no_next_cursor(client) -> None:
    assert client.get("/organizations").json() == {"items": [], "next_cursor": None}


@pytest.mark.parametrize(
    "cursor",
    [
        "not-a-cursor!",
        base64.urlsafe_b64encode(b"{}").decode(),
        encode_cursor(["2025-01-01T00:00:00+00:00"]),
        encode_cursor(["yesterday", "not-a-uuid"]),
    ],
)
def test_bad_cursor_is_a_400(client, cursor) -> None:
    assert client.get("/organizations", params={"cursor": cursor}).status_code == 400


def test_batch_cursor_rejects_other_shapes(client) -> None:
    assert client.get("/batches", params={"cursor": encode_cursor(["x"])}).status_code == 400


@pytest.mark.parametrize("limit", [0, -1, 501])
def test_limit_bounds(client, limit) -> None:
    assert client.get("/events", params={"limit": limit}).status_code == 422


def test_post_batch_accepts_and_is_idempotent(db, client) -> None:
    body = {"source": "api", "expected_count": 3, "records": orgs("Posted", 3)}
    first = client.post("/batches", json=body)
    assert first.status_code == 201
    assert first.json()["inserted"] == 3

    second = client.post("/batches", json=body)
    assert second.status_code == 201
    assert (second.json()["inserted"], second.json()["skipped_duplicates"]) == (0, 3)
    assert count(db, "organizations") == 3


def test_post_batch_rejects_a_short_batch(db, client) -> None:
    body = {"source": "api", "expected_count": 10, "records": orgs("Few", 3)}
    response = client.post("/batches", json=body)
    assert response.status_code == 422
    assert response.json()["status"] == "rejected"
    assert count(db, "organizations") == 0
    assert count(db, "batches") == 1


def test_post_batch_takes_events_and_numbers(db, client) -> None:
    body = {
        "source": "api",
        "expected_count": 1,
        "records": [{"org_ref": "r", "title": "Founded", "starts_on": 2019}],
    }
    response = client.post("/batches", json=body)
    assert response.status_code == 201
    assert response.json()["kind"] == "events"
    assert client.get("/events").json()["items"][0]["starts_on"] == "2019-01-01"


def test_post_batch_has_a_record_limit(db, client) -> None:
    record = {"name": "Same Org", "country": "GB", "website": None}
    # The limit itself is allowed (checked on the model, to avoid loading 10,000 rows).
    BatchIn(source="x", expected_count=1, records=[record] * MAX_BATCH_RECORDS)

    body = {"source": "big", "expected_count": 1, "records": [record] * (MAX_BATCH_RECORDS + 1)}
    response = client.post("/batches", json=body)
    assert response.status_code == 422
    assert "at most 10000 items" in response.text
    assert count(db, "batches") == 0  # refused before anything is recorded


def test_post_batch_validates_its_body(client) -> None:
    assert (
        client.post(
            "/batches", json={"source": "x", "expected_count": 0, "records": []}
        ).status_code
        == 422
    )
    assert client.post("/batches", json={"expected_count": 1, "records": []}).status_code == 422
