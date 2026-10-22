"""The read API, plus POST /batches for loading over HTTP.

    uvicorn app.api:app --reload

List endpoints use cursor pagination. Rows are ordered by (created_at, id), which is unique and
never changes for a stored row, so walking the pages returns every row exactly once even while new
batches arrive (new rows sort after the cursor). The cursor is an opaque token; clients pass back
whatever ``next_cursor`` they were given, and ``next_cursor`` is null on the last page.
"""

import base64
import binascii
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import date, datetime
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import Engine, select, text, tuple_

from app.db import make_engine
from app.ingest import _clean, ingest_records
from app.models import Batch, Event, Organization

DEFAULT_LIMIT = 50
MAX_LIMIT = 500


# --- schemas -------------------------------------------------------------------------------


class BatchOut(BaseModel):
    id: int
    source: str
    expected_count: int
    received_count: int
    status: str
    reason: str | None
    created_at: datetime


class OrganizationOut(BaseModel):
    id: uuid.UUID
    fingerprint: str
    name: str
    country: str
    website: str | None
    batch_id: int
    archived_at: datetime | None
    created_at: datetime


class EventOut(BaseModel):
    id: uuid.UUID
    org_ref: str
    title: str
    starts_on: date
    date_precision: str
    batch_id: int
    created_at: datetime


class Page[T](BaseModel):
    items: list[T]
    next_cursor: str | None


class BatchIn(BaseModel):
    source: str = Field(min_length=1)
    expected_count: int = Field(ge=1)
    kind: Literal["organizations", "events"] | None = None
    records: list[dict[str, str | int | float | None]]


class IngestOut(BaseModel):
    batch_id: int
    kind: str
    status: str
    reason: str | None
    expected_count: int
    received_count: int
    inserted: int
    skipped_duplicates: int
    invalid: int


# --- cursors -------------------------------------------------------------------------------


def encode_cursor(key: list[Any]) -> str:
    raw = json.dumps(key, default=str, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> list[Any]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        key = json.loads(raw)
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(400, "invalid cursor") from exc
    if not isinstance(key, list):
        raise HTTPException(400, "invalid cursor")
    return key


def _time_id_key(cursor: str) -> tuple[datetime, uuid.UUID]:
    key = decode_cursor(cursor)
    try:
        created_at, row_id = key
        return datetime.fromisoformat(created_at), uuid.UUID(row_id)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, "invalid cursor") from exc


def _batch_key(cursor: str) -> int:
    key = decode_cursor(cursor)
    if len(key) != 1 or not isinstance(key[0], int):
        raise HTTPException(400, "invalid cursor")
    return key[0]


# --- app -----------------------------------------------------------------------------------


def create_app(engine: Engine | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned = engine is None
        app.state.engine = engine or make_engine()
        yield
        if owned:
            app.state.engine.dispose()

    app = FastAPI(title="dq-gate", version="0.1.0", lifespan=lifespan)

    def get_engine(request: Request) -> Engine:
        return request.app.state.engine

    EngineDep = Annotated[Engine, Depends(get_engine)]
    Limit = Annotated[int, Query(ge=1, le=MAX_LIMIT)]

    @app.get("/health")
    def health(engine: EngineDep) -> dict[str, str]:
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception as exc:
            raise HTTPException(503, "database unavailable") from exc
        return {"status": "ok"}

    @app.get("/batches")
    def list_batches(
        engine: EngineDep, limit: Limit = DEFAULT_LIMIT, cursor: str | None = None
    ) -> Page[BatchOut]:
        stmt = select(Batch).order_by(Batch.id).limit(limit + 1)
        if cursor:
            stmt = stmt.where(Batch.id > _batch_key(cursor))
        with engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        more = len(rows) > limit
        rows = rows[:limit]
        return Page[BatchOut](
            items=[BatchOut(**row) for row in rows],
            next_cursor=encode_cursor([rows[-1]["id"]]) if more else None,
        )

    @app.post(
        "/batches",
        status_code=201,
        responses={422: {"description": "Batch rejected; the report says why"}},
    )
    def post_batch(body: BatchIn, engine: EngineDep, response: Response) -> IngestOut:
        report = ingest_records(
            engine,
            [_clean(record) for record in body.records],
            source=body.source,
            expected_count=body.expected_count,
            kind=body.kind,
        )
        if report.status == "rejected":
            response.status_code = 422
        return IngestOut(**asdict(report))

    def time_id_page(engine: Engine, model: Any, out: type[BaseModel], stmt, limit, cursor):
        stmt = stmt.order_by(model.created_at, model.id).limit(limit + 1)
        if cursor:
            stmt = stmt.where(tuple_(model.created_at, model.id) > tuple_(*_time_id_key(cursor)))
        with engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        more = len(rows) > limit
        rows = rows[:limit]
        last = rows[-1] if rows else None
        return {
            "items": [out(**row) for row in rows],
            "next_cursor": (
                encode_cursor([last["created_at"].isoformat(), str(last["id"])]) if more else None
            ),
        }

    @app.get("/organizations")
    def list_organizations(
        engine: EngineDep,
        limit: Limit = DEFAULT_LIMIT,
        cursor: str | None = None,
        include_archived: bool = False,
    ) -> Page[OrganizationOut]:
        stmt = select(Organization)
        if not include_archived:
            stmt = stmt.where(Organization.archived_at.is_(None))
        page = time_id_page(engine, Organization, OrganizationOut, stmt, limit, cursor)
        return Page[OrganizationOut](**page)

    @app.get("/events")
    def list_events(
        engine: EngineDep, limit: Limit = DEFAULT_LIMIT, cursor: str | None = None
    ) -> Page[EventOut]:
        page = time_id_page(engine, Event, EventOut, select(Event), limit, cursor)
        return Page[EventOut](**page)

    return app


app = create_app()
