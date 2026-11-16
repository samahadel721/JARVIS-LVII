"""
duplicate_finder.py — صائد الملفات المكررة ومنظّف المساحة (محلي، بلا إنترنت).

يجد الملفات المكررة **بالدليل القاطع لا بالاسم**: تجميع بالحجم ← بصمة أول ٦٤ ك.ب ← البصمة
الكاملة (blake2b). ويتعامل بحذر مع ما يبدو مكررًا وليس مكررًا (نفس الحجم ومحتوى مختلف).

• `scan`   : فحص مجلداتك (افتراضيًا: سطح المكتب، المستندات، التنزيلات، الصور، الفيديو، الموسيقى).
• `report` : تقرير بالمكرر والمساحة المهدرة مرتّبًا بالأكبر أولًا.
• `preview`: تفاصيل مجموعة واحدة (كل نسخ الملف وأيها الأحدث).
• `clean`  : **لا يحذف شيئًا أبدًا** — ينقل النسخ الزائدة إلى سلة محرّر الملفات في ويندوز
             (قابلة للاسترجاع)، ولا يعمل إلا بأمر صريح منك (`confirm=yes`).
• `status` : آخر فحص + حجم منطقة الحجر.

قواعد السلامة المطبَّقة: لا يلمس مجلدات النظام (`Windows`, `Program Files`, `AppData`…)،
ولا يخرج عن المجلدات المسموح بها، ولا يتبع اختصارات الرموز (symlinks)، ولا يقرأ إلا الملفات،
ولا يكتب أي سرّ في السجل.
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import sys
import time
from pathlib import Path
from typing import Any

PLUGIN = {
    "name": "duplicate_finder",
    "description": (
        "Finds duplicate files by content fingerprint (hash), not by name, and reports the wasted "
        "space; it can then move the extra copies to the Windows Recycle Bin after an explicit "
        "confirmation — it never deletes permanently. Use for: 'find duplicate files', 'do I have "
        "duplicate photos', 'how much space do duplicates waste', 'clean up duplicates in my "
        "Downloads', 'show me the biggest duplicate groups', 'duplicate finder status'. Do NOT use "
        "for general disk-space reports (that is storage_analyzer) or for deleting a specific file."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "scan | report | preview | clean | status | help"},
            "path": {"type": "STRING", "description": "Folder to scan (default: your common folders)"},
            "min_size_mb": {"type": "NUMBER", "description": "Ignore files smaller than this, default 1 MB"},
            "group": {"type": "NUMBER", "description": "preview: which group number from report"},
            "keep": {"type": "STRING", "description": "clean: which copy to keep — newest (default) | oldest | first"},
            "limit": {"type": "NUMBER", "description": "clean: how many groups to clean this run, default 5"},
            "confirm": {"type": "STRING", "description": "clean: must be 'yes' — otherwise nothing is touched"},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "duplicate_finder",
    "title": "Duplicate finder — folders, filters and safety",
    "fields": [
        {"key": "scan_roots", "type": "text",
         "label": "Folders to scan (semicolon separated, under your home)",
         "default": "Desktop;Documents;Downloads;Pictures;Videos;Music"},
        {"key": "min_size_mb", "type": "text", "label": "Minimum file size (MB)", "default": "1"},
        {"key": "max_files", "type": "text", "label": "Safety cap on files scanned per run", "default": "60000"},
        {"key": "skip_hidden", "type": "choice", "label": "Skip hidden/system files", "default": "yes",
         "choices": ["yes", "no"]},
    ],
}

T = None            # _tools_core
_STATE = "last_scan"

_SKIP_DIRS = {
    "windows", "program files", "program files (x86)", "programdata", "appdata", "$recycle.bin",
    "system volume information", "node_modules", ".git", "__pycache__", ".venv", "venv", "env",
    "site-packages", "dist", "build", "target", "oneDrive".lower(), "onedrive",
    ".jarvis_trash", "recovery", "perflogs", "msocache", "$windows.~ws", "$windows.~bt",
}


def _boot() -> None:
    """يحمّل النواة المساعدة `_tools_core.py` من نفس المجلد — مرة واحدة."""
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


# ── الإعدادات والمسارات ───────────────────────────────────────────────────────
def _cfg() -> dict:
    if T is None:
        return {f["key"]: f.get("default") for f in PLUGIN_SETTINGS["fields"]}
    return T.cfg(PLUGIN_SETTINGS["namespace"], PLUGIN_SETTINGS["fields"])


def _home() -> Path:
    try:
        return Path.home()
    except Exception:
        return Path(os.path.expanduser("~"))


def _allowed_roots() -> list[Path]:
    """المجلدات المسموح للفحص أن يقرأها: بيت المستخدم وما تحته فقط."""
    return [_home()]


def _resolve_roots(path_param: Any) -> tuple[list[Path], list[str]]:
    """يحوّل الإعدادات/الوسيط إلى مجلدات فعلية موجودة داخل البيت. يعيد (الجذور، ملاحظات)."""
    notes: list[str] = []
    raw = str(path_param or "").strip()
    names = [raw] if raw else [p.strip() for p in str(_cfg().get("scan_roots") or "").split(";") if p.strip()]
    roots: list[Path] = []
    for name in names:
        candidate = Path(os.path.expandvars(os.path.expanduser(name)))
        if not candidate.is_absolute():
            candidate = _home() / name
        if not candidate.is_dir():
            notes.append(f"تجاوزت مجلدًا غير موجود: {candidate}")
            continue
        if not T.within(candidate, _allowed_roots()):
            notes.append(f"رفضت مجلدًا خارج نطاقك المسموح: {candidate}")
            continue
        roots.append(candidate)
    return roots, notes


def _skip_dir(name: str) -> bool:
    return name.lower() in _SKIP_DIRS


def _iter_files(root: Path, min_size: int, cap: int) -> tuple[list[tuple[Path, int]], bool]:
    """يمشي على الملفات ويعيد [(مسار، حجم)] بحد أقصى. يعيد أيضًا: هل بلغنا السقف."""
    found: list[tuple[Path, int]] = []
    skip_hidden = str(_cfg().get("skip_hidden") or "yes").lower().startswith("y")
    try:
        for current, dirs, files in os.walk(str(root), followlinks=False):
            dirs[:] = [d for d in dirs if not _skip_dir(d)]
            if skip_hidden:
                dirs[:] = [d for d in dirs if not d.startswith(".")]
            for filename in files:
                if skip_hidden and filename.startswith("."):
                    continue
                full = Path(current) / filename
                try:
                    if full.is_symlink() or not full.is_file():
                        continue
                    if not T.within(full, [root]):
                        continue
                    size = full.stat().st_size
                except Exception:
                    continue
                if size < min_size:
                    continue
                found.append((full, size))
                if len(found) >= cap:
                    return found, True
    except Exception:
        pass
    return found, False


def _partial_hash(path: Path, chunk: int = 65536) -> str:
    """بصمة أول جزء من الملف — فرز رخيص قبل البصمة الكاملة."""
    digest = hashlib.blake2b(digest_size=16)
    try:
        with open(str(path), "rb") as handle:
            digest.update(handle.read(chunk))
        return digest.hexdigest()
    except Exception:
        return ""


def _full_hash(path: Path, chunk: int = 1024 * 1024) -> str:
    """البصمة الكاملة — هي الدليل القاطع على التكرار."""
    digest = hashlib.blake2b(digest_size=16)
    try:
        with open(str(path), "rb") as handle:
            while True:
                block = handle.read(chunk)
                if not block:
                    break
                digest.update(block)
        return digest.hexdigest()
    except Exception:
        return ""


def _scan(roots: list[Path]) -> dict:
    """الفحص الكامل. يعيد قاموسًا بالمجموعات والمهدر والإحصاءات."""
    try:
        min_size = int(float(str(_cfg().get("min_size_mb") or 1)) * 1024 * 1024)
    except Exception:
        min_size = 1024 * 1024
    try:
        cap = max(1000, int(float(str(_cfg().get("max_files") or 60000))))
    except Exception:
        cap = 60000

    started = time.time()
    files: list[tuple[Path, int]] = []
    capped = False
    for root in roots:
        part, hit_cap = _iter_files(root, max(0, min_size), cap - len(files))
        files.extend(part)
        capped = capped or hit_cap
        if len(files) >= cap:
            break

    by_size: dict[int, list[Path]] = {}
    for path, size in files:
        by_size.setdefault(size, []).append(path)

    groups: list[dict] = []
    for size, paths in by_size.items():
        if len(paths) < 2:
            continue
        by_partial: dict[str, list[Path]] = {}
        for path in paths:
            by_partial.setdefault(_partial_hash(path), []).append(path)
        for candidates in by_partial.values():
            if len(candidates) < 2:
                continue
            by_full: dict[str, list[Path]] = {}
            for path in candidates:
                by_full.setdefault(_full_hash(path), []).append(path)
            for fingerprint, same in by_full.items():
                if len(same) < 2:
                    continue
                groups.append({"hash": fingerprint, "size": size,
                               "files": [str(p) for p in sorted(same, key=lambda x: str(x))]})

    groups.sort(key=lambda g: g["size"] * (len(g["files"]) - 1), reverse=True)
    wasted = sum(g["size"] * (len(g["files"]) - 1) for g in groups)
    return {"at": T.now_iso() if T else "", "roots": [str(r) for r in roots], "files_scanned": len(files),
            "capped": capped, "groups": groups, "wasted": wasted, "seconds": round(time.time() - started, 1)}


def _load_state() -> dict:
    if T is None:
        return {}
    return T.load_json(T.extras_dir() / f"{_STATE}.json", {}) or {}


def _save_state(data: dict) -> bool:
    if T is None:
        return False
    return T.save_json(T.extras_dir() / f"{_STATE}.json", data)


def _fmt_path(path: str, limit: int = 96) -> str:
    text = str(path)
    return text if len(text) <= limit else "…" + text[-limit:]


def _group_keep_path(files: list[str], keep: str) -> str:
    """يحدد أي نسخة تُحفظ حسب السياسة (newest افتراضيًا)."""
    policy = str(keep or "newest").lower()
    if policy == "first":
        return files[0]
    if policy == "oldest":
        return min(files, key=lambda p: (os.path.getmtime(p) if os.path.exists(p) else 0))
    return max(files, key=lambda p: (os.path.getmtime(p) if os.path.exists(p) else 0))


# ── الأفعال ───────────────────────────────────────────────────────────────────
def _act_scan(params: dict, player: Any) -> str:
    del player
    if T is None:
        return "سيدي، مكتبة `_tools_core.py` غير موجودة بجواري — انسخها معي في مجلد plugins."
    roots, notes = _resolve_roots(params.get("path"))
    if not roots:
        return "سيدي، لا أجد مجلدًا صالحًا للفحص.\n  " + "\n  ".join(notes or ["حدّد مجلدًا بـ path أو من الإعدادات."])
    if params.get("min_size_mb") not in (None, ""):
        T.store_settings(PLUGIN_SETTINGS["namespace"], {"min_size_mb": str(params.get("min_size_mb"))})
    started = time.time()
    result = _scan(roots)
    _save_state(result)
    T.log_event("duplicate_finder", "scan", "ok", int((time.time() - started) * 1000),
                f"ملفات {result['files_scanned']} · مجموعات {len(result['groups'])}")

    lines = [f"سيدي، فحصت {result['files_scanned']:,} ملفًا في {result['seconds']} ثانية."]
    lines += [f"  · {note}" for note in notes]
    if result["capped"]:
        lines.append("  ⚠️ بلغت سقف الفحص — النتيجة لما رآه فقط. ارفع السقف من الإعدادات لفحص أعمق.")
    if not result["groups"]:
        lines.append("  ✅ لا ملفات مكررة فوق الحد الأدنى — كل شيء نظيف.")
        return "\n".join(lines)
    lines.append(f"  🔁 وجدت {len(result['groups'])} مجموعة مكررة · المهدر: {T.human_bytes(result['wasted'])}")
    lines.append("  قل «تقرير» لرؤية المجموعات، أو «نظّف مع تأكيد» لنقل الزائد إلى السلة.")
    return "\n".join(lines)


def _act_report(params: dict, player: Any) -> str:
    del player
    if T is None:
        return "سيدي، مكتبة `_tools_core.py` غير موجودة بجواري."
    state = _load_state()
    groups = state.get("groups") or []
    if not groups:
        return "سيدي، لا نتائج فحص محفوظة — قل «افحص المكرر» أولًا."
    try:
        limit = max(1, min(50, int(float(params.get("limit") or 12))))
    except Exception:
        limit = 12
    lines = [f"سيدي، الملفات المكررة (آخر فحص: {state.get('at', '؟')}):",
             f"  الإجمالي: {len(groups)} مجموعة · المهدر {T.human_bytes(state.get('wasted', 0))}", ""]
    for index, group in enumerate(groups[:limit], 1):
        files = group["files"]
        lines.append(f"  [{index}] {T.human_bytes(group['size'])} × {len(files)} نسخ "
                     f"→ مهدر {T.human_bytes(group['size'] * (len(files) - 1))}")
        for path in files[:4]:
            lines.append(f"        {_fmt_path(path)}")
        if len(files) > 4:
            lines.append(f"        … و{len(files) - 4} نسخ أخرى")
    if len(groups) > limit:
        lines.append(f"\n  … و{len(groups) - limit} مجموعة أخرى (ارفع limit للرؤية).")
    lines.append("\n  للتفاصيل الكاملة لمجموعة: action=preview group=رقمها")
    lines.append("  للنقل إلى السلة: action=clean confirm=yes  (لا يُحذف شيء نهائيًا أبدًا)")
    return "\n".join(lines)


def _act_preview(params: dict, player: Any) -> str:
    del player
    if T is None:
        return "سيدي، مكتبة `_tools_core.py` غير موجودة بجواري."
    state = _load_state()
    groups = state.get("groups") or []
    if not groups:
        return "سيدي، لا نتائج محفوظة — افحص أولًا."
    try:
        index = int(float(params.get("group") or 1))
    except Exception:
        index = 1
    if not 1 <= index <= len(groups):
        return f"سيدي، رقم المجموعة خارج النطاق — عندي {len(groups)} مجموعة."
    group = groups[index - 1]
    keep = _group_keep_path(group["files"], params.get("keep"))
    lines = [f"سيدي، المجموعة [{index}] — نفس المحتوى بالحرف ({T.human_bytes(group['size'])} لكل نسخة):",
             f"  البصمة: {group['hash'][:16]}…", ""]
    for path in group["files"]:
        try:
            stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(path)))
        except Exception:
            stamp = "؟"
        mark = "  ← الأحدث (تُحفظ)" if path == keep else "  ← نسخة زائدة"
        lines.append(f"  {stamp}  {_fmt_path(path)}{mark}")
    lines.append(f"\n  لو أمرت بالتنظيف: تُحفظ نسخة واحدة ({_fmt_path(keep, 60)}) وتُنقل الباقي إلى السلة.")
    return "\n".join(lines)


def _act_clean(params: dict, player: Any) -> str:
    if T is None:
        return "سيدي، مكتبة `_tools_core.py` غير موجودة بجواري."
    if str(params.get("confirm") or "").strip().lower() not in ("yes", "true", "1", "نعم"):
        return ("سيدي، التنظيف ينقل ملفاتك إلى السلة — لذلك لا أبدأ دون أمر صريح منك.\n"
                "  راجع أولًا: action=report ثم action=preview group=رقم\n"
                "  وإن كنت موافقًا: action=clean confirm=yes\n"
                "  (لا يُحذف شيء نهائيًا: كل ما أنقله يبقى في سلة محرّر الملفات ويمكن استرجاعه)")
    state = _load_state()
    groups = state.get("groups") or []
    if not groups:
        return "سيدي، لا نتائج محفوظة — افحص أولًا (action=scan)."
    try:
        limit = max(1, min(200, int(float(params.get("limit") or 5))))
    except Exception:
        limit = 5
    keep_policy = str(params.get("keep") or "newest").lower()
    if keep_policy not in ("newest", "oldest", "first"):
        return "سيدي، سياسة الحفظ غير معروفة — استخدم newest أو oldest أو first."

    moved_all: list[str] = []
    errors: list[str] = []
    freed = 0
    handled = 0
    for group in groups[:limit]:
        files = [f for f in group["files"] if os.path.exists(f)]
        if len(files) < 2:
            continue
        keep = _group_keep_path(files, keep_policy)
        victims = [f for f in files if f != keep]
        moved, where, problems = T.recycle_paths(victims)
        moved_all.extend(moved)
        errors.extend(problems)
        freed += group["size"] * len(moved)
        handled += 1
        T.log(player, f"duplicate_finder: نُقلت {len(moved)} نسخة من مجموعة {group['hash'][:8]}")
    remaining = [g for g in groups if g not in groups[:limit]]
    state["groups"] = remaining
    state["wasted"] = sum(g["size"] * (len(g["files"]) - 1) for g in remaining)
    _save_state(state)
    T.log_event("duplicate_finder", "clean", "ok" if moved_all else "none",
                detail=f"نُقل {len(moved_all)} ملفًا")

    lines = [f"سيدي، نُقلت {len(moved_all)} نسخة زائدة من {handled} مجموعة."]
    if freed:
        lines.append(f"  المساحة المستردة: {T.human_bytes(freed)} (تبقى محجوزة حتى تُفرَّغ السلة).")
    lines.append("  ✔ يمكنك استرجاع أي ملف: افتح سلة المحذوفات ← الزر الأيمن ← استعادة.")
    if where:
        lines.append(f"  المكان: {where}")
    lines += [f"  ⚠️ {problem}" for problem in errors[:6]]
    if remaining:
        lines.append(f"  تبقّى {len(remaining)} مجموعة — كرّر الأمر (ارفع limit) حين تشاء.")
    else:
        lines.append("  ✅ لا مكرر متبقٍّ في آخر فحص.")
    return "\n".join(lines)


def _act_status(params: dict, player: Any) -> str:
    del params, player
    if T is None:
        return "سيدي، مكتبة `_tools_core.py` غير موجودة بجواري."
    state = _load_state()
    lines = ["سيدي، حالة صائد المكرر:"]
    if state.get("at"):
        lines.append(f"  آخر فحص: {state['at']} · ملفات {state.get('files_scanned', 0):,} · "
                     f"مجموعات {len(state.get('groups') or [])} · مهدر {T.human_bytes(state.get('wasted', 0))}")
    else:
        lines.append("  لم أفحص بعد — قل «افحص المكرر».")
    lines.append(f"  منطقة الحجر عندي: {T.human_bytes(T.trash_size())} (إن تعذّرت السلة)")
    lines.append("  لا أحذف شيئًا نهائيًا أبدًا — النقل إلى السلة فقط، وبأمر صريح منك.")
    return "\n".join(lines)


def _act_help(params: dict, player: Any) -> str:
    del params, player
    return ("سيدي، صائد الملفات المكررة:\n"
            "  • scan    — فحص (path اختياري، min_size_mb اختياري)\n"
            "  • report  — جدول المجموعات والمهدر (limit)\n"
            "  • preview — تفاصيل مجموعة (group، keep)\n"
            "  • clean   — نقل الزائد إلى السلة (confirm=yes إلزامي، keep، limit)\n"
            "  • status  — آخر فحص وحجم منطقة الحجر\n"
            "  التنظيف لا يحذف نهائيًا: كل شيء يذهب لسلة محرّر الملفات ويمكن استرجاعه.")


_ACTIONS = {"scan": _act_scan, "report": _act_report, "preview": _act_preview,
            "clean": _act_clean, "status": _act_status, "help": _act_help}


def run(parameters: dict, player: Any = None, session_memory: Any = None) -> str:
    """ينفّذ فعلًا واحدًا. لا يرمي أبدًا."""
    del session_memory
    try:
        params = parameters or {}
        action = str(params.get("action") or "report").strip().lower()
        handler = _ACTIONS.get(action)
        if handler is None:
            return ("سيدي، هذا ليس فعلًا من أفعال صائد المكرر — المتاح: "
                    + " | ".join(sorted(_ACTIONS)))
        return handler(params, player)
    except Exception as exc:  # noqa: BLE001
        try:
            T.log("player" if player else None, f"duplicate_finder error: {exc}")
        except Exception:
            pass
        return f"سيدي، حدث خطأ غير متوقع في صائد المكرر ({type(exc).__name__}) — لم يُمَس أي ملف."


# ── فحص ذاتي ─────────────────────────────────────────────────────────────────
def _selftest() -> int:
    import shutil
    import tempfile

    print("duplicate_finder — فحص ذاتي")
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

    tmp = Path(tempfile.mkdtemp(prefix="dup_test_"))
    original_extras, original_scratch = T.extras_dir, T.scratch_dir
    original_home = _home
    T.extras_dir = lambda: tmp / "extras"          # type: ignore[assignment]
    T.scratch_dir = lambda: tmp / "trash"          # type: ignore[assignment]

    # عزل الإعدادات: الفحص لا يلمس مخزن إعداداتك الحقيقي أبدًا
    _orig_cfg, _orig_store, _orig_log = T.cfg, T.store_settings, T.log_event
    _store: dict[str, dict] = {}
    T.cfg = lambda ns, fields: {**T.defaults(fields), **_store.get(ns, {})}       # type: ignore[assignment]
    T.store_settings = lambda ns, values: (_store.setdefault(ns, {}).update(values), True)[1]  # type: ignore[assignment]
    T.log_event = lambda *a, **k: None                                            # type: ignore[assignment]
    globals()["_home"] = lambda: tmp               # type: ignore[assignment]
    try:
        # شجرة اختبار: مكرر حقيقي + نفس الحجم بمحتوى مختلف + صغير جدًا + مجلد نظام
        big = b"A" * (2 * 1024 * 1024)
        (tmp / "المستندات").mkdir()
        (tmp / "التنزيلات").mkdir()
        (tmp / "المستندات" / "تقرير.docx").write_bytes(big)
        (tmp / "التنزيلات" / "تقرير - نسخة.docx").write_bytes(big)
        (tmp / "التنزيلات" / "نفس_الحجم_مختلف.dat").write_bytes(b"B" * (2 * 1024 * 1024))
        (tmp / "المستندات" / "صغير.txt").write_bytes(b"A" * 100)
        (tmp / "المستندات" / "صغير_نسخة.txt").write_bytes(b"A" * 100)
        (tmp / "Windows").mkdir()
        (tmp / "Windows" / "نظام.dll").write_bytes(big)
        T.store_settings(PLUGIN_SETTINGS["namespace"], {"scan_roots": "المستندات;التنزيلات", "min_size_mb": "1"})

        # الفحص
        result = _scan([tmp / "المستندات", tmp / "التنزيلات"])
        check("وجد المجموعة المكررة الحقيقية", len(result["groups"]) == 1, str(len(result["groups"])))
        check("المحتوى المختلف بنفس الحجم لا يُعتبر مكررًا", result["groups"][0]["size"] == 2 * 1024 * 1024)
        check("الملفات الصغيرة مستثناة", all(g["size"] > 100 for g in result["groups"]))
        check("المهدر محسوب صحيحًا", result["wasted"] == 2 * 1024 * 1024, str(result["wasted"]))
        check("الشجرة الكبيرة لم تُمسّ", (tmp / "Windows" / "نظام.dll").exists())

        scanned = _act_scan({}, None)   # يستخدم الإعدادات: المستندات;التنزيلات
        check("scan يقول العدد والمهدر", "مجموعة مكررة" in scanned and "المهدر" in scanned, scanned[:100])
        check("report يعرض المجموعة", "تقرير - نسخة" in _act_report({"limit": "5"}, None))
        check("preview يحدد النسخة الأحدث",
              "الأحدث" in _act_preview({"group": "1", "keep": "newest"}, None))
        check("preview يرفض رقمًا خارج النطاق", "خارج النطاق" in _act_preview({"group": "99"}, None))
        check("status يشرح السياسة", "لا أحذف شيئًا نهائيًا" in _act_status({}, None))

        # التنظيف: البوابة أولًا
        before = _act_clean({}, None)
        check("clean بلا تأكيد يرفض ولا يمسّ شيئًا", "دون أمر صريح" in before
              and (tmp / "التنزيلات" / "تقرير - نسخة.docx").exists())
        check("clean يرفض سياسة حفظ مجهولة",
              "غير معروفة" in _act_clean({"confirm": "yes", "keep": "random"}, None))

        after = _act_clean({"confirm": "yes", "keep": "newest", "limit": "5"}, None)
        check("clean يذكر ما نُقل", "نُقلت 1 نسخة" in after, after[:80])
        pair = (tmp / "المستندات" / "تقرير.docx", tmp / "التنزيلات" / "تقرير - نسخة.docx")
        remaining = [path for path in pair if path.exists()]
        check("نسخة واحدة بالضبط باقية (سياسة الأحدث)", len(remaining) == 1,
              f"باقٍ: {[str(p) for p in remaining]}")
        moved_name = "تقرير.docx" if remaining and remaining[0].name != "تقرير.docx" else "تقرير - نسخة.docx"
        quarantined = list((tmp / "trash").rglob(moved_name))
        check("المنقول موجود كاملًا في منطقة السلة (قابل للاسترجاع)",
              bool(quarantined) and quarantined[0].stat().st_size == 2 * 1024 * 1024,
              f"{moved_name} → {[str(p) for p in quarantined]}")
        check("لا مكرر متبقٍّ بعد التنظيف", "لا مكرر متبقٍّ" in after)
        check("التنظيف الثاني لا يجد شيئًا", "لا نتائج محفوظة" in _act_clean({"confirm": "yes"}, None))

        # الحمايات والواجهة
        roots, notes = _resolve_roots("/etc")
        check("مجلد خارج البيت مرفوض", roots == [] and notes, str(notes))
        check("مجلد غير موجود يُشرح", any("غير موجود" in n for n in _resolve_roots(str(tmp / "لايوجد"))[1]))
        check("سقف الفحص يُحترم", _iter_files(tmp / "المستندات", 0, 1)[1] is True)
        check("فعل مجهول يُرفض بلطف", "ليس فعلًا" in run({"action": "sing"}))
        check("run بلا معاملات لا ينهار", isinstance(run({}), str))
        check("الفحص بلا جذور يعطي رسالة واضحة",
              "لا أجد مجلدًا صالحًا" in _act_scan({"path": str(tmp / "لايوجد")}, None))
    finally:
        T.extras_dir = original_extras                 # type: ignore[assignment]
        T.scratch_dir = original_scratch               # type: ignore[assignment]
        T.cfg, T.store_settings, T.log_event = _orig_cfg, _orig_store, _orig_log  # type: ignore[assignment]
        globals()["_home"] = original_home             # type: ignore[assignment]
        shutil.rmtree(tmp, ignore_errors=True)

    print("RESULT:", "PASS" if problems == 0 else f"{problems} problem(s)")
    return problems


if __name__ == "__main__":
    raise SystemExit(_selftest())
