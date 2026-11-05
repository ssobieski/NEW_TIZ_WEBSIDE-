"""Object-body bounds for Cloudflare R2 reads.

These tests exercise the real boto3 GetObject path against a local S3-style
endpoint. They fail while a caller still pulls the whole object into memory
and pass once each caller streams only what it keeps, and once an object whose
Content-Length is above the process response ceiling is refused before any
body byte is read.

The ceiling is the response cap already configured on SecurityPolicy and
CrawlConfig (max_response_bytes). HEAD and GET are different responses: a
small HEAD Content-Length must not authorize reading a GET body whose
Content-Length is over that ceiling.
"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator
from urllib.parse import parse_qs, unquote, urlparse

import pytest
import urllib3.response

from market_agents.agents.collector import CollectorAgent
from market_agents.config import AppConfig, IndustryConfig
from market_agents.collectors.r2 import CloudflareR2Collector, build_r2_collector
from market_agents.memory import MarketMemory
from market_agents.security import SecurityPolicy
from market_agents.storage import Storage
from market_agents.sync import KnowledgeSync
from market_agents.tools import ToolRegistry

BUCKET = "market-intel"
PREFIX = "monitoring/"
# Characters kept by the current callers. ASCII fixtures are one byte each.
FETCH_DEFAULT_MAX_CHARS = 12_000
SEARCH_KEPT_CHARS = 8_000
COLLECT_KEPT_CHARS = 50_000
# Small read-ahead past the caller's character cap. One chunk, not the object.
READ_AHEAD_MARGIN = 8_192
RESPONSE_CEILING = SecurityPolicy().max_response_bytes
PLACEHOLDER_ACCOUNT = "placeholder-account"
PLACEHOLDER_ACCESS_KEY = "placeholder-access-key"
PLACEHOLDER_SECRET_KEY = "placeholder-secret-key"


@dataclass
class ObjectSpec:
    body: bytes
    chunked: bool = False
    # When set, HEAD reports this Content-Length and GET reports len(body)
    # unless chunked. Used so a size check on a different request cannot
    # authorize the body that is actually served.
    head_content_length: int | None = None

    @property
    def get_content_length(self) -> int | None:
        if self.chunked:
            return None
        return len(self.body)


class BodyProbe:
    """Count bytes returned by urllib3 for marked GetObject bodies.

    botocore wraps that response in StreamingBody and the collector calls
    Body.read(). Marked responses send X-R2-Body so list and HEAD reads are
    not included.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self._orig = urllib3.response.HTTPResponse.read

    def install(self) -> None:
        probe = self
        original = self._orig

        def read(self, amt=None, decode_content=None, cache_content=False):  # noqa: ANN001
            headers = getattr(self, "headers", None)
            marked = headers is not None and str(headers.get("X-R2-Body") or "") == "1"
            if marked:
                probe.calls.append({"event": "start", "amt": amt})
            data = original(
                self,
                amt=amt,
                decode_content=decode_content,
                cache_content=cache_content,
            )
            if marked:
                probe.calls.append({"event": "return", "amt": amt, "n": len(data)})
            return data

        urllib3.response.HTTPResponse.read = read  # type: ignore[method-assign]

    def uninstall(self) -> None:
        urllib3.response.HTTPResponse.read = self._orig  # type: ignore[method-assign]

    def reset(self) -> None:
        self.calls.clear()

    @property
    def started(self) -> bool:
        return any(call.get("event") == "start" for call in self.calls)

    @property
    def body_bytes(self) -> int:
        return sum(int(call["n"]) for call in self.calls if call.get("event") == "return")


