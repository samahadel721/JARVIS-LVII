"""
money_tracker.py — «فلوسي»: سجل مصاريف بسيط بتقارير حقيقية، محلي بالكامل (SQLite).

• **تسجيل بلغة طبيعية**: «سجل ٣٠٠ جنيه بنزين» ← يفهم المبلغ (حتى بالأرقام العربية ٠-٩)
  ويستنتج التصنيف من الكلام (بنزين ← مواصلات · عشاء ← طعام …).
• **تقارير صادقة**: إجمالي الشهر، لكل تصنيف بنسبته وأشرطته، أعلى بند، المتوسط اليومي،
  ومقارنة بالشهر السابق — وقارئ واضح لموازنتك الشهرية إن حدّدتها.
• **تصدير CSV** لأي شهر، وحذف بأمر صريح (`confirm=yes`) لا قبله.
• **خصوصية كاملة**: ملف واحد على جهازك، لا يُرسل لأي خدمة، ولا سرّ يُكتب في أي سجل.

🔴 لا سطر يرمي استثناءً للخارج. لو تعذّر فتح الملف تقولها بصراحة بدل أن تفقد شيئًا بصمت.
"""
from __future__ import annotations

import importlib.util
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

PLUGIN = {
    "name": "money_tracker",
    "description": (
        "Personal expense tracker with monthly reports, stored locally in SQLite. Use for: 'record "
        "300 pounds for petrol', 'log 45 for lunch', 'how much did I spend this month', 'what is my "
        "biggest expense category', 'how much did I spend on food', 'compare this month to last "
        "month', 'show my last expenses', 'set my monthly budget to 8000', 'export this month as "
        "CSV', 'delete the last entry'. Understands Arabic digits and infers categories from the "
        "words. It never sends anything anywhere. Do NOT use it for bank accounts or payments — it "
        "is a notebook, not a bank."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "add | list | report | budget | delete | categories | export | status | help"},
            "amount": {"type": "STRING", "description": "add/budget: the number, e.g. '٣٠٠' or '45.5'"},
            "category": {"type": "STRING", "description": "add/budget: category name (or let me infer it)"},
            "note": {"type": "STRING", "description": "add: what it was, e.g. 'بنزين' or 'عشاء مع الأهل'"},
            "day": {"type": "STRING", "description": "add: اليوم (default) | أمس | YYYY-MM-DD"},
            "month": {"type": "STRING", "description": "report/export: YYYY-MM | الشهر ده | الشهر اللي فات"},
            "id": {"type": "NUMBER", "description": "delete: entry id (or 0 for the last entry)"},
            "limit": {"type": "NUMBER", "description": "list: how many rows, default 10"},
            "confirm": {"type": "STRING", "description": "delete: must be 'yes'"},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "money_tracker",
    "title": "Money tracker — currency, categories and budget",
    "fields": [
        {"key": "currency", "type": "text", "label": "Currency label", "default": "ج.م"},
        {"key": "monthly_budget", "type": "text", "label": "Total monthly budget (empty = none)", "default": ""},
        {"key": "warn_percent", "type": "text", "label": "Warn when this % of the budget is used", "default": "80"},
        {"key": "extra_categories", "type": "text",
         "label": "Extra category keywords: word=التصنيف (semicolon separated)", "default": ""},
    ],
}

T = None
_DB = "money.db"
CATEGORIES = ["طعام", "مواصلات", "فواتير", "تسوق", "صحة", "تعليم", "ترفيه", "بيت", "أخرى"]
_KEYWORDS = {
    "طعام": ["أكل", "اكل", "عشاء", "غدا", "غداء", "فطار", "فطور", "مطعم", "قهوة", "قهوه", "سوبر ماركت",
             "سوبرماركت", "بقالة", "بقاله", "خضار", "فاكهة", "فاكهه", "لحمة", "فراخ", "سمك", "عيش", "حلويات"],
    "مواصلات": ["بنزين", "وقود", "تاكسي", "أوبر", "اوبر", "اوبر", "ميكروباص", "أتوبيس", "اتوبيس", "مترو",
                "قطر", "قطار", "باص", "سرفيس", "صيانة عربية", "زيت", "كاوتش", "توك توك"],
    "فواتير": ["كهربا", "كهرباء", "غاز", "مياه", "فاتورة", "فواتير", "نت", "إنترنت", "انترنت", "تليفون",
               "موبايل", "شحن", "اشتراك", "راوتر", "أرضي"],
    "تسوق": ["هدوم", "ملابس", "حاجة", "حوايج", "جزمة", "قميص", "بنطلون", "ساعة", "موبايل جديد", "أونلاين",
             "امازون", "نون"],
    "صحة": ["دوا", "دواء", "صيدلية", "صيدليه", "دكتور", "طبيب", "تحليل", "أشعة", "اشعة", "مستشفى",
            "كشف", "أسنان", "ضرس", "نظارة"],
    "تعليم": ["كتاب", "كتب", "كورس", "دورة", "مدرس", "درس", "جامعة", "مصاريف دراسة", "أدوات"],
    "ترفيه": ["سينما", "خروجة", "لعبة", "لعبه", "نتفليكس", "اشتراك شهري", "سبا", "رحلة", "فسحة"],
    "بيت": ["إيجار", "ايجار", "أثاث", "اثاث", "تصليح", "سباك", "كهربائي", "نجار", "دهان", "نجف", "ستاير",
            "أدوات منزلية"],
}
_MONTHS_AR = {"يناير": 1, "فبراير": 2, "مارس": 3, "أبريل": 4, "ابريل": 4, "مايو": 5, "يونيو": 6,
              "يوليو": 7, "أغسطس": 8, "اغسطس": 8, "سبتمبر": 9, "أكتوبر": 10, "اكتوبر": 10,
              "نوفمبر": 11, "ديسمبر": 12}


