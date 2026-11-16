"""
_tools_core.py — نواة مساعدة للإضافات الجديدة (تُنسخ معها، ولا تُحمَّل كإضافة لأن اسمها يبدأ بشرطة سفلية).

تجمع كل ما تحتاجه الإضافات الستة بشكل موحّد بدل تكراره في كل ملف:

• **إعدادات**: تستخدم مخزن مشروعك نفسه (`_orch_core`) إن وُجد، وإلا مخزن JSON محلي صغير.
• **سلة المهملات**: نقل آمن إلى سلة محرّر الملفات في ويندوز (SHFileOperationW مع FOF_ALLOWUNDO)،
  ومنطقة حجر داخل المشروع إن تعذّر — **لا حذف نهائي في أي مسار من هذا الملف إطلاقًا**.
• **نداء إضافات أخرى**: عبر `_orch_core.call_plugin` إن وُجد، وإلا تحميل الملف الشقيق مباشرة.
• **أدوات نصية**: تحويل الأرقام العربية ٠-٩، قراءة المبالغ («٣٠٠ جنيه» → 300.0)، حجب الأسرار.
• **حماية المسارات**: `within()` تمنع الخروج عن المجلدات المسموح بها.

🔴 لا سطر يُرمي للخارج، ولا سرّ يُكتب في أي سجل.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

ARABIC_INDIC = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
EXTENDED_INDIC = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")

_HERE = Path(__file__).resolve().parent


# ── المسارات ──────────────────────────────────────────────────────────────────
def plugins_dir() -> Path:
    """مجلد الإضافات (الذي فيه هذا الملف)."""
    return _HERE


def project_root() -> Path:
    """جذر المشروع = المجلد الأب لمجلد الإضافات (حيث main.py و core/)."""
    return _HERE.parent


def extras_dir() -> Path:
    """مجلد إعدادات الإضافات الجديدة داخل config/orchestration (يُنشأ عند الحاجة)."""
    path = project_root() / "config" / "orchestration" / "extras"
    return path


def scratch_dir() -> Path:
    """منطقة الحجر: إلى هنا تُنقل الملفات حين يتعذّر إرسالها لسلة ويندوز."""
    return project_root() / ".jarvis_trash"


def ensure_dirs() -> None:
    """ينشئ المجلدات اللازمة بهدوء."""
    for path in (extras_dir(), scratch_dir()):
        try:
            path.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass


# ── JSON ──────────────────────────────────────────────────────────────────────
def load_json(path: Path, default: Any = None) -> Any:
    """يقرأ JSON، ويعيد الافتراضي عند أي خطأ."""
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default


def save_json(path: Path, data: Any, private: bool = False) -> bool:
    """كتابة ذرّية (ملف مؤقت ثم استبدال). `private=True` تضبط الصلاحيات 0600."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(str(tmp), str(path))
        if private:
            try:
                os.chmod(str(path), 0o600)
            except Exception:
                pass
        return True
    except Exception:
        return False


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d")


# ── الإعدادات: مخزن المشروع أولًا، ثم مخزن محلي ──────────────────────────────
_ORCH: Any = None
_ORCH_TRIED = False


def orch() -> Any:
    """يحاول تحميل `_orch_core.py` (نواة التنسيق) مرة واحدة؛ يعيد None إن غابت."""
    global _ORCH, _ORCH_TRIED
    if _ORCH_TRIED:
        return _ORCH
    _ORCH_TRIED = True
    name = "_jarvis_orch_core"
    if name in sys.modules:
        _ORCH = sys.modules[name]
        return _ORCH
    path = plugins_dir() / "_orch_core.py"
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        _ORCH = module
    except Exception:
        sys.modules.pop(name, None)
        _ORCH = None
    return _ORCH


def defaults(fields: list[dict]) -> dict:
    """قيم افتراضية من تعريف الحقول."""
    return {str(f.get("key")): f.get("default") for f in (fields or [])}


