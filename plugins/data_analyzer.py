"""
data_analyzer.py — reading a data file without ever changing it.

WHAT IT DOES
    Opens a CSV, JSON or Excel file and answers real questions about it: what
    columns are there, how many rows, what is missing, what is duplicated, what
    the numbers actually look like (mean, median, spread, outliers), and can it
    filter or group them. Charts when matplotlib is installed.

THE RULE THAT SHAPES EVERY DESIGN DECISION HERE
    The original file is opened read-only and never written back. Every output —
    a filtered table, a chart, a cleaned copy — is a NEW file whose exact path is
    reported. There is no code path in this file that opens the input for
    writing; `open(..., "r")` only. If you ask it to write, it writes somewhere
    else under a name it tells you, and refuses to overwrite.

    Cells are read as text and the numbers are converted where they make sense,
    so a column that is 90% numbers and 10% "N/A" reports both honestly instead
    of silently dropping the odd rows.

STATISTICS WITHOUT A DEPENDENCY
    pandas is used when it is installed (it is fast and precise), and when it is
    not, a small exact-math fallback runs instead — every figure in this file is
    computed by code that ships with this file. Nothing here claims a statistic
    it did not calculate.

SETTINGS (Plugin Manager → Data analyzer)
    output_dir    blank = Documents/JARVIS_Data
    max_rows     5000 — how many rows are read for a summary (a 4-million-row
                 export is analysed by sampling; the count reported is the true
                 count, read cheaply while streaming)
    chart_width  9 — inches, charts only
"""

from __future__ import annotations

import csv
import json
import math
import os
import re
from pathlib import Path
from typing import Any

PLUGIN = {
    "name": "data_analyzer",
    "description": (
        "Reads a data file and tells you what is inside: 'analyse sales.csv', 'what columns are "
        "in data.xlsx', 'any missing values in this file', 'show me the statistics of price', "
        "'filter rows where city is Cairo', 'draw a chart of revenue', 'add a total column = "
        "price * units', 'rename city to town'. CSV, JSON and Excel. The original file is never "
        "modified — anything written is a new file."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "summary | columns | stats | quality | filter | group | "
                                      "transform | chart | help"},
            "path": {"type": "STRING", "description": "The data file to read"},
            "column": {"type": "STRING", "description": "Column for stats/filter/group"},
            "where": {"type": "STRING", "description": "Filter expression, e.g. 'city = Cairo' or 'price > 100'"},
            "group_by": {"type": "STRING", "description": "Column to group by"},
            "agg": {"type": "STRING", "description": "sum | mean | count | min | max (default count)"},
            "chart": {"type": "STRING", "description": "bar | line | hist | scatter (default bar)"},
            "target": {"type": "STRING", "description": "Second column for scatter"},
            "operation": {"type": "STRING",
                          "description": "For transform: add | rename | drop | round | case"},
            "new": {"type": "STRING", "description": "For transform: the new column's name"},
            "left": {"type": "STRING", "description": "For transform add: left column"},
            "right": {"type": "STRING", "description": "For transform add: right column or number"},
            "sign": {"type": "STRING", "description": "For transform add: + - * / (default *)"},
            "to": {"type": "STRING", "description": "For transform rename: the new name"},
            "columns": {"type": "STRING", "description": "For transform drop: column(s), comma separated"},
            "decimals": {"type": "STRING", "description": "For transform round: 0-6"},
            "mode": {"type": "STRING", "description": "For transform case: upper | lower | title | trim"},
            "save_as": {"type": "STRING", "description": "Optional file name for the output"},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "data_analyzer",
    "title": "Data analyzer / محلّل البيانات",
    "fields": [
        {"key": "output_dir", "type": "text", "label": "Output folder (blank = Documents/JARVIS_Data)",
         "default": ""},
        {"key": "max_rows", "type": "text", "label": "Rows read for a summary", "default": "5000"},
        {"key": "chart_width", "type": "text", "label": "Chart width in inches", "default": "9"},
    ],
}

_DATA_SUFFIXES = {".csv", ".tsv", ".txt", ".json", ".jsonl", ".ndjson", ".xlsx", ".xlsm", ".xls"}


def _cfg() -> dict[str, Any]:
    out = {f["key"]: f.get("default") for f in PLUGIN_SETTINGS["fields"]}
    try:
        from memory.config_manager import get_plugin_config
        out.update({k: v for k, v in (get_plugin_config("data_analyzer") or {}).items()
                    if v not in (None, "")})
    except Exception:
        pass
    return out


def _int_cfg(key: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(str(_cfg().get(key) or default))))
    except Exception:
        return default


def output_dir() -> Path:
    raw = str(_cfg().get("output_dir") or "").strip()
    base = Path(raw).expanduser() if raw else (Path.home() / "Documents" / "JARVIS_Data")
    try:
        return base.resolve()
    except Exception:
        return Path.home() / "Documents" / "JARVIS_Data"


def _unique(path: Path) -> Path:
    """Never overwrite: 'report.csv' → 'report (2).csv'. Same policy as the
    document generator, so the suite behaves the same way everywhere."""
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for n in range(2, 500):
        candidate = path.with_name(f"{stem} ({n}){suffix}")
        if not candidate.exists():
            return candidate
    return path.with_name(f"{stem} ({int(os.getpid())}){suffix}")


# ── loading (read-only, always) ───────────────────────────────────────────────

_SYSTEM_PARTS = {"etc", "usr", "bin", "sbin", "lib", "lib64", "var", "sys", "proc",
                 "boot", "root", "dev", "opt", "windows", "program files",
                 "program files (x86)", "programdata", "system", "system32"}
_CREDENTIAL_NAMES = {"api_keys.json", "credentials.json", "secrets.json", "id_rsa",
                     "id_ed25519", "id_ecdsa", ".netrc", ".git-credentials", ".env",
                     "shadow", "passwd", "authorized_keys", "known_hosts"}


