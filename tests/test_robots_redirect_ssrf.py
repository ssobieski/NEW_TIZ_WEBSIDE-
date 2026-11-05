"""Redirect SSRF in the polite crawler.

A public page URL passes ``validate_fetch_url``. The crawler then loads
``robots.txt`` with ``follow_redirects`` and no SSRF check, and the page
clients follow redirects before ``_assert_public_response``. These tests
fail while any of those clients open a connection to a blocked address.
They pass once every redirect hop is refused before the connection.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import threading
from dataclasses import dataclass

import pytest

from market_agents.polite_http import AdaptivePoliteFetcher, CrawlPolicy
from market_agents.security import host_is_blocked, validate_fetch_url

PUBLIC_IP = "1.2.3.4"
METADATA_IP = "169.254.169.254"
RFC1918_IP = "10.66.66.66"
REBIND_HOST = "robots-rebind.invalid"
PAGE_REBIND_HOST = "page-rebind.invalid"

# First resolution of a rebind hostname is this public address. Every later
# resolution is loopback. A check that looks the name up, then lets the HTTP
# client look it up again, still connects to loopback and must fail.
_REBIND_PUBLIC = PUBLIC_IP
_REBIND_PRIVATE = "127.0.0.1"


@dataclass
class Hit:
    dest_ip: str
    path: str
    request_line: str


def _ip_bin() -> str:
    found = shutil.which("ip")
    if found:
        return found
    for candidate in ("/usr/sbin/ip", "/sbin/ip"):
        if os.path.isfile(candidate):
            return candidate
    pytest.fail("iproute2 `ip` is required to bind the public and blocked test addresses")


def _ensure_loopback_alias(ip: str) -> None:
    ip_bin = _ip_bin()
    show = subprocess.run(
        [ip_bin, "addr", "show", "dev", "lo"],
        capture_output=True,
        text=True,
        check=False,
    )
    if show.returncode != 0:
        show = subprocess.run(
            ["sudo", "-n", ip_bin, "addr", "show", "dev", "lo"],
            capture_output=True,
            text=True,
            check=False,
        )
    if show.returncode != 0:
        pytest.fail(f"cannot inspect lo: {show.stderr}")
    if f"inet {ip}/" in show.stdout:
        return
    add = [ip_bin, "addr", "add", f"{ip}/32", "dev", "lo"]
    result = subprocess.run(add, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        result = subprocess.run(
            ["sudo", "-n", *add], capture_output=True, text=True, check=False
        )
    if result.returncode != 0:
        pytest.fail(f"cannot assign {ip} on lo: {result.stderr}")


@pytest.fixture(scope="module", autouse=True)
def _loopback_aliases() -> None:
    """Addresses the SSRF guard treats as public, metadata, and RFC1918.

    Bound on lo so the crawl connects to this process and never leaves the host.
    """
    for ip in (PUBLIC_IP, METADATA_IP, RFC1918_IP):
        _ensure_loopback_alias(ip)


def _policy(**overrides: object) -> CrawlPolicy:
    base = dict(
        enabled=True,
        min_delay_seconds=0.0,
        max_delay_seconds=1.0,
        jitter_seconds=0.0,
        respect_robots_txt=True,
        adaptive=False,
        block_private_networks=True,
        resolve_dns_for_ssrf=True,
        max_retries=0,
        per_host_concurrency=1,
        global_concurrency=2,
        allowed_hosts=[],
    )
    base.update(overrides)
    return CrawlPolicy(**base)  # type: ignore[arg-type]


def _response(status: int, reason: str, headers: dict[str, str], body: bytes) -> bytes:
    hdrs = dict(headers)
    hdrs.setdefault("Content-Length", str(len(body)))
    hdrs.setdefault("Connection", "close")
    lines = [f"HTTP/1.1 {status} {reason}"]
    lines.extend(f"{key}: {value}" for key, value in hdrs.items())
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + body


def _redirect(location: str) -> bytes:
    return _response(302, "Found", {"Location": location}, b"")


def _ok(body: bytes) -> bytes:
    return _response(200, "OK", {"Content-Type": "text/plain"}, body)


class LocalOrigin:
    """HTTP/1.1 listener on every address under test, including IPv6 loopback."""

    def __init__(self, handler) -> None:
        self._handler = handler
        self.hits: list[Hit] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._socks: list[socket.socket] = []
        v4 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        v4.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        v4.bind(("0.0.0.0", 0))
        self.port = v4.getsockname()[1]
        v4.listen(50)
        v4.settimeout(0.2)
        self._socks.append(v4)
        v6 = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        v6.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        v6.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        v6.bind(("::1", self.port))
        v6.listen(50)
        v6.settimeout(0.2)
        self._socks.append(v6)

    def __enter__(self) -> "LocalOrigin":
        for sock in self._socks:
            thread = threading.Thread(target=self._accept, args=(sock,), daemon=True)
            thread.start()
            self._threads.append(thread)
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        for sock in self._socks:
            try:
                sock.close()
            except OSError:
                pass
        for thread in self._threads:
            thread.join(timeout=2)

    def _accept(self, sock: socket.socket) -> None:
        while not self._stop.is_set():
            try:
                conn, _addr = sock.accept()
            except OSError:
                if self._stop.is_set():
                    return
                continue
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(2)
            dest_ip = str(conn.getsockname()[0]).split("%", 1)[0]
            # Count the accept itself. A refusal that arrives after the TCP
            # handshake is still a request that reached the blocked address.
            hit = Hit(dest_ip, "", "")
            with self._lock:
                self.hits.append(hit)
            data = b""
            while b"\r\n\r\n" not in data and len(data) < 65536:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
            line = data.split(b"\r\n", 1)[0].decode("latin1", "replace")
            parts = line.split()
            path = parts[1] if len(parts) > 1 else "/"
            hit.path = path.split("?", 1)[0]
            hit.request_line = line
            payload = self._handler(path, dest_ip)
            conn.sendall(payload)
        except OSError:
            return
        finally:
            try:
                conn.close()
            except OSError:
                pass


def _blocked_hits(hits: list[Hit]) -> list[str]:
    found: list[str] = []
    for hit in hits:
        blocked, reason = host_is_blocked(hit.dest_ip, resolve_dns=False)
        if blocked:
            found.append(f"{hit.dest_ip} {hit.request_line or '(no request bytes)'} ({reason})")
    return found


def _assert_blocked_addresses_not_contacted(hits: list[Hit]) -> None:
    blocked = _blocked_hits(hits)
    assert not blocked, "crawl connected to a blocked address:\n" + "\n".join(blocked)


def _saw(hits: list[Hit], ip: str, path: str) -> bool:
    return any(hit.dest_ip == ip and hit.path.startswith(path) for hit in hits)


def _fetcher(tmp_path, **policy) -> AdaptivePoliteFetcher:
    AdaptivePoliteFetcher.reset_shared()
    return AdaptivePoliteFetcher(policy=_policy(**policy), data_dir=tmp_path)


def _attempt(fetcher: AdaptivePoliteFetcher, url: str):
    try:
        return fetcher.get(url)
    except Exception:
        return None


@pytest.mark.parametrize(
    "location",
    [
        "http://127.0.0.1:{port}/secret",
        "http://localhost:{port}/v1/models",
        f"http://{METADATA_IP}:{{port}}/latest/meta-data/",
        f"http://{RFC1918_IP}:{{port}}/secret",
        "http://[::1]:{port}/secret",
        "http://2130706433:{port}/secret",
        "http://127.1:{port}/secret",
        "http://0x7f000001:{port}/secret",
    ],
)
def test_robots_redirect_does_not_connect_to_blocked_target(location: str, tmp_path) -> None:
    def handler(path: str, _dest: str) -> bytes:
        if path == "/robots.txt":
            return _redirect(location.format(port=origin.port))
        return _ok(b"User-agent: *\nDisallow: /\n")

    with LocalOrigin(handler) as origin:
        page = f"http://{PUBLIC_IP}:{origin.port}/catalog"
        assert validate_fetch_url(page, resolve_dns=True) == page
        _attempt(_fetcher(tmp_path), page)
        assert _saw(origin.hits, PUBLIC_IP, "/robots.txt"), origin.hits
        _assert_blocked_addresses_not_contacted(origin.hits)


def test_robots_public_redirect_is_still_followed(tmp_path) -> None:
    def handler(path: str, _dest: str) -> bytes:
        if path == "/robots.txt":
            return _redirect(f"http://{PUBLIC_IP}:{origin.port}/robots-final.txt")
        if path == "/robots-final.txt":
            return _ok(b"User-agent: *\nAllow: /\n")
        if path == "/catalog":
            return _ok(b"catalog-body")
        return _ok(b"other")

    with LocalOrigin(handler) as origin:
        page = f"http://{PUBLIC_IP}:{origin.port}/catalog"
        response = _attempt(_fetcher(tmp_path), page)
        assert response is not None
        assert response.text == "catalog-body"
        assert _saw(origin.hits, PUBLIC_IP, "/robots-final.txt")
        _assert_blocked_addresses_not_contacted(origin.hits)


def test_robots_redirect_rebind_does_not_use_later_private_answer(tmp_path, monkeypatch) -> None:
    """Hop 1 resolves to the public address; hop 2 resolves to loopback.

    The counter is shared by the SSRF check and by ``socket.create_connection``,
    so validating the name and then resolving it again selects loopback.
    """
    lookups = {"n": 0}
    real_getaddrinfo = socket.getaddrinfo

    def fake_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        if host == REBIND_HOST:
            lookups["n"] += 1
            ip = _REBIND_PUBLIC if lookups["n"] == 1 else _REBIND_PRIVATE
            port_i = 0 if port is None else int(port)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port_i))]
        return real_getaddrinfo(host, port, family, type, proto, flags)

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    def handler(path: str, _dest: str) -> bytes:
        if path == "/robots.txt":
            return _redirect(f"http://{REBIND_HOST}:{origin.port}/hop1")
        if path == "/hop1":
            return _redirect(f"http://{REBIND_HOST}:{origin.port}/hop2")
        return _ok(b"User-agent: *\nAllow: /\n")

    with LocalOrigin(handler) as origin:
        page = f"http://{PUBLIC_IP}:{origin.port}/catalog"
        assert validate_fetch_url(page, resolve_dns=True) == page
        _attempt(_fetcher(tmp_path), page)
        assert _saw(origin.hits, PUBLIC_IP, "/robots.txt"), origin.hits
        _assert_blocked_addresses_not_contacted(origin.hits)


def test_page_redirect_does_not_connect_to_loopback(tmp_path) -> None:
    def handler(path: str, _dest: str) -> bytes:
        if path == "/robots.txt":
            return _ok(b"User-agent: *\nAllow: /\n")
        if path == "/catalog":
            return _redirect(f"http://127.0.0.1:{origin.port}/internal")
        return _ok(b"internal-body")

    with LocalOrigin(handler) as origin:
        page = f"http://{PUBLIC_IP}:{origin.port}/catalog"
        _attempt(_fetcher(tmp_path), page)
        assert _saw(origin.hits, PUBLIC_IP, "/catalog"), origin.hits
        _assert_blocked_addresses_not_contacted(origin.hits)


def test_page_redirect_rebind_does_not_use_later_private_answer(tmp_path, monkeypatch) -> None:
    lookups = {"n": 0}
    real_getaddrinfo = socket.getaddrinfo

    def fake_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        if host == PAGE_REBIND_HOST:
            lookups["n"] += 1
            ip = _REBIND_PUBLIC if lookups["n"] == 1 else _REBIND_PRIVATE
            port_i = 0 if port is None else int(port)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port_i))]
        return real_getaddrinfo(host, port, family, type, proto, flags)

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    def handler(path: str, _dest: str) -> bytes:
        if path == "/robots.txt":
            return _ok(b"User-agent: *\nAllow: /\n")
        if path == "/catalog":
            return _redirect(f"http://{PAGE_REBIND_HOST}:{origin.port}/hop1")
        if path == "/hop1":
            return _redirect(f"http://{PAGE_REBIND_HOST}:{origin.port}/hop2")
        return _ok(b"landed")

    with LocalOrigin(handler) as origin:
        page = f"http://{PUBLIC_IP}:{origin.port}/catalog"
        _attempt(_fetcher(tmp_path), page)
        assert _saw(origin.hits, PUBLIC_IP, "/catalog"), origin.hits
        _assert_blocked_addresses_not_contacted(origin.hits)


def test_disabled_crawl_redirect_does_not_connect_to_loopback(tmp_path) -> None:
    def handler(path: str, _dest: str) -> bytes:
        if path == "/catalog":
            return _redirect(f"http://127.0.0.1:{origin.port}/internal")
        return _ok(b"internal-body")

    with LocalOrigin(handler) as origin:
        page = f"http://{PUBLIC_IP}:{origin.port}/catalog"
        _attempt(_fetcher(tmp_path, enabled=False), page)
        assert _saw(origin.hits, PUBLIC_IP, "/catalog"), origin.hits
        _assert_blocked_addresses_not_contacted(origin.hits)


def test_page_public_redirect_is_still_followed(tmp_path) -> None:
    def handler(path: str, _dest: str) -> bytes:
        if path == "/robots.txt":
            return _ok(b"User-agent: *\nAllow: /\n")
        if path == "/catalog":
            return _redirect(f"http://{PUBLIC_IP}:{origin.port}/landed")
        if path == "/landed":
            return _ok(b"landed-ok")
        return _ok(b"other")

    with LocalOrigin(handler) as origin:
        page = f"http://{PUBLIC_IP}:{origin.port}/catalog"
        response = _attempt(_fetcher(tmp_path), page)
        assert response is not None
        assert response.text == "landed-ok"
        _assert_blocked_addresses_not_contacted(origin.hits)