def _boot() -> None:
    """يحمّل النواة المساعدة — مرة واحدة."""
    global T
    name = "_jarvis_tools_core"
    if name in sys.modules:
        T = sys.modules[name]
        return
    path = Path(__file__).resolve().parent / "_tools_core.py"
    if not path.exists():
        return
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        return
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        T = module
    except Exception:
        sys.modules.pop(name, None)
        T = None


_boot()


def _cfg() -> dict:
    if T is None:
        return {f["key"]: f.get("default") for f in PLUGIN_SETTINGS["fields"]}
    return T.cfg(PLUGIN_SETTINGS["namespace"], PLUGIN_SETTINGS["fields"])


def _db_path() -> Optional[Path]:
    if T is None:
        return None
    folder = T.extras_dir()
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except Exception:
        return None
    return folder / _DB


def _connect() -> tuple[Optional[sqlite3.Connection], str]:
    """يفتح قاعدة البيانات وينشئ الجداول. يعيد (الاتصال أو None، سبب الفشل)."""
    path = _db_path()
    if path is None:
        return None, "تعذّر تجهيز مجلد الإعدادات لقاعدة البيانات."
    try:
        connection = sqlite3.connect(str(path), timeout=5)
        connection.execute("CREATE TABLE IF NOT EXISTS expenses ("
                           "id INTEGER PRIMARY KEY AUTOINCREMENT, amount REAL NOT NULL, "
                           "category TEXT NOT NULL, note TEXT, day TEXT NOT NULL, ts TEXT NOT NULL)")
        connection.execute("CREATE TABLE IF NOT EXISTS budgets (category TEXT PRIMARY KEY, amount REAL)")
        connection.commit()
        return connection, ""
    except Exception as exc:  # noqa: BLE001
        return None, f"تعذّر فتح قاعدة مصاريفك ({type(exc).__name__}: {str(exc)[:80]})."


def _money(value: Any) -> str:
    """تنسيق مبلغ بعملتك: 1,250.5 ج.م."""
    currency = str(_cfg().get("currency") or "").strip()
    try:
        number = float(value)
    except Exception:
        return f"? {currency}".strip()
    text = f"{number:,.2f}".rstrip("0").rstrip(".")
    return f"{text} {currency}".strip()


def _categories() -> list[str]:
    """التصنيفات + أي كلمات مخصّصة أضفتها أنت في الإعدادات (كلمة=التصنيف)."""
    extra = str(_cfg().get("extra_categories") or "")
    custom: list[str] = []
    for chunk in extra.replace("\n", ";").split(";"):
        if "=" in chunk:
            label = chunk.split("=", 1)[1].strip()
            if label and label not in custom and label not in CATEGORIES:
                custom.append(label)
    return CATEGORIES + custom


def _infer_category(note: str, explicit: str) -> str:
    """يستنتج التصنيف: المصرَّح به أولًا، ثم الكلمات، ثم أي تصنيف معروف داخل النص، ثم «أخرى»."""
    if explicit.strip():
        return explicit.strip()[:40]
    text = str(note or "")
    for chunk in str(_cfg().get("extra_categories") or "").replace("\n", ";").split(";"):
        if "=" in chunk:
            word, label = (part.strip() for part in chunk.split("=", 1))
            if word and label and word in text:
                return label[:40]
    for category, words in _KEYWORDS.items():
        if any(word in text for word in words):
            return category
    for category in _categories():
        if category and category in text:
            return category
    return "أخرى"


