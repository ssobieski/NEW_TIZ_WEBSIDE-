"""Parse untrusted RSS/Atom/XML feeds without DTDs or entity expansion.

Every feed parser in the package goes through this module. A document is
decoded once, checked for DOCTYPE / ENTITY declarations, and only that exact
text is handed to ElementTree or feedparser, so the check and the parser can
never disagree about which characters they saw.
"""

from __future__ import annotations

import codecs
import io
import re
import urllib.parse
from typing import Any
from xml.etree import ElementTree as ET
from xml.parsers import expat

import feedparser
from feedparser import http as feedparser_http


class UnsafeXMLError(ValueError):
    """Feed declares a DTD or entities and was refused before parsing."""


_ENTITY_DECL = re.compile(r"<!\s*ENTITY", re.IGNORECASE)
_DOCTYPE_DECL = re.compile(r"<!\s*DOCTYPE", re.IGNORECASE)
_FIRST_ELEMENT = re.compile(r"<[^!?/\s]")
_HEADER_CHARSET = re.compile(r"charset\s*=\s*[\"']?([A-Za-z0-9._:-]+)", re.IGNORECASE)
_XML_DECL_ENCODING = re.compile(
    rb"^\s*<\?xml[^>]*?encoding\s*=\s*[\"']([A-Za-z0-9._:-]+)[\"']"
)
# UTF-32 LE must be tested before UTF-16 LE: its BOM starts with FF FE.
_BOMS = (
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)
# BOM-less documents, recognised from their leading "<?" (XML 1.0 appendix F).
_SNIFFS = (
    (b"\x00\x00\x00\x3c", "utf-32-be"),
    (b"\x3c\x00\x00\x00", "utf-32-le"),
    (b"\x00\x3c\x00\x3f", "utf-16-be"),
    (b"\x3c\x00\x3f\x00", "utf-16-le"),
    (b"\x4c\x6f\xa7\x94", "cp037"),
)
_FEEDPARSER_URL_SCHEMES = ("http", "https", "ftp", "file", "feed")


class _PrologEnd(Exception):
    pass


def refuse_dtd(text: str) -> None:
    """Raise UnsafeXMLError if ``text`` declares a DOCTYPE or any entity.

    Without a DTD the only references a parser can resolve are the predefined
    and character entities, so the parsed text can never outgrow the input.
    """
    if _ENTITY_DECL.search(text):
        raise UnsafeXMLError("feed declares XML entities; refused")

    def _doctype(*_args: Any) -> None:
        raise UnsafeXMLError("feed declares a DOCTYPE; refused")

    def _root(*_args: Any) -> None:
        raise _PrologEnd

    parser = expat.ParserCreate()
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    parser.StartDoctypeDeclHandler = _doctype
    parser.EntityDeclHandler = _doctype
    parser.UnparsedEntityDeclHandler = _doctype
    parser.StartElementHandler = _root
    try:
        parser.Parse(text, True)
    except _PrologEnd:
        return
    except expat.ExpatError:
        # Not well-formed before the root element (feedparser still reads
        # such feeds loosely), so look for a DOCTYPE ahead of the first tag.
        first = _FIRST_ELEMENT.search(text)
        head = text[: first.start()] if first else text
        if _DOCTYPE_DECL.search(head):
            raise UnsafeXMLError("feed declares a DOCTYPE; refused") from None


def decode_xml_bytes(data: bytes, content_type: str | None = None) -> str:
    candidates: list[str] = []
    for prefix, encoding in (*_BOMS, *_SNIFFS):
        if data.startswith(prefix):
            candidates.append(encoding)
            break
    header = _HEADER_CHARSET.search(content_type or "")
    if header:
        candidates.append(header.group(1))
    declared = _XML_DECL_ENCODING.match(data[:1024])
    if declared:
        candidates.append(declared.group(1).decode("ascii"))
    candidates.extend(["utf-8", "windows-1252"])
    for encoding in candidates:
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", errors="replace")


def parse_xml(document: str | bytes) -> ET.Element:
    """ElementTree parse that refuses DTDs. Raises UnsafeXMLError or ET.ParseError."""
    text = decode_xml_bytes(document) if isinstance(document, bytes) else document
    refuse_dtd(text)
    return ET.fromstring(text)


def _read_resource(location: str, result: feedparser.FeedParserDict) -> bytes:
    if urllib.parse.urlparse(location)[0] in _FEEDPARSER_URL_SCHEMES:
        return feedparser_http.get(
            location, None, None, feedparser.USER_AGENT, None, None, None, result
        )
    try:
        with open(location, "rb") as handle:
            return handle.read()
    except (OSError, UnicodeEncodeError, TypeError, ValueError):
        return location.encode("utf-8")


def parse_feed(location: str) -> feedparser.FeedParserDict:
    """feedparser.parse(location) that refuses feeds declaring a DTD or entities."""
    result = feedparser.FeedParserDict(
        bozo=False,
        entries=[],
        feed=feedparser.FeedParserDict(),
        headers={},
    )
    try:
        data = _read_resource(location, result)
    except Exception as exc:  # noqa: BLE001
        result.update({"bozo": True, "bozo_exception": exc})
        return result
    if not data:
        return result

    headers = dict(result.get("headers") or {})
    text = decode_xml_bytes(data, headers.get("content-type"))
    try:
        refuse_dtd(text)
    except UnsafeXMLError as exc:
        result.update({"bozo": True, "bozo_exception": exc})
        return result

    # feedparser must decode exactly the text checked above, not re-sniff it.
    mime = (headers.get("content-type") or "").split(";", 1)[0].strip() or "application/xml"
    headers["content-type"] = f"{mime}; charset=utf-8"
    href = str(result.get("href") or "")
    if href:
        headers["content-location"] = urllib.parse.urljoin(
            href, str(headers.get("content-location") or "")
        )
    parsed = feedparser.parse(io.BytesIO(text.encode("utf-8")), response_headers=headers)
    for key in ("href", "status", "etag", "modified", "modified_parsed"):
        if key in result:
            parsed[key] = result[key]
    return parsed
