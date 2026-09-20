"""Provider-supplied URLs are untrusted until reduced to a plain absolute http(s) link."""

from urllib.parse import urlsplit


def safe_href(url: str | None) -> str | None:
    """Only absolute http(s) URLs without whitespace or control characters may become links;
    anything else (javascript:, data:, relative paths, garbage) renders as plain text."""
    if not url:
        return None
    candidate = url.strip()
    if any(ch.isspace() or ord(ch) < 0x20 for ch in candidate):
        return None
    parsed = urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return candidate
