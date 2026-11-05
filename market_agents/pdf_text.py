"""Ekstrakcja tekstu z niezaufanych PDF z budżetem pracy parsera.

``PdfReader.pages`` w pypdf spłaszcza całe drzewo stron, a ``extract_text``
dekompresuje i tokenizuje cały content stream (oraz każde wywołanie formy
XObject i każdy font zasobów), zanim wywołujący przytnie wynik do
``max_pages`` / ``max_chars``. Tutaj praca parsera jest ograniczona z góry:

- drzewo stron jest przechodzone leniwie, tylko do ``max_pages`` liści,
- limity pypdf (dekompresja, tokeny, tablice, słowniki, formy) obowiązują
  w kontekście wywołania,
- strumienie treści, fonty i wywołania form są liczone do jednego budżetu
  wyprowadzonego z ``max_chars`` *przed* ich parsowaniem przez pypdf.
"""

from __future__ import annotations

import io
import re
from typing import Any, Callable, Iterator

import pypdf
from pypdf import PageObject, PdfReader
from pypdf.errors import LimitReachedError
from pypdf.generic import ArrayObject, DictionaryObject, IndirectObject, NameObject, StreamObject

MAX_PAGES_LIMIT = 64

_WORK_PER_CHAR = 64
_MIN_WORK = 64 * 1024
_MAX_WORK = 1024 * 1024
# Koszt extract_text rośnie szybciej niż liniowo z liczbą operatorów strumienia.
_MAX_STREAM_TOKENS = 100_000
_MIN_DECODED_STREAM = 2 * 1024 * 1024
_MAX_DECODED_STREAM = 8 * 1024 * 1024

_OBJECT_COST = 64
_FONT_COST = 1024

_MIN_TREE_ENTRIES = 256
_TREE_ENTRIES_PER_PAGE = 32
_MAX_TREE_DEPTH = 32
_MAX_FORM_DEPTH = 16
_MAX_RESOURCE_ENTRIES = 512
_MAX_FORM_INVOCATIONS_PER_PAGE = 64

_INHERITABLE = ("/Resources", "/MediaBox", "/CropBox", "/Rotate")


class PdfLimitExceeded(Exception):
    """Dokument wymaga więcej pracy parsera, niż pozwala budżet wywołania."""


class _Budget:
    def __init__(self, limit: int) -> None:
        self.remaining = limit
        self.exhausted = False

    def check(self) -> None:
        # pypdf łapie każdy Exception z wnętrza formy XObject i kontynuuje,
        # więc po wyczerpaniu budżetu każde kolejne wywołanie musi rzucać.
        if self.exhausted:
            raise PdfLimitExceeded("budżet parsera PDF wyczerpany")

    def charge(self, amount: int) -> None:
        self.check()
        self.remaining -= amount
        if self.remaining < 0:
            self.exhausted = True
            raise PdfLimitExceeded("budżet parsera PDF wyczerpany")


def work_budget(max_chars: int) -> int:
    return min(_MAX_WORK, max(_MIN_WORK, max_chars * _WORK_PER_CHAR))


def _parser_limits(work: int, max_pages: int) -> dict[str, Any]:
    decoded = min(_MAX_DECODED_STREAM, max(_MIN_DECODED_STREAM, 2 * work))
    tree_entries = _tree_entry_limit(max_pages)
    return {
        "zlib_maximum_output_length": decoded,
        "lzw_maximum_output_length": decoded,
        "run_length_maximum_output_length": decoded,
        "brotli_maximum_output_length": decoded,
        "jbig2_maximum_output_length": decoded,
        "array_based_stream_maximum_output_length": decoded,
        "image_maximum_buffer_size": decoded,
        "zlib_maximum_recovery_input_length": 512 * 1024,
        "stream_filters_maximum_length": 4,
        "stream_decoding_work_maximum_length": 4 * decoded,
        "stream_parsing_work_maximum_count": min(work, _MAX_STREAM_TOKENS),
        "array_maximum_element_count": 50_000,
        "dictionary_maximum_element_count": 10_000,
        "page_tree_maximum_entries": tree_entries,
        "page_tree_maximum_depth": _MAX_TREE_DEPTH,
        "page_tree_maximum_page_entries": tree_entries * 64,
        "xform_maximum_invocations_per_extraction": _MAX_FORM_INVOCATIONS_PER_PAGE,
        "disable_legacy_handling": True,
    }


def _tree_entry_limit(max_pages: int) -> int:
    return max(_MIN_TREE_ENTRIES, max_pages * _TREE_ENTRIES_PER_PAGE)