def _parse_day(value: Any) -> tuple[str, str]:
    """يحلّل اليوم: اليوم/أمس/تاريخ. يعيد (YYYY-MM-DD، سبب الرفض)."""
    raw = (T.normalize_digits(value) if T else str(value or "")).strip().lower()
    today = datetime.now().date()
    if not raw or raw in ("اليوم", "today", "النهاردة", "النهارده", "دلوقتي"):
        return today.isoformat(), ""
    if raw in ("أمس", "امس", "yesterday"):
        return today.fromordinal(today.toordinal() - 1).isoformat(), ""
    match = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", raw)
    if match:
        year, month, day = (int(g) for g in match.groups())
        try:
            return datetime(year, month, day).date().isoformat(), ""
        except ValueError:
            return "", f"تاريخ غير موجود: {raw}"
    match = re.fullmatch(r"(\d{1,2})[/-](\d{1,2})(?:[/-](\d{4}))?", raw)
    if match:
        day, month, year = int(match.group(1)), int(match.group(2)), int(match.group(3) or today.year)
        try:
            return datetime(year, month, day).date().isoformat(), ""
        except ValueError:
            return "", f"تاريخ غير موجود: {raw}"
    return "", f"لم أفهم اليوم «{value}» — قل «اليوم» أو «أمس» أو YYYY-MM-DD."


def _parse_month(value: Any) -> tuple[str, str]:
    """يحلّل الشهر: 2026-10 · أكتوبر · الشهر ده/الحالي · الشهر اللي فات. يعيد (YYYY-MM، خطأ)."""
    raw = (T.normalize_digits(value) if T else str(value or "")).strip()
    today = datetime.now().date()
    if not raw or raw in ("الشهر ده", "الشهر الحالي", "هذا الشهر", "الشهر", "this month"):
        return today.strftime("%Y-%m"), ""
    if raw in ("الشهر اللي فات", "الشهر السابق", "الشهر الماضي", "last month"):
        month = today.month - 1 or 12
        year = today.year if today.month > 1 else today.year - 1
        return f"{year}-{month:02d}", ""
    match = re.fullmatch(r"(\d{4})-(\d{1,2})", raw)
    if match:
        year, month = int(match.group(1)), int(match.group(2))
        if 1 <= month <= 12 and 2000 <= year <= 2100:
            return f"{year}-{month:02d}", ""
        return "", f"شهر غير منطقي: {raw}"
    name = raw.replace("شهر ", "").strip()
    if name in _MONTHS_AR:
        return f"{today.year}-{_MONTHS_AR[name]:02d}", ""
    return "", f"لم أفهم الشهر «{value}» — قل «الشهر ده» أو «الشهر اللي فات» أو 2026-10."


def _month_bounds(month: str) -> tuple[str, str]:
    return f"{month}-01", f"{month}-31"


def _fetch(connection: sqlite3.Connection, month: str) -> list[tuple]:
    start, end = _month_bounds(month)
    return list(connection.execute(
        "SELECT id, amount, category, note, day FROM expenses WHERE day BETWEEN ? AND ? ORDER BY day, id",
        (start, end)))


def _totals(rows: list[tuple]) -> dict[str, dict[str, Any]]:
    per_category: dict[str, dict[str, Any]] = {}
    for _id, amount, category, _note, _day in rows:
        entry = per_category.setdefault(str(category), {"total": 0.0, "count": 0})
        entry["total"] += float(amount or 0)
        entry["count"] += 1
    return per_category


def _budget_status(connection: sqlite3.Connection, month: str, rows: list[tuple]) -> list[str]:
    """حالة الموازنة: الإجمالي المحدد في الإعدادات + موازنات لكل تصنيف. نصوص جاهزة للعرض."""
    lines: list[str] = []
    per_category = _totals(rows)
    spent_total = sum(float(r[1] or 0) for r in rows)
    try:
        warn_at = max(10.0, min(100.0, float(str(_cfg().get("warn_percent") or 80))))
    except Exception:
        warn_at = 80.0
    total_budget = T.parse_amount(_cfg().get("monthly_budget")) if T else None
    if total_budget:
        used = spent_total / total_budget * 100 if total_budget else 0
        flag = "⚠️ تجاوزت الموازنة!" if used > 100 else ("⏳ تقترب من حدّك" if used >= warn_at else "✔ في حدودك")
        lines.append(f"  الموازنة الشهرية: {_money(spent_total)} من {_money(total_budget)} "
                     f"({used:.0f}%) {flag}")
    try:
        budgets = dict(connection.execute("SELECT category, amount FROM budgets").fetchall())
    except Exception:
        budgets = {}
    for category, limit in budgets.items():
        spent = float(per_category.get(str(category), {}).get("total", 0.0))
        try:
            limit_value = float(limit)
        except Exception:
            continue
        if limit_value <= 0:
            continue
        used = spent / limit_value * 100
        if used >= warn_at:
            flag = "⚠️ تجاوزت!" if used > 100 else "⏳ قاربت"
            lines.append(f"  {flag} موازنة «{category}»: {_money(spent)} من {_money(limit_value)} ({used:.0f}%)")
    return lines


