"""Hardened streaming XML parser (core/adapters/knowledge/safe_xml.py)."""

from __future__ import annotations

import pytest

from tower.core.adapters.knowledge.safe_xml import StreamingXml, UnsafeXml, parse_document, text_of

FEED = b"""<?xml version="1.0"?>
<rss xmlns:dc="http://purl.org/dc/elements/1.1/"><channel><title>T</title>
<item><title>One</title><dc:creator>A</dc:creator><description>x <b>y</b> z</description></item>
<item><title>Two</title></item>
</channel></rss>"""


def test_selected_elements_are_yielded_with_local_names() -> None:
    items = list(parse_document(FEED, select={"item"}))
    assert [i.findtext("title") for i in items] == ["One", "Two"]
    assert items[0].findtext("creator") == "A"
    assert text_of(items[0].find("description")) == "x y z"


def test_streaming_by_small_chunks_gives_the_same_result() -> None:
    parser = StreamingXml(select={"item"})
    out = []
    for i in range(0, len(FEED), 7):
        out.extend(parser.feed(FEED[i : i + 7]))
    out.extend(parser.close())
    assert [i.findtext("title") for i in out] == ["One", "Two"]


@pytest.mark.parametrize(
    "doc",
    [
        b'<!DOCTYPE r [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]><r>&b;</r>',  # laughs
        b'<!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]><r>&x;</r>',  # XXE
        b'<!DOCTYPE r SYSTEM "http://example.org/r.dtd"><r/>',  # external DTD
        b'<!DOCTYPE r [<!ENTITY % p SYSTEM "http://example.org/p">%p;]><r/>',  # parameter
        b"<r><unclosed></r>",
    ],
)
def test_unsafe_or_malformed_documents_are_rejected(doc: bytes) -> None:
    with pytest.raises(UnsafeXml):
        list(parse_document(doc, select={"r"}))


def test_size_and_depth_limits() -> None:
    with pytest.raises(UnsafeXml, match="too large"):
        list(parse_document(FEED, select={"item"}, max_bytes=50))
    deep = b"<a>" * 300 + b"</a>" * 300
    with pytest.raises(UnsafeXml, match="too deep"):
        list(parse_document(deep, select={"a"}))
