# Layout v2 conformance fixtures

Each `<name>.json` is one case both builders must agree on (layout-v2 contract, V6):

```jsonc
{
  "name": "text-spans",
  "about": "what it exercises",
  "kind": "ticket" | "certificate" | "badge",   // which tokens are the design's own
  "skipPreprinted": true,                       // optional: printing on pre-printed stock
  "layout": { …v2 layout… },
  "values": { "token": "text", … },
  "context": { "qr": "…", "colors": {…}, "images": {…} },
  "expected": "<div class=\"moa-doc\" …>…</div>"   // normalised
}
```

`expected` is the Python builder's `build_body(layout, values, context=…, kind=…, skip_preprinted=…)`
(the `<div class="moa-doc">` alone, without the page around it), normalised as below. The TypeScript
`buildHtml` body must normalise to the same string.

## The normaliser

Both sides compare `normalise(html)`, which is (`tests/layout_v2_cases.py`):

1. Parse the fragment as HTML.
2. Each element is written `<tag a="v" b="w">`, its attributes **sorted by name**. An attribute with
   no value keeps an empty value (`data-qr` is the exception, written bare).
3. A `style` attribute is split into declarations on `;` outside parentheses; each declaration is
   `prop:value` with `prop` trimmed and lower-cased and `value` trimmed with its inner whitespace
   collapsed to one space; empty declarations are dropped; the **order is kept** (CSS order matters);
   they are joined with `;` and no trailing `;`.
4. Text and attribute values are decoded (entities) and written again escaping exactly
   `&` `<` `>` `"` `'` as `&amp;` `&lt;` `&gt;` `&quot;` `&#39;`.
5. Text that is only whitespace (between tags) is dropped; other text is kept exactly, newlines too.
6. Void elements (`img`, `br`, …) have no end tag; every other element has one.
7. Inside `div.moa-qr`, the QR's `<svg>…</svg>` is replaced by `<svg data-qr></svg>`: the QR's
   modules depend on the encoder's mask choice, so only that the QR is there is compared (the
   server's QR is `app/core/doc_designs/qr.py`, ECC M, `quietZone` modules of border).

What the normaliser does **not** paper over, so both builders must write it the same way:

- numbers: `num(v)` = round half up to 3 decimals (`Math.floor(v * 1000 + 0.5) / 1000`), then the
  shortest round-trip form (`String(n)` in JS, `repr` in Python), integers without `.0`, never `-0`;
- colours: `#RRGGBB`/`#RRGGBBAA` upper case; `var:accent` → `vars.accent` (`#000000` when unset);
  `brand.primary` / `brand.secondary` / `ticket_type` → the context's colours;
- the order of declarations in each style, and which optional declarations are written
  (rotation only when not 0, opacity only when not 1, background/radius/shadow only when set);
- SVG markup: the stored (already sanitised) markup with `#RRGGBB` literals recoloured, every `id`,
  `url(#…)` and `href="#…"` prefixed with `<layer id>-`, and
  `preserveAspectRatio="…" width="100%" height="100%" style="display:block"` inserted first in the
  root `<svg`;
- what is left out: hidden layers, unmet `showIf`, text that fills to nothing (after trimming), an
  image with no picture (unless a guest photo with the initials placeholder), and with
  `skipPreprinted` a preprinted layer whose text carries no per-guest token.

Regenerate after a deliberate change to the builder:
`LAYOUT_V2_WRITE_FIXTURES=1 pytest tests/test_layout_v2.py -k fixtures`.
