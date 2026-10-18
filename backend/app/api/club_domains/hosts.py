"""What a club types as its domain, made into one host name, or refused.

`normalise` takes whatever was pasted -- `https://WWW.TheirClub.org/about`,
`www.théclub.fr.`, `www.theirclub.org:443` -- and returns the host in lower
case and punycode, with no scheme, port, path or trailing dot. It refuses:

- anything that is not a host name: an IP address, a single label, a label
  too long, a wildcard (422 INVALID_HOST);
- our own names: anything under myotherapp.in, or the old domains
  (422 INVALID_HOST);
- a bare registrable domain, `theirclub.org` (422 APEX_NOT_SUPPORTED):
  Cloudflare for SaaS takes a CNAME, and an apex cannot carry one at most
  registrars. The club uses `www.theirclub.org` and forwards the apex to it
  at its registrar.

Where the registrable domain ends is read from the Public Suffix List
through `tldextract` when it is installed (its bundled copy, never the
network). Without it, a short list of the two-level suffixes clubs are
likely to have (`co.in`, `ac.in`, `org.uk`, ...) stands in: anything else
is read as one label of suffix.
"""
from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

import idna

from app.config import settings
from app.response import CustomHTTPException

INVALID_HOST = "INVALID_HOST"
APEX_NOT_SUPPORTED = "APEX_NOT_SUPPORTED"

_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")

#: Domains that are ours, old and new: never a club's.
OUR_DOMAINS = frozenset({"myotherapp.in", "myotherapp.com", "finnet.app"})

#: Two-level public suffixes, for when tldextract is not installed.
TWO_LEVEL_SUFFIXES = frozenset({
    # India
    "co.in", "net.in", "org.in", "firm.in", "gen.in", "ind.in", "ac.in", "edu.in", "res.in", "gov.in", "mil.in", "nic.in",
    # United Kingdom
    "co.uk", "org.uk", "me.uk", "ltd.uk", "plc.uk", "net.uk", "ac.uk", "gov.uk", "sch.uk", "nhs.uk",
    # Commonwealth and Asia-Pacific
    "com.au", "net.au", "org.au", "edu.au", "gov.au", "asn.au", "id.au",
    "co.nz", "net.nz", "org.nz", "ac.nz", "school.nz", "govt.nz",
    "com.sg", "net.sg", "org.sg", "edu.sg", "gov.sg",
    "com.my", "net.my", "org.my", "edu.my", "gov.my",
    "com.pk", "net.pk", "org.pk", "edu.pk", "gov.pk",
    "com.bd", "net.bd", "org.bd", "edu.bd", "gov.bd", "ac.bd",
    "com.np", "net.np", "org.np", "edu.np", "gov.np",
    "com.lk", "net.lk", "org.lk", "edu.lk", "gov.lk", "ac.lk",
    "co.jp", "ne.jp", "or.jp", "ac.jp", "go.jp",
    "co.kr", "ne.kr", "or.kr", "ac.kr", "go.kr",
    "com.cn", "net.cn", "org.cn", "edu.cn", "gov.cn",
    "com.hk", "net.hk", "org.hk", "edu.hk", "gov.hk",
    "com.tw", "net.tw", "org.tw", "edu.tw", "gov.tw",
    "co.id", "or.id", "ac.id", "go.id", "web.id",
    "co.th", "or.th", "ac.th", "go.th", "in.th",
    "com.ph", "net.ph", "org.ph", "edu.ph", "gov.ph",
    "com.vn", "net.vn", "org.vn", "edu.vn", "gov.vn",
    # Africa and the Middle East
    "co.za", "org.za", "net.za", "ac.za", "gov.za", "edu.za",
    "co.ke", "or.ke", "ac.ke", "go.ke", "ne.ke",
    "com.ng", "org.ng", "edu.ng", "gov.ng", "net.ng",
    "com.eg", "edu.eg", "gov.eg", "org.eg",
    "com.sa", "edu.sa", "gov.sa", "org.sa", "net.sa",
    "co.ae", "ac.ae", "gov.ae", "net.ae", "org.ae",
    "co.il", "org.il", "ac.il", "gov.il", "net.il",
    "com.tr", "org.tr", "edu.tr", "gov.tr", "net.tr",
    # The Americas and Europe
    "com.br", "net.br", "org.br", "edu.br", "gov.br",
    "com.mx", "org.mx", "edu.mx", "gob.mx", "net.mx",
    "com.ar", "org.ar", "edu.ar", "gob.ar", "net.ar",
    "com.co", "org.co", "edu.co", "gov.co", "net.co",
    "com.ua", "org.ua", "edu.ua", "gov.ua", "net.ua",
    "co.at", "or.at", "ac.at", "gv.at",
})

