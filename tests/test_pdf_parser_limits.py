"""Resource bounds for catalog PDF parsing.

These tests are the spec for the parser-limit bug. They fail while the
vulnerability is present and pass once extraction stops doing work the
page and character caps do not pay for.

What the product does today (market_agents/collectors/catalog.py):
fetch_pdf_text, collect() on a PDF source, and extract_product_tech_schema
all download the body and call _extract_pdf_text. That builds
``PdfReader`` on the entire buffer, then slices ``reader.pages``. The
slice happens only after pypdf has already walked the page tree.

pypdf 6.21.4 is the version ``pypdf>=4.2.0`` resolves to here. In that
version ``PdfReader.pages`` is a virtual list whose length and slice both
call ``get_num_pages``, and ``get_num_pages`` flattens every page into
``flattened_pages`` before the caller's ``max_pages`` slice is applied
(``pypdf/_page.py`` ``_VirtualList.__getitem__``, ``pypdf/_doc_common.py``
``get_num_pages`` / ``_flatten``). ``PageObject.extract_text`` then
decompresses the whole content stream and tokenizes it
(``EncodedStreamObject.get_data`` → ``filters.decode_stream_data``,
``ContentStream.operations``) before catalog.py truncates to ``max_chars``.
Library defaults (75MB zlib output, 100_000 page-tree entries, 2_000_000
content tokens) sit far above ``max_pages`` / ``max_chars``. The 15MB
response cap in the polite fetcher does not apply to the payloads below.

A later fix may abort when a tight parser limit trips, or it may stop
after ``max_pages`` without materializing the rest of the tree. Either
one keeps the calls inside the budgets. Flattening or decompressing
first and only then returning an error does not: the budget is the peak
allocated while the call runs.
"""

from __future__ import annotations

import threading
import time
import tracemalloc
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from market_agents.collectors.catalog import CatalogCollector
from market_agents.config import (
    AgentsConfig,
    AppConfig,
    CatalogSource,
    CrawlConfig,
    IndustryConfig,
    SecurityConfig,
    SourcesConfig,
)
from market_agents.memory import MarketMemory
from market_agents.polite_http import AdaptivePoliteFetcher, CrawlPolicy
from market_agents.tools import ToolRegistry

# 25_000 copies of one page. Measured on pypdf 6.21.4: the file is ~150KB
# (under the 15MB response cap) and reader.pages[0] flattens all 25_000
# pages, peaking at about 17MB. Parsing the /Kids array alone peaks at
# about 2.6MB, so a reader that does not materialize every page stays
# under the budget with room to spare.
_PAGE_TREE_COUNT = 25_000
_PAGE_TREE_PEAK_BYTES = 8 * 1024 * 1024

# 80_000 content operators, flate-compressed to under 1KB. Measured:
# extract_text takes ~4.7s and peaks at about 26MB, then returns no text.
# max_chars never sees that work. A limit that stops decompression or
# tokenization early finishes well under both budgets.
_STREAM_OPERATORS = 80_000
_STREAM_PEAK_BYTES = 6 * 1024 * 1024
_STREAM_SECONDS = 1.5

_RESPONSE_CAP = 15_000_000


def _build_pdf(objects: list[bytes]) -> bytes:
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode()
        out += body
        if not body.endswith(b"\n"):
            out += b"\n"
        out += b"endobj\n"
    xref = len(out)
    size = len(objects) + 1
    out += f"xref\n0 {size}\n".encode()
    out += b"0000000000 65535 f \n"
    for index in range(1, size):
        out += f"{offsets[index]:010d} 00000 n \n".encode()
    out += (
        f"trailer << /Size {size} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(out)


def _page_tree_pdf(page_count: int) -> bytes:
    """One real page, referenced ``page_count`` times from /Kids."""
    kids = b" ".join([b"3 0 R"] * page_count)
    return _build_pdf(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Count %d /Kids [%s] >>" % (page_count, kids),
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 10 10] "
            b"/Resources << /ProcSet [/PDF] >> >>",
        ]
    )


def _stream_bomb_pdf(operators: int) -> bytes:
    """Tiny flate stream that expands into ``operators`` content operators."""
    raw = b"0 0 Td\n" * operators
    compressed = zlib.compress(raw, 9)
    stream = (
        b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(compressed)
        + compressed
        + b"\nendstream"
    )
    return _build_pdf(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Count 1 /Kids [3 0 R] >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 10 10] "
            b"/Resources << /ProcSet [/PDF] >> /Contents 4 0 R >>",
            stream,
        ]
    )


def _benign_pdf() -> bytes:
    text = b"BT /F1 12 Tf 10 50 Td (PRICE-LIST-OK) Tj ET"
    stream = b"<< /Length %d >>\nstream\n" % len(text) + text + b"\nendstream"
    return _build_pdf(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Count 1 /Kids [3 0 R] >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            stream,
        ]
    )