class _R2Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def do_HEAD(self) -> None:  # noqa: N802
        self._serve(head=True)

    def do_GET(self) -> None:  # noqa: N802
        self._serve(head=False)

    def _serve(self, *, head: bool) -> None:
        # A client that refuses the body must be able to return without the
        # handler blocking forever on a full-object write.
        self.connection.settimeout(10)
        server: R2Endpoint = self.server  # type: ignore[assignment]
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)
        parts = [part for part in path.split("/") if part]
        if not parts or parts[0] != server.bucket:
            self.send_error(404)
            return
        if "list-type" in query:
            self._list(query)
            return
        if len(parts) < 2:
            self.send_error(404)
            return
        key = "/".join(parts[1:])
        spec = server.objects.get(key)
        if spec is None:
            self.send_error(404)
            return
        if head:
            length = (
                spec.head_content_length
                if spec.head_content_length is not None
                else spec.get_content_length
            )
            server.responses.append(
                {"method": "HEAD", "key": key, "content_length": length}
            )
            self.send_response(200)
            if length is not None:
                self.send_header("Content-Length", str(length))
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            return
        server.responses.append(
            {
                "method": "GET",
                "key": key,
                "content_length": spec.get_content_length,
                "chunked": spec.chunked,
            }
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("X-R2-Body", "1")
        if spec.chunked:
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self._write_chunked(spec.body)
            return
        self.send_header("Content-Length", str(len(spec.body)))
        self.end_headers()
        self._write_all(spec.body)

    def _list(self, query: dict[str, list[str]]) -> None:
        server: R2Endpoint = self.server  # type: ignore[assignment]
        prefix = (query.get("prefix") or [""])[0]
        try:
            max_keys = int((query.get("max-keys") or ["1000"])[0])
        except ValueError:
            max_keys = 1000
        keys = [key for key in server.objects if key.startswith(prefix)][:max_keys]
        server.responses.append({"method": "LIST", "prefix": prefix, "keys": list(keys)})
        chunks = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            "<ListBucketResult>",
            "<IsTruncated>false</IsTruncated>",
            f"<Name>{server.bucket}</Name>",
            f"<Prefix>{prefix}</Prefix>",
            f"<KeyCount>{len(keys)}</KeyCount>",
            f"<MaxKeys>{max_keys}</MaxKeys>",
        ]
        for key in keys:
            chunks.append(f"<Contents><Key>{key}</Key><Size>1</Size></Contents>")
        chunks.append("</ListBucketResult>")
        body = "".join(chunks).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/xml")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _write_all(self, body: bytes) -> None:
        server: R2Endpoint = self.server  # type: ignore[assignment]
        view = memoryview(body)
        sent = 0
        try:
            while sent < len(view):
                chunk = view[sent : sent + 65_536]
                self.wfile.write(chunk)
                sent += len(chunk)
                server.body_bytes_written += len(chunk)
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            return

    def _write_chunked(self, body: bytes) -> None:
        server: R2Endpoint = self.server  # type: ignore[assignment]
        view = memoryview(body)
        sent = 0
        try:
            while sent < len(view):
                chunk = bytes(view[sent : sent + 8_192])
                sent += len(chunk)
                self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii"))
                self.wfile.write(chunk)
                self.wfile.write(b"\r\n")
                server.body_bytes_written += len(chunk)
            self.wfile.write(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            return


class R2Endpoint(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, objects: dict[str, ObjectSpec]) -> None:
        super().__init__(("127.0.0.1", 0), _R2Handler)
        self.bucket = BUCKET
        self.objects = objects
        self.responses: list[dict[str, object]] = []
        self.body_bytes_written = 0
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host}:{port}"

    def close(self) -> None:
        self.shutdown()
        self.server_close()


def _ascii_object(size: int, prefix: bytes) -> bytes:
    if len(prefix) > size:
        return prefix[:size]
    return prefix + (b"x" * (size - len(prefix)))


def _json_object() -> bytes:
    payload = {"note": "carbide", "tail": "y" * 18_000}
    return json.dumps(payload).encode("utf-8")


@contextmanager
def serve(objects: dict[str, ObjectSpec]) -> Iterator[R2Endpoint]:
    server = R2Endpoint(objects)
    try:
        yield server
    finally:
        server.close()


@pytest.fixture
def body_probe() -> Iterator[BodyProbe]:
    probe = BodyProbe()
    probe.install()
    try:
        yield probe
    finally:
        probe.uninstall()


def _config(tmp_path: Path, endpoint: str) -> AppConfig:
    cfg = AppConfig(industry=IndustryConfig(name="R2 bounds", keywords=["carbide"]))
    cfg.agents.data_dir = str(tmp_path)
    cfg.agents.report_dir = str(tmp_path / "reports")
    cfg.agents.governance.enabled = False
    cfg.agents.governance.audit_dir = str(tmp_path / "audit")
    cfg.sources.firm_discovery.enabled = False
    cfg.sources.notion.enabled = False
    r2 = cfg.sources.cloudflare_r2
    r2.enabled = True
    r2.bucket = BUCKET
    r2.prefix = PREFIX
    r2.endpoint_url = endpoint
    r2.max_objects = 50
    return cfg


