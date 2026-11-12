"""The designer's templates, each a package in templates/<key>/ (the
interface is feature-register/site-templates-contract.md):

    template.json         what the template has: its sections (the movable
                          ones in their default order), its image slots, its
                          accent colour, its fonts, one column or two
    page.html.j2          the frame: head, nav, fixed sections, {{ sections_html }}
    sections/<key>.html.j2  one per section
    style.css             the designer's stylesheet (hashed for the CSP)
    sample.json           the render input that reproduces the designer's page
    thumb.png, thumb-mobile.png   the dashboard's gallery pictures

Read once per process, validated, every template compiled up front: a
package that is not right is left out (and logged) rather than taking every
club's page down with it; tests load strictly. One Jinja2 Environment per
package, rooted at its folder, autoescaping everything, never re-reading a
file (auto_reload off).

CATALOGUE is the order the dashboard lists them in, and the order a plan's
`website_templates` counts (the first N; app/api/subscriptions/gates.py).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from jinja2 import Environment, FileSystemLoader, TemplateError
from markupsafe import Markup

from app.api.sites.models import DEFAULT_TEMPLATE

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent / "templates"

CATALOGUE = ("classic", "editorial", "night", "poster", "bento", "swiss", "gala", "timeline", "portal", "zine")

#: The section keys a template may have; each uses the ones its design has.
CANONICAL = ("nav", "hero", "profile", "about", "expertise", "stats", "upcoming", "past", "photos", "speakers", "team", "sponsors", "join", "footer")
#: Never moved, hidden or retitled: the frame's own parts.
FRAME = frozenset({"nav", "hero", "profile", "footer"})
#: The image slots a template may declare. Named the same in every template,
#: so a club's choices carry over when it switches template.
SLOT_KEY = re.compile(r"^(?:hero\.cover|hero\.image|about\.image|join\.image|event\.fallback|photos\.([1-9][0-9]?))$")
TITLE_MAX = 60
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
_VAR = re.compile(r"^--[a-z][a-z0-9-]{0,40}$")
_ANCHOR = re.compile(r"^[a-z][a-z0-9-]{0,40}$")
_KEY = re.compile(r"^[a-z][a-z0-9_-]{0,19}$")


class PackageError(ValueError):
    """A template package that cannot be used, and why."""


@dataclass(frozen=True)
class Section:
    key: str
    label: str
    default_title: str
    hideable: bool
    anchor: str


@dataclass(frozen=True)
class Slot:
    key: str
    label: str
    size: tuple[int, int]
    section: Optional[str]


@dataclass(frozen=True, eq=False)
class Package:
    key: str
    name: str
    blurb: str
    layout: str
    fonts: tuple[str, ...]
    fixed_top: tuple[str, ...]
    fixed_side: tuple[str, ...]
    fixed_bottom: tuple[str, ...]
    sections: tuple[Section, ...]
    slots: tuple[Slot, ...]
    accent_default: str
    accent_vars: tuple[str, ...]
    path: Path
    css: Markup
    env: Environment = field(repr=False)
    sample: Optional[dict] = field(default=None, repr=False)

    @property
    def movable(self) -> tuple[str, ...]:
        """The movable sections, in the template's default order."""
        return tuple(s.key for s in self.sections)

    @property
    def hideable(self) -> frozenset:
        return frozenset(s.key for s in self.sections if s.hideable)

    def section(self, key: str) -> Optional[Section]:
        return next((s for s in self.sections if s.key == key), None)

    @property
    def slot_keys(self) -> tuple[str, ...]:
        return tuple(s.key for s in self.slots)

    def slot(self, key: str) -> Optional[Slot]:
        return next((s for s in self.slots if s.key == key), None)

    @property
    def photo_slots(self) -> tuple[Slot, ...]:
        """The gallery's slots, photos.1 first."""
        return tuple(sorted((s for s in self.slots if s.key.startswith("photos.")), key=lambda s: int(s.key.split(".")[1])))

    def thumb(self, kind: str) -> Optional[Path]:
        """`thumb.png` (desktop) or `thumb-mobile.png`, when the package has it."""
        p = self.path / ("thumb-mobile.png" if kind == "mobile" else "thumb.png")
        return p if p.is_file() else None


