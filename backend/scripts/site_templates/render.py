"""The reference renderer for the club-site template packages.

`app/api/sites/templates/<key>/` holds one package per designer template
(see feature-register/site-templates-contract.md). The platform's registry
renders them for real; this module renders them the same way with nothing
but Jinja2, for the parity harness (parity.py) and the package test
(tests/test_site_templates.py). The rules both follow:

* One Environment over the templates folder: FileSystemLoader(templates/),
  autoescape on, trim_blocks/lstrip_blocks off, the default Undefined (a
  missing optional field renders empty and is falsy), auto_reload off.
  Template names are "<key>/page.html.j2", "<key>/sections/<section>.html.j2";
  "_shared/macros.html.j2" holds helpers every package imports.
* Each movable section, in the club's order minus the hidden ones, renders
  with the render input plus `section` = {key, title, index}: title is the
  club's override or the template's default_title; index counts the
  sections actually shown (1, 2, ...). A section whose output is blank is
  left out (no data -> no section) and doesn't take an index.
* page.html.j2 renders with the render input plus `css` (style.css as
  Markup, byte for byte, so the CSP hash of the file matches the <style>),
  `sections_html` (the sections joined by newlines, Markup), `section_keys`
  (the keys shown, in order: the nav links follow them), and optionally
  `head_extra` / `body_end` (Markup the platform adds: JSON-LD, the visit
  script). Fixed sections (nav, hero, profile, footer) are included by the
  page itself.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import jinja2
from markupsafe import Markup

BACKEND = Path(__file__).resolve().parents[2]
TEMPLATES_DIR = BACKEND / "app" / "api" / "sites" / "templates"


def packages(root: Path = TEMPLATES_DIR) -> list[str]:
    """Every package key: the folders with a template.json."""
    return sorted(p.parent.name for p in root.glob("*/template.json"))


def meta(key: str, root: Path = TEMPLATES_DIR) -> dict:
    return json.loads((root / key / "template.json").read_text(encoding="utf-8"))


def sample(key: str, root: Path = TEMPLATES_DIR) -> dict:
    return json.loads((root / key / "sample.json").read_text(encoding="utf-8"))


def stylesheet(key: str, root: Path = TEMPLATES_DIR) -> str:
    return (root / key / "style.css").read_text(encoding="utf-8")


@lru_cache(maxsize=4)
def environment(root: str = str(TEMPLATES_DIR)) -> jinja2.Environment:
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(root),
        autoescape=True,
        trim_blocks=False,
        lstrip_blocks=False,
        auto_reload=False,
        undefined=jinja2.Undefined,
    )


def render_sections(key: str, data: dict, *, order: list[str] | None = None, hidden: list[str] | tuple = (), root: Path = TEMPLATES_DIR) -> list[tuple[str, str]]:
    """(section key, html) for every section shown, in order."""
    m = meta(key, root)
    env = environment(str(root))
    movable = [s["key"] for s in m["sections"]]
    titles = {s["key"]: s["default_title"] for s in m["sections"]}
    titles.update({k: v for k, v in ((data.get("site") or {}).get("title_overrides") or {}).items() if v})
    if order is None:
        order = movable
    # The platform normalises the order; do the same: unknown keys dropped,
    # missing ones appended in the template's default order.
    order = [k for k in order if k in movable] + [k for k in movable if k not in order]
    out: list[tuple[str, str]] = []
    for k in order:
        if k in hidden:
            continue
        html = env.get_template(f"{key}/sections/{k}.html.j2").render(**data, section={"key": k, "title": titles[k], "index": len(out) + 1})
        if html.strip():
            out.append((k, html.strip()))
    return out


def render(key: str, data: dict, *, order: list[str] | None = None, hidden: list[str] | tuple = (), root: Path = TEMPLATES_DIR, head_extra: str = "", body_end: str = "") -> str:
    sections = render_sections(key, data, order=order, hidden=hidden, root=root)
    env = environment(str(root))
    return env.get_template(f"{key}/page.html.j2").render(
        **data,
        css=Markup(stylesheet(key, root)),
        sections_html=Markup("\n".join(h for _, h in sections)),
        section_keys=[k for k, _ in sections],
        head_extra=Markup(head_extra),
        body_end=Markup(body_end),
    )


if __name__ == "__main__":  # python scripts/site_templates/render.py classic > /tmp/classic.html
    import sys

    k = sys.argv[1]
    sys.stdout.write(render(k, sample(k)))
