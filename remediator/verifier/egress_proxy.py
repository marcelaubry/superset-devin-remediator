"""Allowlisting CONNECT proxy: the only route out of the probe executor's network.

The executor sits on an `internal` Compose network and can reach nothing but this proxy.
Probe children (git, npm, yarn, node) are handed `HTTPS_PROXY`, so every download is a
`CONNECT host:443` request here. The proxy accepts only exact hostnames from
`EGRESS_ALLOWED_HOSTS` on port 443, forwards bytes without inspecting TLS, and refuses
everything else: plain-HTTP methods, other ports, IP literals, wildcard hosts, or any host
outside the list. It holds no credentials and runs in the credential-free verifier image.

Denials are logged with the requested host so an unexpected download shows up in the proxy
log; allowed connections are counted, not logged per host, to keep logs low-cardinality.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import re
import sys
from collections.abc import Awaitable, Callable

log = logging.getLogger("remediator.egress_proxy")

DEFAULT_ALLOWED_HOSTS = (
    "github.com",
    "codeload.github.com",
    "objects.githubusercontent.com",
    "registry.npmjs.org",
    "registry.yarnpkg.com",
    # Superset's package-lock.json resolves `xlsx` from the SheetJS CDN, not npm.
    "cdn.sheetjs.com",
)
_ALLOWED_PORT = 443
_HEADER_LIMIT = 8 * 1024
_HEADER_TIMEOUT_SECONDS = 10.0
_CONNECT_TIMEOUT_SECONDS = 15.0
_HOSTNAME = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


def parse_allowed_hosts(raw: str | None) -> frozenset[str]:
    """Exact lowercase hostnames only; wildcards and IP literals are configuration errors."""
    if raw is None:
        return frozenset(DEFAULT_ALLOWED_HOSTS)
    hosts: set[str] = set()
    for item in raw.split(","):
        host = item.strip().lower()
        if not host:
            continue
        if not _HOSTNAME.match(host):
            raise ValueError(f"EGRESS_ALLOWED_HOSTS entry is not an exact hostname: {item!r}")
        hosts.add(host)
    if not hosts:
        raise ValueError("EGRESS_ALLOWED_HOSTS must name at least one host")
    return frozenset(hosts)


def parse_connect_target(request_line: bytes) -> tuple[str, int] | None:
    """`CONNECT host:port HTTP/1.x` → (host, port); anything else is None."""
    try:
        text = request_line.decode("ascii").rstrip("\r\n")
    except UnicodeDecodeError:
        return None
    parts = text.split(" ")
    if len(parts) != 3 or parts[0] != "CONNECT" or not parts[2].startswith("HTTP/1."):
        return None
    host, sep, port_text = parts[1].rpartition(":")
    if not sep or not port_text.isdigit() or not host:
        return None
    return host.lower(), int(port_text)


def decide(target: tuple[str, int] | None, allowed: frozenset[str]) -> str | None:
    """None when the CONNECT is permitted, else the reason it is refused."""
    if target is None:
        return "only CONNECT is supported"
    host, port = target
    if port != _ALLOWED_PORT:
        return f"port {port} is not permitted"
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        pass
    else:
        return "IP literals are not permitted"
    if host not in allowed:
        return f"host {host} is not in the egress allowlist"
    return None


class EgressProxy:
    def __init__(self, allowed: frozenset[str]) -> None:
        self.allowed = allowed
        self.allowed_connections = 0
        self.denied_connections = 0
        self.dial: Callable[
            [str, int], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]
        ] = asyncio.open_connection

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        upstream_writer: asyncio.StreamWriter | None = None
        try:
            try:
                request_line = await asyncio.wait_for(
                    reader.readline(), timeout=_HEADER_TIMEOUT_SECONDS
                )
                await asyncio.wait_for(_discard_headers(reader), timeout=_HEADER_TIMEOUT_SECONDS)
            except (TimeoutError, asyncio.LimitOverrunError, ValueError):
                await _respond(writer, 400, "Bad Request")
                return
            target = parse_connect_target(request_line)
            reason = decide(target, self.allowed)
            if reason is not None or target is None:
                self.denied_connections += 1
                log.warning("egress denied: %s", reason)
                await _respond(writer, 403, "Forbidden")
                return
            try:
                upstream_reader, upstream = await asyncio.wait_for(
                    self.dial(*target), timeout=_CONNECT_TIMEOUT_SECONDS
                )
            except (TimeoutError, OSError):
                await _respond(writer, 502, "Bad Gateway")
                return
            upstream_writer = upstream
            self.allowed_connections += 1
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            await asyncio.gather(_pipe(reader, upstream), _pipe(upstream_reader, writer))
        finally:
            for stream in (writer, upstream_writer):
                if stream is not None:
                    stream.close()

    async def serve(self, host: str, port: int) -> asyncio.AbstractServer:
        return await asyncio.start_server(self.handle, host, port, limit=_HEADER_LIMIT)


async def _discard_headers(reader: asyncio.StreamReader) -> None:
    total = 0
    while True:
        line = await reader.readline()
        total += len(line)
        if total > _HEADER_LIMIT:
            raise ValueError("headers too large")
        if line in (b"\r\n", b"\n", b""):
            return


async def _respond(writer: asyncio.StreamWriter, status: int, text: str) -> None:
    writer.write(f"HTTP/1.1 {status} {text}\r\nConnection: close\r\n\r\n".encode())
    try:
        await writer.drain()
    except (ConnectionError, OSError):
        pass


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while chunk := await reader.read(64 * 1024):
            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, OSError, asyncio.CancelledError):
        pass
    finally:
        if writer.can_write_eof():
            try:
                writer.write_eof()
            except (ConnectionError, OSError):
                pass


async def _main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(message)s")
    allowed = parse_allowed_hosts(os.environ.get("EGRESS_ALLOWED_HOSTS"))
    port = int(os.environ.get("EGRESS_PROXY_PORT", "3128"))
    proxy = EgressProxy(allowed)
    server = await proxy.serve("0.0.0.0", port)
    log.info("egress proxy listening on %d for %s", port, ",".join(sorted(allowed)))
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(_main())