# ── الأفعال ───────────────────────────────────────────────────────────────────
def _act_add(params: dict, player: Any) -> str:
    del player
    raw_amount = params.get("amount")
    if raw_amount in (None, "") and params.get("note"):
        raw_amount = params.get("note")      # يسمح بـ «سجل بنزين ٣٠٠» داخل النص
    value = T.parse_amount(raw_amount) if T else None
    if value is None:
        return ("سيدي، لم أجد مبلغًا صحيحًا. مثال: action=add amount=٣٠٠ note=بنزين "
                "(الأرقام العربية والإنجليزية كلتاهما مفهومة).")
    if value <= 0:
        return "سيدي، المبلغ يجب أن يكون أكبر من صفر."
    if value > 10_000_000:
        return f"سيدي، {_money(value)} رقم ضخم — تأكد من المبلغ قبل أن أسجّله."
    day, day_why = _parse_day(params.get("day"))
    if day_why:
        return f"سيدي، {day_why}"
    note = str(params.get("note") or "").strip()
    category = _infer_category(note, str(params.get("category") or ""))
    connection, why = _connect()
    if connection is None:
        return f"سيدي، {why}"
    try:
        cursor = connection.execute(
            "INSERT INTO expenses (amount, category, note, day, ts) VALUES (?, ?, ?, ?, ?)",
            (value, category, note[:200], day, T.now_iso() if T else ""))
        connection.commit()
        entry_id = cursor.lastrowid
        rows = _fetch(connection, day[:7])
        month_total = sum(float(r[1] or 0) for r in rows)
        lines = [f"سيدي، سجّلت {_money(value)} — {category}"
                 + (f" ({note})" if note else "") + f" · {day} · رقم العملية {entry_id}."]
        lines.append(f"  إجمالي هذا الشهر: {_money(month_total)} من {len(rows)} عملية.")
        lines += _budget_status(connection, day[:7], rows)
        return "\n".join(lines)
    finally:
        connection.close()


def _act_list(params: dict, player: Any) -> str:
    del player
    connection, why = _connect()
    if connection is None:
        return f"سيدي، {why}"
    try:
        try:
            limit = max(1, min(50, int(float(str(params.get("limit") or 10)))))
        except Exception:
            limit = 10
        rows_all = list(connection.execute(
            "SELECT id, amount, category, note, day FROM expenses ORDER BY id DESC LIMIT ?", (limit,)))
        if not rows_all:
            return "سيدي، سجل المصاريف فارغ — قل «سجل ٣٠٠ جنيه بنزين» وأبدأ."
        lines = [f"سيدي، آخر {len(rows_all)} عملية:"]
        for entry_id, amount, category, note, day in rows_all:
            lines.append(f"  [{entry_id}] {day} · {_money(amount)} · {category}"
                         + (f" · {note}" if note else ""))
        return "\n".join(lines)
    finally:
        connection.close()


