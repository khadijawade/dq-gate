import pytest

from app.fingerprint import fingerprint, normalize_domain, normalize_name


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Northwind Traders", "northwind traders"),
        ("NORTHWIND   traders", "northwind traders"),
        ("Northwind Traders, Inc.", "northwind traders"),
        ("Northwind Traders Inc", "northwind traders"),
        ("Northwind Traders Ltd.", "northwind traders"),
        ("Northwind Traders L.L.C.", "northwind traders"),
        ("Northwind Traders Pty Ltd", "northwind traders"),  # stacked suffixes
        ("Northwind Traders Co., Ltd", "northwind traders"),
        ("  Northwind-Traders  ", "northwind traders"),  # hyphen is a separator
        ("O'Hara Logistics", "ohara logistics"),  # apostrophe joins
        ("O’Hara Logistics", "ohara logistics"),  # curly apostrophe too
        ("Café Société", "cafe societe"),  # accents folded
        ("Salt & Pepper", "salt and pepper"),
        ("Salt and Pepper", "salt and pepper"),
        ("Inc", "inc"),  # a name that is only a suffix is kept
        ("Incline Partners", "incline partners"),  # suffix match is whole-token only
        ("Limited Edition Books", "limited edition books"),  # suffix only stripped at the end
        ("Ltd Ventures Inc", "ltd ventures"),
        ("Zürich Data GmbH", "zurich data"),
        ("東京 Labs", "東京 labs"),  # non-Latin letters survive
    ],
)
def test_normalize_name(raw: str, expected: str) -> None:
    assert normalize_name(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://www.northwind.example/about", "northwind.example"),
        ("http://NORTHWIND.example", "northwind.example"),
        ("northwind.example", "northwind.example"),
        ("www.northwind.example/", "northwind.example"),
        ("https://northwind.example:8443/x?y=1", "northwind.example"),
        ("https://shop.northwind.example", "shop.northwind.example"),  # subdomains are kept
        ("northwind.example.", "northwind.example"),
        ("", ""),
        ("   ", ""),
        (None, ""),
    ],
)
def test_normalize_domain(raw: str | None, expected: str) -> None:
    assert normalize_domain(raw) == expected


def test_variants_share_a_fingerprint() -> None:
    base = fingerprint("Northwind Traders", "GB", "northwind.example")
    assert fingerprint("northwind traders, ltd.", "gb", "https://www.northwind.example/") == base
    assert fingerprint("NORTHWIND TRADERS INC", " GB ", "http://northwind.example") == base


def test_country_and_domain_separate_organizations() -> None:
    base = fingerprint("Northwind Traders", "GB", "northwind.example")
    assert fingerprint("Northwind Traders", "IE", "northwind.example") != base
    assert fingerprint("Northwind Traders", "GB", "northwind-traders.example") != base
    assert fingerprint("Northwind Traders", "GB", None) != base


def test_fingerprint_is_deterministic_and_fixed_width() -> None:
    first = fingerprint("Northwind Traders", "GB", "northwind.example")
    assert first == fingerprint("Northwind Traders", "GB", "northwind.example")
    assert len(first) == 32
    int(first, 16)  # hex


def test_empty_name_is_rejected() -> None:
    with pytest.raises(ValueError):
        fingerprint("  ...  ", "GB", None)
