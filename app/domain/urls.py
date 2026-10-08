"""The instance's public URL (issue #17): the base of every link sent out in
Discord posts and emails. Always an explicit owner setting, never the request's Host."""

import ipaddress
from urllib.parse import urlsplit

LOOPBACK_NAMES = frozenset({"localhost", "localhost.localdomain"})


class InvalidPublicUrl(ValueError):
    pass


def parse_public_url(text: str, *, dev: bool) -> str:
    """`https://host[:port]` with nothing after it (a trailing `/` is dropped);
    `http://` only in dev. Raises InvalidPublicUrl with a message for the form."""
    value = text.strip()
    try:
        parts = urlsplit(value)
        port = parts.port  # raises on a bad port
    except ValueError as exc:
        raise InvalidPublicUrl("That isn't a valid URL.") from exc
    allowed = ("https", "http") if dev else ("https",)
    if parts.scheme not in allowed:
        raise InvalidPublicUrl(
            "Use an https:// address, e.g. https://picks.example.com."
            if not dev
            else "Use an http:// or https:// address."
        )
    if not parts.hostname:
        raise InvalidPublicUrl("Add the host name, e.g. https://picks.example.com.")
    if parts.username is not None or parts.password is not None:
        raise InvalidPublicUrl("Leave out any user name or password.")
    if parts.path not in ("", "/") or parts.query or parts.fragment or "?" in value or "#" in value:
        raise InvalidPublicUrl("Only the address itself: no path, ? or #.")
    if any(c.isspace() for c in value):
        raise InvalidPublicUrl("That isn't a valid URL.")
    host = parts.hostname
    netloc = f"[{host}]" if ":" in host else host
    return f"{parts.scheme}://{netloc}{f':{port}' if port is not None else ''}"


def is_loopback(url: str) -> bool:
    """True when links built from `url` can only work on the server itself."""
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return False
    if host.lower() in LOOPBACK_NAMES or host.lower().endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_unspecified
