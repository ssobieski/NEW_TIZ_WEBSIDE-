"""Response bodies must be refused at max_response_bytes while they are read.

These tests open a local HTTP server and drive the real fetcher (httpx, not a
stand-in). An oversized body is delivered slowly enough that a streaming cap
can close the socket. The tests fail while the client reads the whole body
into memory before applying the cap.
"""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass
from typing import Literal
from unittest.mock import patch

import pytest

from market_agents.collectors.catalog import CatalogCollector
from market_agents.config import CatalogSource
from market_agents.polite_http import (
    AdaptivePoliteFetcher,
    CrawlBlockedError,
    CrawlPolicy,
)

# httpcore HTTP11Connection.READ_NUM_BYTES (httpcore 1.x shipped with httpx 0.28).
# A streaming reader may already have this much in the current recv when it stops.
_HTTPCORE_READ_BYTES = 64 * 1024
_CAP = 32 * 1024
_HUGE = 1_500_000
_HUGE_BODY = b"Z" * _HUGE
_PAGE_BODY = b"ok"


@dataclass
class _Route:
    kind: Literal["chunked", "length", "redirect"]
    body: bytes = b""
    location: str = "/blob"
    content_type: str = "application/octet-stream"


@dataclass
class _Transfer:
    path: str
    payload_bytes: int
    outcome: str  # "ok" | "closed" | "timeout"
    sndbuf: int