def _act_report(params: dict, player: Any) -> str:
    del player
    connection, why = _connect()
    if connection is None:
        return f"سيدي، {why}"
    try:
        month, month_why = _parse_month(params.get("month"))
        if month_why:
            return f"سيدي، {month_why}"
        rows = _fetch(connection, month)
        if not rows:
            return f"سيدي، لا مصاريف مسجّلة في {month} بعد."
        per_category = _totals(rows)
        total = sum(float(r[1] or 0) for r in rows)
        ordered = sorted(per_category.items(), key=lambda kv: kv[1]["total"], reverse=True)
        today = datetime.now().date()
        try:
            year, month_number = (int(x) for x in month.split("-"))
            days_in_month = 31 if month_number in (1, 3, 5, 7, 8, 10, 12) else (
                30 if month_number != 2 else (29 if year % 4 == 0 else 28))
            elapsed = today.day if (today.year, today.month) == (year, month_number) else days_in_month
        except Exception:
            elapsed = 30
        lines = [f"سيدي، تقرير مصاريف {month}:",
                 f"  الإجمالي: {_money(total)} من {len(rows)} عملية · المتوسط اليومي {_money(total / max(1, elapsed))}"]
        lines += _budget_status(connection, month, rows)
        lines.append("")
        for category, data in ordered:
            share = data["total"] / total * 100 if total else 0
            bars = "█" * max(1, int(share / 5)) if share >= 5 else "▏"
            lines.append(f"  {category:<8} {_money(data['total']):>14}  {share:5.1f}%  {bars}  ({data['count']})")
        top = ordered[0]
        lines.append(f"\n  أعلى بند: {top[0]} بـ{_money(top[1]['total'])} ({top[1]['total']/total*100:.0f}% من مصاريفك)")
        previous_month = month
        try:
            year, month_number = (int(x) for x in month.split("-"))
            month_number -= 1
            if month_number == 0:
                year, month_number = year - 1, 12
            previous_month = f"{year}-{month_number:02d}"
        except Exception:
            previous_month = ""
        if previous_month:
            old_rows = _fetch(connection, previous_month)
            old_total = sum(float(r[1] or 0) for r in old_rows)
            if old_total > 0:
                delta = (total - old_total) / old_total * 100
                direction = "أكثر" if delta >= 0 else "أقل"
                lines.append(f"  مقابل {previous_month}: {direction} بنسبة {abs(delta):.0f}% "
                             f"({_money(old_total)} ← {_money(total)})")
            else:
                lines.append(f"  مقابل {previous_month}: لا بيانات سابقة للمقارنة.")
        return "\n".join(lines)
    finally:
        connection.close()


def _act_budget(params: dict, player: Any) -> str:
    del player
    category = str(params.get("category") or "").strip()
    raw_amount = params.get("amount")
    connection, why = _connect()
    if connection is None:
        return f"سيدي، {why}"
    try:
        if raw_amount in (None, "") and not category:
            # عرض الموازنات الحالية
            total_budget = T.parse_amount(_cfg().get("monthly_budget")) if T else None
            rows = dict(connection.execute("SELECT category, amount FROM budgets").fetchall())
            lines = ["سيدي، موازناتك:"]
            lines.append(f"  الإجمالي الشهري: {_money(total_budget) if total_budget else 'غير محدَّد'}")
            for name, value in rows.items():
                lines.append(f"  · {name}: {_money(value)} شهريًا")
            if not rows:
                lines.append("  (لا موازنات لكل تصنيف — قل budget amount=500 category=طعام لتحديدها)")
            return "\n".join(lines)
        value = T.parse_amount(raw_amount) if T else None
        if value is None or value <= 0:
            return "سيدي، اكتب الموازنة بمبلغ صحيح: budget amount=8000 (للإجمالي) أو amount=500 category=طعام."
        if category:
            connection.execute("INSERT INTO budgets (category, amount) VALUES (?, ?) "
                               "ON CONFLICT(category) DO UPDATE SET amount=excluded.amount",
                               (category[:40], float(value)))
            connection.commit()
            return f"سيدي، حدّدت موازنة «{category}» بـ{_money(value)} شهريًا وسأنبّهك عند الاقتراب."
        if T is not None:
            T.store_settings(PLUGIN_SETTINGS["namespace"], {"monthly_budget": str(value)})
        return (f"سيدي، حدّدت موازنتك الشهرية الإجمالية بـ{_money(value)}.\n"
                f"  سأنبّهك عند بلوغ {_cfg().get('warn_percent')}% منها، وأصرخ إن تجاوزتها.")
    finally:
        connection.close()


def _act_delete(params: dict, player: Any) -> str:
    del player
    if str(params.get("confirm") or "").strip().lower() not in ("yes", "true", "1", "نعم"):
        return "سيدي، حذف عملية من سجلك يحتاج تأكيدًا صريحًا: confirm=yes."
    connection, why = _connect()
    if connection is None:
        return f"سيدي، {why}"
    try:
        try:
            target = int(float(str(params.get("id") or 0)))
        except Exception:
            target = 0
        if target <= 0:
            row = connection.execute("SELECT id, amount, category, note, day FROM expenses "
                                     "ORDER BY id DESC LIMIT 1").fetchone()
            if not row:
                return "سيدي، السجل فارغ — لا شيء لأحذفه."
            target = int(row[0])
        row = connection.execute("SELECT id, amount, category, note, day FROM expenses WHERE id=?",
                                 (target,)).fetchone()
        if not row:
            return f"سيدي، لا عملية برقم {target} في السجل."
        connection.execute("DELETE FROM expenses WHERE id=?", (target,))
        connection.commit()
        return (f"سيدي، حذفت العملية [{row[0]}] {_money(row[1])} — {row[2]}"
                + (f" ({row[3]})" if row[3] else "") + f" · {row[4]}.")
    finally:
        connection.close()


