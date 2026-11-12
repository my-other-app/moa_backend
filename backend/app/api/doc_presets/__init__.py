"""The designer's ticket, certificate and ID card designs, wired in.

feature-register/doc-designs-contract.md is the contract. Backend A's
app.core.doc_designs holds the 35 templates, their manifests, the fonts and
the render service's client; this package is what the rest of the backend
asks:

- config.py: a design's `config.preset` (D6), and the code defaults (D7).
- engine.py: the one door to app.core.doc_designs -- the catalogue, checking,
  filling, rendering, and degraded mode (D9).
- values.py: one mapping per kind from our rows to the designer's tokens,
  with the club's bindings (D5).
- photos.py: a guest's photo for a badge, or their initials.
- jobs.py: a preset document as a render job, kept in doc_cache (D8).
- pins.py: the design an issued certificate keeps (contract §6).

A design without `config.preset` is drawn by the paths that drew it before,
unchanged: the layered canvas (app.core.layout) or the HTML templates.
"""