class _BodyServer:
    """HTTP/1.1 server that counts payload bytes the client actually accepts."""

    def __init__(self, routes: dict[str, _Route]) -> None:
        self._routes = routes
        self.transfers: list[_Transfer] = []
        self._lock = threading.Lock()
        self._cv = threading.Condition()
        self._active = 0
        self._stop = threading.Event()
        self._error: BaseException | None = None
        self._lsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._lsock.bind(("127.0.0.1", 0))
        self._lsock.listen(16)
        self._lsock.settimeout(0.2)
        self.port = self._lsock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, name="body-limit-server", daemon=True)
        self._thread.start()

    def __enter__(self) -> "_BodyServer":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._stop.set()
        try:
            self._lsock.close()
        except OSError:
            pass
        self._thread.join(timeout=3)

    def wait_idle(self, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        with self._cv:
            while self._active:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cv.wait(remaining)
        if self._error is not None:
            raise self._error

    def transfers_for(self, path: str) -> list[_Transfer]:
        with self._lock:
            return [item for item in self.transfers if item.path == path]

    def _note_active(self, delta: int) -> None:
        with self._cv:
            self._active += delta
            self._cv.notify_all()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _addr = self._lsock.accept()
            except TimeoutError:
                continue
            except OSError:
                if self._stop.is_set():
                    return
                continue
            self._note_active(1)
            try:
                self._handle(conn)
            except Exception as exc:  # noqa: BLE001
                self._error = exc
            finally:
                try:
                    conn.close()
                except OSError:
                    pass
                self._note_active(-1)

    def _handle(self, conn: socket.socket) -> None:
        conn.settimeout(5)
        try:
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8 * 1024)
        except OSError:
            pass
        sndbuf = 16 * 1024
        try:
            sndbuf = int(conn.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF))
        except OSError:
            pass
        raw = self._read_headers(conn)
        if raw is None:
            return
        path = _request_path(raw)
        route = self._routes.get(path)
        if route is None:
            self._send_raw(conn, _response_head(404, b"missing", content_length=True))
            self._record(path, 0, "ok", sndbuf)
            return
        if route.kind == "redirect":
            host = _header(raw, "host") or f"127.0.0.1:{self.port}"
            location = route.location
            if location.startswith("/"):
                location = f"http://{host}{location}"
            head = (
                b"HTTP/1.1 302 Found\r\n"
                b"Location: " + location.encode("ascii") + b"\r\n"
                b"Content-Length: 0\r\n"
                b"Connection: close\r\n"
                b"\r\n"
            )
            outcome = self._send_raw(conn, head)
            self._record(path, 0, outcome, sndbuf)
            return
        if route.kind == "length":
            head = _response_head(
                200,
                route.body,
                content_length=True,
                content_type=route.content_type,
            )
            outcome = self._send_raw(conn, head)
            if outcome != "ok":
                self._record(path, 0, outcome, sndbuf)
                return
            sent, outcome = self._send_payload(conn, route.body)
            self._record(path, sent, outcome, sndbuf)
            return
        head = _response_head(
            200,
            route.body,
            content_length=False,
            content_type=route.content_type,
        )
        outcome = self._send_raw(conn, head)
        if outcome != "ok":
            self._record(path, 0, outcome, sndbuf)
            return
        sent, outcome = self._send_chunked(conn, route.body)
        self._record(path, sent, outcome, sndbuf)

    def _record(self, path: str, payload_bytes: int, outcome: str, sndbuf: int) -> None:
        with self._lock:
            self.transfers.append(_Transfer(path, payload_bytes, outcome, sndbuf))

    @staticmethod
    def _read_headers(conn: socket.socket) -> bytes | None:
        data = b""
        while b"\r\n\r\n" not in data:
            try:
                part = conn.recv(4096)
            except TimeoutError:
                return None
            except OSError:
                return None
            if not part:
                return None
            data += part
            if len(data) > 65536:
                return None
        return data

    @staticmethod
    def _send_raw(conn: socket.socket, data: bytes) -> str:
        view = memoryview(data)
        while view:
            try:
                sent = conn.send(view)
            except TimeoutError:
                return "timeout"
            except ConnectionError:
                return "closed"
            except OSError:
                return "closed"
            if sent <= 0:
                return "closed"
            view = view[sent:]
        return "ok"

    def _send_payload(self, conn: socket.socket, body: bytes) -> tuple[int, str]:
        view = memoryview(body)
        sent_total = 0
        step = 8 * 1024
        while view:
            piece = view[:step]
            try:
                sent = conn.send(piece)
            except TimeoutError:
                return sent_total, "timeout"
            except ConnectionError:
                return sent_total, "closed"
            except OSError:
                return sent_total, "closed"
            if sent <= 0:
                return sent_total, "closed"
            sent_total += sent
            view = view[sent:]
        return sent_total, "ok"

    def _send_chunked(self, conn: socket.socket, body: bytes) -> tuple[int, str]:
        view = memoryview(body)
        offset = 0
        step = 8 * 1024
        while offset < len(view):
            chunk = view[offset : offset + step]
            prefix = f"{len(chunk):x}\r\n".encode("ascii")
            outcome = self._send_raw(conn, prefix)
            if outcome != "ok":
                return offset, outcome
            sent, outcome = self._send_payload(conn, chunk)
            offset += sent
            if outcome != "ok":
                return offset, outcome
            outcome = self._send_raw(conn, b"\r\n")
            if outcome != "ok":
                return offset, outcome
        outcome = self._send_raw(conn, b"0\r\n\r\n")
        if outcome != "ok":
            return offset, outcome
        return offset, "ok"


def _response_head(
    status: int,
    body: bytes,
    *,
    content_length: bool,
    content_type: str = "application/octet-stream",
) -> bytes:
    reason = "OK" if status == 200 else "ERR"
    lines = [
        f"HTTP/1.1 {status} {reason}",
        f"Content-Type: {content_type}",
        "Connection: close",
    ]
    if content_length:
        lines.append(f"Content-Length: {len(body)}")
    else:
        lines.append("Transfer-Encoding: chunked")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")


def _request_path(raw: bytes) -> str:
    line = raw.split(b"\r\n", 1)[0].decode("iso-8859-1", errors="replace")
    parts = line.split(" ")
    target = parts[1] if len(parts) >= 2 else "/"
    path = target.split("?", 1)[0]
    if path.startswith("http://") or path.startswith("https://"):
        path = "/" + path.split("/", 3)[-1]
    return path or "/"