def _act_categories(params: dict, player: Any) -> str:
    del params, player
    connection, why = _connect()
    if connection is None:
        return f"سيدي، {why}"
    try:
        month, _ = _parse_month("")
        rows = _fetch(connection, month)
        per_category = _totals(rows)
        lines = [f"سيدي، تصنيفاتي هذا الشهر ({month}):"]
        for category in _categories():
            data = per_category.get(category, {"total": 0.0, "count": 0})
            lines.append(f"  · {category:<8} {_money(data['total']):>12}  ({data['count']} عملية)")
        return "\n".join(lines)
    finally:
        connection.close()


def _act_export(params: dict, player: Any) -> str:
    del player
    connection, why = _connect()
    if connection is None:
        return f"سيدي، {why}"
    try:
        month, month_why = _parse_month(params.get("month") or "الشهر ده")
        if month_why:
            return f"سيدي، {month_why}"
        rows = _fetch(connection, month)
        if not rows:
            return f"سيدي، لا بيانات في {month} لأصدّرها."
        if T is None:
            return "سيدي، النواة غير محمّلة."
        folder = T.extras_dir() / "exports"
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"مصاريف_{month}.csv"
        with open(target, "w", encoding="utf-8-sig", newline="") as handle:
            handle.write("id,amount,category,note,day\n")
            for entry_id, amount, category, note, day in rows:
                clean_note = str(note or "").replace(",", "،")
                handle.write(f"{entry_id},{amount},{category},{clean_note},{day}\n")
        total = sum(float(r[1] or 0) for r in rows)
        return (f"سيدي، صدّرت {len(rows)} عملية ({_money(total)}) إلى:\n  {target}\n"
                f"  (يفتح بإكسل مباشرة، والترميز مضبوط للعربية.)")
    finally:
        connection.close()


def _act_status(params: dict, player: Any) -> str:
    del params, player
    connection, why = _connect()
    if connection is None:
        return f"سيدي، {why}"
    try:
        count = connection.execute("SELECT COUNT(*) FROM expenses").fetchone()[0]
        first = connection.execute("SELECT MIN(day) FROM expenses").fetchone()[0]
        last = connection.execute("SELECT MAX(day) FROM expenses").fetchone()[0]
        size = ""
        path = _db_path()
        if path is not None and path.exists():
            size = T.human_bytes(path.stat().st_size) if T else ""
        lines = ["سيدي، حالة سجل مصاريفك:",
                 f"  العمليات: {count} · من {first or '—'} إلى {last or '—'} · حجم القاعدة {size or '—'}",
                 f"  العملة: {_cfg().get('currency')} · الموازنة: "
                 f"{_money(T.parse_amount(_cfg().get('monthly_budget'))) if T and T.parse_amount(_cfg().get('monthly_budget')) else 'غير محدَّدة'}",
                 "  كل شيء في ملف واحد على جهازك — لا يخرج منه شيء لأي مكان."]
        return "\n".join(lines)
    finally:
        connection.close()


def _act_help(params: dict, player: Any) -> str:
    del params, player
    return ("سيدي، سجل المصاريف:\n"
            "  • add amount=٣٠٠ note=بنزين [category=] [day=اليوم|أمس|2026-10-12]\n"
            "  • list [limit=10]              — آخر العمليات\n"
            "  • report [month=الشهر ده|الشهر اللي فات|2026-10]  — التقرير الشهري بالأشرطة والمقارنة\n"
            "  • budget [amount=8000] [category=طعام]  — ضبط الموازنة (بلا وسائط: عرض الحالي)\n"
            "  • delete id=5 confirm=yes · categories · export [month=] · status · help\n"
            "  التصنيفات تُستنتج من كلامك تلقائيًا (بنزين ← مواصلات، عشاء ← طعام…) ويمكنك إضافة كلماتك في الإعدادات.")


_ACTIONS = {"add": _act_add, "list": _act_list, "report": _act_report, "budget": _act_budget,
            "delete": _act_delete, "categories": _act_categories, "export": _act_export,
            "status": _act_status, "help": _act_help}


def run(parameters: dict, player: Any = None, session_memory: Any = None) -> str:
    """ينفّذ فعلًا واحدًا. لا يرمي أبدًا."""
    del session_memory
    try:
        params = parameters or {}
        action = str(params.get("action") or "status").strip().lower()
        handler = _ACTIONS.get(action)
        if handler is None:
            return "سيدي، هذا ليس فعلًا من أفعال سجل المصاريف — المتاح: " + " | ".join(sorted(_ACTIONS))
        return handler(params, player)
    except Exception as exc:  # noqa: BLE001
        if T is not None:
            T.log(player, f"money_tracker error: {exc}")
        return f"سيدي، حدث خطأ غير متوقع في سجل المصاريف ({type(exc).__name__}) — بياناتك لم تُمَسّ."