class _PdfHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        pdf = self.server.pdf_bytes  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", str(len(pdf)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(pdf)

    def log_message(self, fmt: str, *args: object) -> None:
        return


class _PdfSite:
    def __init__(self, pdf: bytes) -> None:
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _PdfHandler)
        self._httpd.pdf_bytes = pdf  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        host, port = self._httpd.server_address[:2]
        self.url = f"http://{host}:{port}/pricelist.pdf"

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


def _fetcher(tmp: Path) -> AdaptivePoliteFetcher:
    # Private-host block is off only so this process can stand in for the
    # attacker's public host. The bytes still travel through httpx and the
    # response-size check in AdaptivePoliteFetcher.get.
    return AdaptivePoliteFetcher(
        policy=CrawlPolicy(
            enabled=True,
            min_delay_seconds=0,
            max_delay_seconds=0,
            jitter_seconds=0,
            max_retries=0,
            backoff_factor=1,
            respect_robots_txt=False,
            adaptive=False,
            block_private_networks=False,
            resolve_dns_for_ssrf=False,
            max_response_bytes=_RESPONSE_CAP,
        ),
        data_dir=tmp,
    )


def _measure(fn) -> tuple[float, int]:
    tracemalloc.start()
    started = time.perf_counter()
    try:
        fn()
    except Exception:
        # A fix may raise once a parser limit trips. The budget still applies
        # to whatever was allocated before that exception left the parser.
        pass
    finally:
        elapsed = time.perf_counter() - started
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    return elapsed, peak


def _assert_page_tree_budget(elapsed: float, peak: int) -> None:
    del elapsed  # page-tree cost shows up as retained PageObjects, not time
    assert peak < _PAGE_TREE_PEAK_BYTES, (
        f"page cap did not constrain the parser: peak {peak} bytes "
        f"while walking a {_PAGE_TREE_COUNT}-page tree "
        f"(budget {_PAGE_TREE_PEAK_BYTES} bytes)"
    )


def _assert_stream_budget(elapsed: float, peak: int) -> None:
    assert peak < _STREAM_PEAK_BYTES, (
        f"character cap did not constrain decompression: peak {peak} bytes "
        f"(budget {_STREAM_PEAK_BYTES} bytes)"
    )
    assert elapsed < _STREAM_SECONDS, (
        f"character cap did not constrain content-stream parsing: "
        f"{elapsed:.2f}s for {_STREAM_OPERATORS} operators "
        f"(budget {_STREAM_SECONDS}s)"
    )


@pytest.fixture()
def workspace(tmp_path: Path):
    site_holder: list[_PdfSite] = []

    def open_site(pdf: bytes) -> _PdfSite:
        site = _PdfSite(pdf)
        site_holder.append(site)
        return site

    yield tmp_path, open_site
    for site in site_holder:
        site.close()


def test_small_pdf_text_still_extracted(workspace):
    """A one-page catalog PDF still yields its text. Guards a fix that rejects every file."""
    tmp, open_site = workspace
    pdf = _benign_pdf()
    assert len(pdf) < _RESPONSE_CAP
    site = open_site(pdf)
    collector = CatalogCollector(
        CatalogSource(
            name="benign",
            brand="Benign",
            kind="pdf",
            url=site.url,
            extract_text=True,
            max_pdf_pages=8,
            max_pdf_chars=12000,
        ),
        fetcher=_fetcher(tmp),
    )
    text = collector._extract_pdf_text(site.url, max_pages=8, max_chars=12000)
    assert "PRICE-LIST-OK" in text
    assert len(text) <= 12000


def test_downloaded_pdf_is_under_the_response_cap():
    page_tree = _page_tree_pdf(_PAGE_TREE_COUNT)
    bomb = _stream_bomb_pdf(_STREAM_OPERATORS)
    assert len(page_tree) < _RESPONSE_CAP
    assert len(bomb) < _RESPONSE_CAP
    # The hostile files are small enough that the polite fetcher's byte cap
    # accepts them. The budgets below are the control that cap does not provide.
    assert len(page_tree) < 1_000_000
    assert len(bomb) < 100_000


def test_extract_pdf_text_does_not_materialize_full_page_tree(workspace):
    tmp, open_site = workspace
    pdf = _page_tree_pdf(_PAGE_TREE_COUNT)
    site = open_site(pdf)
    collector = CatalogCollector(
        CatalogSource(
            name="hostile-catalog",
            brand="Hostile",
            kind="pdf",
            url=site.url,
            extract_text=True,
            max_pdf_pages=1,
            max_pdf_chars=64,
        ),
        fetcher=_fetcher(tmp),
    )

    def run() -> None:
        collector._extract_pdf_text(site.url, max_pages=1, max_chars=64)

    elapsed, peak = _measure(run)
    _assert_page_tree_budget(elapsed, peak)