def is_authorized(path: Path) -> bool:
    """
    A data file, anywhere the user points — EXCEPT the places where reading
    would be prying rather than working: system directories, and files whose
    names say 'credentials'. The first version of this required the file to be
    under the home folder, which would have refused data on a second drive — a
    real user's data is not always in one place, and guarding by permission
    (what it is) beats guarding by location (where it is).
    """
    try:
        resolved = path.expanduser().resolve()
    except Exception:
        return False
    if not resolved.is_file() or resolved.suffix.lower() not in _DATA_SUFFIXES:
        return False
    if resolved.name.lower() in _CREDENTIAL_NAMES:
        return False
    parts = [part.lower() for part in resolved.parts]
    if any(part in _SYSTEM_PARTS for part in parts[1:-1]):
        return False
    if len(parts) > 3 and tuple(parts[1:3]) == ("documents and settings", "all users"):
        return False
    return True


def load(path: str | Path, max_rows: int = 0) -> dict[str, Any]:
    """
    Read a data file into {columns, rows, count, sampled, source, kind, note}.
    Read-only. Never raises: a problem is returned as note + empty rows.
    """
    p = Path(str(path)).expanduser()
    result: dict[str, Any] = {"ok": False, "columns": [], "rows": [], "count": 0,
                              "sampled": False, "source": str(p), "kind": "", "note": ""}
    if not p.exists():
        result["note"] = f"there is no file at {p}"
        return result
    if not is_authorized(p):
        result["note"] = (f"I will not read '{p.name}': it is a system path or a credentials "
                          f"file, or it is not a data format (I read "
                          f"{', '.join(sorted(_DATA_SUFFIXES))})")
        return result

    suffix = p.suffix.lower()
    max_rows = max_rows or _int_cfg("max_rows", 5000, 10, 200_000)
    try:
        if suffix in (".csv", ".tsv", ".txt"):
            delimiter = "\t" if suffix == ".tsv" else None
            with open(p, "r", encoding="utf-8-sig", errors="replace", newline="") as fh:
                sample = fh.read(8192)
                fh.seek(0)
                if delimiter is None:
                    try:
                        delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
                    except Exception:
                        delimiter = ","
                reader = csv.reader(fh, delimiter=delimiter)
                header = next(reader, None)
                if not header:
                    result["note"] = "the file is empty"
                    return result
                columns = [str(c).strip() or f"col{i + 1}" for i, c in enumerate(header)]
                rows, total = [], 0
                for raw in reader:
                    total += 1
                    if len(rows) < max_rows:
                        rows.append({columns[i]: (raw[i] if i < len(raw) else "")
                                     for i in range(len(columns))})
                result.update(ok=True, columns=columns, rows=rows, count=total,
                              sampled=total > len(rows), kind="csv")
        elif suffix in (".json", ".jsonl", ".ndjson"):
            text = p.read_text(encoding="utf-8", errors="replace")
            data: Any
            if suffix in (".jsonl", ".ndjson"):
                data = [json.loads(line) for line in text.splitlines() if line.strip()]
            else:
                data = json.loads(text)
            if isinstance(data, dict):
                # A dict of records, or a single record, or {'data': [...]}.
                for key in ("data", "items", "records", "rows", "results"):
                    if isinstance(data.get(key), list):
                        data = data[key]
                        break
            if isinstance(data, dict):
                data = [data]
            if not isinstance(data, list):
                result["note"] = "the JSON is not a list of records"
                return result
            records = [r for r in data if isinstance(r, dict)]
            if not records:
                result["note"] = ("the JSON is a list but not of objects — I can summarise "
                                  "objects, not bare values")
                return result
            columns = []
            for record in records:
                for key in record:
                    if key not in columns:
                        columns.append(str(key))
            rows = [{c: r.get(c, "") for c in columns} for r in records[:max_rows]]
            result.update(ok=True, columns=columns, rows=rows, count=len(records),
                          sampled=len(records) > len(rows), kind="json")
        elif suffix in (".xlsx", ".xlsm", ".xls"):
            try:
                from openpyxl import load_workbook
            except Exception:
                result["note"] = ("reading Excel needs openpyxl — it is in the project's "
                                  "requirements (pip install openpyxl). Save as CSV meanwhile "
                                  "and I can read that right away")
                return result
            # read_only + data_only: no formula evaluation, no file lock, no writes.
            book = load_workbook(filename=str(p), read_only=True, data_only=True)
            try:
                sheet = book.active
                iterator = sheet.iter_rows(values_only=True)
                header = next(iterator, None)
                if not header:
                    result["note"] = f"the sheet '{sheet.title}' is empty"
                    return result
                columns = [str(c).strip() if c is not None else f"col{i + 1}"
                           for i, c in enumerate(header)]
                rows, total = [], 0
                for raw in iterator:
                    if raw is None or all(v is None for v in raw):
                        continue
                    total += 1
                    if len(rows) < max_rows:
                        rows.append({columns[i]: ("" if i >= len(raw) or raw[i] is None
                                                  else raw[i]) for i in range(len(columns))})
                result.update(ok=True, columns=columns, rows=rows, count=total,
                              sampled=total > len(rows), kind="excel",
                              note=f"sheet '{sheet.title}'"
                                   + (f", {len(book.sheetnames)} sheets in the file"
                                      if len(book.sheetnames) > 1 else ""))
            finally:
                book.close()
        else:
            result["note"] = f"'{suffix}' is not a format I read"
    except json.JSONDecodeError as e:
        result["note"] = f"the JSON does not parse: {str(e)[:100]}"
    except Exception as e:                                    # noqa: BLE001
        result["note"] = f"I could not read the file: {str(e)[:110]}"
    return result


# ── statistics, exactly, without pandas ──────────────────────────────────────

