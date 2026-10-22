"""Deterministic organization fingerprints.

Two records describe the same organization when their normalized name, country code and
website domain agree. The fingerprint is a hash of those three parts, so it is stable across
runs and machines and can be shared with other services as a reference.
"""

import hashlib
import re
import unicodedata
from urllib.parse import urlsplit

# Matched as whole trailing tokens after punctuation is removed ("L.L.C." becomes "llc").
LEGAL_SUFFIXES = frozenset(
    {
        "ab", "ag", "as", "bv", "co", "company", "corp", "corporation", "gmbh", "inc",
        "incorporated", "kg", "llc", "llp", "lp", "ltd", "limited", "nv", "oy", "plc",
        "pty", "sa", "sarl", "srl",
    }
)  # fmt: skip

_JOINERS = re.compile(r"[.'’`]")  # removed outright: "o'neil" -> "oneil", "l.l.c." -> "llc"
_SEPARATORS = re.compile(r"[\W_]+")  # everything else that is not a letter or digit


def normalize_name(name: str) -> str:
    text = unicodedata.normalize("NFKD", name)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold().replace("&", " and ")
    text = _JOINERS.sub("", text)
    tokens = _SEPARATORS.sub(" ", text).split()
    # Strip suffixes from the end only, and never strip the name down to nothing.
    while len(tokens) > 1 and tokens[-1] in LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def normalize_country(country: str) -> str:
    return country.strip().upper()


def normalize_domain(website: str | None) -> str:
    if not website or not website.strip():
        return ""
    raw = website.strip().lower()
    if "://" not in raw:
        raw = "//" + raw
    host = urlsplit(raw).hostname or ""
    host = host.rstrip(".")
    return host.removeprefix("www.")


def fingerprint(name: str, country: str, website: str | None) -> str:
    normalized = normalize_name(name)
    if not normalized:
        raise ValueError(f"name {name!r} normalizes to nothing")
    key = "|".join((normalized, normalize_country(country), normalize_domain(website)))
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
