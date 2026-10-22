"""Write deterministic synthetic sample batches.

    python -m scripts.make_sample_data --seed 42 --out data/

Everything is invented: names are built from a small word list, websites use the reserved
``.example`` domain, and org_refs are fingerprints computed here. The same seed always writes
byte-identical files.

Files written under OUT:

messy/organizations.csv           accepted; spelling-variant duplicates and a few incomplete rows
messy/organizations_short.csv     rejected; fewer rows than its expected count
messy/organizations_incomplete.csv rejected; too many rows missing required fields
messy/events.json                 accepted; some org_refs point nowhere, a few duplicates
clean/organizations.csv           passes every check
clean/events.json                 passes every check, every org_ref resolves
manifest.json                     each file's kind, source, expected count and what it contains
"""

import argparse
import csv
import json
import random
from datetime import date, timedelta
from pathlib import Path

from app.fingerprint import fingerprint

FIRST = (
    "Amber", "Birch", "Cobalt", "Delta", "Ember", "Granite", "Harbor", "Indigo", "Juniper",
    "Kestrel", "Lumen", "Maple", "Onyx", "Pine", "Quartz", "Raven", "Sable", "Tidal", "Umber",
    "Vale", "Willow", "Yarrow", "Zephyr", "Cedar", "Flint",
)  # fmt: skip
SECOND = ("Ridge", "Field", "Stone", "Brook", "Point", "Gate", "Crest", "Hollow", "Line", "Bay")
THIRD = (
    "Analytics", "Logistics", "Foods", "Robotics", "Health", "Studio", "Labs", "Energy",
    "Freight", "Textiles", "Media", "Systems",
)  # fmt: skip
COUNTRIES = ("GB", "US", "DE", "FR", "NL", "IE", "ES", "CA", "AU", "JP", "BR", "IN", "SE", "KE")
SUFFIXES = ("Inc", "Inc.", ", Inc.", "Ltd", "Ltd.", "LLC", "L.L.C.", "GmbH", "Pty Ltd", "Corp")
EVENT_TITLES = (
    "Annual report", "Funding round", "Product launch", "Office opening", "Partnership signed",
    "Leadership change", "Conference talk", "Rebrand", "Acquisition closed", "Hiring drive",
)  # fmt: skip

ORG_FIELDS = ("name", "country", "website")


class Sample:
    def __init__(self, seed: int) -> None:
        self.rng = random.Random(seed)
        self.used_names: set[str] = set()

    # --- organizations ---------------------------------------------------------------------

    def new_org(self) -> dict[str, str]:
        while True:
            name = " ".join(self.rng.choice(words) for words in (FIRST, SECOND, THIRD))
            if name not in self.used_names:
                break
        self.used_names.add(name)
        slug = name.lower().replace(" ", "")
        website = self.rng.choice(
            (f"https://{slug}.example", f"https://www.{slug}.example/", f"{slug}.example")
        )
        return {"name": name, "country": self.rng.choice(COUNTRIES), "website": website}

    def orgs(self, n: int) -> list[dict[str, str]]:
        return [self.new_org() for _ in range(n)]

    def variant(self, org: dict[str, str]) -> dict[str, str]:
        """Same organization, written differently: case, punctuation, suffix, URL form."""
        name, country, website = org["name"], org["country"], org["website"]
        domain = website.split("//")[-1].removeprefix("www.").rstrip("/")
        change = self.rng.choice(("upper", "lower", "suffix", "hyphen", "suffix+case"))
        if change == "upper":
            name = name.upper()
        elif change == "lower":
            name, country = name.lower(), country.lower()
        elif change == "suffix":
            suffix = self.rng.choice(SUFFIXES)
            name = name + (suffix if suffix.startswith(",") else " " + suffix)
        elif change == "hyphen":
            name = name.replace(" ", "-", 1) + "."
        else:
            name = name.upper() + " " + self.rng.choice(SUFFIXES).lstrip(", ")
        website = self.rng.choice((f"http://www.{domain}", f"{domain}/", f"https://{domain}/about"))
        return {"name": name, "country": country, "website": website}

    def incomplete(self, org: dict[str, str]) -> dict[str, str]:
        field = self.rng.choice(("name", "country"))
        return {**org, field: ""}

    # --- events ----------------------------------------------------------------------------

    def events(self, refs: list[str], n: int) -> list[dict[str, str]]:
        seen: set[tuple[str, str, str]] = set()
        out = []
        while len(out) < n:
            ref = self.rng.choice(refs)
            title = self.rng.choice(EVENT_TITLES)
            day = date(2023, 1, 1) + timedelta(days=self.rng.randrange(3 * 365))
            starts_on = self.rng.choice(
                (day.isoformat(), day.isoformat(), day.strftime("%Y-%m"), str(day.year))
            )
            if (ref, title, starts_on) in seen:
                continue
            seen.add((ref, title, starts_on))
            out.append({"org_ref": ref, "title": title, "starts_on": starts_on})
        return out

    def ghost_ref(self) -> str:
        """A well-formed fingerprint that no generated organization has."""
        return f"{self.rng.getrandbits(128):032x}"


