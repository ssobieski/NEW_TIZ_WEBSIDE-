"""SSRF and response-size specs for feed collectors.

These tests fail while RssCollector, FirmDiscoveryCollector, and
SocialMediaCollector pass feed URLs to feedparser.parse. That client follows
redirects and reads the body with no private-network check and no size cap.

They pass once those collectors download through the guarded fetcher
(validate the URL, refuse redirects onto loopback / link-local metadata /
private addresses before connecting, and stop at CrawlPolicy.max_response_bytes
without reading the rest). A check of only the original URL, followed by
feedparser.parse on that URL, still fails the redirect tests.

LiteratureCollector is not covered: its feed path calls
AdaptivePoliteFetcher.get_text, which already runs validate_fetch_url.
SocialMediaCollector HTML discovery and parse_post use that fetcher too.
Firm discovery search queries are the same feedparser.parse call as feed_urls;
these tests pass feed_urls only, so they do not contact Google News.
"""

from __future__ import annotations

import socket
import subprocess
import threading
import time
from datetime import datetime, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import urlparse

import pytest

from market_agents.agents.collector import CollectorAgent
from market_agents.agents.orchestrator import Orchestrator
from market_agents.collectors.firm_discovery import FirmDiscoveryCollector
from market_agents.collectors.rss import RssCollector
from market_agents.collectors.social import SocialMediaCollector
from market_agents.config import (
    AgenticConfig,
    AgentsConfig,
    AppConfig,
    CrawlConfig,
    FirmDiscoveryConfig,
    FleetConfig,
    IndustryConfig,
    LlmConfig,
    RssSource,
    SocialMediaSource,
    SourcesConfig,
)
from market_agents.firms import KnownFirmsIndex
from market_agents.polite_http import AdaptivePoliteFetcher, CrawlPolicy
from market_agents.security import validate_fetch_url
from market_agents.storage import Storage

SENTINEL = "InternalFeedSentinelTitle"
CAP = CrawlPolicy().max_response_bytes
TAIL = b"T" * (4 * 1024 * 1024)
TAIL_SLACK = 262144

PUBLIC_IP = "1.2.3.4"
LOOPBACK_IP = "127.0.0.1"
METADATA_IP = "169.254.169.254"
PRIVATE_IP = "10.255.255.1"
LOCAL_IPS = (PUBLIC_IP, LOOPBACK_IP, METADATA_IP, PRIVATE_IP)

KINDS = ("rss", "firm", "youtube", "social_feed")
REDIRECT_TARGETS = ("loopback", "metadata", "private")


def _ensure_local_ip(ip: str) -> None:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((ip, 0))
    except OSError:
        probe.close()
        subprocess.run(
            ["sudo", "-n", "ip", "addr", "add", f"{ip}/32", "dev", "lo"],
            check=True,
        )
    else:
        probe.close()


@pytest.fixture(scope="session", autouse=True)
def _local_feed_addresses() -> None:
    for ip in LOCAL_IPS:
        _ensure_local_ip(ip)


@pytest.fixture(autouse=True)
def _reset_shared_fetcher() -> None:
    AdaptivePoliteFetcher.reset_shared()
    yield
    AdaptivePoliteFetcher.reset_shared()


def _feed_xml() -> bytes:
    published = format_datetime(datetime.now(timezone.utc))
    xml = f"""<?xml version="1.0"?>
<rss version="2.0"><channel>
<item>
<title>ZentaraTools GmbH launches carbide catalog {SENTINEL}</title>
<link>http://{PUBLIC_IP}/zentara-story</link>
<description>{SENTINEL} startup new manufacturer of cutting tools</description>
<pubDate>{published}</pubDate>
</item>
</channel></rss>
"""
    return xml.encode()


def _html_body() -> bytes:
    return (
        b"<html><head><title>"
        + SENTINEL.encode()
        + b"</title></head><body><p>"
        + SENTINEL.encode()
        + b"</p></body></html>"
    )


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        self.server.hits.append(path)  # type: ignore[attr-defined]
        if path == "/robots.txt":
            self.send_error(404)
            return
        route = self.server.routes.get(path)  # type: ignore[attr-defined]
        if route is None:
            self.send_error(404)
            return
        route(self)

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()


def _start(ip: str, routes: dict[str, Callable[[_Handler], None]]) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((ip, 0), _Handler)
    httpd.hits = []  # type: ignore[attr-defined]
    httpd.routes = routes  # type: ignore[attr-defined]
    httpd.extra_sent = -1  # type: ignore[attr-defined]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    httpd._test_thread = thread  # type: ignore[attr-defined]
    return httpd


def _stop(servers: list[ThreadingHTTPServer]) -> None:
    for httpd in servers:
        httpd.shutdown()
        httpd.server_close()


