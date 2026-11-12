"""Older designs as v2 layouts (layout-v2 contract, V4).

`from_v1(v1_layout)` -- a saved v1 layout (config.layout, drawn by
reportlab), mechanically. `from_preset(preset)` -- a saved config.preset:
its design's v2 starter, with the preset's accent, wording, own words,
bindings and lanyard choice applied.
"""
from __future__ import annotations

from typing import Any, Mapping


def from_v1(v1_layout: Mapping[str, Any]) -> dict:
    raise NotImplementedError("from_v1 lands with the converter")


def from_preset(preset: Mapping[str, Any]) -> dict:
    raise NotImplementedError("from_preset lands with the starters")
