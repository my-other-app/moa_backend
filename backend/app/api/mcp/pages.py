"""The pages a person sees while connecting an assistant to their club.

Server-rendered and self-contained: one stylesheet inline, no script, no
font or image from anywhere else, in the dashboard's colours (Meridian),
light or dark as the browser prefers. Every value from outside -- an app's
name, an address, an email -- is escaped. Each page forbids framing and
lets its forms post only here (and, after consent, redirect only to the app's
registered address).
"""
from html import escape
from urllib.parse import urlsplit

from fastapi.responses import HTMLResponse

from app.api.mcp import scopes as catalogue

_STYLE = """
:root{--canvas:#f4f5f2;--surface:#fff;--sunken:#f0f1ee;--line:#e0e3de;--ink:#15181a;--ink-2:#596059;--ink-3:#868d89;
--brand:#0f8a73;--brand-ink:#fff;--brand-soft:#e3f3ee;--refuse:#a2271b;--refuse-soft:#fbe9e6;--caution:#8a5a0f;--caution-soft:#fbf2dd}
@media (prefers-color-scheme:dark){:root{--canvas:#0b0e0d;--surface:#151918;--sunken:#1b201e;--line:#2a302d;--ink:#eef1ee;
--ink-2:#a4aba6;--ink-3:#777e7a;--brand:#17a58f;--brand-ink:#06201c;--brand-soft:#10302a;--refuse:#f08a7d;--refuse-soft:#3a1914;
--caution:#e8b45c;--caution-soft:#33270f}}
*{box-sizing:border-box}html,body{margin:0;background:var(--canvas);color:var(--ink)}
body{font:15px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;min-height:100vh;display:flex;
align-items:flex-start;justify-content:center;padding:48px 16px}
.card{width:100%;max-width:440px;background:var(--surface);border:1px solid var(--line);border-radius:16px;padding:28px;
box-shadow:0 1px 2px rgba(16,20,16,.04),0 8px 24px -16px rgba(16,20,16,.14)}
.brand{display:flex;align-items:center;gap:10px;font-weight:650;letter-spacing:-.01em;margin-bottom:22px}
.mark{width:28px;height:28px;border-radius:8px;background:var(--brand);color:var(--brand-ink);display:grid;place-items:center;
font-size:13px;font-weight:700}
h1{font-size:20px;line-height:1.3;margin:0 0 6px;letter-spacing:-.01em}
p{margin:0 0 14px;color:var(--ink-2)}
label{display:block;font-size:13px;font-weight:600;margin:14px 0 6px}
input[type=email],input[type=password],input[type=text]{width:100%;padding:10px 12px;border:1px solid var(--line);
border-radius:10px;background:var(--sunken);color:var(--ink);font:inherit}
input:focus{outline:2px solid var(--brand);outline-offset:1px}
.actions{display:flex;gap:10px;margin-top:22px}
button{flex:1;padding:11px 14px;border-radius:10px;border:1px solid var(--line);background:var(--surface);color:var(--ink);
font:inherit;font-weight:600;cursor:pointer}
button.primary{background:var(--brand);border-color:var(--brand);color:var(--brand-ink)}
.error{background:var(--refuse-soft);color:var(--refuse);border-radius:10px;padding:10px 12px;margin:0 0 14px;font-size:14px}
.warn{background:var(--caution-soft);color:var(--caution);border-radius:10px;padding:10px 12px;margin:14px 0 0;font-size:13px}
.who{background:var(--sunken);border-radius:10px;padding:10px 12px;font-size:14px;margin:0 0 16px;color:var(--ink-2)}
.who b{color:var(--ink)}
ul.scopes{list-style:none;padding:0;margin:8px 0 0}
ul.scopes li{display:flex;gap:10px;padding:10px 0;border-top:1px solid var(--line)}
ul.scopes li:first-child{border-top:0}
ul.scopes small{display:block;color:var(--ink-3);font-size:13px}
ul.scopes li.off{opacity:.55}
.choice{display:flex;gap:10px;align-items:center;padding:12px;border:1px solid var(--line);border-radius:10px;margin-top:8px}
.foot{margin-top:18px;font-size:12.5px;color:var(--ink-3)}
a{color:var(--brand)}
"""


def _csp(form_targets: tuple[str, ...]) -> str:
    targets = " ".join(["'self'", *form_targets])
    return f"default-src 'none'; style-src 'unsafe-inline'; img-src https: data:; form-action {targets}; frame-ancestors 'none'; base-uri 'none'"


def form_target(redirect_uri: str) -> str:
    """What a consent form's redirect may reach, as a CSP source."""
    parts = urlsplit(redirect_uri)
    if parts.scheme.lower() in ("http", "https"):
        return f"{parts.scheme.lower()}://{parts.netloc.lower()}"
    return f"{parts.scheme.lower()}:"


def render(title: str, body: str, *, status: int = 200, form_targets: tuple[str, ...] = ()) -> HTMLResponse:
    html = (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<meta name=referrer content=no-referrer>"
        f"<title>{escape(title)} · MyOtherApp</title><style>{_STYLE}</style></head><body>"
        "<main class=card><div class=brand><span class=mark>M</span>MyOtherApp</div>"
        f"{body}</main></body></html>"
    )
    return HTMLResponse(
        html,
        status_code=status,
        headers={"Content-Security-Policy": _csp(form_targets), "Cache-Control": "no-store", "X-Frame-Options": "DENY"},
    )


def _error(message: str | None) -> str:
    return f"<div class=error role=alert>{escape(message)}</div>" if message else ""