def _creds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("CF_ACCOUNT_ID", PLACEHOLDER_ACCOUNT)
    monkeypatch.setenv("CF_R2_ACCESS_KEY_ID", PLACEHOLDER_ACCESS_KEY)
    monkeypatch.setenv("CF_R2_SECRET_ACCESS_KEY", PLACEHOLDER_SECRET_KEY)


def _collector(cfg: AppConfig) -> CloudflareR2Collector:
    return build_r2_collector(cfg.sources.cloudflare_r2, cfg.r2_credentials())


def _tools(cfg: AppConfig) -> ToolRegistry:
    return ToolRegistry(cfg, MarketMemory(cfg.data_path))


def _assert_capped(probe: BodyProbe, server: R2Endpoint, limit: int) -> None:
    assert probe.started, (
        "harness did not observe a GetObject body read; "
        f"server wrote {server.body_bytes_written} body bytes; "
        f"responses={server.responses!r}"
    )
    assert probe.body_bytes <= limit, (
        f"read {probe.body_bytes} object body bytes, limit is {limit}; "
        f"calls={probe.calls!r}; server wrote {server.body_bytes_written}"
    )


def _assert_body_unread(probe: BodyProbe, server: R2Endpoint) -> None:
    assert not probe.started, (
        f"body read started ({probe.body_bytes} bytes, calls={probe.calls!r}) "
        f"before the object was refused; server wrote {server.body_bytes_written}; "
        f"responses={server.responses!r}"
    )
    assert probe.body_bytes == 0


def test_response_ceiling_matches_configured_http_cap() -> None:
    """The R2 ceiling these tests enforce is the cap already in config."""
    from market_agents.config import CrawlConfig

    assert RESPONSE_CEILING == 15_000_000
    assert CrawlConfig().max_response_bytes == RESPONSE_CEILING
    assert SecurityPolicy().max_response_bytes == RESPONSE_CEILING


def test_fetch_r2_object_small_object_returns_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body_probe: BodyProbe
) -> None:
    key = PREFIX + "small.txt"
    body = b"PREFIX-carbide small note"
    with serve({key: ObjectSpec(body)}) as server:
        _creds(monkeypatch)
        cfg = _config(tmp_path, server.url)
        result = _tools(cfg).call(
            "fetch_r2_object", {"key": key, "max_chars": 100}
        )
    assert result["ok"] is True
    assert result["excerpt"].startswith("PREFIX-carbide")
    assert body_probe.body_bytes == len(body)


def test_fetch_r2_object_max_chars_does_not_download_the_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body_probe: BodyProbe
) -> None:
    key = PREFIX + "notes.txt"
    body = _ascii_object(300_000, b"PREFIX-carbide\n")
    with serve({key: ObjectSpec(body)}) as server:
        _creds(monkeypatch)
        cfg = _config(tmp_path, server.url)
        result = _tools(cfg).call(
            "fetch_r2_object", {"key": key, "max_chars": 64}
        )
        _assert_capped(body_probe, server, 64 + READ_AHEAD_MARGIN)
    assert result["ok"] is True
    assert result["key"] == key
    assert result["excerpt"].startswith("PREFIX-carbide")
    assert len(result["excerpt"]) <= 64


def test_fetch_r2_object_default_max_chars_bounds_the_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body_probe: BodyProbe
) -> None:
    key = PREFIX + "notes.txt"
    body = _ascii_object(300_000, b"PREFIX-carbide\n")
    with serve({key: ObjectSpec(body)}) as server:
        _creds(monkeypatch)
        cfg = _config(tmp_path, server.url)
        result = _tools(cfg).call("fetch_r2_object", {"key": key})
        _assert_capped(
            body_probe, server, FETCH_DEFAULT_MAX_CHARS + READ_AHEAD_MARGIN
        )
    assert result["ok"] is True
    assert result["excerpt"].startswith("PREFIX-carbide")


