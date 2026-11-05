"""SSRF: the fetcher must connect only to an address it already accepted.

These tests fail while the vulnerability is present. A passing run means every
hop resolved a name, rejected a non-public answer, connected to the accepted
address, and did not follow a redirect inside the HTTP client.

DNS answers are the attacker's input. Every validation lookup — including a
second call to ``host_is_blocked`` / ``validate_fetch_url``, which calls
``socket.getaddrinfo`` outside ``socket.create_connection`` — still returns the
public address. The lookup ``socket.create_connection`` makes (httpcore
``SyncBackend.connect_tcp`` → ``socket.create_connection``) returns the private
address. Re-checking the name and then handing the hostname to httpx therefore
still fails. Connecting to the accepted address literal passes.
"""

from __future__ import annotations

import socket
import subprocess
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from market_agents.parsing import IntelligentParser
from market_agents.polite_http import AdaptivePoliteFetcher, CrawlPolicy
from market_agents.security import host_is_blocked

PUBLIC_IP = "192.0.0.9"
PUBLIC_BODY = b"public-origin-body"
PRIVATE_BODY = b"private-origin-body"
REBIND_HOST = "rebind.ssrf.test.invalid"
STABLE_HOST = "public.ssrf.test.invalid"

# Addresses the guard classifies as non-public. 192.0.0.9 is the opposite:
# Python 3.12 ipaddress treats it as global (private-network exception).
PRIVATE_TARGETS = [
    pytest.param("127.0.0.1", id="loopback"),
    pytest.param("10.66.77.8", id="rfc1918-10"),
    pytest.param("192.168.77.8", id="rfc1918-192"),
    pytest.param("169.254.169.254", id="metadata"),
    pytest.param("::1", id="ipv6-loopback"),
]


def _ensure_local_address(ip: str) -> None:
    if ":" in ip:
        return
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((ip, 0))
        return
    except OSError:
        pass
    finally:
        probe.close()
    subprocess.run(
        ["sudo", "-n", "ip", "addr", "add", f"{ip}/32", "dev", "lo"],
        check=True,
        capture_output=True,
    )


def _literal_answer(ip: str, port: int | str | None) -> list[Any]:
    """Real resolver result for a numeric address (no network name lookup)."""
    numeric_port = 0 if port is None else port
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    return socket.getaddrinfo(ip, numeric_port, family, socket.SOCK_STREAM)


def _connect_lookup(stack: list[traceback.FrameSummary] | None = None) -> bool:
    frames = stack if stack is not None else traceback.extract_stack()
    return any(frame.name == "create_connection" for frame in frames)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802
        server: _Server = self.server  # type: ignore[assignment]
        server.hits.append(self.path.split("?", 1)[0])
        redirect = server.redirect_to
        prefix = server.redirect_prefix
        if redirect and (prefix is None or self.path.split("?", 1)[0].startswith(prefix)):
            body = b""
            self.send_response(302)
            self.send_header("Location", redirect)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
            return
        body = server.body
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        return


