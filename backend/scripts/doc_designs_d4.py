"""The D4 edits to the designer's templates, as data (doc-designs contract, D4).

    python scripts/doc_designs_d4.py --from DIR --kind certificate     # DIR: the package's templates/
    python scripts/doc_designs_d4.py --changes                          # rewrite CHANGES.md only

The templates under app/core/doc_designs/templates/ are the designer's files
with only these edits. When the designer sends a new version, run this on
the new package: every edit is found by what it holds (the element with
`{{gate}}`, its parent; an exact piece of text), each must match exactly
once, and one that no longer matches stops the run and names itself, to be
redone by hand. CHANGES.md is written from the same list, so the record and
the files never disagree.

The kinds of edit (contract D4):
 a  `data-optional` / `-any` / `-hide` on blocks to leave out when a value is
    empty (gate, row and seat for general admission; a job title and its
    separator; a logo; a second signatory; a grade);
 b  `data-option="slot"` on wording a certificate offers in other words;
 c  the QR placeholders become the real QR (`{{qr_code_src}}` for tickets);
 d  `data-fit` on values the fit script fits (fit.js);
 e  `data-lanyard` on the lanyard pill;
 f  the accent: certificates already say {{accent_color}}, cards --accent;
    nothing to edit;
 -  the Google Fonts <link> removed (fonts are ours: D3).
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable

BACKEND = Path(__file__).resolve().parents[1]
DEST = BACKEND / "app" / "core" / "doc_designs" / "templates"
CHANGES = BACKEND / "app" / "core" / "doc_designs" / "CHANGES.md"
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


class EditFailed(Exception):
    pass


# ─── Finding elements ────────────────────────────────────────────────────────


@dataclass
class El:
    tag: str
    start: int
    open_end: int
    end: int


class _Tree(HTMLParser):
    def __init__(self, src):
        super().__init__(convert_charrefs=False)
        self.src = src
        self.lines = [0] + [m.end() for m in re.finditer("\n", src)]
        self.stack, self.els = [], []

    def _at(self):
        line, col = self.getpos()
        return self.lines[line - 1] + col

    def handle_starttag(self, tag, attrs):
        s = self._at()
        e = s + len(self.get_starttag_text())
        if tag in VOID:
            self.els.append(El(tag, s, e, e))
        else:
            self.stack.append((tag, s, e))

    def handle_startendtag(self, tag, attrs):
        s = self._at()
        e = s + len(self.get_starttag_text())
        self.els.append(El(tag, s, e, e))

    def handle_endtag(self, tag):
        s = self._at()
        end = self.src.index(">", s) + 1
        while self.stack:
            t, st, oe = self.stack.pop()
            if t == tag:
                self.els.append(El(t, st, oe, end))
                return


def elements(src: str) -> list[El]:
    t = _Tree(src)
    t.feed(src)
    t.close()
    return t.els


def _find(src: str, anchor: str, nth: int | None) -> int:
    hits = [m.start() for m in re.finditer(re.escape(anchor), src)]
    if nth is None and len(hits) > 1 and re.fullmatch(r"\{\{\w+\}\}", anchor):
        # A token also named in an attribute (the QR's aria-label): the
        # element whose content it is.
        hits = [m.start() + 1 for m in re.finditer(re.escape(f">{anchor}<"), src)]
    if nth is None:
        if len(hits) != 1:
            raise EditFailed(f"{anchor!r}: found {len(hits)} times")
        return hits[0]
    if nth >= len(hits):
        raise EditFailed(f"{anchor!r}: found {len(hits)} times, wanted number {nth + 1}")
    return hits[nth]


def element_at(src: str, anchor: str, up: int = 0, nth: int | None = None) -> El:
    """The smallest element whose extent holds `anchor` (its text, or a piece
    of its own start tag), then `up` levels out."""
    pos = _find(src, anchor, nth)
    chain = sorted((e for e in elements(src) if e.start <= pos and pos + len(anchor) <= e.end),
                   key=lambda e: e.end - e.start)
    if len(chain) <= up:
        raise EditFailed(f"{anchor!r}: no element {up} level(s) out")
    return chain[up]


def _anchor(a: str) -> str:
    """A bare token name means the token itself."""
    return "{{%s}}" % a if re.fullmatch(r"\w+", a) else a


# ─── Edits ───────────────────────────────────────────────────────────────────


@dataclass
class Edit:
    why: str
    apply: Callable[[str], str]


def attrs(anchor: str, add: str, why: str, up: int = 0, nth: int | None = None) -> Edit:
    """Add attributes to the element holding `anchor` (`up` levels out)."""
    def run(src):
        e = element_at(src, _anchor(anchor), up, nth)
        head = re.match(r"<[\w-]+", src[e.start:]).end() + e.start
        return src[:head] + " " + add + src[head:]
    return Edit(why, run)


def style(anchor: str, css: str, why: str, up: int = 0, nth: int | None = None) -> Edit:
    """Append declarations to the style of the element holding `anchor`."""
    def run(src):
        e = element_at(src, _anchor(anchor), up, nth)
        tag = src[e.start:e.open_end]
        m = re.search(r'style="([^"]*)"', tag)
        if not m:
            raise EditFailed(f"{anchor!r}: no style to extend")
        new = tag[:m.end(1)] + "; " + css + tag[m.end(1):]
        return src[:e.start] + new + src[e.open_end:]
    return Edit(why, run)


def sub(old: str, new: str, why: str, count: int = 1) -> Edit:
    def run(src):
        n = src.count(old)
        if n != count:
            raise EditFailed(f"{old[:60]!r}: found {n} times, expected {count}")
        return src.replace(old, new)
    return Edit(why, run)


def inner(anchor: str, new: str, why: str, up: int = 0) -> Edit:
    """Replace the content of the element holding `anchor`."""
    def run(src):
        e = element_at(src, _anchor(anchor), up)
        close = src.rindex("</", e.start, e.end)
        return src[:e.open_end] + new + src[close:]
    return Edit(why, run)


GOOGLE = Edit("Google Fonts <link> removed: the fonts are served by us (D3)",
              lambda s: re.sub(r'<link rel="stylesheet" href="https://fonts\.googleapis\.com/[^"]*">\n', "", s, count=1)
              if "fonts.googleapis.com" in s else (_ for _ in ()).throw(EditFailed("no Google Fonts link")))


def row_seat(why="Row and seat each optional, the separator only between both (general admission)") -> Edit:
    return sub("{{row}} · {{seat}}",
               '<span data-optional="row">{{row}}</span><span data-optional="row seat"> · </span>'
               '<span data-optional="seat">{{seat}}</span>', why)


def pair(a: str, b: str, sep: str = " · ", why: str = "") -> Edit:
    return sub("{{%s}}%s{{%s}}" % (a, sep, b),
               '<span data-optional="%s">{{%s}}</span><span data-optional="%s %s">%s</span>'
               '<span data-optional="%s">{{%s}}</span>' % (a, a, a, b, sep, b, b),
               why or f"{a} and {b} each optional; the separator only between both")


def ticket_qr() -> Edit:
    def run(src):
        e = element_at(src, '<div data-slot="qr"', 0)
        close = src.rindex("</", e.start, e.end)
        tag = src[e.start:e.open_end]
        tag = tag.replace('aria-label="Ticket QR code"', 'aria-label="QR code for ticket {{ticket_id}}"')
        tag = re.sub(r'style="', 'style="padding: 12px; ', tag, count=1)
        img = '<img src="{{qr_code_src}}" alt="" style="display: block; width: 100%; height: 100%">'
        return src[:e.start] + tag + img + src[close:]
    return Edit("QR placeholder (corner marks and [QR CODE]) replaced by the real code, 12 px quiet zone inside the "
                "white panel, aria-label names the ticket (README: QR code)", run)


LANYARD = attrs("width: 56px; height: 12px", "data-lanyard", "data-lanyard on the lanyard pill: the option hides it in place (D4 e)")
PHOTO = attrs('src="{{photo_url}}"', 'data-slot="photo"', 'data-slot="photo" on the photo: without one, an initials tile (D5)')
LOGO = attrs('data-slot="logo"', 'data-optional="logo_src"', 'data-optional="logo_src" on the logo: no logo, no empty frame')


def fit(a: str, mode: str, why: str = "", up: int = 0, lines: int | None = None, extra: str = "",
        nth: int | None = None) -> Edit:
    add = f'data-fit="{mode}"' + (f' data-fit-lines="{lines}"' if lines else "") + (f" {extra}" if extra else "")
    return attrs(a, add, why or f"{a}: fit {mode}" + (f", at most {lines} line(s)" if lines else ""), up=up, nth=nth)


def opt(a: str, up: int = 1, mode: str = "data-optional", tokens: str | None = None, why: str = "",
        nth: int | None = None) -> Edit:
    tokens = tokens or a
    what = {"data-optional": "left out", "data-optional-hide": "hidden in place",
            "data-optional-any": "left out"}[mode]
    return attrs(a, f'{mode}="{tokens}"', why or f"{_label(a)} {what} when {tokens.replace(' ', ' and ')} "
                 + ("are" if " " in tokens else "is") + " empty", up=up, nth=nth)


def _label(a: str) -> str:
    return a if re.fullmatch(r"\w+", a) else "the block"


def box(a: str, up: int, why: str = "") -> Edit:
    return attrs(a, "data-fit-box", why or f"data-fit-box: {_label(a)} fits inside this ancestor ({up} up)", up=up)


def option(text: str, slot: str) -> Edit:
    return attrs(text, f'data-option="{slot}"', f'"{slot}" wording is an option (the designer\'s words stay the default)')


# ─── Per kind: what every file gets ──────────────────────────────────────────


def common(kind: str) -> list[Edit]:
    if kind == "ticket":
        return [GOOGLE, ticket_qr()]
    if kind == "badge":
        return [GOOGLE, LANYARD]
    return [GOOGLE]


SPECS: dict[str, dict[str, list[Edit]]] = {"ticket": {}, "certificate": {}, "badge": {}}  # filled below


# ─── Tickets ─────────────────────────────────────────────────────────────────
# Every ticket: the h1 shrinks to 60% then takes a second line; the tagline,
# doors, gate, row and seat may be empty (general admission); names shrink
# inside their stub; codes shrink and never truncate. Grids with fixed column
# shares keep an empty cell's place (hide) so the venue keeps its width.

def _ticket_common(seat_mode: str = "data-optional-any", tagline: bool = True) -> list[Edit]:
    return [
        fit("event_name", "title"),
        *([attrs("tagline", 'data-optional="tagline" data-fit="line"', "tagline left out when empty; one line, ellipsis")]
          if tagline else []),
        row_seat(),
        attrs('<span data-optional="row">', 'data-fit="line"', "seat value: one line, ellipsis", up=1),
        attrs('<span data-optional="row">', f'{seat_mode}="row seat"',
              "SEAT block " + ("left out" if seat_mode == "data-optional-any" else "hidden in place")
              + " when there is neither row nor seat", up=2),
    ]


SPECS["ticket"] = {
    "01-dusk-horizon.html": _ticket_common() + [
        fit("app_name", "name"), box("app_name", 2),
        fit("date", "line"), fit("venue", "line"),
        opt("doors_time"), fit("doors_time", "line"),
        opt("gate"), fit("gate", "line"),
        fit("ticket_type", "name"), box("ticket_type", 2), fit("ticket_id", "code"),
        fit("attendee_name", "name"), box("attendee_name", 2),
        opt("venue"), opt("ticket_type", why="the PASS block left out without a ticket type"),
    ],
    "02-midnight-arcade.html": _ticket_common() + [
        fit("app_name", "name"), box("app_name", 1),
        fit("date", "line"), fit("venue", "line"),
        opt("doors_time", mode="data-optional-hide"), fit("doors_time", "line"),
        opt("gate", mode="data-optional-hide"), fit("gate", "line"),
        fit("ticket_type", "name"), box("ticket_type", 1), fit("ticket_id", "code"),
        fit("attendee_name", "name"), box("attendee_name", 2),
        opt("venue", mode="data-optional-hide"), opt("ticket_type", up=0),
    ],
    "03-daylight-boarding-pass.html": _ticket_common(tagline=False) + [
        fit("app_name", "name"), box("app_name", 2), fit("ticket_type", "name"),
        opt("doors_time", mode="data-optional-hide"),
        fit("date", "line"), fit("venue", "line"),
        opt("gate", mode="data-optional-hide"), fit("gate", "line"),
        fit("attendee_name", "name"), fit("ticket_id", "code"),
        opt("venue", mode="data-optional-hide"), opt("ticket_type", up=0), opt("start_time", mode="data-optional-hide"),
    ],
    "04-live-session.html": _ticket_common() + [
        fit("app_name", "name"), box("app_name", 1),
        fit("date", "line"), fit("venue", "line"),
        opt("doors_time"), fit("doors_time", "line"),
        opt("gate"), fit("gate", "line"),
        fit("ticket_type", "name"), fit("attendee_name", "name"), fit("ticket_id", "code"),
        opt("venue"), opt("ticket_type", why="the PASS block left out without a ticket type"),
    ],
    "05-grand-premiere.html": _ticket_common() + [
        fit("{{app_name}} PRESENTS", "name"), box("{{app_name}} PRESENTS", 2),
        fit("date", "line"), fit("venue", "line"),
        opt("doors_time", up=0, mode="data-optional-hide"), opt(">DOORS<", up=0, mode="data-optional-hide", tokens="doors_time"),
        fit("doors_time", "line"),
        opt("gate", up=0, mode="data-optional-hide"), opt(">GATE<", up=0, mode="data-optional-hide", tokens="gate"),
        fit("gate", "line"),
        fit("ticket_type", "name"), box("ticket_type", 2),
        fit("attendee_name", "name"), fit("ticket_id", "code"),
        opt("venue", up=0, mode="data-optional-hide"), opt(">VENUE<", up=0, mode="data-optional-hide", tokens="venue"), opt("ticket_type", up=0),
    ],
    "06-garden-party.html": _ticket_common() + [
        attrs("event_name", "data-fit-wrap", "the title block is narrow on purpose: two lines at full size first"),
        fit("ticket_type", "name"), box("ticket_type", 1), fit("ticket_id", "code"),
        fit("attendee_name", "name"), box("attendee_name", 2),
        fit("app_name", "name"), box("app_name", 1),
        fit("date", "line"), fit("venue", "line"),
        opt("doors_time"), fit("doors_time", "line"),
        opt("gate"), fit("gate", "line"),
        opt("venue"), opt("ticket_type", up=0),
    ],
    "07-blueprint.html": _ticket_common() + [
        attrs("event_name", "data-fit-wrap", "the title block is narrow on purpose: two lines at full size first"),
        fit("app_name", "name"), box("app_name", 1), fit("REV. {{ticket_type}}", "name"),
        fit("date", "line"), fit("venue", "line"),
        opt("doors_time", up=0, mode="data-optional-hide"), opt(">DOORS<", up=0, mode="data-optional-hide", tokens="doors_time"),
        fit("doors_time", "line"),
        opt("gate", up=0, mode="data-optional-hide"), opt(">GATE<", up=0, mode="data-optional-hide", tokens="gate"),
        fit("gate", "line"),
        fit("attendee_name", "name"), box("attendee_name", 1),
        fit("ticket_id", "code"), box("ticket_id", 1),
        opt("venue", up=0, mode="data-optional-hide"), opt(">VENUE<", up=0, mode="data-optional-hide", tokens="venue"), opt("REV. {{ticket_type}}", up=0, tokens="ticket_type"),
    ],
    "08-pop-block.html": _ticket_common() + [
        attrs("event_name", "data-fit-wrap", "the title block is narrow on purpose: two lines at full size first"),
        fit("app_name", "name"), box("app_name", 1),
        box("venue", 2),
        opt("doors_time"), opt("gate"),
        fit("venue", "line"), fit("date", "line"), fit("doors_time", "line"), fit("gate", "line"),
        fit("ticket_type", "name"), box("ticket_type", 1), fit("ticket_id", "code"),
        fit("attendee_name", "name"), box("attendee_name", 2),
        opt("venue"), opt("ticket_type", up=0),
    ],
    "09-northern-lights.html": _ticket_common() + [
        fit("app_name", "name"), box("app_name", 1),
        fit("date", "line"), fit("venue", "line"),
        opt("doors_time"), fit("doors_time", "line"),
        opt("gate"), fit("gate", "line"),
        fit("ticket_type", "name"), box("ticket_type", 1), fit("ticket_id", "code"),
        fit("attendee_name", "name"), box("attendee_name", 2),
        opt("venue"), opt("ticket_type", up=0),
    ],
    "10-match-day.html": [
        fit("event_name", "title"),
        attrs("tagline", 'data-optional="tagline" data-fit="line"', "tagline left out when empty; one line, ellipsis"),
        fit("app_name", "name"), box("app_name", 2), sub("{{date}} · KICK-OFF {{start_time}}", '{{date}}<span data-optional="start_time"> · KICK-OFF {{start_time}}</span>', "kick-off time optional"),
        fit("{{date}}<span", "name"),
        opt("gate"), opt("row"), opt("seat"),
        fit("venue", "line"),
        opt("Doors {{doors_time}}", up=0, tokens="doors_time"), fit("Doors {{doors_time}}", "line"),
        fit("ticket_type", "name"), box("ticket_type", 1), fit("ticket_id", "code"),
        fit("attendee_name", "name"), box("attendee_name", 2),
        opt("venue", up=0), opt(">VENUE<", up=0, tokens="venue"), opt("venue", up=1, mode="data-optional-any", tokens="venue doors_time", why="the VENUE box left out without venue or doors"), opt("ticket_type", up=0),
    ],
}


# ─── Certificates ────────────────────────────────────────────────────────────
# Certificates are flex layouts sized by their content: a value kept on one
# line by force would widen its whole panel. So every fit here is "wrap"
# (the designer's own wrapping), shrunk until it takes its lines (one for
# names of signatories, institutes and numbers). Names and statements wrap, then shrink (README: "reduce the font size rather
# than run to three lines"); a signatory, a grade, a duration, a logo, a
# second signatory may be absent; the title, subtitle and statement lines are
# options with the designer's words as one of the choices.

def _signatory(first: str = "signatory_name", design: str = "signatory_designation", up: int = 1) -> list[Edit]:
    return [
        opt(first, up=up, why=f"the signatory's block (line, name, title) left out when there is no {first}"),
        opt(design, up=0), fit(first, "wrap", lines=1),
    ]


SPECS["certificate"] = {
    "01-heritage.html": [
        LOGO,
        fit("institute_name", "wrap", lines=1), box("institute_name", 2),
        option(">of Completion<", "subtitle"),
        fit("recipient_name", "wrap", lines=2),
        option(">has successfully completed the programme<", "statement"),
        fit("course_title", "wrap", lines=2),
        sub("held from {{start_date}} to {{end_date}} &middot; {{duration_hours}} hours &middot; Grade {{grade}}",
            'held from {{start_date}} to {{end_date}}<span data-optional="duration_hours"> &middot; {{duration_hours}}'
            ' hours</span><span data-optional="grade"> &middot; Grade {{grade}}</span>',
            "duration and grade each optional, with their separators"),
        option("held from {{start_date}}", "details"),
        fit("held from {{start_date}}", "wrap", lines=2),
        *_signatory(),
        fit("certificate_id", "wrap", lines=1, extra='data-fit-min="40%"'), box("certificate_id", 2),
    ],
    "02-midnight-deco.html": [
        fit("institute_name", "wrap", lines=1, up=1), box("institute_name", 3),
        option(">OF EXCELLENCE<", "subtitle"),
        fit("recipient_name", "wrap", lines=2),
        sub(", completed on {{completion_date}} with grade {{grade}}.",
            ', completed on {{completion_date}}<span data-optional="grade"> with grade {{grade}}</span>.',
            "grade optional"),
        option("for outstanding achievement in", "statement"),
        fit("for outstanding achievement in", "wrap", lines=3),
        *_signatory(),
        fit("certificate_id", "wrap", lines=1, extra='data-fit-min="40%"'),
    ],
    "03-bold-geometric.html": [
        LOGO,
        fit("institute_name", "wrap", lines=2),
        option(">of Achievement &middot; {{issue_year}}<", "panel_title"),
        option(">Certificate of Achievement<", "heading"),
        fit("No. {{certificate_id}}", "wrap", lines=1, extra='data-fit-min="40%"'), box("No. {{certificate_id}}", 1),
        fit("recipient_name", "wrap", lines=2),
        sub("</strong>, a {{duration_hours}}-hour programme, with grade {{grade}}.",
            '</strong><span data-optional="duration_hours">, a {{duration_hours}}-hour programme</span>'
            '<span data-optional="grade">, with grade {{grade}}</span>.', "duration and grade each optional"),
        option("for successfully completing", "statement"),
        fit("for successfully completing", "wrap", lines=3),
        *_signatory(),
        opt("issue_city", up=0),
    ],
    "04-verified-pass.html": [
        fit("institute_name", "wrap", lines=1, up=1), box("institute_name", 2),
        option(">CERTIFICATE OF COMPLETION<", "kicker"),
        fit("recipient_name", "wrap", lines=2),
        option("and met every requirement of the programme.", "statement"),
        fit("and met every requirement of the programme.", "wrap", lines=3),
        opt("{{duration_hours}} hours", up=1, tokens="duration_hours"),
        opt("grade"), opt("expiry_date"),
        opt("signatory_name", up=2, why="the signature row left out when there is no signatory"),
        opt("signatory_designation", up=0), fit("signatory_name", "wrap", lines=1),
    ],
    "05-botanical.html": [
        LOGO,
        fit("institute_name", "wrap", lines=1), box("institute_name", 1),
        option(">Certificate of Completion<", "title"),
        fit("recipient_name", "wrap", lines=2),
        sub(", completed on {{completion_date}} with grade {{grade}}.",
            ', completed on {{completion_date}}<span data-optional="grade"> with grade {{grade}}</span>.',
            "grade optional"),
        option("for growing through every module of", "statement"),
        fit("for growing through every module of", "wrap", lines=3),
        fit("certificate_id", "wrap", lines=1, extra='data-fit-min="40%"'), box("certificate_id", 2),
        *_signatory(),
    ],
    "06-blueprint.html": [
        LOGO,
        fit("institute_name", "wrap", lines=1, up=1), box("institute_name", 2),
        option(">CERTIFICATE OF COMPLETION<", "kicker"),
        fit("recipient_name", "wrap", lines=2),
        sub("</strong> to specification over {{duration_hours}} hours.",
            '</strong> to specification<span data-optional="duration_hours"> over {{duration_hours}} hours</span>.',
            "duration optional"),
        option("has built, tested and completed", "statement"),
        fit("has built, tested and completed", "wrap", lines=3),
        opt("signatory_name", why="the signatory's block left out when there is no signatory"),
        opt("signatory_designation", up=1), fit("signatory_name", "wrap", lines=1),
        opt("grade", up=0, mode="data-optional-hide"), opt(">GRADE<", up=0, mode="data-optional-hide", tokens="grade"),
    ],
    "07-swiss-minimal.html": [
        option(">Of completion &middot; Awarded to<", "kicker"),
        fit("recipient_name", "wrap", lines=2),
        option("for completing <strong", "statement"),
        fit("for completing <strong", "wrap", lines=3),
        fit("certificate_id", "wrap", lines=1, extra='data-fit-min="40%"'), box("certificate_id", 1),
        opt("{{duration_hours}} hours", up=1, tokens="duration_hours"),
        sub('padding: 11px 0; border-top: 1px solid #0A0A0A; border-bottom: 1px solid #0A0A0A">',
            'padding: 11px 0; border-top: 1px solid #0A0A0A">',
            "the table's closing line moved from the Grade row to the table, so it stays when Grade is left out"),
        style("grid-column: span 5; display: flex; flex-direction: column", "border-bottom: 1px solid #0A0A0A",
              "(the closing line, on the table: same pixels)"),
        opt("grade"),
        *_signatory(),
    ],
    "08-royal-ribbon.html": [
        fit("institute_name", "wrap", lines=1),
        option(">Certificate of Merit<", "title"), fit(">Certificate of Merit<", "wrap", lines=1),
        fit("recipient_name", "wrap", lines=2),
        sub("</strong>, securing {{rank_or_grade}} on {{completion_date}}.",
            '</strong><span data-optional="rank_or_grade">, securing {{rank_or_grade}}</span> on {{completion_date}}.',
            "rank optional"),
        option("in recognition of outstanding performance in", "statement"),
        fit("in recognition of outstanding performance in", "wrap", lines=3),
        *_signatory(),
        *_signatory("second_signatory_name", "second_signatory_designation"),
        fit("No. {{certificate_id}}", "wrap", lines=1, extra='data-fit-min="40%"'), box("No. {{certificate_id}}", 3),
    ],
    "09-terminal.html": [
        fit("institute_name", "wrap", lines=1, up=1), box("institute_name", 2),
        option(">&gt; CERTIFICATE OF COMPLETION<", "kicker"),
        fit("recipient_name", "wrap", lines=2),
        option("has shipped every requirement of", "statement"),
        fit("has shipped every requirement of", "wrap", lines=3),
        opt("{{duration_hours}} hours", up=0, tokens="duration_hours"),
        opt(">duration:<", up=0, tokens="duration_hours"),
        opt("grade", up=0), opt(">grade:<", up=0, tokens="grade"),
        opt("signatory_name", up=2, why="the signature row left out when there is no signatory"),
        opt("signatory_designation", up=0), fit("signatory_name", "wrap", lines=1),
    ],
    "10-confetti.html": [
        fit("institute_name", "wrap", lines=1), box("institute_name", 2),
        option(">Certificate of Participation<", "title"), fit(">Certificate of Participation<", "wrap", lines=1),
        fit("recipient_name", "wrap", lines=2),
        option("for joining in and giving it everything at", "statement"),
        fit("for joining in and giving it everything at", "wrap", lines=3),
        *_signatory(),
        fit("No. {{certificate_id}}", "wrap", lines=1, extra='data-fit-min="40%"'), box("No. {{certificate_id}}", 3),
    ],
}


# ─── ID cards ────────────────────────────────────────────────────────────────
# README: names and roles shrink to fit, down to 18 px (first<br>last measured
# by the wider line); a value we do not have is left out with its separator.
# README: names and roles shrink to fit (first<br>last measured by the wider
# line); a value we do not have is left out with its separator.

SPECS["badge"] = {
    "01-ticket-stub.html": [
        PHOTO,
        fit("event_name", "wrap", lines=2), box("event_name", 1),
        sub("{{event_date}}<br>{{event_venue}}",
            '{{event_date}}<span data-optional="event_venue"><br>{{event_venue}}</span>', "venue optional"),
        fit("{{event_date}}<span", "name"),
        fit("badge_id", "code"), box("badge_id", 2),
        sub("{{first_name}}<br>{{last_name}}", '{{first_name}}<span data-optional="last_name"><br>{{last_name}}</span>', "last name optional: a one-word name prints on the first line, the second goes"),
        fit("{{first_name}}<span data-optional=\"last_name\">", "name", extra='data-fit-min="18"'),
        pair("job_title", "organization"),
        attrs('<span data-optional="job_title">', 'data-optional-any="job_title organization" data-fit="line"',
              "the job title line left out when there is neither", up=1),
        fit("role", "name", extra='data-fit-min="18"'), box("role", 2),
        sub("{{access_level}}<br>Scan the code at every gate",
            '<span data-optional="access_level">{{access_level}}<br></span>Scan the code at every gate',
            "access level optional"),
    ],
    "02-arch.html": [
        PHOTO,
        fit("org_name", "name"), box("org_name", 1),
        opt("EST. {{org_founded_year}}", up=0, tokens="org_founded_year"),
        fit("full_name", "name", extra='data-fit-min="18"'),
        opt("membership_tier", why="the tier line (and its rules) left out when there is no tier"),
        fit("member_id", "code"), fit("valid_thru", "line"),
    ],
    "03-role-rail.html": [
        PHOTO,
        fit("role", "name", extra='data-fit-min="18"'),
        pair("event_name", "event_year"),
        attrs('<span data-optional="event_name">', 'data-fit="line"', "event line: one line, ellipsis", up=1),
        sub("{{first_name}}<br>{{last_name}}", '{{first_name}}<span data-optional="last_name"><br>{{last_name}}</span>', "last name optional: a one-word name prints on the first line, the second goes"),
        fit("{{first_name}}<span data-optional=\"last_name\">", "name", extra='data-fit-min="18"'),
        pair("job_title", "organization"),
        attrs('<span data-optional="job_title">', 'data-optional-any="job_title organization" data-fit="line"',
              "the job title line left out when there is neither", up=1),
        pair("session_hall", "session_time"),
        attrs('<span data-optional="session_hall">', 'data-fit="line"', "session: one line, ellipsis", up=1),
        attrs('<span data-optional="session_hall">', 'data-optional-any="session_hall session_time"',
              "SESSION left out when there is no hall or time", up=2),
        opt("access_level"), fit("access_level", "line"),
    ],
    "04-big-name.html": [
        fit("event_name", "name"), box("event_name", 1),
        fit("first_name", "name", extra='data-fit-min="18"'), opt("last_name", up=0, why="last name optional: a one-word name keeps the first line"), fit("last_name", "name", extra='data-fit-min="18"'),
        opt("organization", why="the organisation line left out when there is none"), fit("organization", "line"),
        fit("role", "name", extra='data-fit-min="18"'), box("role", 2),
        sub("{{access_level}}<br>ID {{badge_id}}",
            '<span data-optional="access_level">{{access_level}}<br></span>ID {{badge_id}}', "access level optional"),
    ],
    "05-monogram.html": [
        fit("org_name", "name"), box("org_name", 1),
        fit("full_name", "name", extra='data-fit-min="18"'),
        pair("role", "city"), fit('<span data-optional="role">', "name", up=1, extra='data-fit-min="18"'),
        fit("member_id", "code"), opt("joined_date"),
    ],
    "06-thermal-mono.html": [
        fit("event_name", "name"), box("event_name", 1),
        fit("first_name", "name", extra='data-fit-min="18"'), opt("last_name", up=0, why="last name optional: a one-word name keeps the first line"), fit("last_name", "name", extra='data-fit-min="18"'),
        opt("organization", up=0), fit("organization", "line"),
        sub("{{checkin_time}} · KIOSK {{kiosk_id}}",
            '<span data-optional="checkin_time">{{checkin_time}}</span><span data-optional="checkin_time kiosk_id">'
            ' · </span><span data-optional="kiosk_id">KIOSK {{kiosk_id}}</span>', "check-in time and kiosk each optional"),
        attrs('<span data-optional="checkin_time">', 'data-optional-any="checkin_time kiosk_id"',
              "CHECK-IN left out when there is no time or kiosk (a preview)", up=1),
        fit("role", "name", extra='data-fit-min="18"'), box("role", 1),
        sub("{{access_level}}<br>ZONE {{zone}}",
            '<span data-optional="access_level">{{access_level}}</span><span data-optional="access_level zone"><br></span>'
            '<span data-optional="zone">ZONE {{zone}}</span>', "access level and zone each optional"),
    ],
    "07-polaroid.html": [
        PHOTO,
        fit("org_name", "name"), box("org_name", 1), opt("city", up=0),
        opt("member since {{member_since_year}}", up=0, tokens="member_since_year"),
        fit("full_name", "name", extra='data-fit-min="18"'), fit("role", "name", extra='data-fit-min="18"'), fit("member_id", "code"),
    ],
    "08-full-bleed.html": [
        PHOTO,
        pair("event_name", "event_year"), fit('<span data-optional="event_name">', "name", up=1),
        fit("role", "name", extra='data-fit-min="18"'),
        fit("full_name", "name", extra='data-fit-min="18"'),
        pair("job_title", "organization"),
        attrs('<span data-optional="job_title">', 'data-optional-any="job_title organization" data-fit="line"',
              "the job title line left out when there is neither", up=1),
        opt("booth_no"), fit("badge_id", "code"),
    ],
    "09-boarding-pass.html": [
        fit("event_name", "name"), box("event_name", 1),
        opt("day_label"), opt("hall"),
        opt("day_label", up=2, mode="data-optional-any", tokens="day_label hall",
            why="the FROM / TO row left out when there is neither day nor hall"),
        fit("full_name", "name", extra='data-fit-min="18"'), opt("organization", up=0), fit("organization", "line"),
        fit("role", "name", extra='data-fit-min="18"'), opt("zone", mode="data-optional-hide"), fit("badge_id", "code"),
        opt("doors_open_time"),
    ],
    "10-colour-blocks.html": [
        fit("full_name", "name", extra='data-fit-min="18"'),
        pair("org_name", "chapter"), fit('<span data-optional="org_name">', "line", up=1),
        fit("role", "name", extra='data-fit-min="18"'), box("role", 2),
        sub("No. {{member_id}} · Valid {{valid_thru}}",
            'No. {{member_id}}<span data-optional="valid_thru"> · Valid {{valid_thru}}</span>', "valid date optional"),
    ],
    "11-night-grid.html": [
        fit("&gt; {{event_name}}", "line"),
        sub("// {{event_tagline}} · {{event_date}}",
            '// <span data-optional="event_tagline">{{event_tagline}} · </span>{{event_date}}', "tagline optional"),
        fit("// <span", "line"),
        sub("{{first_name}}<br>{{last_name}}", '{{first_name}}<span data-optional="last_name"><br>{{last_name}}</span>', "last name optional: a one-word name prints on the first line, the second goes"),
        fit("{{first_name}}<span data-optional=\"last_name\">", "name", extra='data-fit-min="18"'),
        opt("team: {{team_name}}", up=0, tokens="team_name"),
        fit("role", "name", extra='data-fit-min="18"'),
        opt("table_no", up=0), opt("track", up=0),
    ],
    "12-stamp.html": [
        fit("org_name", "name"), fit("full_name", "name", extra='data-fit-min="18"'),
        opt("membership_tier", up=0),
        opt("joined_date"),
        opt("city", up=0),
    ],
    "13-sash.html": [
        fit("event_name", "name"), box("event_name", 1),
        sub("{{first_name}}<br>{{last_name}}", '{{first_name}}<span data-optional="last_name"><br>{{last_name}}</span>', "last name optional: a one-word name prints on the first line, the second goes"),
        fit("{{first_name}}<span data-optional=\"last_name\">", "name", extra='data-fit-min="18"'),
        opt("organization", up=0), fit("organization", "line"),
        fit("role", "name", extra='data-fit-min="18"'), box("role", 0, "data-fit-box: the sash fits its role within itself (it runs off the card)"),
        opt("access_level"), fit("badge_id", "code"),
    ],
    "14-fine-line.html": [
        fit("org_name", "name"), box("org_name", 1),
        opt("membership_tier", why="the tier line left out when there is no tier"),
        sub("{{first_name}}<br>{{last_name}}", '{{first_name}}<span data-optional="last_name"><br>{{last_name}}</span>', "last name optional: a one-word name prints on the first line, the second goes"),
        fit("{{first_name}}<span data-optional=\"last_name\">", "name", extra='data-fit-min="18"'),
        opt("Member since {{member_since_year}}", up=0, tokens="member_since_year"),
    ],
    "15-race-bib.html": [
        fit("org_name", "name"), box("org_name", 1),
        fit("bib_number", "code"), fit("full_name", "name", extra='data-fit-min="18"'),
        opt("category", up=0, mode="data-optional-hide"), opt(">CATEGORY<", up=0, mode="data-optional-hide", tokens="category"),
        opt("wave", up=0, mode="data-optional-hide"), opt(">WAVE<", up=0, mode="data-optional-hide", tokens="wave"),
        sub('<div style="flex-grow: 1; box-sizing: border-box; border: 2px dashed #555555; display: flex; align-items: '
            'center; justify-content: center; font-size: 12px; font-weight: 600; letter-spacing: 0.1em; color: #555555">'
            '[SPONSOR LOGOS]</div>',
            '<div data-optional-hide="sponsor_src" style="flex-grow: 1; min-width: 0; box-sizing: border-box; display: flex; '
            'align-items: center; justify-content: center"><img data-optional="sponsor_src" src="{{sponsor_src}}" alt="" '
            'style="display: block; max-width: 100%; max-height: 84px; object-fit: contain"></div>',
            "[SPONSOR LOGOS] placeholder replaced by the sponsor image (README); with none, the space stays empty "
            "so the QR keeps its place"),
    ],
}


def edits_for(kind: str, name: str) -> list[Edit]:
    return common(kind) + SPECS[kind].get(name, [])


def apply(kind: str, name: str, src: str) -> str:
    for e in edits_for(kind, name):
        try:
            src = e.apply(src)
        except EditFailed as exc:
            raise EditFailed(f"{kind}/{name}: {e.why}: {exc}") from None
    return src


def changes_md() -> str:
    out = [
        "# Changes to the designer's templates",
        "",
        "Generated by `backend/scripts/doc_designs_d4.py` from its edit lists -- do not edit by hand.",
        "The files as received are commit `8e11672` (`git diff 8e11672 -- templates/` shows every edit).",
        "Only the D4 edits of the doc-designs contract are made; colours, spacing, artwork and type are the designer's.",
        "",
        "Attributes the edits add, read by `compile.py` and `fit.js`:",
        "",
        "- `data-optional=\"a b\"`: printed only when every value is there; `data-optional-any`: when any is;",
        "  `data-optional-hide`: as data-optional, but kept in place, invisible, so a grid keeps its shape.",
        "- `data-option=\"slot\"`: wording a certificate offers in other words (the template keeps the designer's).",
        "- `data-fit=\"name|code|title|wrap|line\"`: how a long value fits (fit.js).",
        "- `data-lanyard`, `data-slot=\"photo\"`, `data-slot=\"signature\"` (the designer's), `data-slot=\"logo\"`.",
        "",
    ]
    for kind in ("ticket", "certificate", "badge"):
        out.append(f"## {kind.capitalize()}s")
        out.append("")
        out.append("Every file: " + "; ".join(e.why for e in common(kind)) + ".")
        out.append("")
        for name in sorted(p.name for p in (DEST / kind).glob("*.html")):
            out.append(f"### {name}")
            out.append("")
            for e in SPECS[kind].get(name, []):
                out.append(f"- {e.why}")
            out.append("")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--from", dest="src")
    ap.add_argument("--kind", choices=("ticket", "certificate", "badge"))
    ap.add_argument("--changes", action="store_true")
    args = ap.parse_args()
    if args.src:
        for path in sorted(Path(args.src).glob("*.html")):
            out = apply(args.kind, path.name, path.read_text())
            (DEST / args.kind / path.name).write_text(out)
            print(f"{args.kind}/{path.name}: {len(edits_for(args.kind, path.name))} edits", file=sys.stderr)
    CHANGES.write_text(changes_md() + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