def cfg(namespace: str, fields: list[dict]) -> dict:
    """القيم الحالية = الافتراضي + المحفوظ (الفارغ لا يُلغي الافتراضي).

    نقرأ **القيم المحفوظة** فعلًا من مخزن المشروع (لا دالة الافتراضيات) لئلا تطغى
    الافتراضيات على ما حفظناه في مخزننا المحلي حين يتعذّر مخزن المشروع.
    """
    out = defaults(fields)
    local = {str(k): v for k, v in (load_json(extras_dir() / f"{namespace}.json", {}) or {}).items()
             if v not in (None, "")}
    out.update(local)
    online = orch()
    if online is not None and hasattr(online, "stored_settings"):
        try:
            stored = online.stored_settings(namespace) or {}
            out.update({str(k): v for k, v in stored.items() if v not in (None, "")})
        except Exception:
            pass
    return out


def store_settings(namespace: str, values: dict) -> bool:
    """يحفظ قيمًا في مخزن المشروع إن وُجد، وإلا في مخزننا المحلي."""
    online = orch()
    if online is not None and hasattr(online, "store_settings"):
        try:
            if online.store_settings(namespace, values):
                return True
        except Exception:
            pass
    # احتياطي: مخزننا المحلي — حتى لا تتوقف الإضافة إن غاب مخزن المشروع
    ensure_dirs()
    current = dict(load_json(extras_dir() / f"{namespace}.json", {}) or {})
    current.update({str(k): v for k, v in (values or {}).items()})
    return save_json(extras_dir() / f"{namespace}.json", current)


# ── نداء إضافات أخرى ─────────────────────────────────────────────────────────
def sibling_module(filename: str, module_name: str) -> Optional[Any]:
    """يحمّل ملفًا شقيقًا (.py) من مجلد الإضافات بأمان — يعيد الوحدة أو None."""
    name = f"_jarvis_sib_{module_name}"
    if name in sys.modules:
        return sys.modules[name]
    path = plugins_dir() / filename
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        return None
    return module


def call_plugin(name: str, parameters: Optional[dict] = None,
                timeout_s: float = 25.0) -> tuple[bool, str]:
    """ينادي إضافة أخرى بأمان. يعيد (نجح، نص النتيجة أو سبب الفشل).

    الطريق الأول: نواة التنسيق (فيها مهلة ووقاية من الانهيار). الثاني: الملف الشقيق مباشرة.
    لا ترمي أبدًا، ولا تعطّل الإضافة الأم إن غابت الإضافة المطلوبة.
    """
    clean = str(name or "").strip()
    if not clean:
        return False, "اسم الإضافة مفقود."
    online = orch()
    if online is not None and hasattr(online, "call_plugin"):
        try:
            if hasattr(online, "run_with_timeout"):
                done, result, timed_out = online.run_with_timeout(
                    lambda: online.call_plugin(clean, parameters or {}), timeout_s)
                if timed_out:
                    return False, f"الإضافة {clean} تجاوزت {timeout_s:.0f} ثانية."
                if not done:
                    return False, f"الإضافة {clean} فشلت: {str(result)[:120]}"
                return True, str(result)
            return True, str(online.call_plugin(clean, parameters or {}))
        except Exception as exc:  # noqa: BLE001
            return False, f"تعذّر نداء {clean} ({type(exc).__name__}: {str(exc)[:100]})"
    module = sibling_module(f"{clean}.py", clean)
    if module is None:
        return False, f"الإضافة {clean} غير موجودة في مجلد plugins."
    try:
        return True, str(module.run(parameters or {}))
    except Exception as exc:  # noqa: BLE001
        return False, f"الإضافة {clean} أخطأت ({type(exc).__name__}: {str(exc)[:100]})"


def notify(title: str, body: str, urgent: bool = False) -> tuple[bool, str]:
    """إشعار للمستخدم: عبر notification_center إن وُجد، وإلا يُكتب في ملف سجل صغير."""
    action = "urgent" if urgent else "notify"
    ok, text = call_plugin("notification_center", {"action": action, "title": title, "body": body}, timeout_s=15)
    if ok:
        return True, text
    try:
        ensure_dirs()
        fallback = extras_dir() / "notifications.log"
        with open(fallback, "a", encoding="utf-8") as handle:
            handle.write(f"{now_iso()} | {title} | {body}\n")
        return False, f"notification_center غير متاحة ({text[:60]}) — سُجّل الإشعار في {fallback}"
    except Exception:
        return False, f"تعذّر الإشعار ({text[:60]})"


