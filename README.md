# dq-gate

[![CI](https://github.com/khadijawade/dq-gate/actions/workflows/ci.yml/badge.svg)](https://github.com/khadijawade/dq-gate/actions/workflows/ci.yml)
[![Nightly integrity](https://github.com/khadijawade/dq-gate/actions/workflows/integrity.yml/badge.svg)](https://github.com/khadijawade/dq-gate/actions/workflows/integrity.yml)

A small data-quality gate. It takes in messy batches of organization and event records, drops
duplicates, rejects whole batches that are short or incomplete before keeping anything, checks
that every event points at a real organization, and serves the clean data through a paginated
API. Everything here is synthetic and was written from scratch for this repository.

## The problem

Analysis downstream of an ingest pipeline quietly assumes two things: each real-world entity
appears once, and a batch that says it holds 10,000 rows holds 10,000 rows. When the first
assumption fails, "Northwind Traders Ltd" and "NORTHWIND TRADERS" are counted as two customers,
joins fan out, and every total that touches them is inflated. When the second fails, a truncated
export loads without complaint, and a missing day or region looks like a real drop instead of a
broken file. Neither failure throws an error. Both are cheaper to stop at the door than to find
in a dashboard weeks later.

## Design decisions

**A partial unique index plus `INSERT ... ON CONFLICT`.** Each organization gets a fingerprint:
a hash of its normalized name (lowercased, accents folded, punctuation and trailing legal
suffixes like "Inc", "Ltd", "L.L.C." removed, spaces collapsed), its country code, and its website
domain. A unique index on `fingerprint WHERE archived_at IS NULL` means there is at most one *live*
row per organization, while archived rows keep their history and do not block the organization
coming back. Rows are written with `ON CONFLICT (fingerprint) WHERE archived_at IS NULL DO
NOTHING`, so the database settles duplicates atomically: no read-then-write race, and loading the
same file twice stores nothing the second time. Events have no natural unique column, so each
event's id is a UUIDv5 of its content (`org_ref`, title, start date, precision) and conflicts on
the primary key the same way.

**Validate before keep.** A batch is judged as a whole before any row is stored. It is rejected
if fewer than 95% of the rows the sender promised arrived (`received_count / expected_count <
0.95`), or if more than 5% of the rows are missing a required field. A rejected batch is still
recorded in `batches` with status `rejected`, its counts and a reason such as `short batch: received 76 of 100
expected (76.0%, minimum 95%)`, so the failure is visible and auditable, but none of its rows are
kept. Partial loads are worse than no load: they look plausible. Rows that are present but
malformed (an unreadable date, a country that is not a two-letter code) are skipped one by one and
reported as `invalid`; they do not reject the batch.

**An unconstrained `org_ref`, and why the integrity check exists.** `events.org_ref` holds an
organization fingerprint but is deliberately *not* a foreign key. It models a reference into data
owned by another service, which a database constraint cannot enforce. Events are therefore allowed
in even when their organization has not arrived (yet), and `python -m app.integrity` is the
enforcement instead: it lists events whose `org_ref` matches no organization, exits 1 if there
are any, and runs nightly in CI. A reference to an archived organization still counts as
resolved, since that organization exists.

**Cursor pagination.** `GET /organizations` and `GET /events` order by `(created_at, id)`, which
is unique and never changes for a stored row, and fetch the next page with a row-value comparison
`(created_at, id) > (:c, :i)`. Unlike `OFFSET`, this stays fast on deep pages and does not skip or
repeat rows when new batches arrive mid-walk. The cursor is an opaque token to clients
(base64-encoded, not encrypted); `next_cursor` is `null` on the last page.

## Quick start

Needs Python 3.12 and Docker. Postgres is published on host port **5433** so it does not collide
with a local Postgres.

```bash
docker compose up -d --wait
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'

alembic upgrade head
python -m scripts.make_sample_data --seed 42 --out data/

# messy sample: duplicates, incomplete rows, a short batch, events pointing nowhere
python -m app.ingest data/messy/organizations.csv --source sample-messy-orgs --expected-count 238
python -m app.ingest data/messy/organizations_short.csv --source sample-short-orgs --expected-count 100
python -m app.ingest data/messy/events.json --source sample-messy-events --expected-count 323
python -m app.integrity

uvicorn app.api:app          # http://127.0.0.1:8000/docs
pytest
```

`data/manifest.json` lists every sample file with its expected count and what should happen when
it is loaded. The clean sample (`data/clean/`) passes every check.

Configuration is two environment variables: `DATABASE_URL` (default
`postgresql+psycopg://dq:dq@localhost:5433/dq_gate`) for the app, and `TEST_DATABASE_URL` (default
the same server, database `dq_gate_test`, created on first run) for the tests.

### Commands

| Command | What it does |
|---|---|
| `python -m app.ingest FILE --source NAME --expected-count N` | Load a `.csv` or `.json` batch in one transaction. Prints a JSON report. Exits 0 if accepted, 2 if rejected. |
| `python -m app.integrity` | List events whose `org_ref` matches no organization. Exits 0 if none, 1 if any, with the count and first 20 ids. |
| `python -m scripts.make_sample_data --seed 42 --out data/` | Write the deterministic messy and clean samples. The same seed writes byte-identical files. |

A batch file holds one kind of record: a file with a `title` or `org_ref` column is events,
otherwise organizations (`--kind` overrides). Organizations need `name` and `country`; `website`
is optional. Events need `org_ref`, `title` and `starts_on`, which may be `YYYY-MM-DD`, `YYYY-MM`
or `YYYY` and sets `date_precision` to `day`, `month` or `year`.

### API

| Endpoint | |
|---|---|
| `GET /health` | `{"status": "ok"}`, or 503 if the database is unreachable. |
| `GET /batches?limit&cursor` | Every batch, accepted and rejected, in id order, with its `inserted_count`, `duplicate_count` and `invalid_count`. |
| `POST /batches` | `{"source", "expected_count", "kind"?, "records": [...]}`. Same logic as the CLI. 201 with the report if accepted, 422 with the report if rejected. |
| `GET /organizations?limit&cursor&include_archived` | Live organizations; `include_archived=true` adds archived ones. |
| `GET /events?limit&cursor` | All events. |

`limit` is 1 to 500, default 50. Responses are `{"items": [...], "next_cursor": "..." | null}`. A
malformed cursor is a 400.

## Results from real runs

Every number in this section was produced by the command shown, run in this repository on
2026-10-22 (macOS, Python 3.12.11, `postgres:16` from `docker-compose.yml`), starting from an
empty database.

### Test suite

`ruff check . && ruff format --check . && pytest`:

```
All checks passed!
19 files already formatted
============================= test session starts ==============================
platform darwin -- Python 3.12.11, pytest-8.3.5, pluggy-1.7.0
rootdir: /Users/khadijawade/Developer/GitHub/dq-gate
configfile: pyproject.toml
testpaths: tests
plugins: anyio-4.15.1
collected 100 items

tests/test_api.py .......................                                [ 23%]
tests/test_fingerprint.py ..................................             [ 57%]
tests/test_ingest.py ................                                    [ 73%]
tests/test_sample_data.py ........                                       [ 81%]
tests/test_validation.py ...................                             [100%]

============================= 100 passed in 4.14s ==============================
```

The tests run against real Postgres, not mocks. They cover fingerprint normalization edge cases;
loading the same batch twice leaving the row count unchanged; short and incomplete batches being
rejected with nothing stored, and good batches accepted; the partial unique index allowing a
fingerprint back in after the old row is archived; the integrity check exiting 1 on the messy
sample and 0 on the clean one; and walking every API page returning each row exactly once, in
order, for page sizes from 1 to 500.

### Ingesting the messy sample

After `python -m scripts.make_sample_data --seed 42 --out data/`:

| Command | Status | Inserted | Skipped as duplicates | Invalid | Exit |
|---|---|---|---|---|---|
| `python -m app.ingest data/messy/organizations.csv --source sample-messy-orgs --expected-count 238` | accepted | 200 | 30 | 8 | 0 |
| `python -m app.ingest data/messy/organizations_short.csv --source sample-short-orgs --expected-count 100` | rejected: `short batch: received 76 of 100 expected (76.0%, minimum 95%)` | 0 | 0 | 0 | 2 |
| `python -m app.ingest data/messy/events.json --source sample-messy-events --expected-count 323` | accepted | 312 | 6 | 5 | 0 |

Then `python -m app.integrity` printed
`integrity failed: 12 event(s) with an org_ref that matches no organization`, listed the 12 ids,
and exited 1.

With the API running (`uvicorn app.api:app`), walking `GET /events?limit=100` by following
`next_cursor` returned 312 events over 4 pages, and `GET /events?cursor=zzz` returned 400.

### The nightly integrity job, replayed locally

The steps in `.github/workflows/integrity.yml`, run against a fresh empty database: migrations
applied, `data/clean/organizations.csv` accepted with 50 inserted and 0 invalid,
`data/clean/events.json` accepted with 120 inserted and 0 invalid, and `python -m app.integrity`
printed `integrity ok: every event's org_ref matches an organization` and exited 0.

## Limitations

- **Synthetic data only.** Names come from a small word list and websites use the reserved
  `.example` domain. Real data would need a richer fingerprint (transliteration, abbreviations,
  fuzzy matching) and some human review of near-misses. Exact-match fingerprints will miss
  "Intl" vs "International".
- **Single node.** One Postgres, one API process, whole batches in one transaction. Very large
  batches would want streaming validation and chunked commits behind a staging table.
- **No auth.** Anyone who can reach the API can read everything and post batches.
- **First write wins.** A duplicate is skipped, not merged, so a later, better spelling of an
  organization does not replace the first one.
- **Same-batch timestamps.** Rows in one batch share `created_at` (the transaction time), so within
  a batch the API order is by UUID, which is stable but not meaningful.

## Layout

```
app/           api.py, models.py, ingest.py, fingerprint.py, validation.py, integrity.py, db.py
migrations/    Alembic; 0001 creates the tables and partial unique index, 0002 adds batch counts
scripts/       make_sample_data.py
tests/         run against real Postgres
.github/       ci.yml (lint and tests on push and pull request), integrity.yml (nightly)
```

## License

MIT. See [LICENSE](LICENSE).