def _header(raw: bytes, name: str) -> str:
    want = name.lower()
    for line in raw.split(b"\r\n")[1:]:
        if b":" not in line:
            continue
        key, value = line.split(b":", 1)
        if key.decode("iso-8859-1").strip().lower() == want:
            return value.decode("iso-8859-1").strip()
    return ""


def _policy(**overrides: object) -> CrawlPolicy:
    base = dict(
        enabled=True,
        min_delay_seconds=0.0,
        max_delay_seconds=5.0,
        jitter_seconds=0.0,
        respect_robots_txt=False,
        adaptive=False,
        max_retries=0,
        timeout_seconds=8.0,
        block_private_networks=False,
        resolve_dns_for_ssrf=False,
        max_redirects=3,
        max_response_bytes=_CAP,
        per_host_concurrency=1,
        global_concurrency=2,
    )
    base.update(overrides)
    return CrawlPolicy(**base)  # type: ignore[arg-type]


def _fetcher(tmp_path, **policy_overrides: object) -> AdaptivePoliteFetcher:
    AdaptivePoliteFetcher.reset_shared()
    return AdaptivePoliteFetcher(policy=_policy(**policy_overrides), data_dir=tmp_path)


def _allowance(transfer: _Transfer) -> int:
    # httpcore reads headers and body in 64 KiB recvs. A client that stops at
    # the cap can already have one of those in userspace, and the kernel can
    # have accepted another plus the send buffer before the close is visible.
    return _CAP + (2 * _HTTPCORE_READ_BYTES) + transfer.sndbuf + 8 * 1024


def _assert_capped(server: _BodyServer, path: str) -> None:
    server.wait_idle()
    rows = server.transfers_for(path)
    assert rows, f"no request reached {path}; saw {[t.path for t in server.transfers]}"
    for row in rows:
        limit = _allowance(row)
        assert limit < _HUGE // 2, f"send buffer {row.sndbuf} is too large to distinguish a full read"
        assert row.outcome != "timeout", (
            f"{path} stayed open after the client stopped reading "
            f"({row.payload_bytes} bytes queued)"
        )
        assert row.payload_bytes <= limit, (
            f"{path} accepted {row.payload_bytes} payload bytes before the "
            f"connection closed; cap is {_CAP}, allowance is {limit}"
        )
        assert row.payload_bytes < len(_HUGE_BODY)
        assert row.outcome == "closed" or row.payload_bytes <= _CAP


def test_small_chunked_response_is_returned(tmp_path):
    body = b"hello-chunked"
    routes = {"/small": _Route("chunked", body, content_type="text/plain")}
    with _BodyServer(routes) as server:
        fetcher = _fetcher(tmp_path)
        text = fetcher.get_text(f"http://127.0.0.1:{server.port}/small")
        server.wait_idle()
    assert text == "hello-chunked"
    rows = server.transfers_for("/small")
    assert len(rows) == 1
    assert rows[0].outcome == "ok"
    assert rows[0].payload_bytes == len(body)


def test_chunked_response_without_content_length_is_not_fully_buffered(tmp_path):
    routes = {"/huge": _Route("chunked", _HUGE_BODY)}
    url = None
    with _BodyServer(routes) as server:
        url = f"http://127.0.0.1:{server.port}/huge"
        fetcher = _fetcher(tmp_path)
        with pytest.raises(CrawlBlockedError):
            fetcher.get(url)
        with pytest.raises(CrawlBlockedError):
            fetcher.get_bytes(url)
        with pytest.raises(CrawlBlockedError):
            fetcher.get_text(url)
        _assert_capped(server, "/huge")