def _bytes_response(body: bytes, content_type: str) -> Callable[[_Handler], None]:
    def _send(handler: _Handler) -> None:
        handler.send_response(200)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.wfile.write(body)

    return _send


def _redirect(location: str) -> Callable[[_Handler], None]:
    def _send(handler: _Handler) -> None:
        handler.send_response(302)
        handler.send_header("Location", location)
        handler.send_header("Content-Length", "0")
        handler.send_header("Connection", "close")
        handler.end_headers()

    return _send


def _oversized(handler: _Handler) -> None:
    """Send exactly CAP bytes, pause, then a tail larger than the socket window."""
    handler.send_response(200)
    handler.send_header("Content-Type", "application/octet-stream")
    handler.send_header("Transfer-Encoding", "chunked")
    handler.send_header("Connection", "close")
    handler.end_headers()
    remaining = CAP
    chunk = b"B" * 8192
    try:
        while remaining:
            piece = chunk if remaining >= len(chunk) else chunk[:remaining]
            block = f"{len(piece):X}\r\n".encode() + piece + b"\r\n"
            handler.wfile.write(block)
            remaining -= len(piece)
        handler.wfile.flush()
    except OSError:
        handler.server.extra_sent = 0  # type: ignore[attr-defined]
        return
    time.sleep(0.5)
    header = f"{len(TAIL):X}\r\n".encode()
    handler.connection.settimeout(2)
    try:
        handler.connection.sendall(header)
    except OSError:
        handler.server.extra_sent = 0  # type: ignore[attr-defined]
        return
    sent = 0
    view = memoryview(TAIL)
    try:
        while sent < len(TAIL):
            n = handler.connection.send(view[sent:])
            if n <= 0:
                break
            sent += n
    except OSError:
        pass
    handler.server.extra_sent = sent  # type: ignore[attr-defined]
    try:
        handler.connection.sendall(b"\r\n0\r\n\r\n")
    except OSError:
        pass


def _invoke(kind: str, url: str) -> object:
    if kind == "rss":
        return RssCollector(RssSource(name="monitored", url=url), lookback_hours=72).collect()
    if kind == "firm":
        config = FirmDiscoveryConfig(
            enabled=True,
            feed_urls=[url],
            search_queries=[],
            max_items=5,
        )
        return FirmDiscoveryCollector(config, KnownFirmsIndex([])).collect()
    if kind == "youtube":
        source = SocialMediaSource(
            name="Channel",
            url="https://www.youtube.com/channel/UC",
            platform="youtube",
            feed_url=url,
            enabled=True,
        )
        return SocialMediaCollector(source, lookback_hours=24 * 30).collect()
    if kind == "social_feed":
        source = SocialMediaSource(
            name="Feed",
            url="https://www.linkedin.com/company/example/",
            platform="linkedin",
            feed_url=url,
            enabled=True,
        )
        return SocialMediaCollector(source, lookback_hours=24 * 30).collect()
    raise AssertionError(kind)


def _dump(result: object) -> str:
    if isinstance(result, BaseException):
        return f"{type(result).__name__}: {result}"
    if isinstance(result, list):
        parts: list[str] = []
        for item in result:
            parts.append(str(getattr(item, "title", "")))
            parts.append(str(getattr(item, "summary", "")))
            parts.append(str(getattr(item, "content", "")))
            parts.append(str(getattr(item, "url", "")))
            parts.append(str(getattr(item, "analysis", "")))
        return "\n".join(parts)
    return repr(result)


def _assert_internal_not_used(fn: Callable[[], object], secret: ThreadingHTTPServer) -> None:
    try:
        result: object = fn()
    except Exception as exc:  # noqa: BLE001
        result = exc
    assert secret.hits == [], f"internal service was requested: {secret.hits}"  # type: ignore[attr-defined]
    assert SENTINEL not in _dump(result)