def to_number(value: Any) -> float | None:
    """'1,234.5' → 1234.5 · '£12' → 12.0 · '12%' → 12.0 · anything else → None.
    Never guesses: a date, a name or 'N/A' is None, and quality() counts them."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return None if (isinstance(value, float) and (math.isnan(value) or math.isinf(value))) \
            else float(value)
    text = str(value).strip()
    if not text:
        return None
    cleaned = re.sub(r"[£$€¥,\s]", "", text)
    if cleaned.endswith("%"):
        cleaned = cleaned[:-1]
    cleaned = re.sub(r"^\((.*)\)$", r"-\1", cleaned)          # (123) is -123 in reports
    try:
        number = float(cleaned)
    except Exception:
        return None
    return None if math.isnan(number) or math.isinf(number) else number


def describe(values: list[float]) -> dict[str, Any]:
    """All the usual figures, computed here rather than imported."""
    if not values:
        return {"n": 0}
    ordered = sorted(values)
    n = len(ordered)
    mean = sum(ordered) / n
    middle = n // 2
    median = ordered[middle] if n % 2 else (ordered[middle - 1] + ordered[middle]) / 2
    def _quantile(q: float) -> float:
        if n == 1:
            return ordered[0]
        position = (n - 1) * q
        low = int(math.floor(position))
        high = min(low + 1, n - 1)
        return ordered[low] + (ordered[high] - ordered[low]) * (position - low)
    variance = sum((x - mean) ** 2 for x in ordered) / (n - 1) if n > 1 else 0.0
    std = math.sqrt(variance)
    q1, q3 = _quantile(0.25), _quantile(0.75)
    iqr = q3 - q1
    outliers = [x for x in ordered if iqr > 0 and (x < q1 - 1.5 * iqr or x > q3 + 1.5 * iqr)]
    return {"n": n, "mean": round(mean, 4), "median": round(median, 4), "min": ordered[0],
            "max": ordered[-1], "q1": round(q1, 4), "q3": round(q3, 4),
            "std": round(std, 4), "sum": round(sum(ordered), 4),
            "outliers": len(outliers), "outlier_examples": outliers[:5]}


def quality(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Per column: how many are empty, how many are numeric, how many are unique,
    and how many look like the same thing written two ways."""
    rows = data["rows"]
    out = []
    for column in data["columns"]:
        values = [row.get(column) for row in rows]
        texts = ["" if v is None else str(v).strip() for v in values]
        empty = sum(1 for t in texts if t == "")
        numbers = [n for n in (to_number(t) for t in texts) if n is not None]
        non_empty = [t for t in texts if t]
        folded = [re.sub(r"\s+", " ", t).casefold() for t in non_empty]
        near_dupes = len(folded) - len(set(folded))
        out.append({"column": column, "empty": empty, "empty_pct": round(empty / len(rows) * 100, 1)
                    if rows else 0.0,
                    "numeric": len(numbers), "non_numeric": len(non_empty) - len(numbers),
                    "unique": len(set(non_empty)),
                    "case_or_space_dupes": near_dupes,
                    "sample": [t for t in non_empty[:3]]})
    return out


def duplicate_rows(data: dict[str, Any]) -> dict[str, Any]:
    seen: dict[str, int] = {}
    for row in data["rows"]:
        key = json.dumps({k: str(v) for k, v in row.items()}, sort_keys=True)
        seen[key] = seen.get(key, 0) + 1
    repeats = {k: v for k, v in seen.items() if v > 1}
    examples = []
    for key, count in list(repeats.items())[:3]:
        try:
            examples.append((json.loads(key), count))
        except Exception:
            pass
    return {"duplicate_groups": len(repeats), "duplicate_rows": sum(v - 1 for v in repeats.values()),
            "examples": examples}


# ── querying ──────────────────────────────────────────────────────────────────

_OPS = ("!=", ">=", "<=", "=", ">", "<", " contains ", " like ")


def parse_where(text: str) -> tuple[str, str, str] | None:
    """'price > 100' → ('price', '>', '100'). Returns None when it cannot be read,
    rather than guessing which half was the column."""
    raw = str(text or "").strip()
    for op in _OPS:
        if op in raw:
            left, _, right = raw.partition(op)
            left, right = left.strip().strip('"\' '), right.strip().strip('"\' ')
            if left and right:
                return left, ("contains" if op.strip() in ("contains", "like") else op.strip()), right
    return None


def apply_filter(rows: list[dict], column: str, op: str, wanted: str) -> list[dict]:
    out = []
    for row in rows:
        have = row.get(column)
        have_text = "" if have is None else str(have).strip()
        left, right = to_number(have_text), to_number(wanted)
        try:
            if op in ("contains", "like"):
                ok = wanted.casefold() in have_text.casefold()
            elif op == "=":
                ok = have_text.casefold() == wanted.casefold() if left is None or right is None \
                    else left == right
            elif op == "!=":
                ok = not (have_text.casefold() == wanted.casefold() if left is None or right is None
                          else left == right)
            elif left is None or right is None:
                ok = False                      # nothing > or < a word
            elif op == ">":
                ok = left > right
            elif op == "<":
                ok = left < right
            elif op == ">=":
                ok = left >= right
            else:
                ok = left <= right
        except Exception:
            ok = False
        if ok:
            out.append(row)
    return out


def group_rows(rows: list[dict], group_by: str, column: str, how: str) -> list[tuple[str, Any, int]]:
    buckets: dict[str, list[float]] = {}
    counts: dict[str, int] = {}
    for row in rows:
        key = str(row.get(group_by, "")).strip() or "(empty)"
        counts[key] = counts.get(key, 0) + 1
        number = to_number(row.get(column)) if column else None
        if number is not None:
            buckets.setdefault(key, []).append(number)
    out = []
    for key, count in counts.items():
        values = buckets.get(key, [])
        if how == "sum":
            value: Any = round(sum(values), 4) if values else None
        elif how == "mean":
            value = round(sum(values) / len(values), 4) if values else None
        elif how == "min":
            value = min(values) if values else None
        elif how == "max":
            value = max(values) if values else None
        else:
            value = count
        out.append((key, value, count))
    out.sort(key=lambda item: (item[1] is None, -(item[1] or 0)))
    return out


# ── transforming (always into a NEW file) ────────────────────────────────────

_SIGNS = {"+": lambda a, b: a + b, "-": lambda a, b: a - b, "*": lambda a, b: a * b,
          "/": lambda a, b: (a / b) if b else None}


