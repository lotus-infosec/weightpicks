import pytest

from app.domain.urls import InvalidPublicUrl, is_loopback, parse_public_url


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("https://picks.example.com", "https://picks.example.com"),
        ("  https://picks.example.com/  ", "https://picks.example.com"),
        ("https://Picks.Example.com:8443", "https://picks.example.com:8443"),
        ("https://[2001:db8::1]", "https://[2001:db8::1]"),
    ],
)
def test_accepts(text: str, expected: str) -> None:
    assert parse_public_url(text, dev=False) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "picks.example.com",
        "http://picks.example.com",
        "ftp://picks.example.com",
        "https://",
        "https://picks.example.com/app",
        "https://picks.example.com/?a=1",
        "https://picks.example.com/#x",
        "https://picks.example.com?",
        "https://user:pw@picks.example.com",
        "https://picks.example.com:99999",
        "https://picks example.com",
        "javascript:alert(1)",
    ],
)
def test_rejects(text: str) -> None:
    with pytest.raises(InvalidPublicUrl):
        parse_public_url(text, dev=False)


def test_http_only_in_dev() -> None:
    assert parse_public_url("http://192.168.1.20:8000/", dev=True) == "http://192.168.1.20:8000"
    with pytest.raises(InvalidPublicUrl):
        parse_public_url("http://192.168.1.20:8000", dev=False)


@pytest.mark.parametrize(
    ("url", "loopback"),
    [
        ("http://127.0.0.1:8000", True),
        ("http://127.9.9.9", True),
        ("http://localhost:8000", True),
        ("http://app.localhost", True),
        ("http://[::1]:8000", True),
        ("http://0.0.0.0:8000", True),
        ("https://picks.example.com", False),
        ("http://192.168.1.20:8000", False),
    ],
)
def test_is_loopback(url: str, loopback: bool) -> None:
    assert is_loopback(url) is loopback