try:  # pragma: no cover - only where tldextract is installed
    import tldextract

    _EXTRACT = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)
except Exception:  # noqa: BLE001
    _EXTRACT = None


def _invalid(message: str) -> CustomHTTPException:
    return CustomHTTPException(422, message=message, error_code=INVALID_HOST)


def _ours() -> set[str]:
    """Our own domains, plus whatever the settings name (the club-site base,
    the CNAME target's), so a moved setting still refuses its own domain."""
    out = set(OUR_DOMAINS)
    base = urlsplit(settings.PUBLIC_CLUB_SITE_URL.replace("{slug}", "x")).hostname or ""
    if base.count(".") >= 2:
        out.add(base.split(".", 1)[1])
    target = (settings.SITES_CNAME_TARGET or "").strip().lower().rstrip(".")
    if target.count(".") >= 2:
        out.add(target.split(".", 1)[1])
    return out


def is_ours(host: str) -> bool:
    return any(host == d or host.endswith("." + d) for d in _ours())


def _strip(raw: str) -> str:
    """The host part of whatever was pasted."""
    value = (raw or "").strip()
    if not value:
        raise _invalid("Enter your domain, for example www.yourclub.org.")
    if "://" not in value:
        value = "//" + value.lstrip("/")
    try:
        parts = urlsplit(value)
        host = parts.hostname or ""
        parts.port  # noqa: B018 -- raises on a port that is not a number
    except ValueError as exc:
        raise _invalid("That doesn't look like a domain. Enter it like www.yourclub.org.") from exc
    if parts.username is not None or parts.password is not None:
        raise _invalid("That doesn't look like a domain. Enter it like www.yourclub.org.")
    return host.strip().rstrip(".").lower()


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def _punycode(host: str) -> str:
    try:
        return idna.encode(host, uts46=True).decode("ascii").lower()
    except (idna.IDNAError, UnicodeError) as exc:
        raise _invalid("That domain has characters a domain can't have.") from exc


def registrable(host: str) -> tuple[str | None, bool]:
    """(the registrable domain, whether `host` is exactly it). The domain is
    None when `host` is a public suffix itself or has no suffix we know."""
    if _EXTRACT is not None:  # pragma: no cover - only where tldextract is installed
        ext = _EXTRACT(host)
        if not ext.suffix or not ext.domain:
            return None, False
        domain = f"{ext.domain}.{ext.suffix}"
        return domain, not ext.subdomain
    labels = host.split(".")
    suffix = 2 if ".".join(labels[-2:]) in TWO_LEVEL_SUFFIXES else 1
    if len(labels) <= suffix:
        return None, False
    domain = ".".join(labels[-(suffix + 1):])
    return domain, len(labels) == suffix + 1


def normalise(raw: str) -> str:
    """The host to connect, or 422 INVALID_HOST / APEX_NOT_SUPPORTED."""
    host = _strip(raw)
    if not host:
        raise _invalid("That doesn't look like a domain. Enter it like www.yourclub.org.")
    if _is_ip(host):
        raise _invalid("Enter a domain name, not an IP address.")
    if "*" in host:
        raise _invalid("A wildcard can't be connected. Enter one domain, like www.yourclub.org.")
    host = _punycode(host)
    labels = host.split(".")
    if len(host) > 253 or len(labels) < 2 or not all(_LABEL.match(label) for label in labels):
        raise _invalid("That doesn't look like a domain. Enter it like www.yourclub.org.")
    if labels[-1].isdigit():
        raise _invalid("Enter a domain name, not an IP address.")
    if is_ours(host):
        raise _invalid("That's a MyOtherApp address. Enter a domain your club owns.")
    domain, apex = registrable(host)
    if domain is None:
        raise _invalid("That's a domain ending, not a domain. Enter it like www.yourclub.org.")
    if apex:
        raise CustomHTTPException(
            422,
            message=f"Use www.{host} (or another subdomain) and forward {host} to it at your registrar.",
            error_code=APEX_NOT_SUPPORTED,
            errors={"host": host, "suggestion": f"www.{host}"},
        )
    return host