def log_event(plugin: str, action: str, status: str, duration_ms: int = 0, detail: str = "") -> None:
    """سجل منظَّم عبر نواة التنسيق إن وُجد، وإلا ملف JSONL محلي. لا يحوي أي سرّ."""
    online = orch()
    if online is not None and hasattr(online, "log_event"):
        try:
            online.log_event(plugin, action, status, duration_ms, detail)
            return
        except Exception:
            pass
    try:
        ensure_dirs()
        line = {"at": now_iso(), "plugin": plugin, "action": action, "status": status,
                "duration_ms": int(duration_ms), "detail": redact_text(detail, 200)}
        with open(extras_dir() / "extras_log.jsonl", "a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ── السلة الآمنة (لا حذف نهائي أبدًا) ────────────────────────────────────────
def _windows_recycle(items: list[str]) -> tuple[bool, str]:
    """يرسل الملفات لسلة محرّر الملفات في ويندوز. يعيد (نجح، سبب الفشل)."""
    if os.name != "nt":
        return False, "ليست ويندوز."
    try:
        import ctypes
        from ctypes import wintypes

        class _Op(ctypes.Structure):
            _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT),
                        ("pFrom", wintypes.LPCWSTR), ("pTo", wintypes.LPCWSTR),
                        ("fFlags", ctypes.c_uint16), ("fAnyOperationsAborted", wintypes.BOOL),
                        ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]

        flags = 0x0040 | 0x0010 | 0x0004 | 0x0400   # ALLOWUNDO | NOCONFIRMATION | SILENT | NOERRORUI
        joined = "\0".join(items) + "\0\0"
        op = _Op(None, 3, joined, None, flags, False, None, None)   # FO_DELETE
        result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
        if result != 0:
            return False, f"أعاد ويندوز رمز {result}."
        if op.fAnyOperationsAborted:
            return False, "أُلغيت العملية من النظام."
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {str(exc)[:80]}"


def recycle_paths(paths: Iterable[Any]) -> tuple[list[str], str, list[str]]:
    """ينقل ملفات/مجلدات إلى السلة أو منطقة حجر — ولا يحذف حذفًا نهائيًا بحال.

    يعيد (المنقولة، وصف المكان، الأخطاء). لا يرمي أبدًا.
    """
    items = [str(Path(p)) for p in (paths or []) if str(p or "").strip()]
    moved: list[str] = []
    errors: list[str] = []
    if not items:
        return moved, "", ["لا ملفات محدَّدة."]
    existing = [p for p in items if Path(p).exists()]
    for missing in [p for p in items if p not in existing]:
        errors.append(f"غير موجود: {missing}")
    if not existing:
        return moved, "", errors
    if os.name == "nt":
        ok, why = _windows_recycle(existing)
        if ok:
            return list(existing), "سلة محرّر الملفات (يمكن استرجاعها بالزر الأيمن ← استعادة)", errors
        errors.append(f"تعذّر إرسالها لسلة ويندوز ({why}) — سأنقلها لمنطقة الحجر بدلًا من ذلك.")
    try:
        target = scratch_dir() / time.strftime("%Y%m%d_%H%M%S")
        target.mkdir(parents=True, exist_ok=True)
        for item in existing:
            src = Path(item)
            dest = target / src.name
            counter = 1
            while dest.exists():
                dest = target / f"{src.stem}_{counter}{src.suffix}"
                counter += 1
            shutil.move(str(src), str(dest))
            moved.append(str(dest))
        return moved, f"منطقة الحجر: {target}", errors
    except Exception as exc:  # noqa: BLE001
        errors.append(f"{type(exc).__name__}: {str(exc)[:100]}")
        return moved, "", errors


def trash_size() -> int:
    """حجم منطقة الحجر بالبايت (لعرضه في الحالة)."""
    total = 0
    try:
        for path in scratch_dir().rglob("*"):
            if path.is_file():
                total += path.stat().st_size
    except Exception:
        pass
    return total


