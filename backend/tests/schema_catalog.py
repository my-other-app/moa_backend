"""What a database's schema is, read from its catalogue, in a form two
databases can be compared by (tests/test_schema_matches_models.py).

`catalogue(conn)` maps each object to how it is defined:

- ("table", name): its kind (r: table, S: sequence, v: view ...);
- ("column", table, column): its type and whether it may be NULL;
- ("constraint", table, name): its kind and definition (NOT VALID left
  out: a constraint the migrations add NOT VALID is validated later, and
  checks the same rows meanwhile);
- ("index", table, name): its definition;
- ("trigger", table, name): its definition;
- ("function", name): its definition, whitespace folded;
- ("enum", name): its labels, in order;
- ("storage", table): its storage parameters (fillfactor ...).

Column defaults are not compared: the models set most of theirs in
Python, the migrations in SQL. A table only one side has is one
difference, not one per column, constraint and index of it.
"""
from __future__ import annotations

import re

QUERIES = {
    "table": """
        SELECT relname, relkind::text FROM pg_class
        WHERE relnamespace = 'public'::regnamespace AND relkind IN ('r', 'p', 'v', 'm', 'S')
    """,
    "column": """
        SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod), CASE WHEN a.attnotnull THEN 'NOT NULL' ELSE 'NULL' END
        FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid
        WHERE c.relnamespace = 'public'::regnamespace AND c.relkind IN ('r', 'p') AND a.attnum > 0 AND NOT a.attisdropped
    """,
    "constraint": """
        SELECT t.relname, c.conname, c.contype::text, pg_get_constraintdef(c.oid)
        FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid
        WHERE c.connamespace = 'public'::regnamespace
    """,
    "index": "SELECT tablename, indexname, indexdef FROM pg_indexes WHERE schemaname = 'public'",
    "trigger": """
        SELECT c.relname, t.tgname, pg_get_triggerdef(t.oid) FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
        WHERE NOT t.tgisinternal AND c.relnamespace = 'public'::regnamespace
    """,
    "function": """
        SELECT p.proname, pg_get_functiondef(p.oid) FROM pg_proc p
        WHERE p.pronamespace = 'public'::regnamespace AND p.prokind = 'f'
          AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = p.oid AND d.deptype = 'e')
    """,
    "enum": """
        SELECT t.typname, string_agg(e.enumlabel, ',' ORDER BY e.enumsortorder)
        FROM pg_type t JOIN pg_enum e ON e.enumtypid = t.oid
        WHERE t.typnamespace = 'public'::regnamespace GROUP BY t.typname
    """,
    "storage": """
        SELECT relname, array_to_string(reloptions, ',') FROM pg_class
        WHERE relnamespace = 'public'::regnamespace AND relkind IN ('r', 'p') AND reloptions IS NOT NULL
    """,
}


def _fold(text: str) -> str:
    return " ".join(text.split())


def catalogue(conn) -> dict[tuple, object]:
    """Every object of the public schema, by key (a DB-API connection)."""
    out: dict[tuple, object] = {}
    with conn.cursor() as cur:
        for kind, sql in QUERIES.items():
            cur.execute(sql)
            for row in cur.fetchall():
                if row[0] == "alembic_version":
                    continue  # alembic's own bookkeeping, only the migrated side has it
                if kind == "column":
                    out[(kind, row[0], row[1])] = (row[2], row[3])
                elif kind == "constraint":
                    out[(kind, row[0], row[1])] = (row[2], re.sub(r"\s+NOT VALID$", "", row[3]))
                elif kind in ("index", "trigger"):
                    out[(kind, row[0], row[1])] = row[2]
                elif kind == "function":
                    out[(kind, row[0])] = _fold(row[1])
                else:
                    out[(kind, row[0])] = row[1]
    return out


def differences(head: dict, models: dict) -> dict[tuple, tuple]:
    """Each key whose definition differs, or that one side lacks: key ->
    (the migrations' definition, the models'), None for a side without it.
    The objects of a table one side lacks are left out (the table is the
    difference)."""
    only = {key[1] for key in set(head) ^ set(models) if key[0] == "table"}
    out = {}
    for key in set(head) | set(models):
        if key[0] in ("column", "constraint", "index", "trigger", "storage") and key[1] in only:
            continue
        if head.get(key) != models.get(key):
            out[key] = (head.get(key), models.get(key))
    return out