def _limited_configuration(work: int, max_pages: int) -> Any:
    get_configuration = getattr(pypdf, "get_configuration", None)
    if get_configuration is None or getattr(pypdf, "apply_configuration", None) is None:
        raise RuntimeError("pypdf>=6.21.0 wymagany do ograniczonej ekstrakcji PDF")
    try:
        return get_configuration().with_overwrites(**_parser_limits(work, max_pages))
    except TypeError as exc:
        raise RuntimeError("pypdf>=6.21.0 wymagany do ograniczonej ekstrakcji PDF") from exc


def _resolve(obj: Any) -> Any:
    return obj.get_object() if hasattr(obj, "get_object") else obj


def _check_entries(container: Any) -> None:
    if len(container) > _MAX_RESOURCE_ENTRIES:
        raise PdfLimitExceeded("zbyt wiele wpisów w zasobach PDF")


def _decoded_size(stream: StreamObject, budget: _Budget) -> int:
    budget.charge(_OBJECT_COST)
    try:
        size = len(stream.get_data() or b"")
    except LimitReachedError as exc:
        budget.exhausted = True
        raise PdfLimitExceeded(str(exc)) from exc
    budget.charge(size)
    return size + _OBJECT_COST


def _array_len(obj: Any) -> int:
    obj = _resolve(obj)
    return len(obj) if isinstance(obj, ArrayObject) else 0


def _font_cost(font: Any, budget: _Budget) -> int:
    budget.charge(_FONT_COST)
    cost = _FONT_COST
    if not isinstance(font, DictionaryObject):
        return cost
    to_unicode = _resolve(font.get("/ToUnicode"))
    if isinstance(to_unicode, StreamObject):
        cost += _decoded_size(to_unicode, budget)
    elif font.get("/Subtype") == "/Type1":
        # Bez /ToUnicode pypdf czyta kodowanie z osadzonego programu fontu Type1.
        descriptor = _resolve(font.get("/FontDescriptor"))
        if isinstance(descriptor, DictionaryObject):
            for key in ("/FontFile", "/FontFile3"):
                program = _resolve(descriptor.get(key))
                if isinstance(program, StreamObject):
                    cost += _decoded_size(program, budget)
    arrays = _array_len(font.get("/Widths"))
    encoding = _resolve(font.get("/Encoding"))
    if isinstance(encoding, DictionaryObject):
        arrays += _array_len(encoding.get("/Differences"))
    descendants = _resolve(font.get("/DescendantFonts"))
    if isinstance(descendants, ArrayObject):
        _check_entries(descendants)
        for descendant in descendants:
            descendant = _resolve(descendant)
            if isinstance(descendant, DictionaryObject):
                arrays += _array_len(descendant.get("/W"))
    budget.charge(arrays)
    return cost + arrays


def _scan_resources(
    resources: Any,
    budget: _Budget,
    forms: dict[str, int],
    seen: dict[int, int],
    depth: int,
) -> int:
    """Nalicza fonty zasobów i rejestruje koszt każdej formy XObject.

    Zwraca koszt, który pypdf ponosi przy każdym wejściu w te zasoby.
    """
    resources = _resolve(resources)
    if not isinstance(resources, DictionaryObject):
        return 0
    cost = 0
    fonts = _resolve(resources.get("/Font"))
    if isinstance(fonts, DictionaryObject):
        _check_entries(fonts)
        for font in fonts.values():
            cost += _font_cost(_resolve(font), budget)
    xobjects = _resolve(resources.get("/XObject"))
    if isinstance(xobjects, DictionaryObject):
        _check_entries(xobjects)
        for name, ref in xobjects.items():
            budget.charge(_OBJECT_COST)
            xobj = _resolve(ref)
            if not isinstance(xobj, StreamObject) or xobj.get("/Subtype") != "/Form":
                continue
            form_cost = seen.get(id(xobj))
            if form_cost is None:
                if depth >= _MAX_FORM_DEPTH:
                    raise PdfLimitExceeded("zbyt głębokie zagnieżdżenie form XObject")
                seen[id(xobj)] = 0
                form_cost = _decoded_size(xobj, budget) + _scan_resources(
                    xobj.get("/Resources"), budget, forms, seen, depth + 1
                )
                seen[id(xobj)] = form_cost
            forms[str(name)] = max(forms.get(str(name), 0), form_cost + _OBJECT_COST)
    return cost


def _charge_contents(contents: Any, budget: _Budget) -> None:
    contents = _resolve(contents)
    streams = list(contents) if isinstance(contents, ArrayObject) else [contents]
    if len(streams) > _MAX_RESOURCE_ENTRIES:
        raise PdfLimitExceeded("zbyt wiele strumieni treści strony")
    for item in streams:
        stream = _resolve(item)
        if isinstance(stream, StreamObject):
            _decoded_size(stream, budget)