def test_content_length_over_cap_is_refused_before_the_body_is_read(tmp_path):
    routes = {"/huge": _Route("length", _HUGE_BODY)}
    with _BodyServer(routes) as server:
        fetcher = _fetcher(tmp_path)
        with pytest.raises(CrawlBlockedError):
            fetcher.get(f"http://127.0.0.1:{server.port}/huge")
        _assert_capped(server, "/huge")


def test_disabled_policy_still_applies_response_byte_cap(tmp_path):
    routes = {"/huge": _Route("chunked", _HUGE_BODY)}
    with _BodyServer(routes) as server:
        fetcher = _fetcher(tmp_path, enabled=False)
        with pytest.raises(CrawlBlockedError):
            fetcher.get(f"http://127.0.0.1:{server.port}/huge")
        _assert_capped(server, "/huge")


def test_robots_txt_chunked_body_is_capped(tmp_path):
    routes = {
        "/robots.txt": _Route("chunked", _HUGE_BODY, content_type="text/plain"),
        "/page": _Route("length", _PAGE_BODY, content_type="text/plain"),
    }
    with _BodyServer(routes) as server:
        fetcher = _fetcher(tmp_path, respect_robots_txt=True)
        try:
            response = fetcher.get(f"http://127.0.0.1:{server.port}/page")
        except CrawlBlockedError:
            response = None
        _assert_capped(server, "/robots.txt")
        if response is not None:
            assert response.content == _PAGE_BODY


def test_robots_txt_redirect_to_chunked_body_is_capped(tmp_path):
    routes = {
        "/robots.txt": _Route("redirect", location="/blob"),
        "/blob": _Route("chunked", _HUGE_BODY, content_type="text/plain"),
        "/page": _Route("length", _PAGE_BODY, content_type="text/plain"),
    }
    with _BodyServer(routes) as server:
        fetcher = _fetcher(tmp_path, respect_robots_txt=True)
        try:
            response = fetcher.get(f"http://127.0.0.1:{server.port}/page")
        except CrawlBlockedError:
            response = None
        _assert_capped(server, "/blob")
        if response is not None:
            assert response.content == _PAGE_BODY


def test_catalog_pdf_meta_does_not_buffer_oversized_body(tmp_path):
    routes = {"/catalog.pdf": _Route("chunked", _HUGE_BODY, content_type="application/pdf")}
    with _BodyServer(routes) as server:
        url = f"http://127.0.0.1:{server.port}/catalog.pdf"
        collector = CatalogCollector(
            CatalogSource(name="Oversized", brand="Zed", kind="pdf", url=url),
            fetcher=_fetcher(tmp_path),
        )
        meta = collector._pdf_meta(url)
        _assert_capped(server, "/catalog.pdf")
    assert meta.get("ok") is False
    assert len(str(meta.get("error") or "")) < _CAP


def test_catalog_pdf_extract_does_not_buffer_or_parse_oversized_body(tmp_path):
    routes = {"/catalog.pdf": _Route("chunked", _HUGE_BODY, content_type="application/pdf")}
    seen: list[bytes] = []

    class _RecordingReader:
        def __init__(self, stream, *args: object, **kwargs: object) -> None:
            raw = stream.getvalue() if hasattr(stream, "getvalue") else b""
            seen.append(raw)
            self.pages: list[object] = []

    with _BodyServer(routes) as server:
        url = f"http://127.0.0.1:{server.port}/catalog.pdf"
        collector = CatalogCollector(
            CatalogSource(
                name="Oversized",
                brand="Zed",
                kind="pdf",
                url=url,
                extract_text=True,
            ),
            fetcher=_fetcher(tmp_path),
        )
        with patch("pypdf.PdfReader", _RecordingReader):
            text = collector._extract_pdf_text(url)
        _assert_capped(server, "/catalog.pdf")
    assert seen == []
    assert len(text) < _CAP
    assert _HUGE_BODY[:64] not in text.encode("utf-8", errors="ignore")
