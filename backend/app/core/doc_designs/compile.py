"""A designer's template, read once into pieces that fill quickly.

A template stays the designer's HTML with the designer's `{{token}}`s. The
D4 edits (CHANGES.md) only add attributes, which this reads:

- `data-optional="a b"` -- the element prints only when every one of these
  values is there; otherwise it is left out (a label and its value, a
  separator, a logo). `data-optional-any="a b"`: when any one is there.
  `data-optional-hide="a b"`: as data-optional, but an empty one keeps its
  place, invisible (`visibility: hidden`), so a grid or a row keeps its shape.
- `data-option="slot"` -- the element's content is wording the design offers
  in other words (a certificate's title, subtitle, statement): the template
  keeps the designer's own as the default, and an option replaces it.
- `data-lanyard` -- the lanyard slot's pill, hidden (in place) when the
  option is off.
- `data-slot="signature"` -- a signature line; a signature image goes inside.
- `data-slot="photo"` -- the photo <img>; without a photo, an initials tile.

Positions come from the standard library's HTML parser, and every piece is a
slice of the original text: nothing is re-serialised, so what is printed is
byte for byte the designer's markup around the values.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Union

TOKEN = re.compile(r"\{\{(\w+)\}\}")
VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"})
MARKERS = ("data-optional", "data-optional-any", "data-optional-hide", "data-option", "data-lanyard", "data-slot")


@dataclass(frozen=True)
class Tok:
    name: str


@dataclass
class Block:
    kind: str  # optional | option | lanyard | signature | photo
    open: list  # nodes: the start tag (it may hold tokens)
    hidden_open: list  # the start tag with visibility: hidden
    close: str
    children: list = field(default_factory=list)
    need_all: tuple = ()
    need_any: tuple = ()
    hide: bool = False
    slot: str = ""


Node = Union[str, Tok, Block]


def text_nodes(text: str) -> list:
    out: list = []
    pos = 0
    for m in TOKEN.finditer(text):
        if m.start() > pos:
            out.append(text[pos:m.start()])
        out.append(Tok(m.group(1)))
        pos = m.end()
    if pos < len(text):
        out.append(text[pos:])
    return out


def hidden(open_tag: str) -> str:
    """The start tag with `visibility: hidden` first in its style."""
    if re.search(r'\sstyle="', open_tag):
        return re.sub(r'(\sstyle=")', r"\1visibility: hidden; ", open_tag, count=1)
    return re.sub(r"\s*/?>$", lambda m: ' style="visibility: hidden"' + m.group(0), open_tag, count=1)


@dataclass
class _Elem:
    tag: str
    attrs: dict
    start: int
    open_end: int
    end: int
    close_start: int
    inner: list = field(default_factory=list)


class _Scanner(HTMLParser):
    def __init__(self, src: str):
        super().__init__(convert_charrefs=False)
        self.src = src
        self.line_starts = [0] + [m.end() for m in re.finditer("\n", src)]
        self.stack: list = []
        self.found: list[_Elem] = []

    def _offset(self) -> int:
        line, col = self.getpos()
        return self.line_starts[line - 1] + col

    def _keep(self, tag, attrs, start, open_end, end, close_start) -> None:
        if any(k in attrs for k in MARKERS):
            self.found.append(_Elem(tag, attrs, start, open_end, end, close_start))

    def handle_starttag(self, tag, attrs):
        start = self._offset()
        open_end = start + len(self.get_starttag_text())
        attrs = {k: (v or "") for k, v in attrs}
        if tag in VOID:
            self._keep(tag, attrs, start, open_end, open_end, open_end)
        else:
            self.stack.append((tag, attrs, start, open_end))

    def handle_startendtag(self, tag, attrs):
        start = self._offset()
        end = start + len(self.get_starttag_text())
        self._keep(tag, {k: (v or "") for k, v in attrs}, start, end, end, end)

    def handle_endtag(self, tag):
        close_start = self._offset()
        end = self.src.index(">", close_start) + 1
        while self.stack:
            t, attrs, start, open_end = self.stack.pop()
            if t == tag:
                self._keep(t, attrs, start, open_end, end, close_start)
                return


def _nest(elems: list[_Elem]) -> list[_Elem]:
    """Elements in document order, each inside the one that contains it."""
    top: list[_Elem] = []
    stack: list[_Elem] = []
    for e in sorted(elems, key=lambda e: (e.start, -e.end)):
        while stack and not (stack[-1].start <= e.start and e.end <= stack[-1].end):
            stack.pop()
        (stack[-1].inner if stack else top).append(e)
        stack.append(e)
    return top


def _block(src: str, e: _Elem, signature_index: list) -> Block:
    a = e.attrs
    open_tag = src[e.start:e.open_end]
    void = e.end == e.open_end
    children = [] if void else _nodes(src, e.open_end, e.close_start, e.inner, signature_index)
    close = "" if void else src[e.close_start:e.end]
    kind, slot = "optional", ""
    need_all: tuple = ()
    need_any: tuple = ()
    hide = False
    if "data-optional" in a:
        need_all = tuple(a["data-optional"].split())
    if "data-optional-hide" in a:
        need_all, hide = tuple(a["data-optional-hide"].split()), True
    if "data-optional-any" in a:
        need_any = tuple(a["data-optional-any"].split())
    if not (need_all or need_any):
        if "data-option" in a:
            kind, slot = "option", a["data-option"]
        elif "data-lanyard" in a:
            kind = "lanyard"
        elif a.get("data-slot") == "signature":
            kind, slot = "signature", str(len(signature_index))
            signature_index.append(1)
        elif a.get("data-slot") == "photo":
            kind = "photo"
        else:  # a data-slot the fill does not touch (qr, logo): plain markup
            return Block("plain", text_nodes(open_tag), text_nodes(open_tag), close, children)
    elif a.get("data-slot") == "signature":
        raise ValueError("a signature slot cannot itself be optional; wrap it")
    return Block(kind, text_nodes(open_tag), text_nodes(hidden(open_tag)), close, children,
                 need_all=need_all, need_any=need_any, hide=hide, slot=slot)


def _nodes(src: str, a: int, b: int, elems: list[_Elem], signature_index: list) -> list:
    out: list = []
    pos = a
    for e in elems:
        out += text_nodes(src[pos:e.start])
        out.append(_block(src, e, signature_index))
        pos = e.end
    out += text_nodes(src[pos:b])
    return out


def compile_fragment(html: str) -> list:
    """An HTML fragment (a template's body, an option's wording) as nodes."""
    scanner = _Scanner(html)
    scanner.feed(html)
    scanner.close()
    return _nodes(html, 0, len(html), _nest(scanner.found), [])


@dataclass
class Compiled:
    title: str
    styles: list[str]  # the head's <style> blocks, verbatim
    body: list  # nodes


_COMMENT = re.compile(r"<!--.*?-->", re.S)


def compile_template(html: str) -> Compiled:
    """A whole template: its head's styles, and its body as nodes. Comments
    are dropped (the tickets' explain the tokens with `{{token}}` in them)."""
    head = html[: html.index("<body")]
    body_start = html.index(">", html.index("<body")) + 1
    body = _COMMENT.sub("", html[body_start: html.rindex("</body>")])
    if "fonts.googleapis.com" in head:
        raise ValueError("a template must not load fonts from Google (contract D3)")
    title = re.search(r"<title>(.*?)</title>", head, re.S)
    styles = re.findall(r"<style>(.*?)</style>", head, re.S)
    return Compiled(title.group(1) if title else "", styles, compile_fragment(body.strip("\n")))


def tokens_in(nodes: list) -> set[str]:
    """Every token named anywhere in these nodes."""
    out: set[str] = set()
    for n in nodes:
        if isinstance(n, Tok):
            out.add(n.name)
        elif isinstance(n, Block):
            out |= tokens_in(n.open) | tokens_in(n.children) | set(n.need_all) | set(n.need_any)
    return out


def guarded(nodes: list, guards: frozenset = frozenset()) -> list[tuple[str, frozenset]]:
    """Each token occurrence with the tokens whose emptiness removes or hides
    the block around it (`data-optional`, `data-optional-hide`). An
    occurrence of an optional token must be guarded by itself; a
    `data-optional-any` block guards nothing on its own."""
    out = []
    for n in nodes:
        if isinstance(n, Tok):
            out.append((n.name, guards))
        elif isinstance(n, Block):
            g = guards | frozenset(n.need_all) if n.kind == "optional" else guards
            out += [(t, g) for t, _ in guarded(n.open)]
            out += guarded(n.children, g)
    return out


def blocks(nodes: list, kind: str | None = None):
    for n in nodes:
        if isinstance(n, Block):
            if kind is None or n.kind == kind:
                yield n
            yield from blocks(n.children, kind)
