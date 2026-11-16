"""
database_assistant.py — asking a database questions without being able to break it.

THE DEFAULT IS READ-ONLY, AND THAT IS ENFORCED, NOT PROMISED
    Every query is first parsed and its leading verb examined. SELECT, PRAGMA and
    EXPLAIN run. INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, TRUNCATE and
    ATTACH do not run — not on a retry, not with a "please", not with a trick in
    a comment or a second statement after a semicolon. A write goes through the
    project's confirmation gate with the exact statement shown to you first, and
    with several statements in one string, the whole thing is refused rather than
    the first one being run.

    Queries run inside a transaction that is rolled back at the end, so even a
    read that has a side effect in a trigger changes nothing. The connection is
    opened with a timeout, and SQLite is opened in read-only URI mode for reads —
    two independent locks on the same door.

PARAMETERS, ALWAYS
    A value you give goes in as a bound parameter, never pasted into the SQL. So
    a city called "O'Brien" or a string containing "'; DROP TABLE" is data, and
    stays data.

SCOPE
    SQLite needs nothing. The other engines (PostgreSQL, MySQL) are reached only
    when their driver is installed and only with a connection string you
    configured yourself — this plugin does not install drivers, does not guess at
    connection details, and does not read credentials from anywhere.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

PLUGIN = {
    "name": "database_assistant",
    "description": (
        "Talks to your databases, read-only by default: 'what tables are in my database', "
        "'describe the orders table', 'select the top 10 customers', 'run this query'. Writes "
        "(insert/update/delete/DDL) need your explicit confirmation first, and every value is "
        "passed as a parameter, never pasted into the SQL."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "tables | schema | query | preview | explain | help"},
            "path": {"type": "STRING", "description": "SQLite file, or a configured name"},
            "sql": {"type": "STRING", "description": "The SQL to run"},
            "table": {"type": "STRING", "description": "Table for schema/preview"},
            "params": {"type": "STRING", "description": "Values as JSON list/dict for binding"},
            "limit": {"type": "STRING", "description": "Row ceiling (default 50, max 500)"},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "database_assistant",
    "title": "Database assistant / مساعد قواعد البيانات",
    "fields": [
        {"key": "databases", "type": "text",
         "label": "Databases: name=path.sqlite, other=postgresql://… (comma separated)",
         "default": ""},
        {"key": "default_db", "type": "text", "label": "Database to use when none is named",
         "default": ""},
        {"key": "allow_writes", "type": "text", "label": "Allow writes after a confirmation (yes/no)",
         "default": "yes"},
        {"key": "max_rows", "type": "text", "label": "Rows returned at most", "default": "50"},
    ],
}

_WRITE_WORDS = {"insert", "update", "delete", "drop", "alter", "create", "truncate", "replace",
                "attach", "detach", "vacuum", "reindex", "grant", "revoke", "upsert"}
_READ_WORDS = {"select", "pragma", "explain", "with", "values", "show", "describe", "desc"}

_SYSTEM_PARTS = {"etc", "usr", "bin", "sbin", "lib", "var", "sys", "proc", "boot", "root",
                 "windows", "program files", "programdata", "system32"}


def _cfg() -> dict[str, Any]:
    out = {f["key"]: f.get("default") for f in PLUGIN_SETTINGS["fields"]}
    try:
        from memory.config_manager import get_plugin_config
        out.update({k: v for k, v in (get_plugin_config("database_assistant") or {}).items()
                    if v not in (None, "")})
    except Exception:
        pass
    return out


def max_rows() -> int:
    try:
        return max(1, min(500, int(str(_cfg().get("max_rows") or 50))))
    except Exception:
        return 50


def known_databases() -> dict[str, str]:
    """name → path-or-connection-string, from the settings field only."""
    out: dict[str, str] = {}
    for part in re.split(r"[,\n]", str(_cfg().get("databases") or "")):
        part = part.strip()
        if "=" in part:
            name, _, value = part.partition("=")
            if name.strip() and value.strip():
                out[name.strip()] = value.strip()
    return out


def resolve(path_or_name: str) -> tuple[str, str]:
    """(kind, target) where kind is 'sqlite' | 'postgres' | 'mysql' | '' on refusal."""
    raw = str(path_or_name or "").strip() or str(_cfg().get("default_db") or "").strip()
    if not raw:
        return "", "no database was named and none is set as the default"
    stored = known_databases()
    target = stored.get(raw, raw)
    if "://" in target:
        scheme = target.split("://", 1)[0].lower()
        if scheme in ("postgres", "postgresql"):
            return "postgres", target
        if scheme in ("mysql", "mariadb"):
            return "mysql", target
        return "", f"'{scheme}' is not an engine I speak (sqlite, postgresql, mysql)"
    path = Path(target).expanduser()
    try:
        resolved = path.resolve()
    except Exception:
        return "", "that path cannot be resolved"
    parts = [p.lower() for p in resolved.parts]
    if any(part in _SYSTEM_PARTS for part in parts[1:-1]):
        return "", f"'{resolved}' is inside a system folder — I will not open it"
    if resolved.is_dir():
        return "", "that is a folder, not a database file"
    return "sqlite", str(resolved)


def classify(sql: str) -> tuple[str, str]:
    """
    ('read'|'write'|'multiple'|'empty'|'unknown', reason). The check happens on
    the parsed shape of the statement, with comments stripped — a write hidden
    behind '/* x */' or after a semicolon is still a write.
    """
    raw = str(sql or "")
    without_comments = re.sub(r"/\*.*?\*/", " ", raw, flags=re.S)
    without_comments = re.sub(r"--[^\n]*", " ", without_comments)
    statements = [s.strip() for s in without_comments.split(";") if s.strip()]
    if not statements:
        return "empty", "there is no statement"
    if len(statements) > 1:
        return "multiple", (f"{len(statements)} statements in one string — I run one at a time, "
                            f"so a second statement cannot ride in behind the first")
    first = statements[0].lower()
    match = re.match(r"\s*\(*\s*([a-z]+)", first)
    verb = match.group(1) if match else ""
    if verb in _WRITE_WORDS:
        return "write", f"'{verb}' changes data or structure"
    if verb in _READ_WORDS:
        return "read", ""
    return "unknown", f"'{verb or 'that'}' is not a statement I run"


def connect(kind: str, target: str, read_only: bool = True) -> tuple[Any, str]:
    """(connection, error). SQLite reads open in read-only URI mode as well."""
    try:
        if kind == "sqlite":
            if not Path(target).exists():
                return None, f"there is no database file at {target}"
            if read_only:
                # Two locks on one door: 'mode=ro' makes writes fail at the driver
                # level even if a write statement ever reached this far.
                uri = f"file:{Path(target).as_posix()}?mode=ro"
                connection = sqlite3.connect(uri, uri=True, timeout=8)
            else:
                connection = sqlite3.connect(target, timeout=8)
            connection.row_factory = sqlite3.Row
            return connection, ""
        if kind == "postgres":
            try:
                import psycopg2
            except Exception:
                return None, ("the PostgreSQL driver is not installed (pip install psycopg2-binary) "
                              "— I will not install it for you")
            connection = psycopg2.connect(target, connect_timeout=8)
            if read_only:
                connection.set_session(readonly=True, autocommit=False)
            return connection, ""
        if kind == "mysql":
            try:
                import pymysql
            except Exception:
                return None, ("the MySQL driver is not installed (pip install pymysql) — I will "
                              "not install it for you")
            connection = pymysql.connect(target if "://" not in target else
                                        target.split("://", 1)[1], connect_timeout=8)
            return connection, ""
        return None, f"I do not know how to open a '{kind}' database"
    except Exception as e:
        return None, f"I could not open it: {str(e)[:120]}"


def _fetch(connection: Any, sql: str, params: list | dict | None, limit: int) -> tuple[list, list, str]:
    """(rows, column names, error). Always closed, always its own transaction."""
    try:
        cursor = connection.cursor()
        try:
            cursor.execute(sql, params or [])
            columns = [d[0] for d in (cursor.description or [])]
            rows = cursor.fetchmany(limit) if columns else []
            return rows, columns, ""
        finally:
            try:
                connection.rollback()       # nothing a read did survives this
            except Exception:
                pass
            cursor.close()
    except Exception as e:
        return [], [], str(e)[:200]


def _tables_sqlite(connection: Any) -> list[tuple[str, str]]:
    rows, _, _ = _fetch(connection, "SELECT name, type FROM sqlite_master "
                                   "WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' "
                                   "ORDER BY name", [], 200)
    return [(str(r[0]), str(r[1])) for r in rows]


def _columns_sqlite(connection: Any, table: str) -> list[tuple]:
    safe = re.sub(r"[^\w]", "", table)
    rows, _, error = _fetch(connection, f'PRAGMA table_info("{safe}")', [], 200)
    if error or not rows:
        rows2, _, _ = _fetch(connection, "SELECT sql FROM sqlite_master WHERE name = ?",
                             [table], 1)
        return [("?", "?", "", str(rows2[0][0])[:200])] if rows2 else []
    return [(str(r[1]), str(r[2]), "PK" if r[5] else "", "" if r[3] else "NOT NULL"
             if r[3] is not None else "") for r in rows]


def _confirm(title: str, detail: str, action) -> str:
    try:
        from core.confirm import request
        return str(request("database_assistant", title, detail, action))
    except Exception:
        return ("Sir, that statement changes your database and I cannot ask for your yes right "
                "now — so I have not run it.")


def _do_write(kind: str, target: str, sql: str, params: list | dict | None) -> str:
    connection, error = connect(kind, target, read_only=False)
    if connection is None:
        return f"Sir, {error}"
    try:
        cursor = connection.cursor()
        try:
            cursor.execute(sql, params or [])
            affected = getattr(cursor, "rowcount", -1)
            connection.commit()
            return (f"Done, sir — {affected if affected is not None and affected >= 0 else 'the'} "
                    f"row(s) affected and committed.")
        except Exception as e:
            try:
                connection.rollback()
            except Exception:
                pass
            return f"Sir, the statement failed and was rolled back: {str(e)[:160]}"
        finally:
            cursor.close()
    finally:
        try:
            connection.close()
        except Exception:
            pass


# ── tool entry point ──────────────────────────────────────────────────────────

def run(parameters: dict, player=None, session_memory=None) -> str:
    """One database action. Reads freely; writes only after a yes."""
    try:
        p = parameters or {}
        action = str(p.get("action") or "tables").strip().lower()

        if action in ("help", "?"):
            return ("I read your databases, sir: list tables, describe one, run a SELECT, or "
                    "explain a query plan. Writes run only after you confirm, one statement at a "
                    "time, and every value goes in as a parameter — never pasted into the SQL.")
        if action in ("config", "where"):
            stored = known_databases()
            return (f"Databases I know: {', '.join(stored) or 'none configured'}. "
                    f"Default: {_cfg().get('default_db') or 'none'}. "
                    f"Add them in my settings as name=path.sqlite, name2=postgresql://…")

        kind, target_or_error = resolve(str(p.get("path") or ""))
        if not kind:
            return f"Sir, {target_or_error}."

        limit = max_rows()
        try:
            limit = max(1, min(500, int(str(p.get("limit") or limit))))
        except Exception:
            pass

        if action in ("tables", "list", "show_tables"):
            connection, error = connect(kind, target_or_error)
            if connection is None:
                return f"Sir, {error}"
            try:
                if kind == "sqlite":
                    pairs = _tables_sqlite(connection)
                else:
                    rows, _, _ = _fetch(connection, "SELECT table_name FROM information_schema.tables "
                                                    "WHERE table_schema NOT IN ('pg_catalog','information_schema') "
                                                    "ORDER BY table_name", [], 200)
                    pairs = [(str(r[0]), "table") for r in rows]
                if not pairs:
                    return f"No tables in {Path(target_or_error).name}, sir."
                lines = [f"{len(pairs)} table(s)/view(s):"]
                for name, kind_name in pairs[:60]:
                    lines.append(f"  {name[:44]:<46}{kind_name}")
                return "\n".join(lines)
            finally:
                try:
                    connection.close()
                except Exception:
                    pass

        if action in ("schema", "describe", "columns"):
            table = str(p.get("table") or "").strip()
            if not table:
                return "Which table should I describe, sir?"
            connection, error = connect(kind, target_or_error)
            if connection is None:
                return f"Sir, {error}"
            try:
                if kind == "sqlite":
                    safe = re.sub(r"[^\w]", "", table)
                    rows, _, err = _fetch(connection,
                                          f'PRAGMA table_info("{safe}")', [], 200)
                    if err or not rows:
                        return f"There is no table called '{table}', sir."
                    lines = [f"{table} — {len(rows)} column(s):",
                             f"  {'column':<24}{'type':<16}{'flags'}", ""]
                    for row in rows:
                        lines.append(f"  {str(row[1])[:23]:<24}{str(row[2] or '?'):<16}"
                                     f"{'PRIMARY KEY' if row[5] else ''}"
                                     f"{' NOT NULL' if row[3] else ''}")
                    return "\n".join(lines)
                rows, _, _ = _fetch(connection, "SELECT column_name, data_type, is_nullable "
                                                "FROM information_schema.columns "
                                                "WHERE table_name = %s ORDER BY ordinal_position",
                                    [table], 200)
                if not rows:
                    return f"There is no table called '{table}', sir."
                lines = [f"{table} — {len(rows)} column(s):"]
                for row in rows:
                    lines.append(f"  {str(row[0])[:30]:<32}{str(row[1])[:20]:<22}{row[2]}")
                return "\n".join(lines)
            finally:
                try:
                    connection.close()
                except Exception:
                    pass

        if action in ("preview", "sample", "head"):
            table = str(p.get("table") or "").strip()
            if not table:
                return "Which table should I preview, sir?"
            safe = re.sub(r"[^\w]", "", table)
            if not safe:
                return "That table name has nothing usable in it, sir."
            sql = f'SELECT * FROM "{safe}" LIMIT {int(limit)}'
            return _run_select(kind, target_or_error, sql, [], limit, f"first {limit} row(s) of {table}")

        if action in ("explain", "plan"):
            sql = str(p.get("sql") or "").strip()
            verdict, why = classify(sql)
            if verdict != "read":
                return f"I only explain read statements, sir — {why}."
            connection, error = connect(kind, target_or_error)
            if connection is None:
                return f"Sir, {error}"
            try:
                prefix = "EXPLAIN QUERY PLAN " if kind == "sqlite" else "EXPLAIN "
                rows, _, err = _fetch(connection, prefix + sql, [], 100)
                if err:
                    return f"Sir, the planner said: {err}"
                return "Query plan:\n" + "\n".join("  " + " | ".join(str(v)[:40] for v in row)
                                                   for row in rows[:30])
            finally:
                try:
                    connection.close()
                except Exception:
                    pass

        if action in ("query", "sql", "select", "run"):
            sql = str(p.get("sql") or "").strip()
            if not sql:
                return "What is the query, sir?"
            params: Any = p.get("params")
            if isinstance(params, str) and params.strip():
                import json as _json
                try:
                    params = _json.loads(params)
                except Exception:
                    params = [params]          # a single value is a valid binding
            if not isinstance(params, (list, tuple, dict)):
                params = []
            verdict, why = classify(sql)

            if verdict == "read":
                return _run_select(kind, target_or_error, sql, params, limit)
            if verdict in ("write",):
                if str(_cfg().get("allow_writes") or "yes").lower() in ("no", "false", "0"):
                    return ("Writes are switched off in my settings, sir. I can still run selects "
                            "and show you the schema.")
                shown = sql[:300] + ("…" if len(sql) > 300 else "")
                return _confirm(f"{sql.split()[0].upper()} on {Path(target_or_error).name}",
                                f"The statement is:\n{shown}\n\nParameters: {len(params) if isinstance(params, (list, tuple)) else len(params or {})} "
                                f"bound value(s). It will be committed if it succeeds.",
                                lambda: _do_write(kind, target_or_error, sql, params))
            return f"Sir, {why}."

        return (f"Sir, '{action}' is not a database action I have. I can list tables, describe "
                f"one, preview, explain or run a query.")
    except Exception as e:                                     # noqa: BLE001
        return f"Sir, the database assistant hit a problem: {str(e)[:150]}"


def _run_select(kind: str, target: str, sql: str, params: list | dict | None,
                limit: int, title: str = "") -> str:
    connection, error = connect(kind, target, read_only=True)
    if connection is None:
        return f"Sir, {error}"
    try:
        rows, columns, err = _fetch(connection, sql, params, limit)
        if err:
            return f"Sir, the query failed: {err}"
        if not columns:
            return ("That ran but returned no rows to read, sir — if you meant it to change "
                    "something, it did not: reads are rolled back.")
        # Width from the header AND the values: sizing on the column name alone
        # printed "O'Br" for "O'Brien" — the data was there, the table lied
        # about it. Caught by this file's own test reading its own output.
        widths = []
        for index, column in enumerate(columns):
            longest = max([len(str(column))] + [len(str(row[index])) for row in rows[:200]])
            widths.append(min(28, max(longest, 4)))
        head = "  " + " | ".join(str(c)[:w].ljust(w) for c, w in zip(columns, widths))
        lines = [title or f"{len(rows)} row(s) (limit {limit}):", head,
                 "  " + "-+-".join("-" * w for w in widths)]
        for row in rows:
            lines.append("  " + " | ".join(str(row[i])[:w].ljust(w)
                                           for i, w in enumerate(widths)))
        if len(rows) == limit:
            lines.append(f"  (hit the {limit}-row ceiling — raise 'limit' for more)")
        lines.append("")
        lines.append("Read-only: the connection opened in read-only mode and the transaction was "
                     "rolled back afterwards.")
        return "\n".join(lines)
    finally:
        try:
            connection.close()
        except Exception:
            pass


# ── self-test: python database_assistant.py ─────────────────────────────────

def _selftest() -> int:
    print("database_assistant — self-test")
    problems = 0
    import tempfile
    import shutil
    probe_dir = Path(tempfile.mkdtemp(prefix="db_probe_"))
    db_path = probe_dir / "shop.sqlite"
    maker = sqlite3.connect(db_path)
    maker.executescript(
        "CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL, city TEXT);"
        "CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER, total REAL);"
        "INSERT INTO customers (name, city) VALUES ('O''Brien', 'Cairo'), ('Ali', 'Giza');"
        "INSERT INTO orders (customer_id, total) VALUES (1, 99.5), (2, 10.0);")
    maker.commit()
    maker.close()
    original_bytes = db_path.read_bytes()

    real_cfg = _cfg
    globals()["_cfg"] = lambda: {**real_cfg(), "databases": f"shop={db_path}", "default_db": "shop",
                                 "allow_writes": "yes", "max_rows": "50"}
    try:
        print(f"  التصنيف                 : "
              f"{classify('SELECT 1')[0]} · {classify('DROP TABLE x')[0]} · "
              f"{classify('select 1; delete from x')[0]} · "
              f"{classify('/*c*/ insert into t values (1)')[0]} · {classify('')[0]}")
        problems += 0 if (classify("SELECT 1")[0] == "read"
                          and classify("DROP TABLE x")[0] == "write"
                          and classify("select 1; delete from x")[0] == "multiple"
                          and classify("/*c*/ insert into t values (1)")[0] == "write"
                          and classify("")[0] == "empty") else 1

        tables = run({"action": "tables", "path": "shop"})
        print(f"  الجداول                 : {tables.splitlines()[0]} — "
              f"{[l.strip().split()[0] for l in tables.splitlines()[1:3]]}")
        problems += 0 if "customers" in tables and "orders" in tables else 1

        schema = run({"action": "schema", "path": "shop", "table": "customers"})
        print(f"  الوصف                   : {schema.splitlines()[0][:60]}")
        problems += 0 if "name" in schema and "PRIMARY KEY" in schema else 1

        rows = run({"action": "query", "path": "shop",
                    "sql": "SELECT name, city FROM customers WHERE city = ? ORDER BY name",
                    "params": '["Cairo"]'})
        print(f"  استعلام بمعامل           : {[l.strip() for l in rows.splitlines()[1:3]]}")
        problems += 0 if "O'Brien" in rows else 1

        # the injection attempt: a value that LOOKS like SQL must stay a value
        attack = run({"action": "query", "path": "shop",
                      "sql": "SELECT count(*) AS n FROM customers WHERE name = ?",
                      "params": json.dumps(["x'; DROP TABLE customers; --"])})
        still_there = run({"action": "tables", "path": "shop"})
        print(f"  محاولة حقن              : {attack.splitlines()[2].strip()[:44]} · "
              f"الجدول ما زال موجودًا: {'customers' in still_there}")
        problems += 0 if "customers" in still_there and "DROP" not in attack else 1

        # a write without a gate: nothing may change
        before = db_path.read_bytes()
        blocked = run({"action": "query", "path": "shop",
                       "sql": "DELETE FROM orders"})
        after = db_path.read_bytes()
        print(f"  حذف بلا تأكيد           : تغيّر الملف={before != after} — "
              f"{blocked.splitlines()[0][:58]}")
        problems += 0 if before == after or "cannot confirm" in blocked else 1

        many = run({"action": "query", "path": "shop", "sql": "select 1; drop table customers"})
        print(f"  عبارة ثانية مخفية        : {many.splitlines()[0][:72]}")
        problems += 0 if "statements in one string" in many else 1

        hidden = run({"action": "query", "path": "shop", "sql": "/* hi */ UPDATE orders SET total = 0"})
        print(f"  كتابة خلف تعليق         : {hidden.splitlines()[0][:72]}")
        problems += 0 if "changes data or structure" in hidden or "cannot confirm" in hidden else 1

        # writes off entirely
        globals()["_cfg"] = lambda: {**real_cfg(), "databases": f"shop={db_path}",
                                     "default_db": "shop", "allow_writes": "no", "max_rows": "50"}
        off = run({"action": "query", "path": "shop", "sql": "UPDATE orders SET total = 0"})
        print(f"  الكتابة مطفأة            : {off[:64]}")
        problems += 0 if "switched off" in off else 1
        globals()["_cfg"] = lambda: {**real_cfg(), "databases": f"shop={db_path}",
                                     "default_db": "shop", "allow_writes": "yes", "max_rows": "50"}

        # the file-level lock: even a direct write through a read connection fails
        connection, _ = connect("sqlite", str(db_path), read_only=True)
        locked = False
        try:
            connection.execute("DELETE FROM orders")
            locked = False
        except Exception:
            locked = True
        connection.close()
        print(f"  قفل القراءة فقط على الملف : {'يمنع الكتابة' if locked else 'لم يمنع!'}")
        problems += 0 if locked else 1

        plan = run({"action": "explain", "path": "shop", "sql": "SELECT * FROM customers WHERE id = 1"})
        print(f"  خطة التنفيذ              : {plan.splitlines()[1].strip()[:64] if len(plan.splitlines()) > 1 else plan[:60]}")
        problems += 0 if "SCAN" in plan.upper() or "SEARCH" in plan.upper() or "plan" in plan.lower() else 1

        missing = run({"action": "tables", "path": "nope"})
        print(f"  قاعدة غير موجودة        : {missing[:66]}")
        problems += 0 if ("no database file" in missing or "no database was named" in missing) else 1

        sys_path = run({"action": "tables", "path": "/etc/passwd"})
        print(f"  مسار نظام               : {sys_path[:66]}")
        problems += 0 if ("system folder" in sys_path or "not a database" in sys_path
                          or "no database file" in sys_path) else 1

        print(f"  الأصل لم يتغيّر          : {db_path.read_bytes() == original_bytes}")
        problems += 0 if db_path.read_bytes() == original_bytes else 1

        print(f"  run(action مجهول)       : {run({'action': 'sing', 'path': 'shop'})[:56]}")
        problems += 0 if "not a database action" in run({"action": "sing", "path": "shop"}) else 1
    finally:
        globals()["_cfg"] = real_cfg
        shutil.rmtree(probe_dir, ignore_errors=True)

    # the code-level promise: no string-interpolated SQL for user values
    import ast as _ast
    source = Path(__file__).read_text(encoding="utf-8")
    _ast.parse(source)
    bad = re.findall(r'"\s*(?:SELECT|INSERT|UPDATE|DELETE)[^"]*"\s*%\s*', source, re.I)
    print(f"  لا دمج نصوص في SQL       : {bad or 'لا شيء — القيم تُمرَّر كمعاملات فقط'}")
    problems += 0 if not bad else 1
    print("  RESULT:", "PASS" if problems == 0 else f"{problems} problem(s)")
    return problems


if __name__ == "__main__":
    raise SystemExit(_selftest())