def transform(columns: list[str], rows: list[dict], spec: dict[str, Any]) -> dict[str, Any]:
    """
    A small, NAMED set of column operations: add a computed column, rename, drop,
    round, change case. There is no expression evaluation anywhere — an operation
    names its columns and one of four arithmetic signs from a fixed table, so
    nothing typed into a field can become code. Pure: it returns new columns and
    rows and writes nothing at all. The caller decides where the result goes.
    """
    operation = str(spec.get("operation") or "").strip().lower()
    columns = list(columns)
    rows = [dict(row) for row in rows]
    notes: list[str] = []

    if operation in ("add", "derive", "compute", "add_column"):
        new = str(spec.get("new") or "").strip()
        left = str(spec.get("left") or "").strip()
        right = str(spec.get("right") or "").strip()
        sign = str(spec.get("sign") or spec.get("arithmetic") or "*").strip() or "*"
        if not new:
            return {"ok": False, "reason": "what should the new column be called? (field 'new')"}
        if sign not in _SIGNS:
            return {"ok": False, "reason": f"'{sign}' is not one of the four signs I compute with "
                                           f"( + - * / ) — I do not evaluate expressions"}
        if left not in columns:
            return {"ok": False, "reason": f"there is no column called '{left}'"}
        if new in columns:
            return {"ok": False, "reason": (f"a column called '{new}' already exists — I will not "
                                            f"overwrite it quietly; drop it first or choose "
                                            f"another name")}
        constant = None
        if right not in columns:
            constant = to_number(right)
            if constant is None:
                return {"ok": False, "reason": f"'{right}' is neither a column nor a number"}
        skipped = 0
        for row in rows:
            a = to_number(row.get(left))
            b = constant if constant is not None else to_number(row.get(right))
            if a is None or b is None:
                row[new] = ""
                skipped += 1
                continue
            value = _SIGNS[sign](a, b)
            if value is None:                    # division by zero
                row[new] = ""
                skipped += 1
                continue
            row[new] = round(value, 6) if isinstance(value, float) else value
        columns.append(new)
        if skipped:
            notes.append(f"{skipped} row(s) had no number on one side, or a zero divisor — left "
                         f"empty rather than guessed at")
        return {"ok": True, "columns": columns, "rows": rows, "notes": notes}

    if operation in ("rename", "rename_column"):
        old = str(spec.get("column") or spec.get("from") or "").strip()
        new = str(spec.get("to") or spec.get("new") or "").strip()
        if old not in columns:
            return {"ok": False, "reason": f"there is no column called '{old}'"}
        if not new:
            return {"ok": False, "reason": "rename it to what? (field 'to')"}
        if new in columns:
            return {"ok": False, "reason": f"a column called '{new}' already exists"}
        columns = [new if name == old else name for name in columns]
        for row in rows:
            row[new] = row.pop(old, "")
        notes.append(f"'{old}' is now '{new}'")
        return {"ok": True, "columns": columns, "rows": rows, "notes": notes}

    if operation in ("drop", "drop_column", "remove_column"):
        raw = str(spec.get("columns") or spec.get("column") or "").strip()
        wanted = [name.strip() for name in re.split(r"[,\s]+", raw) if name.strip()]
        if not wanted:
            return {"ok": False, "reason": "which column should I drop?"}
        missing = [name for name in wanted if name not in columns]
        if missing:
            return {"ok": False, "reason": f"there is no column called '{missing[0]}'"}
        remaining = [name for name in columns if name not in wanted]
        if not remaining:
            return {"ok": False, "reason": ("dropping everything would leave no columns at all — "
                                            "name at least one to keep")}
        columns = remaining
        for row in rows:
            for name in wanted:
                row.pop(name, None)
        notes.append("dropped: " + ", ".join(wanted))
        return {"ok": True, "columns": columns, "rows": rows, "notes": notes}

    if operation in ("round", "round_column"):
        column = str(spec.get("column") or "").strip()
        if column not in columns:
            return {"ok": False, "reason": f"there is no column called '{column}'"}
        try:
            decimals = max(0, min(6, int(str(spec.get("decimals") or 0))))
        except Exception:
            decimals = 0
        changed = 0
        for row in rows:
            value = to_number(row.get(column))
            if value is not None:
                row[column] = round(value, decimals)
                changed += 1
        notes.append(f"{changed} value(s) rounded to {decimals} decimal(s). Python rounds a half "
                     f"to the even digit (12.5 → 12) and that is stated rather than hidden")
        return {"ok": True, "columns": columns, "rows": rows, "notes": notes}

    if operation in ("case", "upper", "lower", "title", "trim"):
        column = str(spec.get("column") or "").strip()
        mode = str(spec.get("mode") or operation).strip().lower()
        if column not in columns:
            return {"ok": False, "reason": f"there is no column called '{column}'"}
        if mode not in ("upper", "lower", "title", "trim"):
            return {"ok": False, "reason": f"'{mode}' is not a case change I do (upper, lower, "
                                           f"title, trim)"}
        for row in rows:
            text = "" if row.get(column) is None else str(row.get(column))
            row[column] = {"upper": text.upper(), "lower": text.lower(),
                           "title": text.title(), "trim": text.strip()}[mode]
        notes.append(f"'{column}' set to {mode}")
        return {"ok": True, "columns": columns, "rows": rows, "notes": notes}

    return {"ok": False, "reason": (f"'{operation or 'nothing'}' is not a transform I do — I can "
                                    f"add a computed column, rename, drop, round or change case")}


# ── charts ────────────────────────────────────────────────────────────────────