def _app_config(tmp_path, rss_url: str) -> AppConfig:
    return AppConfig(
        industry=IndustryConfig(name="Test", keywords=[]),
        sources=SourcesConfig(
            rss=[RssSource(name="monitored", url=rss_url)],
            firm_discovery=FirmDiscoveryConfig(enabled=False, search_queries=[]),
        ),
        llm=LlmConfig(timeout_seconds=1, base_url="http://127.0.0.1:9"),
        agents=AgentsConfig(
            data_dir=str(tmp_path / "data"),
            report_dir=str(tmp_path / "reports"),
            lookback_hours=72,
            min_relevance_score=0.35,
            post_enrich_profiles=False,
            post_enrich_crm_tasks=False,
            post_enrich_suppliers=False,
            post_enrich_digest=False,
            post_enrich_crm_notion=False,
            fleet=FleetConfig(
                role="worker",
                auto_push_after_run=False,
                auto_pull_before_run=False,
            ),
            crawl=CrawlConfig(
                min_delay_seconds=0,
                jitter_seconds=0,
                respect_robots_txt=False,
                max_retries=0,
                timeout_seconds=5,
                max_response_bytes=CAP,
            ),
            agentic=AgenticConfig(enabled=False),
        ),
    )


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("target", REDIRECT_TARGETS)
def test_feed_redirect_is_not_followed_to_internal_host(kind: str, target: str) -> None:
    target_ip = {"loopback": LOOPBACK_IP, "metadata": METADATA_IP, "private": PRIVATE_IP}[target]
    secret = _start(target_ip, {"/feed": _bytes_response(_feed_xml(), "application/rss+xml")})
    secret_url = f"http://{target_ip}:{secret.server_address[1]}/feed"
    public = _start(PUBLIC_IP, {"/feed": _redirect(secret_url)})
    public_url = f"http://{PUBLIC_IP}:{public.server_address[1]}/feed"
    try:
        assert validate_fetch_url(public_url) == public_url
        with pytest.raises(Exception):
            validate_fetch_url(secret_url)
        _assert_internal_not_used(lambda: _invoke(kind, public_url), secret)
    finally:
        _stop([public, secret])


@pytest.mark.parametrize("kind", KINDS)
def test_feed_url_on_metadata_address_is_not_fetched(kind: str) -> None:
    secret = _start(METADATA_IP, {"/latest/meta-data/": _bytes_response(_feed_xml(), "application/rss+xml")})
    secret_url = f"http://{METADATA_IP}:{secret.server_address[1]}/latest/meta-data/"
    try:
        with pytest.raises(Exception):
            validate_fetch_url(secret_url)
        _assert_internal_not_used(lambda: _invoke(kind, secret_url), secret)
    finally:
        _stop([secret])


def test_rss_collector_does_not_fetch_internal_html_body() -> None:
    secret = _start(LOOPBACK_IP, {"/page": _bytes_response(_html_body(), "text/html")})
    secret_url = f"http://{LOOPBACK_IP}:{secret.server_address[1]}/page"
    try:
        _assert_internal_not_used(lambda: _invoke("rss", secret_url), secret)
    finally:
        _stop([secret])


@pytest.mark.parametrize("kind", KINDS)
def test_feed_download_does_not_read_past_max_response_bytes(kind: str) -> None:
    public = _start(PUBLIC_IP, {"/huge": _oversized})
    public_url = f"http://{PUBLIC_IP}:{public.server_address[1]}/huge"
    try:
        assert validate_fetch_url(public_url) == public_url
        try:
            _invoke(kind, public_url)
        except Exception:
            pass
        extra = public.extra_sent  # type: ignore[attr-defined]
        assert 0 <= extra < TAIL_SLACK, (
            f"{kind} read {extra} bytes past max_response_bytes ({CAP})"
        )
    finally:
        _stop([public])


def test_collector_agent_drops_feed_redirected_to_metadata(tmp_path) -> None:
    secret = _start(METADATA_IP, {"/feed": _bytes_response(_feed_xml(), "application/rss+xml")})
    secret_url = f"http://{METADATA_IP}:{secret.server_address[1]}/feed"
    public = _start(PUBLIC_IP, {"/feed": _redirect(secret_url)})
    public_url = f"http://{PUBLIC_IP}:{public.server_address[1]}/feed"
    try:
        config = _app_config(tmp_path, public_url)
        agent = CollectorAgent(config, Storage(config.data_path, config.report_path))
        _assert_internal_not_used(agent.run, secret)
    finally:
        _stop([public, secret])


def test_orchestrator_report_omits_feed_redirected_to_loopback(tmp_path) -> None:
    secret = _start(LOOPBACK_IP, {"/feed": _bytes_response(_feed_xml(), "application/rss+xml")})
    secret_url = f"http://{LOOPBACK_IP}:{secret.server_address[1]}/feed"
    public = _start(PUBLIC_IP, {"/feed": _redirect(secret_url)})
    public_url = f"http://{PUBLIC_IP}:{public.server_address[1]}/feed"
    try:
        config = _app_config(tmp_path, public_url)
        orchestrator = Orchestrator(config)
        try:
            result = orchestrator.run(skip_llm=True)
        except Exception as exc:  # noqa: BLE001
            result = exc
        assert secret.hits == [], f"internal service was requested: {secret.hits}"  # type: ignore[attr-defined]
        report_text = ""
        if not isinstance(result, BaseException):
            report_text = result.json_path.read_text(encoding="utf-8")
            report_text += result.md_path.read_text(encoding="utf-8")
            items_path = config.data_path / "items.jsonl"
            if items_path.exists():
                report_text += items_path.read_text(encoding="utf-8")
        assert SENTINEL not in report_text
        assert SENTINEL not in _dump(result)
    finally:
        _stop([public, secret])