def _form_visitor(budget: _Budget, forms: dict[str, int]) -> Callable[..., None]:
    def before(operator: bytes, operands: Any, *_: Any) -> None:
        if operator == b"Do" and operands:
            budget.charge(forms.get(str(operands[0]), 0))
        else:
            budget.check()

    return before


def _kids(node: DictionaryObject) -> list[Any]:
    kids = _resolve(node.get("/Kids"))
    return kids if isinstance(kids, ArrayObject) else []


def _inherit(parent: dict[NameObject, Any], node: DictionaryObject) -> dict[NameObject, Any]:
    inherited = dict(parent)
    for attr in _INHERITABLE:
        if attr in node:
            inherited[NameObject(attr)] = node[attr]
    return inherited


def _iter_pages(reader: PdfReader, max_pages: int, budget: _Budget) -> Iterator[PageObject]:
    """Pierwsze ``max_pages`` stron bez spłaszczania całego drzewa."""
    root = _resolve(reader.trailer.get("/Root"))
    if not isinstance(root, DictionaryObject):
        return
    top = _resolve(root.get("/Pages"))
    if not isinstance(top, DictionaryObject):
        return
    entries_left = _tree_entry_limit(max_pages)
    stack = [(iter(_kids(top)), _inherit({}, top), id(top))]
    ancestors = {id(top)}
    found = 0
    while stack:
        kids, inherited, node_id = stack[-1]
        ref = next(kids, None)
        if ref is None:
            stack.pop()
            ancestors.discard(node_id)
            continue
        entries_left -= 1
        if entries_left < 0:
            raise PdfLimitExceeded("drzewo stron PDF przekracza limit")
        budget.charge(_OBJECT_COST)
        node = _resolve(ref)
        if not isinstance(node, DictionaryObject) or not node or id(node) in ancestors:
            continue
        node_type = node.get("/Type") or ("/Pages" if "/Kids" in node else "/Page")
        if node_type == "/Pages":
            if len(stack) >= _MAX_TREE_DEPTH:
                raise PdfLimitExceeded("drzewo stron PDF zbyt głębokie")
            stack.append((iter(_kids(node)), _inherit(inherited, node), id(node)))
            ancestors.add(id(node))
            continue
        if node_type != "/Page":
            continue
        page = PageObject(reader, ref if isinstance(ref, IndirectObject) else None)
        page.update(node)
        for attr, value in inherited.items():
            if attr not in page:
                page[attr] = value
        yield page
        found += 1
        if found >= max_pages:
            return


def extract_pdf_text(data: bytes, max_pages: int = 8, max_chars: int = 12000) -> str:
    """Tekst z pierwszych stron PDF; praca parsera ograniczona przez ``max_chars``.

    Rzuca ``PdfLimitExceeded``, gdy limit zadziała, zanim powstał jakikolwiek tekst.
    """
    max_pages = max(1, min(int(max_pages), MAX_PAGES_LIMIT))
    max_chars = max(1, int(max_chars))
    work = work_budget(max_chars)
    configuration = _limited_configuration(work, max_pages)
    budget = _Budget(work)
    parts: list[str] = []
    total = 0
    with pypdf.apply_configuration(configuration):
        try:
            reader = PdfReader(io.BytesIO(data))
            for page in _iter_pages(reader, max_pages, budget):
                forms: dict[str, int] = {}
                try:
                    _scan_resources(page.get("/Resources"), budget, forms, {}, 0)
                    _charge_contents(page.get("/Contents"), budget)
                except LimitReachedError as exc:
                    raise PdfLimitExceeded(str(exc)) from exc
                except PdfLimitExceeded:
                    raise
                except Exception:  # noqa: BLE001
                    continue
                try:
                    text = page.extract_text(visitor_operand_before=_form_visitor(budget, forms))
                except PdfLimitExceeded:
                    raise
                except Exception:  # noqa: BLE001
                    # Strumienie strony są już naliczone, więc limit pojedynczego
                    # strumienia (LimitReachedError) pomija tylko tę stronę.
                    continue
                parts.append(text or "")
                total += len(text or "")
                if total >= max_chars:
                    break
        except (PdfLimitExceeded, LimitReachedError) as exc:
            if total == 0:
                raise PdfLimitExceeded(str(exc)) from exc
    text = re.sub(r"\s+", " ", "\n".join(parts)).strip()
    return text[:max_chars]
