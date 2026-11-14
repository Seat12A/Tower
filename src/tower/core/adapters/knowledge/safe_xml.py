"""Hardened, streaming XML parsing for external documents (LEGI archives, RSS/Atom feeds).

Built directly on ``pyexpat`` (standard library) so that every protection is explicit:

- any ``<!ENTITY …>`` declaration (internal or external, general or parameter) rejects the
  document: no entity expansion at all, hence no "billion laughs" whatever the expat
  version (expat ≥ 2.4.0 also limits amplification; Python 3.13 ships 2.6+ here);
- external entities and external DTD subsets are never fetched (parameter entity
  parsing disabled, external entity reference handler refuses);
- the document size is capped (bytes fed) and so is the depth of the element stack;
- elements are yielded as small ``ElementTree`` subtrees as soon as they close, then
  dropped: memory stays bounded by the largest selected element, never by the document.

Usage::

    parser = StreamingXml(select={"item", "entry"}, max_bytes=5_000_000)
    for chunk in chunks:
        for element in parser.feed(chunk):
            ...
    for element in parser.close():
        ...

Elements are matched by local name (namespace stripped); ``element.tag`` is the local
name, attributes keep their local names too.
"""

from __future__ import annotations

import pyexpat
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Iterator
from typing import Any

__all__ = ["DEFAULT_MAX_BYTES", "StreamingXml", "UnsafeXml", "parse_document", "text_of"]

DEFAULT_MAX_BYTES = 20_000_000
MAX_DEPTH = 256


class UnsafeXml(ValueError):
    """The document was rejected (entity declaration, external entity, size or depth
    limit, or malformed XML). The caller maps it to ``TWR-ATL-010``."""


def _local(name: str) -> str:
    # expat with namespace_separator=" " gives "uri local"; without, "prefix:local".
    name = name.rsplit(" ", 1)[-1]
    return name.rsplit(":", 1)[-1]


class StreamingXml:
    """Incremental parser yielding the selected elements (by local name) once closed."""

    def __init__(
        self,
        *,
        select: Iterable[str],
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_depth: int = MAX_DEPTH,
    ) -> None:
        self._select = frozenset(select)
        self._max_bytes = max_bytes
        self._max_depth = max_depth
        self._fed = 0
        self._ready: list[ET.Element] = []
        self._stack: list[ET.Element | None] = []
        """Open elements; ``None`` above the first selected ancestor (not kept)."""
        self._text: list[str] = []
        parser = pyexpat.ParserCreate(namespace_separator=" ")
        parser.SetParamEntityParsing(pyexpat.XML_PARAM_ENTITY_PARSING_NEVER)
        parser.buffer_text = True
        parser.StartElementHandler = self._start
        parser.EndElementHandler = self._end
        parser.CharacterDataHandler = self._chars
        parser.EntityDeclHandler = self._entity_decl
        parser.ExternalEntityRefHandler = self._external_entity
        parser.StartDoctypeDeclHandler = self._doctype
        self._parser: Any = parser

    # --- protections -----------------------------------------------------------------

    @staticmethod
    def _entity_decl(*_args: object) -> None:
        raise UnsafeXml("entity declarations are not accepted")

    @staticmethod
    def _external_entity(*_args: object) -> int:
        raise UnsafeXml("external entities are not accepted")

    @staticmethod
    def _doctype(_name: str, sysid: str | None, pubid: str | None, _internal: bool) -> None:
        if sysid or pubid:
            raise UnsafeXml("external DTD subsets are not accepted")

    # --- tree building ---------------------------------------------------------------

    def _start(self, name: str, attrs: dict[str, str]) -> None:
        if len(self._stack) >= self._max_depth:
            raise UnsafeXml("document too deep")
        self._flush_text()
        tag = _local(name)
        parent = self._stack[-1] if self._stack else None
        element: ET.Element | None = None
        if parent is not None:
            element = ET.SubElement(parent, tag, {_local(k): v for k, v in attrs.items()})
        elif tag in self._select:
            element = ET.Element(tag, {_local(k): v for k, v in attrs.items()})
        self._stack.append(element)

    def _end(self, name: str) -> None:
        self._flush_text()
        element = self._stack.pop()
        if element is None:
            return
        parent = self._stack[-1] if self._stack else None
        if parent is None:  # outermost selected element: hand it over, keep nothing
            self._ready.append(element)

    def _chars(self, data: str) -> None:
        if self._stack and self._stack[-1] is not None:
            self._text.append(data)

    def _flush_text(self) -> None:
        if not self._text:
            return
        text = "".join(self._text)
        self._text.clear()
        current = self._stack[-1] if self._stack else None
        if current is None:
            return
        if len(current):
            last = current[-1]
            last.tail = (last.tail or "") + text
        else:
            current.text = (current.text or "") + text

    # --- API -------------------------------------------------------------------------

    def feed(self, chunk: bytes) -> list[ET.Element]:
        self._fed += len(chunk)
        if self._fed > self._max_bytes:
            raise UnsafeXml("document too large")
        try:
            self._parser.Parse(chunk, False)
        except pyexpat.ExpatError as exc:
            raise UnsafeXml(f"malformed XML: {exc}") from exc
        ready, self._ready = self._ready, []
        return ready

    def close(self) -> list[ET.Element]:
        try:
            self._parser.Parse(b"", True)
        except pyexpat.ExpatError as exc:
            raise UnsafeXml(f"malformed XML: {exc}") from exc
        ready, self._ready = self._ready, []
        return ready


def parse_document(
    data: bytes, *, select: Iterable[str], max_bytes: int = DEFAULT_MAX_BYTES
) -> Iterator[ET.Element]:
    """All selected elements of a small in-memory document (same protections)."""
    parser = StreamingXml(select=select, max_bytes=max_bytes)
    yield from parser.feed(data)
    yield from parser.close()


def text_of(element: ET.Element | None) -> str:
    """All the text of an element and its descendants, whitespace collapsed."""
    if element is None:
        return ""
    return " ".join("".join(element.itertext()).split())