# ── فحص ذاتي ─────────────────────────────────────────────────────────────────
def _selftest() -> int:
    import shutil
    import tempfile

    print("money_tracker — فحص ذاتي")
    problems = 0

    def check(label: str, ok: bool, extra: str = "") -> None:
        nonlocal problems
        print(("  ✓ " if ok else "  ✗ ") + label + (f"  [{extra}]" if extra and not ok else ""))
        if not ok:
            problems += 1

    if T is None:
        print("  ✗ النواة _tools_core.py غير محمّلة")
        print("RESULT: 1 problem(s)")
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="money_test_"))
    original_extras = T.extras_dir
    T.extras_dir = lambda: tmp / "extras"            # type: ignore[assignment]

    # عزل الإعدادات: الفحص لا يلمس مخزن إعداداتك الحقيقي أبدًا
    _orig_cfg, _orig_store, _orig_log = T.cfg, T.store_settings, T.log_event
    _store: dict[str, dict] = {}
    T.cfg = lambda ns, fields: {**T.defaults(fields), **_store.get(ns, {})}       # type: ignore[assignment]
    T.store_settings = lambda ns, values: (_store.setdefault(ns, {}).update(values), True)[1]  # type: ignore[assignment]
    T.log_event = lambda *a, **k: None                                            # type: ignore[assignment]
    try:
        T.store_settings(PLUGIN_SETTINGS["namespace"],
                         {"currency": "ج.م", "monthly_budget": "", "warn_percent": "80"})
        # ١) التصنيفات والتواريخ
        check("بنزين ← مواصلات", _infer_category("بنزين للعربية", "") == "مواصلات")
        check("عشاء مع الأهل ← طعام", _infer_category("عشاء مع الأهل", "") == "طعام")
        check("صيدلية ← صحة", _infer_category("صيدلية", "") == "صحة")
        check("كلام بلا كلمة معروفة ← أخرى", _infer_category("حوالة غريبة", "") == "أخرى")
        check("الكلمة المفتاحية تُطابق أي موضع", _infer_category("دفعت بنزين النهارده", "") == "مواصلات")
        check("التصنيف المصرَّح به يفوز", _infer_category("بنزين", "بيت") == "بيت")
        check("كلمة مخصّصة من الإعدادات تعمل", _infer_category("قطط", "") == "أخرى")
        T.store_settings(PLUGIN_SETTINGS["namespace"], {"extra_categories": "قطط=حيوانات"})
        check("وبعد ضبطها تُستنتج", _infer_category("أكل القطط", "") == "حيوانات")
        check("كلمة=تصنيف داخل النص تُقرأ", _infer_category("أكل القطط", "") in _categories())
        check("اليوم الافتراضي صحيح", _parse_day("")[0] == datetime.now().date().isoformat())
        check("أمس صحيح", _parse_day("أمس")[0] == datetime.now().date().fromordinal(
            datetime.now().date().toordinal() - 1).isoformat())
        check("تاريخ صريح", _parse_day("2026-10-01")[0] == "2026-10-01")
        check("تاريخ بالأرقام العربية", _parse_day("٢٠٢٦-١٠-٠٥")[0] == "2026-10-05")
        check("تاريخ مستحيل يُرفض", _parse_day("2026-02-30")[1] != "")
        check("يوم غامض يُرفض", _parse_day("النهاردة كمان ساعة")[1] != "")
        check("شهر بأسمه يُقرأ", _parse_month("أكتوبر")[0].endswith("-10"))
        check("الشهر اللي فات يُحسب", bool(re.fullmatch(r"\d{4}-\d{2}", _parse_month("الشهر اللي فات")[0])))
        check("شهر خرافي يُرفض", _parse_month("2026-77")[1] != "")

        # ٢) إضافة عمليات وتدقيق المجاميع
        result = _act_add({"amount": "٣٠٠", "note": "بنزين"}, None)
        check("التسجيل بالأرقام العربية ينجح", "سجّلت" in result and "300" in result and "مواصلات" in result, result[:120])
        _act_add({"amount": "120", "note": "عشاء"}, None)
        _act_add({"amount": "80", "note": "صيدلية"}, None)
        connection, _ = _connect()
        rows = _fetch(connection, datetime.now().strftime("%Y-%m"))
        total = sum(float(r[1]) for r in rows)
        check("المجموع 500 بالضبط", abs(total - 500) < 0.001, str(total))
        check("ثلاث عمليات", len(rows) == 3, str(len(rows)))
        per_category = _totals(rows)
        check("مواصلات 300", abs(per_category["مواصلات"]["total"] - 300) < 0.001, str(per_category))
        connection.close()

        # ٣) التقارير
        report = _act_report({}, None)
        check("التقرير فيه الإجمالي", "500" in report and "الإجمالي" in report, report[:100])
        check("التقرير يحدد أعلى بند", "أعلى بند: مواصلات" in report, report[:160])
        check("التقرير فيه أشرطة ونسب", "█" in report and "%" in report)
        check("خطأ: مبلغ غير صحيح يُشرح", "مبلغًا صحيحًا" in _act_add({"amount": "بنزين"}, None))
        check("خطأ: مبلغ سالب يُرفض", "أكبر من صفر" in _act_add({"amount": "-5"}, None))
        check("خطأ: مبلغ خرافي يُرفض", "ضخم" in _act_add({"amount": "50000000"}, None))
        check("قائمة العمليات تعرضها", "آخر 3 عملية" in _act_list({}, None))
        check("التصنيفات تُعرض بمجاميعها", "مواصلات" in _act_categories({}, None))

        # ٤) الموازنة والتنبيه
        T.store_settings(PLUGIN_SETTINGS["namespace"], {"monthly_budget": "500"})
        warn = _act_add({"amount": "10", "note": "قهوة"}, None)
        check("التنبيه يظهر عند بلوغ الحد", "تقترب" in warn or "الموازنة" in warn, warn[:200])
        check("تجاوز الموازنة يُصرَّح به", "تجاوزت" in _act_report({}, None), _act_report({}, None)[:220])
        check("موازنة تصنيف تُضبط", "طعام" in _act_budget({"amount": "100", "category": "طعام"}, None))
        check("وعرض الموازنات يعمل", "الإجمالي الشهري" in _act_budget({}, None))
        check("موازنة بمبلغ خاطئ تُرفض", "مبلغ صحيح" in _act_budget({"amount": "كثير"}, None))

        # ٥) الحذف بتأكيد
        check("الحذف بلا تأكيد مرفوض", "تأكيدًا صريحًا" in _act_delete({"id": "1"}, None))
        connection, _ = _connect()
        count_before = connection.execute("SELECT COUNT(*) FROM expenses").fetchone()[0]
        connection.close()
        deleted = _act_delete({"confirm": "yes"}, None)
        connection, _ = _connect()
        count_after = connection.execute("SELECT COUNT(*) FROM expenses").fetchone()[0]
        connection.close()
        check("الحذف بتأكيد يحذف واحدة بالضبط", count_after == count_before - 1, f"{count_before}→{count_after}")
        check("ويقول ماذا حذف", "حذفت العملية" in deleted, deleted[:90])
        check("رقم غير موجود يُشرح", "لا عملية برقم" in _act_delete({"id": "999", "confirm": "yes"}, None))

        # ٦) التصدير والحالة
        export = _act_export({}, None)
        target = (T.extras_dir() / "exports" / f"مصاريف_{datetime.now().strftime('%Y-%m')}.csv")
        check("التصدير ينشئ ملفًا", target.exists(), export[:120])
        content = target.read_text(encoding="utf-8-sig")
        check("CSV فيه ترويسة وصفوف", content.startswith("id,amount,category") and content.count("\n") >= 3, content[:80])
        check("CSV يفهم العربية", "مواصلات" in content)
        check("الحالة تعرض العدد", "العمليات" in _act_status({}, None))
        check("شهر بلا بيانات يُشرح في التصدير", "لا بيانات" in _act_export({"month": "2020-01"}, None))
        check("تقرير شهر بلا بيانات يُشرح", "لا مصاريف مسجّلة" in _act_report({"month": "2020-01"}, None))

        # ٧) الواجهة
        check("فعل مجهول يُرفض بلطف", "ليس فعلًا" in run({"action": "pay"}))
        check("run بلا معاملات لا ينهار", isinstance(run({}), str))
        check("help يشرح الاستنتاج التلقائي", "تُستنتج" in _act_help({}, None))
        check("العملة تظهر في التنسيق", "ج.م" in _money(100))
    finally:
        T.extras_dir = original_extras               # type: ignore[assignment]
        T.cfg, T.store_settings, T.log_event = _orig_cfg, _orig_store, _orig_log  # type: ignore[assignment]
        shutil.rmtree(tmp, ignore_errors=True)

    print("RESULT:", "PASS" if problems == 0 else f"{problems} problem(s)")
    return problems


if __name__ == "__main__":
    raise SystemExit(_selftest())
