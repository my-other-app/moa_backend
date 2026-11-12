"""The reference renderer for the club-site template packages.

`app/api/sites/templates/<key>/` holds one package per designer template
(see feature-register/site-templates-contract.md). The platform's registry
(builder B) renders them for real; this module renders them the same way
with nothing but Jinja2, for the parity harness (parity.py) and the package
test (tests/test_site_templates.py). It follows B's protocol (contract,
"Changes", B 1-9):

* One Environment per package, rooted at the package folder:
  FileSystemLoader(<package>), autoescape on, trim_blocks/lstrip_blocks off,
  the default Undefined (a missing optional field renders empty and is
  falsy), auto_reload off, no custom filters. Inside a package, names are
  relative: "page.html.j2", "sections/<key>.html.j2", "macros.html.j2".
* ctx = the render input + {css, titles, year}. Each movable section, in the
  club's order minus the hidden ones, renders with ctx plus `section` =
  {key, title, label, anchor, index}: title is the club's override or the
  default_title, anchor the section root's id (template.json, default the
  key), index the 1-based position among the sections shown (an extension
  of B's protocol; 03-night prints "01 / about" -- without it the template
  prints 01). Output stripped; blank -> left out, and out of `nav`.
* page.html.j2 renders with ctx plus `sections_html` (Markup, joined by
  newlines) and `nav` ([{key, label, title, href: "#" + anchor}], the
  sections shown, in order). Fixed sections are included by the page.
* `css` is style.css as Markup, byte for byte (the CSP hashes the file).
* The accent: when site.accent is a #rrggbb colour, `style="--accent:#…"`
  goes on the <html> tag (the templates only read var(--accent)).
"""
from __future__ import annotations

import json
import re
from datetime import date
from functools import lru_cache
from pathlib import Path

import jinja2
from markupsafe import Markup

BACKEND = Path(__file__).resolve().parents[2]
TEMPLATES_DIR = BACKEND / "app" / "api" / "sites" / "templates"
HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")


def packages(root: Path = TEMPLATES_DIR) -> list[str]:
    """Every package key: the folders with a template.json."""
    return sorted(p.parent.name for p in root.glob("*/template.json"))


def meta(key: str, root: Path = TEMPLATES_DIR) -> dict:
    return json.loads((root / key / "template.json").read_text(encoding="utf-8"))


def sample(key: str, root: Path = TEMPLATES_DIR) -> dict:
    return json.loads((root / key / "sample.json").read_text(encoding="utf-8"))


def stylesheet(key: str, root: Path = TEMPLATES_DIR) -> str:
    return (root / key / "style.css").read_text(encoding="utf-8")


@lru_cache(maxsize=32)
def environment(package_dir: str) -> jinja2.Environment:
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(package_dir),
        autoescape=True,
        trim_blocks=False,
        lstrip_blocks=False,
        auto_reload=False,
        undefined=jinja2.Undefined,
    )


def _context(key: str, data: dict, root: Path) -> tuple[dict, dict]:
    m = meta(key, root)
    titles = {s["key"]: s["default_title"] for s in m["sections"]}
    titles.update({k: v for k, v in ((data.get("site") or {}).get("title_overrides") or {}).items() if v and k in titles})
    ctx = dict(data)
    ctx.update(css=Markup(stylesheet(key, root)), titles=titles, year=data.get("year") or date.today().year)
    return m, ctx


def render_sections(key: str, data: dict, *, order: list[str] | None = None, hidden: list[str] | tuple = (), root: Path = TEMPLATES_DIR) -> list[tuple[dict, str]]:
    """(nav item, html) for every section shown, in order."""
    m, ctx = _context(key, data, root)
    env = environment(str(root / key))
    movable = {s["key"]: s for s in m["sections"]}
    if order is None:
        order = list(movable)
    # The platform normalises the order the same way: unknown keys dropped,
    # missing ones appended in the template's default order.
    order = [k for k in order if k in movable] + [k for k in movable if k not in order]
    out: list[tuple[dict, str]] = []
    for k in order:
        if k in hidden:
            continue
        s = movable[k]
        anchor = s.get("anchor") or k
        section = {"key": k, "title": ctx["titles"][k], "label": s["label"], "anchor": anchor, "index": len(out) + 1}
        html = env.get_template(f"sections/{k}.html.j2").render(ctx, section=section).strip()
        if html:
            out.append(({"key": k, "label": s["label"], "title": section["title"], "href": "#" + anchor}, html))
    return out


def render(key: str, data: dict, *, order: list[str] | None = None, hidden: list[str] | tuple = (), root: Path = TEMPLATES_DIR) -> str:
    sections = render_sections(key, data, order=order, hidden=hidden, root=root)
    _, ctx = _context(key, data, root)
    html = environment(str(root / key)).get_template("page.html.j2").render(
        ctx, sections_html=Markup("\n".join(h for _, h in sections)), nav=[n for n, _ in sections]
    )
    accent = (data.get("site") or {}).get("accent")
    if accent and HEX.match(accent):
        html = re.sub(r"<html(?=[\s>])", f'<html style="--accent:{accent}"', html, count=1)
    return html


if __name__ == "__main__":  # python scripts/site_templates/render.py classic > /tmp/classic.html
    import sys

    k = sys.argv[1]
    sys.stdout.write(render(k, sample(k)))
