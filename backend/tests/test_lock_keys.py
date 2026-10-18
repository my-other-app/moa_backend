"""Every advisory-lock key is declared once, in app/db/lock_keys.py, and no two share a number.

The store sweep and the club-sites sweep both took 773121, so each skipped
the other's turn, silently. These read the source rather than import it: a
lock is a constant at the top of a module, and the rule is about where that
constant comes from.
"""
import ast
import re
from collections import Counter, defaultdict
from pathlib import Path

from app.db import lock_keys

BACKEND = Path(__file__).resolve().parents[1]
HOME = BACKEND / "app" / "db" / "lock_keys.py"
#: The names a module gives its lock constants: SWEEP_LOCK, _SEND_LOCK, LOCK, LOCK_NS...
LOCK_NAME = re.compile(r"^_?([A-Z0-9]+_)*LOCK(_NS)?$")
#: An advisory lock taken on a number written in place.
INLINE = re.compile(r"advisory_(?:xact_)?lock\(\s*-?\d")


def _sources():
    for top in ("app", "scripts"):
        for path in sorted((BACKEND / top).rglob("*.py")):
            if path != HOME:
                yield path


def _lock_constants():
    """(path, name, value node) for every module-level NAME = ... whose name says it is a lock."""
    for path in _sources():
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                name = node.targets[0].id
                if LOCK_NAME.match(name):
                    yield path, name, node.value


def test_no_two_keys_share_a_number():
    keys = lock_keys.all_keys()
    assert len(keys) >= 20
    shared = {v: sorted(n for n, x in keys.items() if x == v) for v, c in Counter(keys.values()).items() if c > 1}
    assert shared == {}


def test_the_store_and_sites_sweeps_no_longer_meet():
    from app.api.sites import sweep as sites
    from app.api.store import sweep as store

    assert store.SWEEP_LOCK == 773_121  # the first to take it keeps it: a deploy mid-sweep runs it once
    assert sites.SITES_LOCK != store.SWEEP_LOCK


def test_every_lock_constant_comes_from_the_one_module():
    literal, owners = [], defaultdict(list)
    for path, name, value in _lock_constants():
        where = f"{path.relative_to(BACKEND)}:{name}"
        if isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name) and value.value.id == "lock_keys":
            assert value.attr in lock_keys.all_keys(), f"{where} names lock_keys.{value.attr}, which is not declared"
            owners[value.attr].append(where)
        elif isinstance(value, ast.Constant) and isinstance(value.value, int):
            literal.append(where)
    assert literal == [], "declare these in app/db/lock_keys.py and point at them from there"
    assert {k: v for k, v in owners.items() if len(v) > 1} == {}, "one module owns each key"
    # Every key is taken by something: a key nobody points at is a stale entry.
    assert set(owners) == set(lock_keys.all_keys())


def test_no_advisory_lock_is_taken_on_a_number_written_in_place():
    found = []
    for path in _sources():
        for i, line in enumerate(path.read_text().splitlines(), 1):
            if INLINE.search(line):
                found.append(f"{path.relative_to(BACKEND)}:{i}")
    assert found == []
