"""Flags what a new migration would do to a live database that it should not.

    python scripts/check_migrations.py                  # the migrations after the cutoff
    python scripts/check_migrations.py --since none     # every migration (old ones too)
    python scripts/check_migrations.py --hot jobs --hot live_polls

Production migrates while the API serves from the same database. A statement
that takes a strong lock on a table people are using -- or waits for one --
stops every request on that table until it is done. Each rule below is one
way a migration has done, or could do, exactly that:

- index-not-concurrent: CREATE INDEX on a hot table without CONCURRENTLY.
  A plain build blocks every write to the table until it finishes.
- concurrently-in-transaction: CONCURRENTLY outside
  `op.get_context().autocommit_block()`. Postgres refuses it in a
  transaction, so the deploy fails half way.
- not-null-without-default: ADD COLUMN ... NOT NULL with no default, on a
  table that has rows. It fails outright on the first row; with a constant
  default it is a catalog change and instant.
- full-table-update: an UPDATE with no WHERE, or one on a hot table that is
  not in batches that commit. One statement locks every row it touches until
  the end of its transaction, and every write to those rows waits.
- alter-type: a column type change (ALTER COLUMN ... TYPE, alter_column
  type_=) rewrites the whole table under ACCESS EXCLUSIVE; ALTER TYPE other
  than ADD VALUE is flagged with it.
- missing-lock-timeout: DDL on a hot table in a transaction that has not run
  `SET LOCAL lock_timeout` first. migrations/env.py sets a session
  lock_timeout as a backstop; the migration's own, shorter one is what
  CONTRACT asks for, and says the author thought about the lock. DDL inside
  an autocommit block is exempt: each statement there is its own
  transaction, under env.py's limit.

Only upgrade() is read (and the module's own functions it calls). A table
the migration creates itself is never hot. Static and best effort: SQL built
at run time from something it cannot follow is not seen.

A finding the author has thought about is silenced on its line (or the line
above) with a comment naming the rule and why:

    op.execute(BACKFILL)  # migration-check: ok full-table-update -- 40 rows, the table is new

Migrations up to and including the cutoff (CUTOFF, the head when this was
written) and everything before them are not read, so old ones are not
rewritten to please it. Exit status 1 when anything is found.
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Iterable, Optional

#: The newest migration on master when this check was introduced
#: (2026-10-18): it and its ancestors are not checked. topic_placeholders
#: landed on master while the check was being written; its two UPDATEs are
#: bounded by the placeholder value on the topic tables (tens of rows), so
#: batches would buy nothing, and a shipped migration is not rewritten.
CUTOFF = "topic_placeholders"

VERSIONS = Path(__file__).resolve().parents[1] / "migrations" / "versions"

#: Tables the API reads or writes on most requests, or that are large: a lock
#: on one of these is felt by users at once. Add to it as tables grow.
HOT_TABLES = frozenset({
    "users", "user_profiles", "user_sessions", "user_device_tokens", "user_interests", "user_blocks",
    "sign_in_attempts",
    "clubs", "club_users_link", "club_members", "club_team_members", "clubs_interests_link",
    "events", "event_registrations_link", "event_interests_link", "event_page_views", "event_ratings_link",
    "event_team_members", "check_ins", "volunteers",
    "interests", "interest_categories", "event_categories",
    "notifications", "deliveries", "delivery_attempts", "message_deliveries", "after_commit_jobs", "jobs",
    "payment_logs", "payment_orders", "razorpay_webhook_logs", "event_store_orders", "club_subscriptions",
    "club_threads", "thread_comments", "thread_views", "thread_likes", "thread_votes",
    "tracking_clicks", "discover_stats",
})

RULES = (
    "index-not-concurrent",
    "concurrently-in-transaction",
    "not-null-without-default",
    "full-table-update",
    "alter-type",
    "missing-lock-timeout",
)

_SUPPRESS = re.compile(r"#\s*migration-check:\s*ok\s+(.*)")
_COMMENTS = re.compile(r"(--[^\n]*)|(/\*.*?\*/)", re.S)
_NAME = r'(?:"?[\w]+"?\.)?"?(\w+)"?'

_SET_LOCK = re.compile(r"^SET\s+(LOCAL\s+)?lock_timeout\b", re.I)
_CREATE_TABLE = re.compile(r"CREATE\s+(?:UNLOGGED\s+|TEMP(?:ORARY)?\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?" + _NAME, re.I)
_CREATE_INDEX = re.compile(
    r"^CREATE\s+(?:UNIQUE\s+)?INDEX\s+(CONCURRENTLY\s+)?(?:IF\s+NOT\s+EXISTS\s+)?(?:\S+\s+)?ON\s+(?:ONLY\s+)?" + _NAME,
    re.I,
)
_DROP_INDEX = re.compile(r"^DROP\s+INDEX\s+(CONCURRENTLY\b)?", re.I)
_REINDEX = re.compile(r"^REINDEX\b.*\bCONCURRENTLY\b", re.I | re.S)
_ALTER_TABLE = re.compile(r"^ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?" + _NAME + r"\s+(.*)$", re.I | re.S)
_ADD_COLUMN = re.compile(r"\bADD\s+(?:COLUMN\s+)?(?:IF\s+NOT\s+EXISTS\s+)?(?!CONSTRAINT\b|PRIMARY\b|UNIQUE\b|CHECK\b|FOREIGN\b|EXCLUDE\b)(\w+)\s+([^,]*)", re.I)
_COLUMN_TYPE = re.compile(r"\bALTER\s+(?:COLUMN\s+)?\w+\s+(?:SET\s+DATA\s+)?TYPE\b", re.I)
_VALIDATE_ONLY = re.compile(r"^VALIDATE\s+CONSTRAINT\s+\w+\s*$", re.I)
_ALTER_TYPE = re.compile(r"^ALTER\s+TYPE\s+\S+\s+(.*)$", re.I | re.S)
_UPDATE = re.compile(r"^UPDATE\s+(?:ONLY\s+)?" + _NAME + r"(?:\s+(?:AS\s+)?\w+)?\s+SET\s+(.*)$", re.I | re.S)
_TRIGGER = re.compile(r"^(?:CREATE|DROP)\s+(?:OR\s+REPLACE\s+)?TRIGGER\b.*?\bON\s+" + _NAME, re.I | re.S)
_LOCK = re.compile(r"^LOCK\s+(?:TABLE\s+)?(?:ONLY\s+)?" + _NAME, re.I)
_WHERE = re.compile(r"\bWHERE\b", re.I)
_LIMIT = re.compile(r"\bLIMIT\b", re.I)
_BIND = re.compile(r"(?<!:):\w+|%\(\w+\)s")


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: [{self.rule}] {self.message}"


@dataclass
class _State:
    autocommit: bool = False
    #: SET LOCAL lock_timeout in this transaction.
    lock_timeout: bool = False
    #: SET lock_timeout (the session's): holds for the rest of the run.
    session_lock_timeout: bool = False
    #: Inside a loop (a batch loop, when it runs UPDATE ... LIMIT).
    loop: bool = False


def _snippet(sql: str) -> str:
    one = " ".join(sql.split())
    return one if len(one) <= 90 else one[:87] + "..."


def _statements(sql: str) -> list[str]:
    """The statements in `sql`, comments stripped. A `;` inside a $$ body
    (a function) is not a separator."""
    text = _COMMENTS.sub(" ", sql)
    out, depth, start = [], 0, 0
    for match in re.finditer(r"\$\$|;", text):
        if match.group() == "$$":
            depth ^= 1
        elif not depth:
            out.append(text[start:match.start()])
            start = match.end()
    out.append(text[start:])
    return [s.strip() for s in out if s.strip()]


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    if isinstance(node, ast.Call):
        return f"{_dotted(node.func)}()"
    return "?"


def _kw(call: ast.Call, name: str) -> Optional[ast.AST]:
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


def _arg(call: ast.Call, index: int, name: str) -> Optional[ast.AST]:
    if len(call.args) > index:
        return call.args[index]
    return _kw(call, name)


def _is_true(node: Optional[ast.AST]) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _is_false(node: Optional[ast.AST]) -> bool:
    return isinstance(node, ast.Constant) and node.value is False


class _Migration:
    """One migration file, read."""

    def __init__(self, path: Path, hot: frozenset):
        self.path = path
        self.source = path.read_text()
        self.lines = self.source.splitlines()
        self.tree = ast.parse(self.source, filename=str(path))
        self.hot = hot
        self.findings: list[Finding] = []
        self.constants: dict[str, object] = {}
        self.functions: dict[str, ast.FunctionDef] = {}
        for node in self.tree.body:
            if isinstance(node, ast.FunctionDef):
                self.functions[node.name] = node
            targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
            value = getattr(node, "value", None)
            for target in targets:
                if isinstance(target, ast.Name) and value is not None:
                    try:
                        self.constants[target.id] = ast.literal_eval(value)
                    except (ValueError, SyntaxError, TypeError):
                        pass
        self.revision = self.constants.get("revision")
        down = self.constants.get("down_revision")
        self.down = tuple(down) if isinstance(down, (tuple, list)) else (down,) if down else ()
        self.created = self._created_tables()

    # -- what the file says --------------------------------------------------

    def _created_tables(self) -> set[str]:
        created = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call) and _dotted(node.func).endswith("create_table") and node.args:
                if isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    created.add(node.args[0].value)
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                created.update(m.group(1) for m in _CREATE_TABLE.finditer(node.value))
        return created

    def _suppressed(self, line: int, rule: str) -> bool:
        for number in (line, line - 1):
            if 1 <= number <= len(self.lines):
                match = _SUPPRESS.search(self.lines[number - 1])
                # The rules named before the "--" that starts the reason.
                if match and rule in re.split(r"[,\s]+", match.group(1).split("--")[0].strip()):
                    return True
        return False

    def _flag(self, line: int, rule: str, message: str) -> None:
        if self._suppressed(line, rule):
            return
        finding = Finding(str(self.path), line, rule, message)
        if finding not in self.findings:
            self.findings.append(finding)

    def _is_hot(self, table: Optional[str]) -> bool:
        return bool(table) and table in self.hot and table not in self.created

    # -- resolving values ----------------------------------------------------

    def _str(self, node: Optional[ast.AST], env: dict) -> Optional[str]:
        if node is None:
            return None
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            value = env.get(node.id, self.constants.get(node.id))
            return value if isinstance(value, str) else None
        if isinstance(node, ast.JoinedStr):
            parts = []
            for value in node.values:
                if isinstance(value, ast.Constant):
                    parts.append(str(value.value))
                else:
                    parts.append(self._str(getattr(value, "value", None), env) or "?")
            return "".join(parts)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = self._str(node.left, env), self._str(node.right, env)
            return left + right if left is not None and right is not None else None
        if isinstance(node, ast.Call):
            func = _dotted(node.func)
            if func.split(".")[-1] == "text" and node.args:
                return self._str(node.args[0], env)
            if isinstance(node.func, ast.Attribute) and node.func.attr == "format":
                base = self._str(node.func.value, env)
                return re.sub(r"\{[^}]*\}", "?", base) if base is not None else None
        return None

    def _iterable(self, node: ast.AST, env: dict) -> Optional[list]:
        if isinstance(node, ast.Name):
            value = env.get(node.id, self.constants.get(node.id))
        elif isinstance(node, ast.Call) and _dotted(node.func) in ("reversed", "list", "tuple", "sorted") and node.args:
            return self._iterable(node.args[0], env)
        else:
            try:
                value = ast.literal_eval(node)
            except (ValueError, SyntaxError, TypeError):
                return None
        return list(value)[:500] if isinstance(value, (list, tuple)) else None

    @staticmethod
    def _bind(target: ast.AST, item) -> dict:
        if isinstance(target, ast.Name):
            return {target.id: item}
        if isinstance(target, (ast.Tuple, ast.List)) and isinstance(item, (tuple, list)) and len(item) == len(target.elts):
            out = {}
            for element, value in zip(target.elts, item):
                out.update(_Migration._bind(element, value))
            return out
        return {}

    # -- walking upgrade() -----------------------------------------------------

    def check(self) -> list[Finding]:
        upgrade = self.functions.get("upgrade")
        if upgrade is not None:
            self._body(upgrade.body, {}, _State(), depth=0)
        return self.findings

    def _body(self, statements: Iterable[ast.stmt], env: dict, state: _State, depth: int) -> None:
        for statement in statements:
            self._statement(statement, env, state, depth)

    def _statement(self, node: ast.stmt, env: dict, state: _State, depth: int) -> None:
        if isinstance(node, (ast.With, ast.AsyncWith)):
            autocommit = any(
                isinstance(item.context_expr, ast.Call) and _dotted(item.context_expr.func).endswith("autocommit_block")
                for item in node.items
            )
            for item in node.items:
                if not autocommit:
                    self._expression(item.context_expr, env, state, depth)
            if autocommit:
                self._body(node.body, env, replace(state, autocommit=True, lock_timeout=False), depth)
                # The block committed what came before it and begins a new
                # transaction after it: a SET LOCAL from before is gone.
                state.lock_timeout = False
            else:
                self._body(node.body, env, state, depth)
            return
        if isinstance(node, (ast.For, ast.AsyncFor)):
            items = self._iterable(node.iter, env)
            loop_state = replace(state, loop=True)
            if items is None:
                self._body(node.body, env, loop_state, depth)
            else:
                for item in items:
                    self._body(node.body, {**env, **self._bind(node.target, item)}, loop_state, depth)
            state.lock_timeout = state.lock_timeout or loop_state.lock_timeout
            state.session_lock_timeout = state.session_lock_timeout or loop_state.session_lock_timeout
            self._body(node.orelse, env, state, depth)
            return
        if isinstance(node, ast.While):
            self._expression(node.test, env, replace(state, loop=True), depth)
            self._body(node.body, env, replace(state, loop=True), depth)
            return
        if isinstance(node, ast.If):
            self._expression(node.test, env, state, depth)
            self._body(node.body, env, state, depth)
            self._body(node.orelse, env, state, depth)
            return
        if isinstance(node, ast.Try):
            for part in (node.body, *[h.body for h in node.handlers], node.orelse, node.finalbody):
                self._body(part, env, state, depth)
            return
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return
        self._expression(node, env, state, depth)

    def _expression(self, node: ast.AST, env: dict, state: _State, depth: int) -> None:
        calls = [n for n in ast.walk(node) if isinstance(n, ast.Call)]
        for call in sorted(calls, key=lambda c: (c.lineno, c.col_offset)):
            self._call(call, env, state, depth)

    def _call(self, call: ast.Call, env: dict, state: _State, depth: int) -> None:
        func = call.func
        name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else None
        dotted = _dotted(func)
        if name in ("execute", "exec_driver_sql") and call.args:
            sql = self._str(call.args[0], env)
            if sql is not None:
                for statement in _statements(sql):
                    self._sql(statement, call.lineno, state)
            return
        if dotted.startswith("op.") and name:
            self._op(name, call, env, state)
            return
        if isinstance(func, ast.Name) and func.id in self.functions and depth < 6:
            helper = self.functions[func.id]
            params = [a.arg for a in helper.args.args]
            bound = {p: self._str(a, env) for p, a in zip(params, call.args)}
            self._body(helper.body, {k: v for k, v in bound.items() if v is not None}, state, depth + 1)

    # -- the rules -------------------------------------------------------------

    def _ddl(self, table: Optional[str], line: int, state: _State, what: str) -> None:
        if not self._is_hot(table) or state.autocommit or state.lock_timeout or state.session_lock_timeout:
            return
        self._flag(
            line, "missing-lock-timeout",
            f"{what} on hot table {table} with no `SET LOCAL lock_timeout = '3s'` before it in this transaction",
        )

    def _index(self, table: Optional[str], concurrently: bool, line: int, state: _State, what: str) -> None:
        if concurrently:
            if not state.autocommit:
                self._flag(line, "concurrently-in-transaction", f"{what} CONCURRENTLY outside an autocommit block: Postgres refuses it in a transaction")
            return
        if self._is_hot(table):
            self._flag(line, "index-not-concurrent", f"{what} on hot table {table} without CONCURRENTLY blocks every write to it while it builds")
            self._ddl(table, line, state, what)

    def _sql(self, sql: str, line: int, state: _State) -> None:
        if _SET_LOCK.match(sql):
            if _SET_LOCK.match(sql).group(1):
                state.lock_timeout = True
            else:
                state.session_lock_timeout = True
            return
        match = _CREATE_INDEX.match(sql)
        if match:
            self._index(match.group(2), bool(match.group(1)), line, state, "CREATE INDEX")
            return
        match = _DROP_INDEX.match(sql)
        if match:
            if match.group(1) and not state.autocommit:
                self._flag(line, "concurrently-in-transaction", "DROP INDEX CONCURRENTLY outside an autocommit block: Postgres refuses it in a transaction")
            return
        if _REINDEX.match(sql) and not state.autocommit:
            self._flag(line, "concurrently-in-transaction", "REINDEX CONCURRENTLY outside an autocommit block: Postgres refuses it in a transaction")
            return
        match = _ALTER_TABLE.match(sql)
        if match:
            table, rest = match.group(1), match.group(2)
            if _VALIDATE_ONLY.match(rest.strip()):
                return  # SHARE UPDATE EXCLUSIVE: blocks no reads or writes
            if table not in self.created:
                for column in _ADD_COLUMN.finditer(rest):
                    clause = column.group(2)
                    if re.search(r"\bNOT\s+NULL\b", clause, re.I) and not re.search(r"\bDEFAULT\b", clause, re.I):
                        self._flag(line, "not-null-without-default", f"ADD COLUMN {column.group(1)} NOT NULL without a DEFAULT on {table}: fails on the first existing row")
                if _COLUMN_TYPE.search(rest):
                    self._flag(line, "alter-type", f"column type change on {table} rewrites the table under ACCESS EXCLUSIVE: {_snippet(sql)}")
            self._ddl(table, line, state, "ALTER TABLE")
            return
        match = _ALTER_TYPE.match(sql)
        if match:
            if not re.match(r"ADD\s+VALUE\b|RENAME\b", match.group(1).strip(), re.I):
                self._flag(line, "alter-type", f"ALTER TYPE: {_snippet(sql)}")
            return
        match = _UPDATE.match(sql)
        if match:
            self._update(match.group(1), match.group(2), sql, line, state)
            return
        match = _TRIGGER.match(sql) or _LOCK.match(sql)
        if match:
            self._ddl(match.group(1), line, state, sql.split()[0].upper() + (" TRIGGER" if "TRIGGER" in sql.upper()[:40] else " TABLE"))

    def _update(self, table: str, rest: str, sql: str, line: int, state: _State) -> None:
        if table in self.created:
            return
        if not _WHERE.search(rest):
            self._flag(line, "full-table-update", f"UPDATE of every row of {table} in one statement; do it in batches that commit: {_snippet(sql)}")
            return
        if table not in self.hot:
            return
        if _LIMIT.search(rest):
            if not state.autocommit:
                self._flag(line, "full-table-update", f"batched UPDATE of hot table {table} inside a transaction: the batches only commit together; run them in an autocommit block")
            return
        if not _BIND.search(rest):
            self._flag(line, "full-table-update", f"UPDATE of hot table {table} not in batches (no LIMIT, no bound range): {_snippet(sql)}")

    def _op(self, name: str, call: ast.Call, env: dict, state: _State) -> None:
        line = call.lineno
        table = lambda i, kw="table_name": self._str(_arg(call, i, kw), env)  # noqa: E731
        if name == "create_index":
            concurrently = _is_true(_kw(call, "postgresql_concurrently"))
            self._index(table(1), concurrently, line, state, "create_index")
        elif name == "drop_index":
            concurrently = _is_true(_kw(call, "postgresql_concurrently"))
            if concurrently:
                if not state.autocommit:
                    self._flag(line, "concurrently-in-transaction", "drop_index CONCURRENTLY outside an autocommit block: Postgres refuses it in a transaction")
            else:
                self._ddl(table(1), line, state, "drop_index")
        elif name == "add_column":
            target = table(0)
            column = _arg(call, 1, "column")
            if isinstance(column, ast.Call) and target not in self.created:
                not_null = _is_false(_kw(column, "nullable")) or _is_true(_kw(column, "primary_key"))
                if not_null and _kw(column, "server_default") is None:
                    column_name = self._str(column.args[0], env) if column.args else "?"
                    self._flag(line, "not-null-without-default", f"add_column {column_name} nullable=False without server_default on {target}: fails on the first existing row")
            self._ddl(target, line, state, "add_column")
        elif name == "alter_column":
            target = table(0)
            if _kw(call, "type_") is not None and target not in self.created:
                self._flag(line, "alter-type", f"alter_column type_= on {target} rewrites the table under ACCESS EXCLUSIVE")
            self._ddl(target, line, state, "alter_column")
        elif name in ("drop_column", "rename_table", "drop_table"):
            self._ddl(table(0), line, state, name)
        elif name in ("create_unique_constraint", "create_check_constraint", "create_primary_key", "create_exclude_constraint"):
            self._ddl(table(1, "table_name"), line, state, name)
        elif name == "drop_constraint":
            self._ddl(table(1, "table_name"), line, state, name)
        elif name == "create_foreign_key":
            self._ddl(table(1, "source_table"), line, state, name)
            self._ddl(table(2, "referent_table"), line, state, name)
        elif name == "batch_alter_table":
            self._ddl(table(0), line, state, name)
        elif name == "execute" and call.args:  # pragma: no cover -- handled in _call
            pass


# ---------------------------------------------------------------------------
# Which files
# ---------------------------------------------------------------------------


def _load(versions: Path, hot: frozenset) -> list[_Migration]:
    return [_Migration(path, hot) for path in sorted(versions.glob("*.py")) if not path.name.startswith("__")]


def grandfathered(migrations: list[_Migration], since: Optional[str]) -> set:
    """`since` and every revision before it: the ones not checked."""
    if not since:
        return set()
    parents = {m.revision: m.down for m in migrations}
    if since not in parents:
        raise SystemExit(f"cutoff revision {since!r} is not in the versions folder")
    seen, todo = set(), [since]
    while todo:
        revision = todo.pop()
        if revision in seen or revision is None:
            continue
        seen.add(revision)
        todo.extend(parents.get(revision, ()))
    return seen


def check(versions: Path = VERSIONS, since: Optional[str] = CUTOFF, hot: Iterable[str] = ()) -> tuple[list[Finding], list[str]]:
    """Every finding in the migrations after `since` (all, when None), and
    the revisions that were read."""
    tables = HOT_TABLES | frozenset(hot)
    migrations = _load(Path(versions), tables)
    old = grandfathered(migrations, since)
    findings, checked = [], []
    for migration in migrations:
        if migration.revision in old:
            continue
        checked.append(str(migration.revision))
        findings.extend(migration.check())
    return findings, checked


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("versions", nargs="?", default=str(VERSIONS), help="the migrations folder")
    parser.add_argument("--since", default=CUTOFF, help=f"check only after this revision (default {CUTOFF}); 'none' for all")
    parser.add_argument("--hot", action="append", default=[], help="another hot table (repeatable)")
    args = parser.parse_args(argv)
    since = None if str(args.since).lower() in ("none", "", "all") else args.since
    findings, checked = check(Path(args.versions), since, args.hot)
    for finding in findings:
        print(finding)
    scope = f"after {since}" if since else "all"
    print(f"{len(findings)} finding(s) in {len(checked)} migration(s) checked ({scope}).", file=sys.stderr)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