def test_search_r2_does_not_download_the_whole_text_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body_probe: BodyProbe
) -> None:
    key = PREFIX + "notes.txt"
    body = _ascii_object(300_000, b"PREFIX-carbide\n")
    with serve({key: ObjectSpec(body)}) as server:
        _creds(monkeypatch)
        cfg = _config(tmp_path, server.url)
        result = _tools(cfg).call("search_r2", {"query": "carbide", "limit": 5})
        _assert_capped(body_probe, server, SEARCH_KEPT_CHARS + READ_AHEAD_MARGIN)
    assert result["ok"] is True
    assert any(hit.get("key") == key for hit in result["hits"])


def test_collector_search_does_not_download_the_whole_text_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body_probe: BodyProbe
) -> None:
    key = PREFIX + "notes.txt"
    body = _ascii_object(300_000, b"PREFIX-carbide\n")
    with serve({key: ObjectSpec(body)}) as server:
        _creds(monkeypatch)
        cfg = _config(tmp_path, server.url)
        hits = _collector(cfg).search("carbide", limit=5)
        _assert_capped(body_probe, server, SEARCH_KEPT_CHARS + READ_AHEAD_MARGIN)
    assert any(hit.get("key") == key for hit in hits)


def _large_notes() -> tuple[str, ObjectSpec]:
    key = PREFIX + "notes.txt"
    return key, ObjectSpec(_ascii_object(300_000, b"PREFIX-carbide\n"))


def test_collect_does_not_download_the_whole_text_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body_probe: BodyProbe
) -> None:
    key, spec = _large_notes()
    with serve({key: spec}) as server:
        _creds(monkeypatch)
        cfg = _config(tmp_path, server.url)
        items = _collector(cfg).collect()
        _assert_capped(body_probe, server, COLLECT_KEPT_CHARS + READ_AHEAD_MARGIN)
    assert items and items[0].content.startswith("PREFIX-carbide")
    assert len(items[0].content) <= COLLECT_KEPT_CHARS


def test_sync_r2_does_not_download_the_whole_text_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body_probe: BodyProbe
) -> None:
    key, spec = _large_notes()
    with serve({key: spec}) as server:
        _creds(monkeypatch)
        cfg = _config(tmp_path, server.url)
        synced = KnowledgeSync(cfg).sync_r2()
        _assert_capped(body_probe, server, COLLECT_KEPT_CHARS + READ_AHEAD_MARGIN)
    assert synced.items == 1


def test_collector_agent_does_not_download_the_whole_text_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body_probe: BodyProbe
) -> None:
    key, spec = _large_notes()
    with serve({key: spec}) as server:
        _creds(monkeypatch)
        cfg = _config(tmp_path, server.url)
        cfg.sources.firm_discovery.enabled = False
        collected = CollectorAgent(cfg, Storage(cfg.data_path, cfg.report_path)).run()
        _assert_capped(body_probe, server, COLLECT_KEPT_CHARS + READ_AHEAD_MARGIN)
    assert any(
        (item.analysis or {}).get("r2_key") == key and len(item.content) <= COLLECT_KEPT_CHARS
        for item in collected
    )


@pytest.mark.parametrize(
    "operation",
    ["fetch_r2_object", "search_r2", "collect"],
)
def test_chunked_object_without_content_length_is_still_capped(
    operation: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    body_probe: BodyProbe,
) -> None:
    key = PREFIX + "chunked.txt"
    body = _ascii_object(100_000, b"PREFIX-carbide\n")
    with serve({key: ObjectSpec(body, chunked=True)}) as server:
        _creds(monkeypatch)
        cfg = _config(tmp_path, server.url)
        if operation == "fetch_r2_object":
            fetched = _tools(cfg).call(
                "fetch_r2_object", {"key": key, "max_chars": 64}
            )
            _assert_capped(body_probe, server, 64 + READ_AHEAD_MARGIN)
            assert fetched["ok"] is True
            assert fetched["excerpt"].startswith("PREFIX-carbide")
            assert len(fetched["excerpt"]) <= 64
        elif operation == "search_r2":
            found = _tools(cfg).call("search_r2", {"query": "carbide", "limit": 5})
            _assert_capped(body_probe, server, SEARCH_KEPT_CHARS + READ_AHEAD_MARGIN)
            assert any(hit.get("key") == key for hit in found["hits"])
        else:
            items = _collector(cfg).collect()
            _assert_capped(body_probe, server, COLLECT_KEPT_CHARS + READ_AHEAD_MARGIN)
            assert items and len(items[0].content) <= COLLECT_KEPT_CHARS