# ─── Loading ─────────────────────────────────────────────────────────────


def _str(meta: dict, name: str, where: str, *, required: bool = True, limit: int = 200) -> str:
    v = meta.get(name)
    if v is None and not required:
        return ""
    if not isinstance(v, str) or not v.strip() or len(v) > limit:
        raise PackageError(f"{where}: `{name}` must be a non-empty string of at most {limit} characters")
    return v.strip()


def _keys(meta: dict, name: str, where: str) -> tuple[str, ...]:
    v = meta.get(name) or []
    if not isinstance(v, list) or any(k not in CANONICAL for k in v):
        raise PackageError(f"{where}: `{name}` must list canonical section keys ({', '.join(CANONICAL)})")
    return tuple(v)


def environment(path: Path) -> Environment:
    """The package's Jinja2 environment: the one the parity harness builds too."""
    return Environment(
        loader=FileSystemLoader(str(path)), autoescape=True, auto_reload=False,
        trim_blocks=False, lstrip_blocks=False,
    )


def load_package(path: Path, key: Optional[str] = None) -> Package:
    """One package, validated and compiled. `key` names it something other
    than its template.json says (the tests' fixture standing in for a
    catalogue key); otherwise the key must be the folder's name."""
    where = str(path)
    try:
        meta = json.loads((path / "template.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise PackageError(f"{where}: no template.json")
    except ValueError as e:
        raise PackageError(f"{where}: template.json is not JSON ({e})")
    if not isinstance(meta, dict):
        raise PackageError(f"{where}: template.json must be an object")
    own_key = _str(meta, "key", where, limit=20)
    if key is None:
        if own_key != path.name:
            raise PackageError(f"{where}: key {own_key!r} is not the folder's name")
        key = own_key
    if not _KEY.match(key):
        raise PackageError(f"{where}: {key!r} is not a template key")
    name = _str(meta, "name", where, limit=40) if key == own_key else key.replace("_", " ").title()
    blurb = _str(meta, "blurb", where, required=False, limit=200)
    layout = meta.get("layout", "single")
    if layout not in ("single", "two_column"):
        raise PackageError(f"{where}: `layout` must be single or two_column")
    fixed_top, fixed_side, fixed_bottom = (_keys(meta, n, where) for n in ("fixed_top", "fixed_side", "fixed_bottom"))
    fixed = set(fixed_top) | set(fixed_side) | set(fixed_bottom)

    sections, seen = [], set()
    for s in meta.get("sections") or []:
        if not isinstance(s, dict):
            raise PackageError(f"{where}: each of `sections` must be an object")
        k = s.get("key")
        if k not in CANONICAL or k in FRAME or k in fixed or k in seen:
            raise PackageError(f"{where}: section {k!r} cannot be a movable section (not canonical, part of the frame, fixed, or twice)")
        seen.add(k)
        title = _str(s, "default_title", f"{where} section {k}", limit=TITLE_MAX)
        anchor = s.get("anchor") or k
        if not _ANCHOR.match(anchor):
            raise PackageError(f"{where}: section {k}'s anchor {anchor!r} is not a plain id")
        sections.append(Section(key=k, label=_str(s, "label", f"{where} section {k}", limit=40), default_title=title, hideable=bool(s.get("hideable", True)), anchor=anchor))

    slots, seen = [], set()
    for s in meta.get("slots") or []:
        k = s.get("key") if isinstance(s, dict) else None
        if not isinstance(k, str) or not SLOT_KEY.match(k) or k in seen:
            raise PackageError(f"{where}: slot {k!r} is not one of hero.cover, hero.image, about.image, photos.N, join.image, event.fallback (or is twice)")
        seen.add(k)
        size = s.get("size")
        if not (isinstance(size, list) and len(size) == 2 and all(isinstance(x, int) and 0 < x <= 4000 for x in size)):
            raise PackageError(f"{where}: slot {k}'s size must be [width, height] in px")
        sec = s.get("section")
        if sec is not None and sec not in CANONICAL:
            raise PackageError(f"{where}: slot {k}'s section {sec!r} is not a section key")
        slots.append(Slot(key=k, label=_str(s, "label", f"{where} slot {k}", limit=60), size=(size[0], size[1]), section=sec))

    accent = meta.get("accent") or {}
    default = accent.get("default")
    if not isinstance(default, str) or not _HEX.match(default):
        raise PackageError(f"{where}: `accent.default` must be a #rrggbb colour")
    vars_ = tuple(accent.get("vars") or ["--accent"])
    if any(not isinstance(v, str) or not _VAR.match(v) for v in vars_):
        raise PackageError(f"{where}: `accent.vars` must be CSS custom property names")
    fonts = tuple(f for f in (meta.get("fonts") or []) if isinstance(f, str))

    try:
        css = (path / "style.css").read_text(encoding="utf-8")
    except FileNotFoundError:
        raise PackageError(f"{where}: no style.css")
    if "</style" in css.lower():
        raise PackageError(f"{where}: style.css must not close its <style> element")

    env = environment(path)
    try:
        env.get_template("page.html.j2")
        for s in sections:
            env.get_template(f"sections/{s.key}.html.j2")
    except TemplateError as e:
        raise PackageError(f"{where}: {type(e).__name__}: {e}")

    sample = None
    if (path / "sample.json").is_file():
        try:
            sample = json.loads((path / "sample.json").read_text(encoding="utf-8"))
        except ValueError as e:
            raise PackageError(f"{where}: sample.json is not JSON ({e})")

    return Package(
        key=key, name=name, blurb=blurb, layout=layout, fonts=fonts,
        fixed_top=fixed_top, fixed_side=fixed_side, fixed_bottom=fixed_bottom,
        sections=tuple(sections), slots=tuple(slots), accent_default=default.lower(), accent_vars=vars_,
        path=path, css=Markup(css), env=env, sample=sample,
    )


_lock = threading.Lock()
_packages: Optional[dict[str, Package]] = None
#: Packages from elsewhere, by the key they stand in for (the tests' fixture).
_extra: dict[str, Path] = {}


def _order(keys) -> list[str]:
    """Catalogue order first, then any others by name."""
    rank = {k: i for i, k in enumerate(CATALOGUE)}
    return sorted(keys, key=lambda k: (rank.get(k, len(CATALOGUE)), k))


def load(*, strict: bool = False) -> dict[str, Package]:
    """Every package under ROOT, and the extras, keyed and in catalogue order."""
    found: dict[str, Package] = {}
    dirs = sorted(p for p in ROOT.iterdir() if p.is_dir() and (p / "template.json").exists()) if ROOT.is_dir() else []
    for path in dirs:
        try:
            pkg = load_package(path)
        except PackageError:
            if strict:
                raise
            logger.exception("site template %s left out", path.name)
            continue
        found[pkg.key] = pkg
    for key, path in _extra.items():
        found[key] = load_package(path, key=key)
    return {k: found[k] for k in _order(found)}


def packages() -> dict[str, Package]:
    global _packages
    if _packages is None:
        with _lock:
            if _packages is None:
                _packages = load()
    return _packages


def add(key: str, path: Path) -> None:
    """Have the package at `path` answer as `key` (the tests' fixture)."""
    global _packages
    with _lock:
        _extra[key] = Path(path)
        _packages = None


def reset() -> None:
    """Read the packages again on next use."""
    global _packages
    with _lock:
        _packages = None


def get(key: Optional[str]) -> Optional[Package]:
    return packages().get(key or "")


def default_key() -> Optional[str]:
    ps = packages()
    if DEFAULT_TEMPLATE in ps:
        return DEFAULT_TEMPLATE
    return next(iter(ps), None)


def resolve(key: Optional[str]) -> Package:
    """The package for a site: its own, else the default one (a template
    taken out of the catalogue). Raises when there are none at all."""
    pkg = get(key) or get(default_key())
    if pkg is None:
        from app.response import CustomHTTPException

        raise CustomHTTPException(503, message="No website templates are installed.", error_code="SITE_TEMPLATES_MISSING")
    return pkg


def file_version(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:10]