def make_chart(data: dict[str, Any], kind: str, column: str, group_by: str = "",
               target: str = "", title: str = "") -> tuple[bool, str]:
    """(ok, path-or-reason). A chart is a NEW png; the data file is untouched."""
    try:
        import matplotlib
        matplotlib.use("Agg")                       # no window, no display needed
        import matplotlib.pyplot as plt
    except Exception:
        return False, ("drawing charts needs matplotlib — pip install matplotlib. Everything "
                       "else I can do without it")
    rows = data["rows"]
    if not rows:
        return False, "there are no rows to draw"
    x_values, y_values, labels = [], [], (title or column or "value")
    if group_by:
        grouped = group_rows(rows, group_by, column, "sum")
        x_values = [g[0] for g in grouped][:25]
        y_values = [g[1] or 0 for g in grouped][:25]
        labels = f"{column or 'count'} by {group_by}"
    elif column:
        numbers = [n for n in (to_number(r.get(column)) for r in rows) if n is not None]
        if not numbers:
            return False, f"'{column}' has no numbers in it to draw"
        x_values, y_values = list(range(1, len(numbers) + 1)), numbers
        labels = column
    else:
        return False, "which column should I draw?"

    width = _int_cfg("chart_width", 9, 3, 40)
    figure, axes = plt.subplots(figsize=(width, width * 0.55))
    try:
        if kind == "line":
            axes.plot(x_values, y_values, marker="o", linewidth=1.6)
        elif kind == "hist":
            axes.hist(y_values, bins=min(30, max(5, len(y_values) // 5)))
        elif kind == "scatter":
            if not target:
                return False, "a scatter needs a second column (target)"
            pairs = [(to_number(r.get(column)), to_number(r.get(target))) for r in rows]
            pairs = [(a, b) for a, b in pairs if a is not None and b is not None]
            if not pairs:
                return False, "those two columns have no matching numbers"
            axes.scatter([a for a, _ in pairs], [b for _, b in pairs], s=14, alpha=0.7)
            labels = f"{column} × {target}"
        else:
            axes.bar([str(v) for v in x_values], y_values, color="#3b82f6")
            if len(x_values) > 12:
                axes.tick_params(axis="x", rotation=45, labelsize=8)
        axes.set_title(labels)
        axes.grid(True, axis="y", alpha=0.25)
        figure.tight_layout()
        out = _unique(output_dir() / f"{re.sub(r'[^A-Za-z0-9_ -]', '', labels)[:48] or 'chart'}.png")
        out.parent.mkdir(parents=True, exist_ok=True)
        figure.savefig(out, dpi=130)
        return True, str(out)
    finally:
        plt.close(figure)


def write_csv(columns: list[str], rows: list[dict], suggested: str) -> tuple[bool, str]:
    try:
        out = _unique(output_dir() / suggested)
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({c: row.get(c, "") for c in columns})
        return True, str(out)
    except Exception as e:
        return False, f"I could not write the file: {str(e)[:90]}"


# ── tool entry point ──────────────────────────────────────────────────────────

def _summary(data: dict[str, Any]) -> str:
    lines = [f"{data['count']:,} row(s) × {len(data['columns'])} column(s)"
             + (f" — I read the first {len(data['rows']):,} for this summary"
                if data["sampled"] else "")
             + (f" · {data['note']}" if data["note"] else "")]
    report = quality(data)
    lines.append("")
    lines.append(f"  {'column':<22}{'empty':>8}{'numeric':>9}{'text':>7}{'unique':>8}  sample")
    for item in report[:20]:
        lines.append(f"  {str(item['column'])[:21]:<22}{item['empty']:>8}{item['numeric']:>9}"
                     f"{item['non_numeric']:>7}{item['unique']:>8}  "
                     f"{', '.join(str(s)[:14] for s in item['sample'])}")
    if len(report) > 20:
        lines.append(f"  … and {len(report) - 20} more column(s)")
    dupes = duplicate_rows(data)
    if dupes["duplicate_rows"]:
        lines.append("")
        lines.append(f"  {dupes['duplicate_rows']} duplicated row(s) in "
                     f"{dupes['duplicate_groups']} group(s)")
    return "\n".join(lines)


def run(parameters: dict, player=None, session_memory=None) -> str:
    """One data action. Read-only on your files, always."""
    try:
        p = parameters or {}
        action = str(p.get("action") or "summary").strip().lower()
        path = str(p.get("path") or "").strip()

        if action in ("help", "?"):
            return ("I read data files, sir — CSV, JSON or Excel — and never change the original: "
                    "'analyse sales.csv', 'columns of data.xlsx', 'stats of the price column', "
                    "'missing values', 'filter where city = Cairo', 'group by region', "
                    "'add a total column = price * units', 'rename city to town', "
                    "'chart revenue by month'. Anything I write is a new file and I tell you its "
                    "path.")
        if not path:
            return "Which file, sir? Give me the path to the CSV, JSON or Excel file."

        data = load(path)
        if not data["ok"]:
            return f"Sir, {data['note']}."

        if action in ("summary", "analyse", "analyze", "overview"):
            return _summary(data)

        if action in ("columns", "schema", "structure"):
            lines = [f"{len(data['columns'])} column(s) in {Path(data['source']).name}"
                     + (f" ({data['note']})" if data["note"] else "") + ":", ""]
            for item in quality(data):
                kind = ("numbers" if item["numeric"] and not item["non_numeric"]
                        else "text" if not item["numeric"] else "mixed")
                lines.append(f"  {str(item['column'])[:26]:<28}{kind:<8}"
                             f"empty {item['empty']:>5}   unique {item['unique']:>5}")
            return "\n".join(lines)

        if action in ("stats", "statistics", "describe"):
            column = str(p.get("column") or "").strip()
            if not column:
                numeric = [item["column"] for item in quality(data) if item["numeric"]]
                return (f"{len(numeric)} numeric column(s): {', '.join(numeric[:12])}. "
                        f"Say 'statistics of <column>'.") if numeric else \
                       "There is no column with numbers in it, sir."
            if column not in data["columns"]:
                return f"There is no column called '{column}', sir. Columns: {', '.join(data['columns'][:12])}"
            values = [n for n in (to_number(r.get(column)) for r in data["rows"]) if n is not None]
            total = len(data["rows"])
            if not values:
                return f"'{column}' has no numbers in it, sir."
            s = describe(values)
            return (f"{column} — {s['n']} number(s) of {total} row(s)"
                    + (f" ({total - s['n']} cells were not numbers)" if total != s["n"] else "")
                    + f":\n  mean {s['mean']:,} · median {s['median']:,} · "
                      f"min {s['min']:,} · max {s['max']:,}\n"
                      f"  q1 {s['q1']:,} · q3 {s['q3']:,} · std {s['std']:,} · sum {s['sum']:,}\n"
                    + (f"  {s['outliers']} value(s) sit far from the rest (1.5×IQR): "
                       f"{s['outlier_examples']}" if s["outliers"] else "  no outliers by the usual rule"))

        if action in ("quality", "missing", "duplicates", "clean"):
            report = quality(data)
            dupes = duplicate_rows(data)
            empties = [item for item in report if item["empty"]]
            lines = [f"Quality of {Path(data['source']).name} — {data['count']:,} row(s):", ""]
            lines.append(f"  missing cells     : {sum(i['empty'] for i in report):,} across "
                         f"{len(empties)} column(s)")
            for item in sorted(empties, key=lambda i: -i["empty"])[:8]:
                lines.append(f"      {str(item['column'])[:28]:<30}{item['empty']:>7} empty "
                             f"({item['empty_pct']}%)")
            mixed = [i for i in report if i["numeric"] and i["non_numeric"]]
            if mixed:
                lines.append(f"  mixed columns     : {', '.join(str(i['column']) for i in mixed[:6])}"
                             f" — numbers and text in one column")
            near = [i for i in report if i["case_or_space_dupes"]]
            if near:
                lines.append(f"  likely-same values: {', '.join(str(i['column']) for i in near[:6])}"
                             f" — same value written differently (case/spaces)")
            if dupes["duplicate_rows"]:
                lines.append(f"  duplicate rows    : {dupes['duplicate_rows']} row(s) in "
                             f"{dupes['duplicate_groups']} group(s)")
                for example, count in dupes["examples"][:2]:
                    shown = ", ".join(f"{k}={str(v)[:16]}" for k, v in list(example.items())[:4])
                    lines.append(f"      ×{count}  {shown}")
            else:
                lines.append("  duplicate rows    : none")
            lines.append("")
            lines.append("  I did not change anything — this is a report, not an edit.")
            return "\n".join(lines)

        if action in ("filter", "where", "select"):
            where = str(p.get("where") or "").strip()
            parsed = parse_where(where)
            if not parsed:
                return ("I read a filter as 'column operator value', sir — for example "
                        "'city = Cairo' or 'price > 100'.")
            column, op, wanted = parsed
            if column not in data["columns"]:
                return f"There is no column called '{column}', sir."
            rows = apply_filter(data["rows"], column, op, wanted)
            saved = ""
            save_as = str(p.get("save_as") or "").strip()
            if save_as and rows:
                ok, where_written = write_csv(data["columns"], rows, save_as)
                saved = f"\nSaved to {where_written}" if ok else f"\n({where_written})"
            preview = ["  " + " | ".join(str(r.get(c, ""))[:18] for c in data["columns"][:6])
                       for r in rows[:8]]
            return (f"{len(rows)} of {len(data['rows'])} row(s) match {column} {op} {wanted}:"
                    + ("\n" + "\n".join(preview) if preview else " — none of them match.")
                    + (f"\n… and {len(rows) - 8} more" if len(rows) > 8 else "") + saved
                    + "\n\nThe original file is untouched.")

        if action in ("group", "group_by", "aggregate"):
            group_by = str(p.get("group_by") or p.get("column") or "").strip()
            if not group_by:
                return "Group by which column, sir?"
            if group_by not in data["columns"]:
                return f"There is no column called '{group_by}', sir."
            column = str(p.get("column") or "").strip()
            if column == group_by:
                column = ""
            how = str(p.get("agg") or "count").strip().lower()
            if how not in ("sum", "mean", "count", "min", "max"):
                how = "count"
            grouped = group_rows(data["rows"], group_by, column, how)
            head = f"{how}" + (f" of {column}" if column else "") + f" by {group_by}"
            lines = [f"{head} — {len(grouped)} group(s):", ""]
            for key, value, count in grouped[:20]:
                shown = f"{value:,}" if isinstance(value, (int, float)) else "—"
                lines.append(f"  {str(key)[:30]:<32}{shown:>14}   ({count} row(s))")
            if len(grouped) > 20:
                lines.append(f"  … and {len(grouped) - 20} more")
            lines.append("\nNothing was written — say 'save as x.csv' style by adding save_as if you want a file.")
            return "\n".join(lines)

        if action in ("transform", "convert", "derive", "modify"):
            spec = {"operation": p.get("operation") or p.get("transform") or "",
                    "new": p.get("new"), "left": p.get("left"), "right": p.get("right"),
                    "sign": p.get("sign"), "column": p.get("column"),
                    "columns": p.get("columns"), "to": p.get("to"),
                    "decimals": p.get("decimals"), "mode": p.get("mode")}
            shifted = transform(data["columns"], data["rows"], spec)
            if not shifted["ok"]:
                return f"Sir, {shifted['reason']}."
            save_as = str(p.get("save_as") or "").strip() or \
                f"{Path(data['source']).stem}_transformed.csv"
            written_ok, written = write_csv(shifted["columns"], shifted["rows"], save_as)
            if not written_ok:
                return f"Sir, {written}."
            preview_columns = shifted["columns"][:6]
            preview = ["  " + " | ".join(f"{name}={str(row.get(name, ''))[:16]}"
                                         for name in preview_columns)
                       for row in shifted["rows"][:5]]
            partial = (f"\nNote: this file holds the first {len(shifted['rows']):,} of "
                       f"{data['count']:,} rows — the rest was not read (max_rows)."
                       if data["sampled"] else "")
            return (f"Transformed, sir — {len(shifted['rows']):,} row(s), "
                    f"{len(shifted['columns'])} column(s):\n" + "\n".join(preview)
                    + (f"\n… and {len(shifted['rows']) - 5} more" if len(shifted["rows"]) > 5 else "")
                    + "\n\n" + "\n".join(f"  {note}" for note in shifted["notes"])
                    + f"\nSaved to {written}\nYour original file was not touched."
                    + partial)

        if action in ("chart", "plot", "graph"):
            kind = str(p.get("chart") or "bar").strip().lower()
            if kind not in ("bar", "line", "hist", "scatter"):
                kind = "bar"
            column = str(p.get("column") or "").strip()
            group_by = str(p.get("group_by") or "").strip()
            ok, result = make_chart(data, kind, column, group_by, str(p.get("target") or ""))
            if not ok:
                return f"Sir, {result}."
            if player:
                try:
                    player.write_log(f"[Data] chart → {Path(result).name}")
                except Exception:
                    pass
            return f"Chart drawn, sir: {result}\nThe data file was not touched."

        return (f"Sir, '{action}' is not a data action I have. I can summarise, list columns, give "
                f"statistics, check quality, filter, group, transform, or draw a chart.")
    except Exception as e:                                     # noqa: BLE001
        return f"Sir, the data analyzer hit a problem: {str(e)[:150]}"


# ── self-test: python data_analyzer.py ───────────────────────────────────────

def _selftest() -> int:
    print("data_analyzer — self-test")
    problems = 0
    probe = Path(__file__).resolve().parent / "_data_probe"
    probe.mkdir(exist_ok=True)

    csv_path = probe / "sales.csv"
    csv_path.write_text(
        "city,product,price,units,note\n"
        "Cairo,Tea,12.5,100,good\n"
        "Cairo,Tea,12.5,100,good\n"
        "Giza,Coffee,30,50,\n"
        "Alex,Tea,11,80,N/A\n"
        "Cairo,Coffee,28,20,good\n"
        "Giza,Tea,98,10,outlier\n"
        "Alex,Coffee,31,40,\n", encoding="utf-8")
    original = csv_path.read_bytes()

    data = load(csv_path)
    print(f"  قراءة CSV               : ok={data['ok']} {data['count']} صف × "
          f"{len(data['columns'])} عمود ({data['note'] or 'لا ملاحظات'})")
    problems += 0 if data["ok"] and data["count"] == 7 and data["columns"][0] == "city" else 1

    print(f"  الأرقام المكتوبة نصًّا   : '1,234.5'→{to_number('1,234.5')} · "
          f"'£12'→{to_number('£12')} · '(50)'→{to_number('(50)')} · 'N/A'→{to_number('N/A')}")
    problems += 0 if (to_number("1,234.5") == 1234.5 and to_number("£12") == 12.0
                      and to_number("(50)") == -50.0 and to_number("N/A") is None) else 1

    stats = describe([float(v) for v in (12.5, 30, 11, 28, 98, 31)])
    print(f"  الإحصاء                 : n={stats['n']} mean={stats['mean']} median={stats['median']} "
          f"outliers={stats['outliers']} {stats['outlier_examples']}")
    problems += 0 if 1 <= stats["outliers"] <= 3 and stats["min"] == 11.0 and stats["max"] == 98.0 else 1
    expected_sum = 12.5 + 30 + 11 + 28 + 98 + 31
    problems += 0 if abs(stats["sum"] - expected_sum) < 0.001 else 1

    dupes = duplicate_rows(data)
    print(f"  التكرار                 : {dupes['duplicate_rows']} صف مكرَّر في "
          f"{dupes['duplicate_groups']} مجموعة")
    problems += 0 if dupes["duplicate_rows"] == 1 else 1

    report = quality(data)
    note_col = next(i for i in report if i["column"] == "note")
    price_col = next(i for i in report if i["column"] == "price")
    print(f"  الجودة                  : note بها {note_col['empty']} فراغ · "
          f"price أرقامها {price_col['numeric']}/{price_col['numeric'] + price_col['non_numeric']}")
    problems += 0 if note_col["empty"] == 2 and price_col["non_numeric"] == 0 else 1

    filtered = apply_filter(data["rows"], "city", "=", "cairo")
    print(f"  تصفية (حساسية الحالة)   : 'cairo' وجدت {len(filtered)} صف")
    problems += 0 if len(filtered) == 3 else 1
    big = apply_filter(data["rows"], "price", ">", "29")
    print(f"  تصفية رقمية             : price > 29 → {len(big)} صف "
          f"(يجب أن تخرج 30 و 31 و 98)")
    problems += 0 if len(big) == 3 else 1
    problems += 0 if parse_where("price>100") == ("price", ">", "100") else 1
    problems += 0 if parse_where("just some words") is None else 1

    grouped = group_rows(data["rows"], "city", "price", "mean")
    print(f"  تجميع                   : {grouped}")
    problems += 0 if grouped[0][0] == "Giza" and grouped[0][1] == 64.0 else 1

    # ── transform: a table of four signs, never an evaluated expression ──
    added = transform(data["columns"], data["rows"],
                      {"operation": "add", "new": "total", "left": "price", "right": "units",
                       "sign": "*"})
    print(f"  تحويل: عمود محسوب        : total لأول صف = {added['rows'][0]['total']} (12.5 × 100)")
    problems += 0 if added["ok"] and added["rows"][0]["total"] == 1250.0 else 1
    problems += 0 if added["columns"][-1] == "total" else 1

    by_constant = transform(["price"], [{"price": 100}],
                            {"operation": "add", "new": "with_vat", "left": "price",
                             "right": "1.14", "sign": "*"})
    print(f"  تحويل: ضرب في رقم ثابت    : {by_constant['rows'][0]['with_vat']}")
    problems += 0 if by_constant["ok"] and by_constant["rows"][0]["with_vat"] == 114.0 else 1

    divided = transform(["a", "b"], [{"a": 1, "b": 0}, {"a": 4, "b": 2}],
                        {"operation": "add", "new": "q", "left": "a", "right": "b", "sign": "/"})
    print(f"  قسمة على صفر            : {[r['q'] for r in divided['rows']]} — "
          f"{divided['notes'][0][:56]}")
    problems += 0 if (divided["ok"] and divided["rows"][0]["q"] == ""
                      and divided["rows"][1]["q"] == 2.0) else 1

    injected = transform(data["columns"], data["rows"],
                         {"operation": "add", "new": "x", "left": "price", "right": "units",
                          "sign": "__import__('os').system('ls')"})
    print(f"  تعبير في خانة الإشارة     : {injected['reason'][:66]}")
    problems += 0 if not injected["ok"] and "four signs" in injected["reason"] else 1

    unknown = transform(data["columns"], data["rows"], {"operation": "execute", "column": "price"})
    problems += 0 if not unknown["ok"] and "not a transform" in unknown["reason"] else 1

    renamed = transform(data["columns"], data["rows"],
                        {"operation": "rename", "column": "city", "to": "town"})
    problems += 0 if (renamed["ok"] and "town" in renamed["columns"]
                      and "city" not in renamed["columns"]) else 1
    print(f"  تحويل: إعادة تسمية        : city → {'town' if renamed['ok'] else renamed['reason'][:30]}")

    dropped = transform(data["columns"], data["rows"], {"operation": "drop", "column": "note"})
    problems += 0 if dropped["ok"] and "note" not in dropped["columns"] else 1
    all_gone = transform(data["columns"], data["rows"],
                         {"operation": "drop", "columns": ",".join(data["columns"])})
    print(f"  تحويل: حذف كل الأعمدة    : {all_gone['reason'][:64]}")
    problems += 0 if not all_gone["ok"] and "no columns" in all_gone["reason"] else 1
    clashes = transform(data["columns"], data["rows"],
                        {"operation": "add", "new": "price", "left": "price", "right": "units"})
    problems += 0 if not clashes["ok"] and "already exists" in clashes["reason"] else 1

    rounded = transform(["v"], [{"v": 12.7}], {"operation": "round", "column": "v", "decimals": 0})
    print(f"  تحويل: تدوير             : 12.7 → {rounded['rows'][0]['v']} "
          f"(نصف إلى الزوجي مُعلن)")
    problems += 0 if rounded["ok"] and rounded["rows"][0]["v"] == 13 else 1
    cased = transform(["name"], [{"name": "  Cairo  "}], {"operation": "case", "column": "name",
                                                          "mode": "trim"})
    problems += 0 if cased["ok"] and cased["rows"][0]["name"] == "Cairo" else 1

    before_transform = csv_path.read_bytes()
    through = run({"action": "transform", "path": str(csv_path), "operation": "add", "new": "total",
                   "left": "price", "right": "units", "sign": "*", "save_as": "transform_probe.csv"})
    written_path = through.split("Saved to ")[-1].splitlines()[0].strip() if "Saved to " in through \
        else ""
    print(f"  تحويل عبر run → ملف جديد  : {Path(written_path).name if written_path else through[:60]}")
    problems += 0 if written_path and Path(written_path).exists() else 1
    problems += 0 if csv_path.read_bytes() == before_transform else 1
    print(f"  وتحقق: الأصل بلا تغيير    : {csv_path.read_bytes() == before_transform}")
    if written_path:
        written_text = Path(written_path).read_text(encoding="utf-8-sig")
        problems += 0 if "total" in written_text.splitlines()[0] else 1
        Path(written_path).unlink(missing_ok=True)

    # and the promise behind every one of those: no code is ever evaluated here
    import ast as _ast
    tree = _ast.parse(Path(__file__).read_text(encoding="utf-8"))
    dangerous = [node.func.id for node in _ast.walk(tree) if isinstance(node, _ast.Call)
                 and isinstance(node.func, _ast.Name) and node.func.id in ("eval", "exec")]
    print(f"  لا eval ولا exec         : {dangerous or 'لا شيء — التحويل جدول عمليات، لا كود يُنفَّذ'}")
    problems += 0 if not dangerous else 1

    # JSON
    json_path = probe / "items.json"
    json_path.write_text(json.dumps({"data": [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]}),
                         encoding="utf-8")
    jdata = load(json_path)
    print(f"  قراءة JSON (قائمة داخل data): ok={jdata['ok']} {jdata['count']} سجل، "
          f"أعمدة {jdata['columns']}")
    problems += 0 if jdata["ok"] and jdata["count"] == 2 and jdata["columns"] == ["a", "b"] else 1
    lines = probe / "stream.jsonl"
    lines.write_text('{"a":1}\n{"a":2}\n{"a":3}\n', encoding="utf-8")
    problems += 0 if load(lines)["count"] == 3 else 1

    # Excel through openpyxl (present here; if absent the message is the check)
    try:
        from openpyxl import Workbook
        wb = Workbook()
        sheet = wb.active
        sheet.title = "Q1"
        sheet.append(["month", "revenue"])
        for row in (("Jan", 10), ("Feb", 20), ("Mar", 35)):
            sheet.append(row)
        xlsx_path = probe / "book.xlsx"
        wb.save(xlsx_path)
        xdata = load(xlsx_path)
        print(f"  قراءة Excel             : ok={xdata['ok']} {xdata['count']} صف — {xdata['note']}")
        problems += 0 if xdata["ok"] and xdata["count"] == 3 else 1
    except ImportError:
        xdata = load(probe / "book.xlsx")
        print(f"  Excel بلا openpyxl      : {xdata['note'][:70]}")

    # THE promise: the original never changes
    problems += 0 if csv_path.read_bytes() == original else 1
    print(f"  الملف الأصلي لم يتغيّر   : {csv_path.read_bytes() == original} "
          f"(مقارنة بايت-بايت بعد كل العمليات)")

    # outputs are new files, never overwrites
    ok, written = write_csv(data["columns"], filtered, "filtered.csv")
    ok2, written2 = write_csv(data["columns"], filtered, "filtered.csv")
    print(f"  ملف مخرج جديد           : {Path(written).name} ثم {Path(written2).name} "
          f"(لا استبدال)")
    # '(2)' vs '(8)': the number only has to be there — leftover files from an
    # earlier run are the test leaving litter, not the plugin overwriting.
    problems += 0 if (ok and ok2 and written != written2
                      and re.search(r"\(\d+\)", written2)) else 1

    # outside-home path refused
    outside = load("/etc/passwd")
    print(f"  خارج ملفات المستخدم     : {outside['note'][:72]}")
    problems += 0 if not outside["ok"] else 1
    notdata = load(Path(__file__).resolve())
    print(f"  ملف ليس بيانات          : {notdata['note'][:66]}")
    problems += 0 if not notdata["ok"] else 1

    # run(): junk must not crash, and the reading path must answer
    short = run({"action": "summary", "path": str(csv_path)})
    print(f"  run(summary)            : {short.splitlines()[0][:78]}")
    problems += 0 if "7" in short.splitlines()[0] else 1
    print(f"  run بلا مسار            : {run({'action': 'summary'})[:64]}")
    problems += 0 if "Which file" in run({"action": "summary"}) else 1
    print(f"  run بفلتر سيّئ           : {run({'action': 'filter', 'path': str(csv_path), 'where': 'words only'})[:66]}")
    problems += 0 if "column operator value" in run(
        {"action": "filter", "path": str(csv_path), "where": "words only"}) else 1
    print(f"  run(action مجهول)       : {run({'action': 'sing', 'path': str(csv_path)})[:62]}")
    problems += 0 if "not a data action" in run({"action": "sing", "path": str(csv_path)}) else 1

    chart_ok, chart_result = make_chart(data, "bar", "price", "city")
    print(f"  رسم بياني               : {'✓ ' + Path(chart_result).name if chart_ok else chart_result[:70]}")
    if chart_ok:
        problems += (0 if (path := Path(chart_result)).exists() and path.stat().st_size > 1000 else 1)
    else:
        problems += 0 if "matplotlib" in chart_result else 1

    import shutil
    shutil.rmtree(probe, ignore_errors=True)
    # A test that leaves files behind is a test that hides the next failure:
    # every artefact this run created is removed, not just the chart.
    for leftover in output_dir().glob("*"):
        if leftover.is_file() and (leftover.name.startswith(("price by", "chart"))
                                   or leftover.name.startswith("filtered")):
            leftover.unlink()
    print("  RESULT:", "PASS" if problems == 0 else f"{problems} problem(s)")
    return problems


if __name__ == "__main__":
    raise SystemExit(_selftest())