def test_fetch_pdf_text_does_not_materialize_full_page_tree(workspace):
    tmp, open_site = workspace
    pdf = _page_tree_pdf(_PAGE_TREE_COUNT)
    site = open_site(pdf)
    collector = CatalogCollector(
        CatalogSource(
            name="hostile-catalog",
            brand="Hostile",
            kind="pdf",
            url=site.url,
            extract_text=True,
        ),
        fetcher=_fetcher(tmp),
    )

    def run() -> None:
        collector.fetch_pdf_text(url=site.url, max_pages=1, max_chars=64)

    elapsed, peak = _measure(run)
    _assert_page_tree_budget(elapsed, peak)


def test_collect_pdf_does_not_materialize_full_page_tree(workspace):
    tmp, open_site = workspace
    pdf = _page_tree_pdf(_PAGE_TREE_COUNT)
    site = open_site(pdf)
    collector = CatalogCollector(
        CatalogSource(
            name="hostile-catalog",
            brand="Hostile",
            kind="pdf",
            url=site.url,
            extract_text=True,
            max_pdf_pages=1,
            max_pdf_chars=64,
        ),
        fetcher=_fetcher(tmp),
    )

    def run() -> None:
        collector.collect(max_items=5)

    elapsed, peak = _measure(run)
    _assert_page_tree_budget(elapsed, peak)


def test_extract_pdf_text_does_not_inflate_content_stream(workspace):
    tmp, open_site = workspace
    pdf = _stream_bomb_pdf(_STREAM_OPERATORS)
    site = open_site(pdf)
    collector = CatalogCollector(
        CatalogSource(
            name="hostile-catalog",
            brand="Hostile",
            kind="pdf",
            url=site.url,
            extract_text=True,
        ),
        fetcher=_fetcher(tmp),
    )

    def run() -> None:
        collector._extract_pdf_text(site.url, max_pages=1, max_chars=64)

    elapsed, peak = _measure(run)
    _assert_stream_budget(elapsed, peak)


def test_fetch_pdf_text_does_not_inflate_content_stream(workspace):
    tmp, open_site = workspace
    pdf = _stream_bomb_pdf(_STREAM_OPERATORS)
    site = open_site(pdf)
    collector = CatalogCollector(
        CatalogSource(
            name="hostile-catalog",
            brand="Hostile",
            kind="pdf",
            url=site.url,
            extract_text=True,
        ),
        fetcher=_fetcher(tmp),
    )

    def run() -> None:
        collector.fetch_pdf_text(url=site.url, max_pages=1, max_chars=64)

    elapsed, peak = _measure(run)
    _assert_stream_budget(elapsed, peak)


def _registry(tmp: Path, monkeypatch: pytest.MonkeyPatch, fetcher: AdaptivePoliteFetcher) -> ToolRegistry:
    monkeypatch.setattr(
        AdaptivePoliteFetcher,
        "shared",
        classmethod(lambda cls, policy=None, data_dir=None: fetcher),
    )
    config = AppConfig(
        industry=IndustryConfig(name="Test", keywords=["catalog"]),
        sources=SourcesConfig(),
        agents=AgentsConfig(
            data_dir=str(tmp),
            security=SecurityConfig(block_private_networks=False),
            crawl=CrawlConfig(
                enabled=True,
                min_delay_seconds=0,
                max_delay_seconds=0,
                jitter_seconds=0,
                max_retries=0,
                respect_robots_txt=False,
                adaptive=False,
                block_private_networks=False,
                resolve_dns_for_ssrf=False,
                max_response_bytes=_RESPONSE_CAP,
            ),
        ),
    )
    return ToolRegistry(config, MarketMemory(tmp))


def test_tool_fetch_pdf_text_does_not_materialize_full_page_tree(workspace, monkeypatch):
    tmp, open_site = workspace
    pdf = _page_tree_pdf(_PAGE_TREE_COUNT)
    site = open_site(pdf)
    tools = _registry(tmp, monkeypatch, _fetcher(tmp))

    def run() -> None:
        tools.call(
            "fetch_pdf_text",
            {"url": site.url, "max_pages": 1, "max_chars": 64},
        )

    elapsed, peak = _measure(run)
    _assert_page_tree_budget(elapsed, peak)


def test_extract_product_tech_schema_does_not_materialize_full_page_tree(workspace, monkeypatch):
    tmp, open_site = workspace
    pdf = _page_tree_pdf(_PAGE_TREE_COUNT)
    site = open_site(pdf)
    tools = _registry(tmp, monkeypatch, _fetcher(tmp))

    def run() -> None:
        tools.call(
            "extract_product_tech_schema",
            {"url": site.url, "max_pages": 1, "register": False},
        )

    elapsed, peak = _measure(run)
    _assert_page_tree_budget(elapsed, peak)
