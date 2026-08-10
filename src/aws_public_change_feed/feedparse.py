"""Strict RSS and Atom parsing.

Chapter 04 requires XML parsed with external entities, DTD processing, and
network resolution disabled, and requires excessive items or item characters to
be rejected.

The parser refuses any DOCTYPE declaration outright. That removes DTDs,
internal and external entity declarations, and entity-expansion attacks as a
class, rather than bounding each one. RSS and Atom have no legitimate need for
a DOCTYPE, so nothing valid is lost.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from xml.etree import ElementTree
from xml.parsers import expat

__all__ = [
    "MAX_ITEMS",
    "MAX_ITEM_CHARACTERS",
    "ParsedItem",
    "FeedParseRejected",
    "parse_feed",
]

MAX_ITEMS = 500
MAX_ITEM_CHARACTERS = 20_000

ATOM = "{http://www.w3.org/2005/Atom}"


class FeedParseRejected(Exception):
    """A feed body failed parser policy."""

    def __init__(self, reason_class: str, detail: str) -> None:
        super().__init__(f"{reason_class}: {detail}")
        self.reason_class = reason_class
        self.detail = detail


@dataclass(frozen=True, slots=True)
class ParsedItem:
    """One feed entry, before normalization."""

    url: str
    title: str
    summary: str = ""
    published_raw: str | None = None


def _qualified(name: str) -> str:
    """Convert expat's ``uri}local`` into ElementTree's ``{uri}local``."""

    return f"{{{name}" if "}" in name else name


def _parse_strict(body: bytes) -> ElementTree.Element:
    """Parse with expat directly so the safety handlers can be registered.

    ``ElementTree.XMLParser`` is the C accelerator and does not expose its
    underlying expat parser, so the DOCTYPE and entity handlers cannot be
    attached to it. Driving expat and feeding a TreeBuilder keeps the same
    resulting tree while making the refusals provable.
    """

    parser = expat.ParserCreate(namespace_separator="}")
    builder = ElementTree.TreeBuilder()

    def reject_doctype(name: str, system_id: Any, public_id: Any, has_internal_subset: Any) -> None:
        raise FeedParseRejected("parser", f"DOCTYPE declarations are not accepted: {name}")

    def reject_external_entity(context: Any, base: Any, system_id: Any, public_id: Any) -> int:
        raise FeedParseRejected("parser", "external entity references are not accepted")

    def reject_entity_declaration(*args: Any, **kwargs: Any) -> None:
        raise FeedParseRejected("parser", "entity declarations are not accepted")

    parser.StartDoctypeDeclHandler = reject_doctype
    parser.ExternalEntityRefHandler = reject_external_entity
    parser.EntityDeclHandler = reject_entity_declaration
    parser.StartElementHandler = lambda name, attrs: builder.start(_qualified(name), attrs)
    parser.EndElementHandler = lambda name: builder.end(_qualified(name))
    parser.CharacterDataHandler = builder.data
    parser.buffer_text = True

    try:
        parser.Parse(body, True)
    except expat.ExpatError as error:
        raise FeedParseRejected("parser", f"feed body is not well-formed XML: {error}") from error

    return builder.close()


def _text(node: ElementTree.Element | None) -> str:
    if node is None or node.text is None:
        return ""
    return node.text


def _atom_link(entry: ElementTree.Element) -> str:
    fallback = ""
    for link in entry.iterfind(f"{ATOM}link"):
        relation = link.get("rel", "alternate")
        href = link.get("href", "")
        if not href:
            continue
        if relation == "alternate":
            return href
        fallback = fallback or href
    return fallback


def _rss_items(root: ElementTree.Element) -> Iterator[ParsedItem]:
    for node in root.iter("item"):
        yield ParsedItem(
            url=_text(node.find("link")).strip(),
            title=_text(node.find("title")),
            summary=_text(node.find("description")),
            published_raw=(_text(node.find("pubDate")).strip() or None),
        )


def _atom_entries(root: ElementTree.Element) -> Iterator[ParsedItem]:
    for node in root.iter(f"{ATOM}entry"):
        summary = _text(node.find(f"{ATOM}summary")) or _text(node.find(f"{ATOM}content"))
        published = _text(node.find(f"{ATOM}published")).strip() or _text(node.find(f"{ATOM}updated")).strip()
        yield ParsedItem(
            url=_atom_link(node).strip(),
            title=_text(node.find(f"{ATOM}title")),
            summary=summary,
            published_raw=published or None,
        )


def parse_feed(
    body: bytes,
    *,
    max_items: int = MAX_ITEMS,
    max_item_characters: int = MAX_ITEM_CHARACTERS,
) -> tuple[ParsedItem, ...]:
    """Parse an RSS or Atom body into items, or raise ``FeedParseRejected``.

    Items missing a URL or a title are dropped rather than failing the feed:
    chapter 04 accepts items with a resolvable canonical URL and a nonempty
    title, and one malformed entry should not discard a whole response.
    """

    for name, value in (("max_items", max_items), ("max_item_characters", max_item_characters)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} must be a positive integer")
    if not body.strip():
        raise FeedParseRejected("parser", "feed body is empty")

    root = _parse_strict(body)

    if root.tag == f"{ATOM}feed":
        produced = _atom_entries(root)
    elif root.tag == "rss" or root.find("channel") is not None:
        produced = _rss_items(root)
    else:
        raise FeedParseRejected("parser", f"unsupported feed root element: {root.tag}")

    items: list[ParsedItem] = []
    for item_number, item in enumerate(produced, start=1):
        if item_number > max_items:
            raise FeedParseRejected("parser", f"feed contains more than {max_items} items")
        if not item.url or not item.title.strip():
            continue
        if len(item.title) + len(item.summary) > max_item_characters:
            raise FeedParseRejected("parser", f"feed item exceeds {max_item_characters} characters")
        items.append(item)

    return tuple(items)