class _Server(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True
    hits: list[str]
    body: bytes
    redirect_to: str | None
    redirect_prefix: str | None


def _start_server(
    ip: str,
    port: int,
    *,
    body: bytes,
    redirect_to: str | None = None,
    redirect_prefix: str | None = None,
) -> _Server:
    _ensure_local_address(ip)
    if ":" in ip:
        class V6(_Server):
            address_family = socket.AF_INET6

        server_cls = V6
        bind = (ip, port, 0, 0)
    else:
        server_cls = _Server
        bind = (ip, port)
    httpd = server_cls(bind, _Handler)
    httpd.hits = []
    httpd.body = body
    httpd.redirect_to = redirect_to
    httpd.redirect_prefix = redirect_prefix
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd


def _stop(server: _Server | None) -> None:
    if server is None:
        return
    server.shutdown()
    server.server_close()


def _policy(*, enabled: bool, robots: bool) -> CrawlPolicy:
    return CrawlPolicy(
        enabled=enabled,
        min_delay_seconds=0.0,
        max_delay_seconds=0.0,
        jitter_seconds=0.0,
        respect_robots_txt=robots,
        adaptive=False,
        max_retries=0,
        per_host_concurrency=1,
        global_concurrency=2,
        block_private_networks=True,
        resolve_dns_for_ssrf=True,
        timeout_seconds=5.0,
        cooldown_on_block_seconds=0.0,
    )


def _fetcher(tmp_path, *, enabled: bool = True, robots: bool = False) -> AdaptivePoliteFetcher:
    AdaptivePoliteFetcher.reset_shared()
    return AdaptivePoliteFetcher(
        policy=_policy(enabled=enabled, robots=robots),
        data_dir=tmp_path,
    )


def _install_dns(monkeypatch: pytest.MonkeyPatch, rebind_to: str) -> None:
    """Public answer for every check; private answer only for the connect lookup."""
    real = socket.getaddrinfo

    def fake(host, port, family=0, type=0, proto=0, flags=0):  # noqa: A002
        name = host.decode("ascii") if isinstance(host, bytes) else str(host or "")
        name = name.strip("[]")
        if name == REBIND_HOST:
            ip = rebind_to if _connect_lookup() else PUBLIC_IP
            return _literal_answer(ip, port)
        if name == STABLE_HOST:
            return _literal_answer(PUBLIC_IP, port)
        return real(host, port, family, type, proto, flags)

    monkeypatch.setattr(socket, "getaddrinfo", fake)


def _clear_proxies(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "no_proxy",
        "NO_PROXY",
    ):
        monkeypatch.delenv(name, raising=False)


def _url(host: str, port: int, path: str) -> str:
    return f"http://{host}:{port}{path}"


def _target_url(ip: str, port: int, path: str) -> str:
    host = f"[{ip}]" if ":" in ip else ip
    return f"http://{host}:{port}{path}"


def _fetch(fetcher: AdaptivePoliteFetcher, url: str) -> dict[str, Any]:
    try:
        return {"response": fetcher.get(url), "error": None}
    except Exception as exc:  # noqa: BLE001 — the assertion is the side effect
        return {"response": None, "error": exc}


@pytest.fixture(autouse=True)
def _no_proxies(monkeypatch: pytest.MonkeyPatch):
    _clear_proxies(monkeypatch)


def test_stable_public_resolution_returns_that_body(tmp_path, monkeypatch):
    """A name that only resolves to a public address is still fetched."""
    _install_dns(monkeypatch, "127.0.0.1")
    public = _start_server(PUBLIC_IP, 0, body=PUBLIC_BODY)
    try:
        port = public.server_address[1]
        blocked, reason = host_is_blocked(STABLE_HOST, resolve_dns=True)
        assert blocked is False, reason
        fetcher = _fetcher(tmp_path)
        outcome = _fetch(fetcher, _url(STABLE_HOST, port, "/doc"))
        assert outcome["error"] is None, outcome["error"]
        assert outcome["response"].content == PUBLIC_BODY
        assert public.hits == ["/doc"]
    finally:
        _stop(public)


@pytest.mark.parametrize("enabled", [True, False], ids=["crawl-enabled", "crawl-disabled"])
@pytest.mark.parametrize("private_ip", PRIVATE_TARGETS)
def test_dns_rebind_connects_only_to_the_accepted_address(
    tmp_path, monkeypatch, enabled, private_ip
):
    """Repeated name checks stay public; the socket must still use that address.

    ``host_is_blocked`` is invoked several times on purpose. Each of those
    lookups returns the public address. Only the later ``create_connection``
    lookup returns ``private_ip``. The response must be the public origin.
    """
    _install_dns(monkeypatch, private_ip)
    public = _start_server(PUBLIC_IP, 0, body=PUBLIC_BODY)
    private = None
    try:
        port = public.server_address[1]
        private = _start_server(private_ip, port, body=PRIVATE_BODY)
        for _ in range(4):
            blocked, reason = host_is_blocked(REBIND_HOST, resolve_dns=True)
            assert blocked is False, reason
        fetcher = _fetcher(tmp_path, enabled=enabled, robots=False)
        outcome = _fetch(fetcher, _url(REBIND_HOST, port, "/doc"))
        assert private.hits == [], (
            f"connected to {private_ip} before any refusal; hits={private.hits} "
            f"error={outcome['error']!r}"
        )
        assert outcome["error"] is None, outcome["error"]
        assert outcome["response"].content == PUBLIC_BODY
        assert PRIVATE_BODY not in outcome["response"].content
        assert public.hits == ["/doc"]
    finally:
        _stop(private)
        _stop(public)


@pytest.mark.parametrize("enabled", [True, False], ids=["crawl-enabled", "crawl-disabled"])
@pytest.mark.parametrize("private_ip", PRIVATE_TARGETS)
def test_redirect_hop_is_not_connected(tmp_path, monkeypatch, enabled, private_ip):
    """A 302 to a private, loopback, or metadata address must not be requested."""
    _install_dns(monkeypatch, private_ip)
    private = _start_server(private_ip, 0, body=PRIVATE_BODY)
    public = None
    try:
        location = _target_url(private_ip, private.server_address[1], "/secret")
        public = _start_server(PUBLIC_IP, 0, body=PUBLIC_BODY, redirect_to=location)
        port = public.server_address[1]
        blocked, reason = host_is_blocked(STABLE_HOST, resolve_dns=True)
        assert blocked is False, reason
        fetcher = _fetcher(tmp_path, enabled=enabled, robots=False)
        outcome = _fetch(fetcher, _url(STABLE_HOST, port, "/page"))
        assert public.hits, "first hop was not fetched; redirect was not exercised"
        assert private.hits == [], (
            f"redirect to {location} was issued; hits={private.hits} "
            f"error={outcome['error']!r}"
        )
        response = outcome["response"]
        if response is not None:
            assert PRIVATE_BODY not in response.content
            final_host = (response.url.host or "").strip("[]").lower()
            assert final_host != private_ip.lower()
    finally:
        _stop(public)
        _stop(private)


@pytest.mark.parametrize("private_ip", PRIVATE_TARGETS)
def test_robots_redirect_is_not_connected(tmp_path, monkeypatch, private_ip):
    """robots.txt is fetched by its own client and must not follow an internal hop."""
    _install_dns(monkeypatch, private_ip)
    private = _start_server(private_ip, 0, body=PRIVATE_BODY)
    public = None
    try:
        location = _target_url(private_ip, private.server_address[1], "/robots-target")
        public = _start_server(
            PUBLIC_IP,
            0,
            body=PUBLIC_BODY,
            redirect_to=location,
            redirect_prefix="/robots.txt",
        )
        port = public.server_address[1]
        fetcher = _fetcher(tmp_path, enabled=True, robots=True)
        outcome = _fetch(fetcher, _url(STABLE_HOST, port, "/page"))
        assert "/robots.txt" in public.hits, public.hits
        assert private.hits == [], (
            f"robots.txt redirect to {location} was issued; hits={private.hits} "
            f"error={outcome['error']!r}"
        )
        response = outcome["response"]
        if response is not None:
            assert PRIVATE_BODY not in response.content
    finally:
        _stop(public)
        _stop(private)


def test_parser_fetch_html_returns_accepted_address_body(tmp_path, monkeypatch):
    """Agent fetch_html / fetch_and_parse uses the same fetcher and the same socket."""
    _install_dns(monkeypatch, "127.0.0.1")
    public = _start_server(PUBLIC_IP, 0, body=PUBLIC_BODY)
    private = None
    try:
        port = public.server_address[1]
        private = _start_server("127.0.0.1", port, body=PRIVATE_BODY)
        fetcher = _fetcher(tmp_path, enabled=True, robots=False)
        parser = IntelligentParser(timeout=5, learn=False, fetcher=fetcher)
        url = _url(REBIND_HOST, port, "/doc")
        try:
            text = parser.fetch_html(url)
            error = None
        except Exception as exc:  # noqa: BLE001
            text = ""
            error = exc
        assert private.hits == [], (
            f"parser fetch connected to 127.0.0.1; hits={private.hits} error={error!r}"
        )
        assert error is None, error
        assert PRIVATE_BODY.decode() not in text
        assert text == PUBLIC_BODY.decode()
    finally:
        _stop(private)
        _stop(public)