def _ref(org: dict[str, str]) -> str:
    return fingerprint(org["name"], org["country"], org["website"])


def _shuffled(rng: random.Random, rows: list) -> list:
    rows = list(rows)
    rng.shuffle(rows)
    return rows


def build(seed: int) -> dict[str, dict]:
    """Return {relative path: {"rows": [...], "meta": {...}}} for every sample file."""
    s = Sample(seed)
    files: dict[str, dict] = {}

    # messy/organizations.csv: 200 organizations, 30 rewritten duplicates, 8 incomplete rows.
    base = s.orgs(200)
    dupes = [s.variant(s.rng.choice(base)) for _ in range(30)]
    broken = [s.incomplete(org) for org in s.orgs(8)]
    rows = _shuffled(s.rng, base + dupes + broken)
    files["messy/organizations.csv"] = {
        "rows": rows,
        "meta": {
            "kind": "organizations",
            "source": "sample-messy-orgs",
            "expected_count": len(rows),
            "expect": {"status": "accepted", "inserted": 200, "duplicates": 30, "invalid": 8},
        },
    }

    # messy/organizations_short.csv: the sender promised 100 rows, 76 arrived.
    short = s.orgs(76)
    files["messy/organizations_short.csv"] = {
        "rows": short,
        "meta": {
            "kind": "organizations",
            "source": "sample-short-orgs",
            "expected_count": 100,
            "expect": {"status": "rejected", "inserted": 0},
        },
    }

    # messy/organizations_incomplete.csv: 6 of 66 rows (9.1%) lack a required field.
    incomplete = _shuffled(s.rng, s.orgs(60) + [s.incomplete(org) for org in s.orgs(6)])
    files["messy/organizations_incomplete.csv"] = {
        "rows": incomplete,
        "meta": {
            "kind": "organizations",
            "source": "sample-incomplete-orgs",
            "expected_count": len(incomplete),
            "expect": {"status": "rejected", "inserted": 0},
        },
    }

    # messy/events.json: 300 good events, 12 pointing nowhere, 6 repeats, 5 missing a field.
    good = s.events([_ref(org) for org in base], 300)
    orphans = s.events([s.ghost_ref() for _ in range(12)], 12)
    repeats = [dict(s.rng.choice(good)) for _ in range(6)]
    missing = [{**event, "title": ""} for event in s.events([_ref(org) for org in base], 5)]
    events = _shuffled(s.rng, good + orphans + repeats + missing)
    files["messy/events.json"] = {
        "rows": events,
        "meta": {
            "kind": "events",
            "source": "sample-messy-events",
            "expected_count": len(events),
            "expect": {
                "status": "accepted",
                "inserted": 312,
                "duplicates": 6,
                "invalid": 5,
                "orphans_after_messy_orgs": 12,
            },
        },
    }

    # clean/: no duplicates, nothing missing, every reference resolves.
    clean_orgs = s.orgs(50)
    files["clean/organizations.csv"] = {
        "rows": clean_orgs,
        "meta": {
            "kind": "organizations",
            "source": "sample-clean-orgs",
            "expected_count": len(clean_orgs),
            "expect": {"status": "accepted", "inserted": 50, "duplicates": 0, "invalid": 0},
        },
    }
    clean_events = s.events([_ref(org) for org in clean_orgs], 120)
    files["clean/events.json"] = {
        "rows": clean_events,
        "meta": {
            "kind": "events",
            "source": "sample-clean-events",
            "expected_count": len(clean_events),
            "expect": {"status": "accepted", "inserted": 120, "duplicates": 0, "invalid": 0},
        },
    }
    return files


def write(seed: int, out: Path) -> dict[str, dict]:
    files = build(seed)
    for rel, spec in files.items():
        path = out / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix == ".csv":
            with path.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=ORG_FIELDS, lineterminator="\n")
                writer.writeheader()
                writer.writerows(spec["rows"])
        else:
            path.write_text(json.dumps(spec["rows"], indent=1) + "\n", encoding="utf-8")
    manifest = {"seed": seed, "files": {rel: spec["meta"] for rel, spec in files.items()}}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.make_sample_data")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=Path, default=Path("data"))
    args = parser.parse_args(argv)
    manifest = write(args.seed, args.out)
    for rel, meta in manifest["files"].items():
        print(f"{args.out / rel}  kind={meta['kind']}  expected_count={meta['expected_count']}")
    print(args.out / "manifest.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