# ── مسارات آمنة ───────────────────────────────────────────────────────────────
def resolve_dir(value: Any, default: Optional[Path] = None) -> Optional[Path]:
    """يحوّل نصًا إلى مجلد موجود. يعيد None إن لم يكن مجلدًا صالحًا."""
    try:
        raw = str(value or "").strip().strip('"').strip("'")
        path = Path(os.path.expandvars(os.path.expanduser(raw))) if raw else (default or Path.home())
        return path if path.is_dir() else None
    except Exception:
        return None


def within(path: Any, roots: Iterable[Any]) -> bool:
    """هل المسار داخل أحد المجلدات المسموح بها؟ (يمنع الخروج بـ .. أو مسارات النظام)."""
    try:
        target = Path(path).resolve()
        for root in roots:
            try:
                base = Path(root).resolve()
            except Exception:
                continue
            if target == base or base in target.parents:
                return True
        return False
    except Exception:
        return False


def resolve_file(value: Any, roots: Optional[list[Path]] = None) -> tuple[Optional[Path], str]:
    """يتحقق من ملف مطلوب: موجود + داخل الجذور المسموح بها. يعيد (الملف أو None، سبب)."""
    try:
        raw = str(value or "").strip().strip('"').strip("'")
        if not raw:
            return None, "لم تحدّد ملفًا."
        path = Path(os.path.expandvars(os.path.expanduser(raw)))
        if not path.exists():
            return None, f"لا أجد الملف: {path}"
        if not path.is_file():
            return None, f"هذا ليس ملفًا: {path}"
        if roots and not within(path, roots):
            return None, f"المسار خارج المجلدات المسموح بها: {path}"
        return path, ""
    except Exception as exc:  # noqa: BLE001
        return None, f"مسار غير صالح ({type(exc).__name__})."


# ── نصوص وأرقام ───────────────────────────────────────────────────────────────
def normalize_digits(text: Any) -> str:
    """يحوّل الأرقام العربية والفارسية إلى إنجليزية."""
    return str(text or "").translate(ARABIC_INDIC).translate(EXTENDED_INDIC)


def parse_amount(text: Any) -> Optional[float]:
    """يقرأ مبلغًا من نص حر: «٣٠٠ جنيه» · «1,250.50 EGP» · «١٢٥٫٧٥» → float أو None."""
    raw = normalize_digits(text).strip()
    if not raw:
        return None
    raw = raw.replace("٬", "").replace(",", "").replace("٫", ".")
    match = re.search(r"-?\d+(?:\.\d+)?", raw)
    if not match:
        return None
    try:
        value = float(match.group(0))
    except Exception:
        return None
    if abs(value) > 1_000_000_000:
        return None
    return value


def human_bytes(size: Any) -> str:
    """حجم مقروء: «1.4 ج.ب»."""
    try:
        value = float(size)
    except Exception:
        return "0 ب"
    for unit, div in (("ت.ب", 1024 ** 4), ("ج.ب", 1024 ** 3), ("م.ب", 1024 ** 2), ("ك.ب", 1024)):
        if value >= div:
            return f"{value / div:.1f} {unit}"
    return f"{int(value)} ب"


def mask_secret(value: Any, keep: int = 4) -> str:
    """يخفي قيمة سرّية: «sk-…9f2a»."""
    text = str(value or "")
    if len(text) <= keep:
        return "•" * max(3, len(text))
    return "•" * 6 + text[-keep:]


_SECRET_WORDS = ("password", "passwd", "secret", "token", "api_key", "apikey", "access_key",
                 "private_key", "master", "pin", "key")


def looks_secret(key: Any) -> bool:
    """هل اسم هذا المفتاح يوحي بأن قيمته سرّية؟"""
    lowered = str(key or "").lower()
    return any(word in lowered for word in _SECRET_WORDS)


def redact_text(text: Any, limit: int = 400) -> str:
    """يحجب أي سلسلة طويلة تشبه المفتاح قبل أي عرض أو تسجيل."""
    raw = str(text or "")
    raw = re.sub(r"\b[A-Za-z0-9_\-]{24,}\b", "[محجوب]", raw)
    if len(raw) > limit:
        raw = raw[:limit] + "…"
    return raw


