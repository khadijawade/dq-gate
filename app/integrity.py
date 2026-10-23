"""Find events whose org_ref names no organization.

    python -m app.integrity

``events.org_ref`` is deliberately not a foreign key: in a real system the organization would
be owned by another service, so the database cannot enforce the link. This check is what
enforces it instead. It exits 0 when every event resolves, and 1 when any do not, printing the
count and the first IDS_SHOWN event ids.

A reference resolves when some organization row has that fingerprint, live or archived. An
archived organization still exists; it just is not the current version.
"""

import sys
import uuid

from sqlalchemy import Engine, text

from app.db import make_engine

IDS_SHOWN = 20

ORPHAN_FILTER = """
    FROM events e
    WHERE NOT EXISTS (SELECT 1 FROM organizations o WHERE o.fingerprint = e.org_ref)
"""


def find_orphans(engine: Engine, limit: int = IDS_SHOWN) -> tuple[int, list[uuid.UUID]]:
    with engine.connect() as conn:
        total = conn.execute(text(f"SELECT count(*) {ORPHAN_FILTER}")).scalar_one()
        ids = (
            conn.execute(
                text(f"SELECT e.id {ORPHAN_FILTER} ORDER BY e.created_at, e.id LIMIT :limit"),
                {"limit": limit},
            )
            .scalars()
            .all()
        )
    return total, list(ids)


def main() -> int:
    engine = make_engine()
    try:
        total, ids = find_orphans(engine)
    finally:
        engine.dispose()

    if total == 0:
        print("integrity ok: every event's org_ref matches an organization")
        return 0
    print(f"integrity failed: {total} event(s) with an org_ref that matches no organization")
    shown = "first " + str(len(ids)) if total > len(ids) else "all"
    print(f"{shown} ids:")
    for event_id in ids:
        print(f"  {event_id}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