def _return_note(app: str, target: str, native: bool) -> str:
    note = f"<p class=foot>After this, {escape(app)} is sent back to <b>{escape(target)}</b>.</p>"
    if native:
        note += (
            f"<div class=warn>{escape(app)} will be reached on this device ({escape(target)}). "
            "Carry on only if you started this from that app.</div>"
        )
    return note


def signin(*, app: str, target: str, native: bool, request: str, email: str = "", error: str | None = None, status: int = 200) -> HTMLResponse:
    body = (
        f"<h1>Connect {escape(app)} to your club</h1>"
        "<p>Sign in with the email and password you use for the club dashboard.</p>"
        f"{_error(error)}"
        "<form method=post action='/oauth/authorize' autocomplete=on>"
        "<input type=hidden name=step value=signin>"
        f"<input type=hidden name=request value='{escape(request, quote=True)}'>"
        "<label for=email>Email</label>"
        f"<input id=email name=email type=email required autocomplete=username value='{escape(email, quote=True)}'>"
        "<label for=password>Password</label>"
        "<input id=password name=password type=password required autocomplete=current-password>"
        "<div class=actions><button class=primary type=submit>Sign in</button></div>"
        "</form>"
        f"{_return_note(app, target, native)}"
    )
    return render("Sign in", body, status=status)


def second_factor(*, app: str, ticket: str, error: str | None = None, status: int = 200) -> HTMLResponse:
    body = (
        "<h1>Two-step sign-in</h1>"
        f"<p>Enter the code from your authenticator app, or a recovery code, to connect {escape(app)}.</p>"
        f"{_error(error)}"
        "<form method=post action='/oauth/authorize'>"
        "<input type=hidden name=step value=totp>"
        f"<input type=hidden name=ticket value='{escape(ticket, quote=True)}'>"
        "<label for=code>Code</label>"
        "<input id=code name=code type=text inputmode=numeric autocomplete=one-time-code required>"
        "<div class=actions><button class=primary type=submit>Continue</button></div>"
        "</form>"
    )
    return render("Two-step sign-in", body, status=status)


def pick_club(*, app: str, ticket: str, choices: list[tuple[int, str, str]], error: str | None = None) -> HTMLResponse:
    rows = "".join(
        f"<label class=choice><input type=radio name=choice value={i} {'checked' if n == 0 else ''} required>"
        f"<span><b>{escape(name)}</b><small style='display:block;color:var(--ink-3)'>{escape(as_who)}</small></span></label>"
        for n, (i, name, as_who) in enumerate(choices)
    )
    body = (
        f"<h1>Which club should {escape(app)} work with?</h1>"
        "<p>You can open more than one club's dashboard. Pick one; connect again for another.</p>"
        f"{_error(error)}"
        "<form method=post action='/oauth/authorize'>"
        "<input type=hidden name=step value=pick>"
        f"<input type=hidden name=ticket value='{escape(ticket, quote=True)}'>"
        f"{rows}"
        "<div class=actions><button class=primary type=submit>Continue</button></div>"
        "</form>"
    )
    return render("Pick a club", body)


def consent(
    *, app: str, app_uri: str | None, target: str, native: bool, redirect_uri: str, ticket: str, club: str, who: str,
    role: str, offered: list[str], unavailable: list[str], error: str | None = None,
) -> HTMLResponse:
    items = "".join(
        f"<li><input type=checkbox name=scope value='{escape(s, quote=True)}' id='s-{escape(s, quote=True)}' checked>"
        f"<label for='s-{escape(s, quote=True)}' style='margin:0;font-weight:500'>{escape(catalogue.SCOPES[s][0])}"
        f"<small>{escape(catalogue.SCOPES[s][1])}</small></label></li>"
        for s in offered
    )
    items += "".join(
        f"<li class=off><span>–</span><span>{escape(catalogue.SCOPES[s][0])}"
        f"<small>Not available to your role ({escape(role)}).</small></span></li>"
        for s in unavailable
    )
    from_where = f" ({escape(urlsplit(app_uri).netloc)})" if app_uri else ""
    body = (
        f"<h1>{escape(app)}{from_where} wants to work with your club</h1>"
        f"<div class=who>Signed in as <b>{escape(who)}</b> · <b>{escape(club)}</b> · {escape(role)}</div>"
        f"{_error(error)}"
        "<form method=post action='/oauth/authorize'>"
        "<input type=hidden name=step value=consent>"
        f"<input type=hidden name=ticket value='{escape(ticket, quote=True)}'>"
        f"<p style='margin-bottom:4px'><b>{escape(app)}</b> will be able to:</p>"
        f"<ul class=scopes>{items}</ul>"
        "<p class=foot>Nothing is published, sent, refunded or deleted from an assistant: it can only draft, "
        "and ask you to approve in the dashboard. You can disconnect it any time from the dashboard's Security page.</p>"
        "<div class=actions><button type=submit name=decision value=deny>Cancel</button>"
        "<button class=primary type=submit name=decision value=allow>Allow</button></div>"
        "</form>"
        f"{_return_note(app, target, native)}"
    )
    return render("Allow access", body, form_targets=(form_target(redirect_uri),))


def message(title: str, text: str, *, status: int = 400, link: tuple[str, str] | None = None) -> HTMLResponse:
    extra = f"<p><a href='{escape(link[1], quote=True)}'>{escape(link[0])}</a></p>" if link else ""
    return render(title, f"<h1>{escape(title)}</h1><p>{escape(text)}</p>{extra}", status=status)