def split_sentences(text: Any, limit: int = 1000) -> list[str]:
    """يقطّع نصًا طويلًا إلى قطع عند حدود الجمل (للتحدث بلا انقطاع)."""
    clean = re.sub(r"\s+", " ", str(text or "")).strip()
    if not clean:
        return []
    parts = re.split(r"(?<=[.!?؟।…])\s+|\n+", clean)
    chunks: list[str] = []
    current = ""
    for part in parts:
        candidate = (current + " " + part).strip()
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        while len(part) > limit:
            chunks.append(part[:limit])
            part = part[limit:]
        current = part
    if current:
        chunks.append(current)
    return chunks


def log(player: Any, text: str) -> None:
    """يكتب في سجل جارفس إن أمكن."""
    try:
        if player is not None and hasattr(player, "write_log"):
            player.write_log(text)
    except Exception:
        pass


# ── فحص ذاتي ─────────────────────────────────────────────────────────────────
def _selftest() -> int:
    """فحص ذاتي للنواة: سلوك المخزن، السلة الآمنة، تحليل المبالغ، وحماية المسارات."""
    import tempfile

    print("_tools_core — فحص ذاتي")
    problems = 0

    def check(label: str, ok: bool, extra: str = "") -> None:
        nonlocal problems
        print(("  ✓ " if ok else "  ✗ ") + label + (f"  [{extra}]" if extra and not ok else ""))
        if not ok:
            problems += 1

    tmp = Path(tempfile.mkdtemp(prefix="tools_core_test_"))
    originals = (extras_dir, scratch_dir, orch)
    globals()["extras_dir"] = lambda: tmp / "extras"
    globals()["scratch_dir"] = lambda: tmp / "trash"
    globals()["orch"] = lambda: None      # عزل: لا يلمس الفحص مخزن إعدادات المشروع الحقيقي

    try:
        # الإعدادات
        fields = [{"key": "name", "default": "افتراضي"}, {"key": "size", "default": "10"}]
        check("الافتراضي يُقرأ قبل الحفظ", cfg("selftest_ns", fields)["name"] == "افتراضي")
        check("الحفظ ينجح", store_settings("selftest_ns", {"name": "محفوظ"}))
        check("المحفوظ يُقرأ بعده", cfg("selftest_ns", fields)["name"] == "محفوظ")
        check("القيمة الفارغة لا تلغي الافتراضي", cfg("selftest_ns", fields)["size"] == "10")
        check("ملف الإعدادات مكتوب فعلًا", (extras_dir() / "selftest_ns.json").exists())

        # JSON
        check("قراءة ملف ناقص تعيد الافتراضي", load_json(tmp / "ghost.json", {"x": 1}) == {"x": 1})
        check("كتابة ذرّية تنجح", save_json(tmp / "a.json", {"k": "قيمة"}, private=True))
        check("والقيمة تُقرأ صحيحة", load_json(tmp / "a.json")["k"] == "قيمة")
        if os.name != "nt":
            check("الملف الخاص 0600", oct((tmp / "a.json").stat().st_mode)[-3:] == "600",
                  oct((tmp / "a.json").stat().st_mode)[-3:])

        # السلة الآمنة — أهم فحص: لا حذف نهائي، والمحتوى قابل للاسترجاع
        victim = tmp / "تكرار.txt"
        victim.write_text("محتوى مهم", encoding="utf-8")
        folder = tmp / "مجلد_قديم"
        folder.mkdir()
        (folder / "داخل.txt").write_text("محتوى آخر", encoding="utf-8")
        moved, where, errors = recycle_paths([victim, folder, tmp / "لا_يوجد.txt"])
        check("الملف والمجلد نُقلا", len(moved) == 2, str(moved))
        check("الأصل اختفى فعليًا", not victim.exists() and not folder.exists())
        check("المحتوى قابل للاسترجاع بالكامل",
              any(p.endswith("تكرار.txt") and Path(p).read_text(encoding="utf-8") == "محتوى مهم" for p in moved),
              str(moved))
        check("سطر خطأ للمفقود فقط", len(errors) == 1 and "لا_يوجد" in errors[0], str(errors))
        check("المكان موصوف", bool(where), where)
        check("حجم منطقة الحجر يُحسب", trash_size() > 0, str(trash_size()))
        check("قائمة فاضية لا ترمي", recycle_paths([])[0] == [])

        # المبالغ والأرقام
        check("الأرقام العربية تُحوَّل", normalize_digits("١٢٣٤٥") == "12345")
        check("«٣٠٠ جنيه» → 300", parse_amount("٣٠٠ جنيه") == 300.0, str(parse_amount("٣٠٠ جنيه")))
        check("«1,250.50 EGP» → 1250.5", parse_amount("1,250.50 EGP") == 1250.5)
        check("«١٢٥٫٧٥» → 125.75", parse_amount("١٢٥٫٧٥") == 125.75)
        check("نص بلا رقم → None", parse_amount("بنزين") is None)
        check("مبلغ خرافي يُرفض", parse_amount("9999999999999") is None)

        # الأحجام والحجب
        check("الميجابايت تتنسّق", "م.ب" in human_bytes(5 * 1024 ** 2), human_bytes(5 * 1024 ** 2))
        check("السرّ يُحجب", mask_secret("sk-abcdef123456").endswith("3456") and "abcdef" not in mask_secret("sk-abcdef123456"))
        check("اسم المفتاح السرّي يُكتشف", looks_secret("api_key") and not looks_secret("category"))
        check("النص الطويل يُحجب فيه ما يشبه المفتاح",
              "[محجوب]" in redact_text("المفتاح A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6 انتهى"))

        # تجزيء النص
        chunks = split_sentences("جملة أولى. جملة ثانية! جملة ثالثة؟", limit=20)
        check("التجزيء يحافظ على كل النص", "".join(chunks).count("جملة") == 3, str(chunks))
        check("لا قطعة تتجاوز الحد بكثير", all(len(c) <= 40 for c in chunks), str([len(c) for c in chunks]))

        # حماية المسارات
        root = tmp / "مسموح"
        inside = root / "داخل" / "ملف.txt"
        inside.parent.mkdir(parents=True)
        inside.write_text("ok", encoding="utf-8")
        outside = tmp / "خارج.txt"
        outside.write_text("no", encoding="utf-8")
        check("الداخل مسموح", within(inside, [root]))
        check("الخارج مرفوض", not within(outside, [root]))
        check("الخروج بـ .. مرفوض", not within(root / ".." / "خارج.txt", [root]))
        check("resolve_file يقبل الداخل", resolve_file(inside, [root])[0] == inside)
        check("resolve_file يرفض الخارج بسبب واضح", resolve_file(outside, [root])[0] is None
              and "خارج" in resolve_file(outside, [root])[1], resolve_file(outside, [root])[1])
        check("resolve_file يعالج النقص بلطف", resolve_file("")[0] is None and "ملفًا" in resolve_file("")[1])

        # نداء إضافات غائبة
        ok_call, why_call = call_plugin("لا_توجد_إضافة_كهذه", {"action": "x"})
        check("إضافة غائبة تُشرح بلطف", (not ok_call) and "غير موجودة" in why_call, why_call)
        check("نداء بلا اسم يُرفض", not call_plugin("")[0])
        real_call = call_plugin
        globals()["call_plugin"] = lambda *a, **k: (False, "معزولة في الفحص")   # لا إشعار حقيقي
        sent_ok, sent_note = notify("عنوان تجريبي", "نص تجريبي")
        globals()["call_plugin"] = real_call
        check("الإشعار يسقط لملف داخل مجلدنا عند غياب الإضافة",
              (not sent_ok) and (extras_dir() / "notifications.log").exists(), sent_note[:90])
        check("سطر السجل المنظّم لا يحوي أسرارًا", (log_event("selftest", "check", "ok", 5, "sk-abcdef1234567890abcdef")) is None)
    finally:
        globals()["extras_dir"] = originals[0]
        globals()["scratch_dir"] = originals[1]
        globals()["orch"] = originals[2]
        shutil.rmtree(tmp, ignore_errors=True)

    print("RESULT:", "PASS" if problems == 0 else f"{problems} problem(s)")
    return problems


if __name__ == "__main__":
    raise SystemExit(_selftest())
