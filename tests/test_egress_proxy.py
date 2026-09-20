import asyncio

import pytest

from remediator.verifier.egress_proxy import (
    DEFAULT_ALLOWED_HOSTS,
    EgressProxy,
    decide,
    parse_allowed_hosts,
    parse_connect_target,
)


def test_allowlist_is_exact_hostnames_only() -> None:
    assert parse_allowed_hosts(None) == frozenset(DEFAULT_ALLOWED_HOSTS)
    assert parse_allowed_hosts(" GitHub.com , registry.npmjs.org,") == {
        "github.com",
        "registry.npmjs.org",
    }
    for bad in ("*.github.com", "1.2.3.4", "github.com:443", "http://github.com", "localhost"):
        with pytest.raises(ValueError):
            parse_allowed_hosts(bad)
    with pytest.raises(ValueError, match="at least one"):
        parse_allowed_hosts(" , ")


def test_connect_parsing_and_decisions() -> None:
    allowed = frozenset({"github.com"})
    assert parse_connect_target(b"CONNECT github.com:443 HTTP/1.1\r\n") == ("github.com", 443)
    assert parse_connect_target(b"GET http://github.com/ HTTP/1.1\r\n") is None
    assert parse_connect_target(b"CONNECT github.com HTTP/1.1\r\n") is None
    assert parse_connect_target(b"\xff\xfe") is None
    assert decide(("github.com", 443), allowed) is None
    assert decide(("GITHUB.com".lower(), 443), allowed) is None
    assert decide(("evil.example", 443), allowed) is not None
    assert decide(("github.com", 80), allowed) is not None
    assert decide(("github.com", 22), allowed) is not None
    assert decide(("140.82.112.3", 443), allowed) is not None
    assert decide(("[::1]", 443), allowed) is not None
    assert decide(None, allowed) is not None


async def _request(port: int, raw: bytes) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(raw)
    await writer.drain()
    try:
        return await asyncio.wait_for(reader.readline(), timeout=5)
    finally:
        writer.close()


async def test_proxy_refuses_everything_but_allowlisted_connect() -> None:
    upstream_seen: list[bytes] = []

    async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        data = await reader.read(64)
        upstream_seen.append(data)
        writer.write(b"echo:" + data)
        await writer.drain()
        writer.close()

    upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
    upstream_port = upstream.sockets[0].getsockname()[1]

    # "allowed.test:443" resolves to the local echo server; anything else is a real refusal.
    async def dial(host: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        assert (host, port) == ("allowed.test", 443)
        return await asyncio.open_connection("127.0.0.1", upstream_port)

    proxy = EgressProxy(frozenset({"allowed.test"}))
    proxy.dial = dial
    server = await proxy.serve("127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        assert (await _request(port, b"GET http://allowed.test/ HTTP/1.1\r\n\r\n")).startswith(
            b"HTTP/1.1 403"
        )
        assert (await _request(port, b"CONNECT other.test:443 HTTP/1.1\r\n\r\n")).startswith(
            b"HTTP/1.1 403"
        )
        assert (await _request(port, b"CONNECT allowed.test:80 HTTP/1.1\r\n\r\n")).startswith(
            b"HTTP/1.1 403"
        )
        assert (await _request(port, b"CONNECT 127.0.0.1:443 HTTP/1.1\r\n\r\n")).startswith(
            b"HTTP/1.1 403"
        )
        assert (await _request(port, b"junk\r\n" + b"x" * 9000 + b"\r\n\r\n")).startswith(
            b"HTTP/1.1 400"
        )
        assert proxy.denied_connections == 4 and proxy.allowed_connections == 0

        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"CONNECT allowed.test:443 HTTP/1.1\r\nHost: allowed.test:443\r\n\r\n")
        await writer.drain()
        assert (await reader.readline()).startswith(b"HTTP/1.1 200")
        assert await reader.readline() == b"\r\n"
        writer.write(b"payload")
        await writer.drain()
        assert await asyncio.wait_for(reader.read(64), timeout=5) == b"echo:payload"
        writer.close()
        assert upstream_seen == [b"payload"]
        assert proxy.allowed_connections == 1
    finally:
        server.close()
        upstream.close()
