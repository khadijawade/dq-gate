"""Batch-level checks that decide whether a batch is kept at all.

A batch is rejected as a whole, with none of its rows stored, when either:

* fewer rows arrived than the sender promised: received / expected below MIN_RECEIVED_RATIO, or
* too many rows are missing a required field: more than MAX_MISSING_RATIO of the rows received.

Rows that are present but malformed (a bad date, a country that is not a 2-letter code) do not
count towards rejection; they are skipped one by one and reported as invalid.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

Kind = Literal["organizations", "events"]

MIN_RECEIVED_RATIO = 0.95
MAX_MISSING_RATIO = 0.05

REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "organizations": ("name", "country"),
    "events": ("org_ref", "title", "starts_on"),
}


@dataclass(frozen=True)
class Verdict:
    accepted: bool
    reason: str | None = None

    @property
    def status(self) -> str:
        return "accepted" if self.accepted else "rejected"


def missing_required(record: Mapping[str, str | None], kind: Kind) -> bool:
    return any(not record.get(field) for field in REQUIRED_FIELDS[kind])


def validate_batch(
    records: Sequence[Mapping[str, str | None]], kind: Kind, expected_count: int
) -> Verdict:
    received = len(records)
    if received / expected_count < MIN_RECEIVED_RATIO:
        return Verdict(
            False,
            f"short batch: received {received} of {expected_count} expected "
            f"({received / expected_count:.1%}, minimum {MIN_RECEIVED_RATIO:.0%})",
        )

    missing = sum(missing_required(record, kind) for record in records)
    if missing / received > MAX_MISSING_RATIO:
        return Verdict(
            False,
            f"too many incomplete rows: {missing} of {received} missing required fields "
            f"({missing / received:.1%}, maximum {MAX_MISSING_RATIO:.0%})",
        )

    return Verdict(True)