def test_get_json_reads_a_complete_document_under_the_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body_probe: BodyProbe
) -> None:
    key = PREFIX + "record.json"
    body = _json_object()
    assert len(body) < RESPONSE_CEILING
    assert len(body) > SEARCH_KEPT_CHARS
    with serve({key: ObjectSpec(body)}) as server:
        _creds(monkeypatch)
        cfg = _config(tmp_path, server.url)
        parsed = _collector(cfg).get_json(key)
        assert body_probe.started
        assert body_probe.body_bytes == len(body)
    assert parsed["note"] == "carbide"
    assert len(parsed["tail"]) == 18_000


def _oversize_spec() -> ObjectSpec:
    # GET is over the ceiling. HEAD reports a tiny length so a separate
    # lookup cannot be reused as permission to read this body.
    body = _ascii_object(RESPONSE_CEILING + 1, b"carbide\n")
    return ObjectSpec(body, head_content_length=8)


def test_head_content_length_is_not_the_get_content_length() -> None:
    """The endpoint used below answers HEAD and GET with different lengths.

    A size check against HEAD must not be treated as permission to read GET.
    """
    key = PREFIX + "oversize.txt"
    spec = _oversize_spec()
    with serve({key: spec}) as server:
        host, port = server.server_address[:2]
        path = f"/{BUCKET}/{key}"
        head_conn = HTTPConnection(host, port, timeout=5)
        head_conn.request("HEAD", path)
        head = head_conn.getresponse()
        head_length = int(head.getheader("Content-Length") or "0")
        head.read()
        head_conn.close()
        get_conn = HTTPConnection(host, port, timeout=5)
        get_conn.request("GET", path)
        got = get_conn.getresponse()
        get_length = int(got.getheader("Content-Length") or "0")
        get_conn.close()
    assert head_length == 8
    assert get_length == RESPONSE_CEILING + 1
    assert head_length != get_length


@pytest.mark.parametrize(
    "operation",
    [
        "fetch_r2_object",
        "search_r2",
        "collector_search",
        "collect",
        "get_json",
        "get_text",
    ],
)
def test_oversize_content_length_is_refused_before_the_body_is_read(
    operation: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    body_probe: BodyProbe,
) -> None:
    key = PREFIX + "oversize.txt"
    spec = _oversize_spec()
    assert spec.head_content_length == 8
    assert spec.get_content_length == RESPONSE_CEILING + 1
    with serve({key: spec}) as server:
        _creds(monkeypatch)
        cfg = _config(tmp_path, server.url)
        if operation == "fetch_r2_object":
            fetched = _tools(cfg).call(
                "fetch_r2_object", {"key": key, "max_chars": 64}
            )
            _assert_body_unread(body_probe, server)
            assert fetched.get("ok") is not True
        elif operation == "search_r2":
            searched = _tools(cfg).call("search_r2", {"query": "carbide", "limit": 5})
            _assert_body_unread(body_probe, server)
            assert searched.get("ok") is True
            assert all(hit.get("key") != key for hit in searched.get("hits") or [])
        elif operation == "collector_search":
            direct = _collector(cfg).search("carbide", limit=5)
            _assert_body_unread(body_probe, server)
            assert all(hit.get("key") != key for hit in direct)
        elif operation == "collect":
            items = _collector(cfg).collect()
            _assert_body_unread(body_probe, server)
            assert all(not item.content for item in items)
        elif operation == "get_json":
            with pytest.raises(Exception) as raised:
                _collector(cfg).get_json(key)
            _assert_body_unread(body_probe, server)
            assert not isinstance(raised.value, json.JSONDecodeError)
        else:
            try:
                text = _collector(cfg).get_text(key)
            except Exception:
                text = ""
            _assert_body_unread(body_probe, server)
            assert text == ""
        gets = [
            row
            for row in server.responses
            if row.get("method") == "GET" and row.get("key") == key
        ]
        assert gets, server.responses
        assert all(row.get("content_length") == RESPONSE_CEILING + 1 for row in gets)
        heads = [row for row in server.responses if row.get("method") == "HEAD"]
        assert all(row.get("content_length") == 8 for row in heads)
