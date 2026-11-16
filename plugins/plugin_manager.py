"""
plugin_manager.py — مدير الإضافات.

يكمل المُحمِّل الموجود ولا يستبدله: يعرض الإضافات من نفس مكانه (مجلد plugins)،
ويحلّل ملفاتها تحليلًا ثابتًا (AST) بلا تنفيذ، ثم يفحص صحة كل إضافة عند الطلب،
ويعرض الحزم الخارجية الناقصة بالاسم الصحيح، ويقبل تفعيل/تعطيل الإضافات من نفس
مخزن المشروع (`save_plugin_enabled`)، ويُعيد تحميل إضافة بأمان.

ما لا يفعله عن قصد: لا يحذف ملفات، ولا يعدّلها، ولا يُعطّل الإضافات الإدارية،
ولا يُعيد تحميل إضافة تُشغّل خادمًا حيًّا (كان سيفقد منفذها بلا داعٍ).
"""
from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path
from typing import Any, Optional

PLUGIN = {
    "name": "plugin_manager",
    "description": (
        "The assistant's own plugin manager: lists every plugin with its status, version, "
        "external packages and loading errors, reports duplicate ids and broken files, "
        "enables or disables plugins through the project's own store, checks a plugin's "
        "configuration, safely reloads a plugin, and shows what each plugin can do. "
        "Use for: 'list the plugins', 'how many plugins are enabled', 'check the plugins', "
        "'diagnose plugin errors', 'what does the notes plugin do', 'disable the voice notes', "
        "'reload the notes plugin'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": ("list | info | health | errors | check | capabilities | dupes | deps | "
                                "enable | disable | reload | rescan | status | help"),
            },
            "name": {"type": "STRING", "description": "Plugin name (or file name)."},
        },
        "required": ["action"],
    },
}

# الإضافات الإدارية لا تُعطّل ولا تُعاد تحميلها من داخل هذه الإضافة (منعًا لإسقاط ما تُشغِّله)
_PROTECTED = frozenset({"plugin_manager", "automation_engine", "workflow_engine",
                        "workflow_scheduler", "remote_api", "web_panel"})


def _core():
    name = "_jarvis_orch_core"
    if name in sys.modules:
        return sys.modules[name]
    path = Path(__file__).resolve().parent / "_orch_core.py"
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


C = _core()


def _log(player, text: str) -> None:
    try:
        if player is not None and hasattr(player, "write_log"):
            player.write_log(text)
    except Exception:
        pass


def _find(name: str) -> Optional[dict]:
    """يبحث بالاسم المعلن أو باسم الملف — كما يفعل المُحمِّل."""
    wanted = str(name or "").strip()
    if not wanted:
        return None
    scan = C.scan_plugins()
    if wanted in scan:
        return scan[wanted]
    for info in scan.values():
        if str(info.get("name")) == wanted or str(info.get("file")) == wanted:
            return info
    return None


def _missing_packages(info: dict) -> list[str]:
    missing = []
    for top in info.get("requires", []) or []:
        try:
            if importlib.util.find_spec(str(top)) is None:
                missing.append(str(top))
        except Exception:
            missing.append(str(top))
    return missing


def _safety(name: str) -> str:
    if str(name) in _PROTECTED:
        return f"'{name}' إضافة إدارية — لا أغيّر حالتها ولا أعيد تحميلها من هنا."
    return ""


def _holds_live_server(module: Any) -> bool:
    """هل تُشغّل هذه الإضافة خادمًا حيًّا الآن؟ إعادة تحميلها ستُسقط الخادم."""
    try:
        for value in list(vars(module).values()):
            running = getattr(value, "running", None)
            if isinstance(running, bool) and running and type(value).__name__ == "MiniHTTP":
                return True
    except Exception:
        return False
    return False


# ── الأفعال ───────────────────────────────────────────────────────────────────
def _act_list(params: dict, player) -> str:
    query = str(params.get("query") or params.get("name") or "").strip().lower()
    scan = C.scan_plugins()
    rows, invalid, disabled, missing_deps = [], 0, 0, 0
    for stem in sorted(scan, key=lambda s: str(scan[s].get("name", s))):
        info = scan[stem]
        name = str(info["name"])
        if query and query not in name.lower() and query not in stem.lower():
            continue
        enabled = C.plugin_enabled(name)
        missing = _missing_packages(info)
        invalid += int(not info["valid"])
        disabled += int(not enabled)
        missing_deps += int(bool(missing))
        flags = []
        if not info["valid"]:
            flags.append("✗ ملف مرفوض")
        if not enabled:
            flags.append("⏸ معطّلة")
        if missing:
            flags.append("📦 ناقص: " + "، ".join(missing))
        if info["has_settings"]:
            flags.append("⚙ إعدادات")
        rows.append(f"  • {name}" + (f" [{info['file']}]" if name != stem else "") + (" — " + " · ".join(flags) if flags else ""))
    header = (f"سيدي، الإضافات: {len(scan)} إضافة · معطّلة: {disabled} · ملفات مرفوضة: {invalid} · "
              f"تنقصها حزم: {missing_deps}")
    body = "\n".join(rows[:80]) if rows else "  (لا نتائج مطابقة)"
    out = header + "\n" + body
    if len(rows) > 80:
        out += f"\n  … و{len(rows) - 80} إضافة أخرى (استخدم query للتصفية)"
    _log(player, out)
    C.log_event("plugin_manager", "list", "ok", 0, "", {"count": len(scan)}, )
    return out


def _act_info(params: dict, player) -> str:
    info = _find(str(params.get("name") or ""))
    if info is None:
        return f"سيدي، لا إضافة باسم '{C.clip(params.get('name'), 40)}'."
    missing = _missing_packages(info)
    lines = [
        f"سيدي، الإضافة '{info['name']}':",
        f"  الملف: {info['file']} · الحجم: {int(info.get('size', 0)) // 1024} ك.ب",
        f"  النسخة: {info.get('version') or 'غير معلَنة'} · البصمة: {info.get('sha256', '')}",
        f"  الحالة: {'سليمة' if info['valid'] else 'مرفوضة'} · {'مفعّلة' if C.plugin_enabled(str(info['name'])) else 'معطّلة'}",
        f"  إعدادات (PLUGIN_SETTINGS): {'نعم، الفضاء ' + str(info.get('namespace') or info['name']) if info['has_settings'] else 'لا'}",
        f"  حزم خارجية: {'، '.join(info.get('requires') or []) or 'لا شيء'}",
    ]
    if missing:
        lines.append(f"  ⚠ ناقص على هذا الجهاز: {'، '.join(missing)}  →  pip install {' '.join(missing)}")
    if info.get("error"):
        lines.append(f"  سبب الرفض: {info['error']}")
    desc = str(info.get("description") or "").strip()
    if desc:
        lines.append(f"  الوظيفة: {C.clip(desc, 400)}")
    out = "\n".join(lines)
    _log(player, out)
    return out


def _act_health(params: dict, player) -> str:
    """فحص حقيقي: استيراد الإضافة والتأكد من وجود run() قابلة للنداء — بلا تنفيذ أي فعل."""
    only = str(params.get("name") or "").strip()
    scan = C.scan_plugins()
    targets = [scan[_find(only)["file"].replace(".py", "")]] if only and _find(only) else list(scan.values())
    ok_count, bad = 0, []
    for info in sorted(targets, key=lambda i: str(i["name"])):
        name = str(info["name"])
        if only and name != str(info.get("name")):
            continue
        if not info["valid"]:
            bad.append((name, info["error"] or "ملف مرفوض"))
            continue
        missing = _missing_packages(info)
        path = C.PLUGINS_DIR / str(info["file"])
        started = time.time()
        try:
            module = C._module_for(path, f"_jarvis_pg_{path.stem}")  # noqa: SLF001 — نفس آلية المُحمِّل
            fn = getattr(module, "run", None)
            if not callable(fn):
                bad.append((name, "لا يوجد run()"))
            elif missing:
                bad.append((name, "حزم ناقصة: " + "، ".join(missing)))
            else:
                ok_count += 1
        except Exception as exc:
            bad.append((name, f"فشل التحميل: {C.clip(exc, 120)}"))
        _ = started
    lines = [f"سيدي، فحص الصحة: {ok_count} سليمة، {len(bad)} تحتاج انتباه."]
    for name, why in bad[:30]:
        lines.append(f"  ✗ {name}: {why}")
    if len(bad) > 30:
        lines.append(f"  … و{len(bad) - 30} أخرى")
    out = "\n".join(lines)
    C.log_event("plugin_manager", "health", "ok", 0, "", {"ok": ok_count, "bad": len(bad)})
    _log(player, out)
    return out


def _act_errors(params: dict, player) -> str:
    del params
    scan = C.scan_plugins(force=True)
    rejected = [(str(i["name"]), str(i["file"]), str(i["error"])) for i in scan.values() if not i["valid"]]
    failures = [row for row in C.tail_log("orchestration", 200)
                if row.get("status") in ("failed", "timeout", "blocked")]
    lines = [f"سيدي، ملفات مرفوضة: {len(rejected)}"]
    for name, file, why in rejected[:20]:
        lines.append(f"  ✗ {name} [{file}]: {why}")
    if not rejected:
        lines.append("  (لا ملفات مرفوضة — كل ملفات plugins سليمة الشكل)")
    lines.append(f"أعطال تنفيذ حديثة في السجل: {len(failures)}")
    for row in failures[:15]:
        lines.append(f"  • {row.get('plugin')} · {row.get('action')} — {row.get('status')} "
                     f"{C.clip(row.get('error', ''), 80)}")
    out = "\n".join(lines)
    _log(player, out)
    return out


def _act_check(params: dict, player) -> str:
    """يتحقق من إعدادات إضافة: هل المطلوب مملوء؟ وهل الحزم موجودة؟ (بلا كشف أي قيمة سرية)"""
    info = _find(str(params.get("name") or ""))
    if info is None:
        return f"سيدي، لا إضافة باسم '{C.clip(params.get('name'), 40)}'."
    name = str(info["name"])
    lines = [f"سيدي، فحص إعدادات '{name}':"]
    missing = _missing_packages(info)
    lines.append(f"  الحزم الخارجية: {'كلها موجودة' if not missing else 'ناقصة → ' + '، '.join(missing)}")
    if not info["has_settings"]:
        lines.append("  لا تُعلن إعدادات (PLUGIN_SETTINGS) — لا شيء لفحصه.")
        out = "\n".join(lines)
        _log(player, out)
        return out
    schema = {}
    try:
        module = C._module_for(C.PLUGINS_DIR / str(info["file"]), f"_jarvis_pg_{Path(str(info['file'])).stem}")
        schema = getattr(module, "PLUGIN_SETTINGS", {}) or {}
    except Exception as exc:
        lines.append(f"  ⚠ تعذّر تحميل الملف لقراءة الإعدادات: {C.clip(exc, 100)}")
    saved = C.stored_settings(str(schema.get("namespace") or name)) if isinstance(schema, dict) else {}
    empty_secrets, filled = [], 0
    for field in (schema.get("fields") if isinstance(schema, dict) else []) or []:
        key = str(field.get("key", ""))
        value = saved.get(key, field.get("default", ""))
        is_secret = C.looks_secret(key)
        if value in (None, ""):
            (empty_secrets if is_secret else empty_secrets).append(key)
        else:
            filled += 1
    if empty_secrets:
        lines.append(f"  حقول فارغة تحتاج ضبطًا: {'، '.join(empty_secrets)}")
        lines.append("  (يُضبط من إعدادات الإضافة في المشروع أو بقولك لجارفس ما تريد)")
    else:
        lines.append(f"  كل الحقول مملوءة ({filled} حقلًا) — والقيم لا تُعرض أبدًا في الفحص.")
    if isinstance(schema, dict) and schema.get("title"):
        lines.append(f"  العنوان في الإعدادات: {schema['title']}")
    out = "\n".join(lines)
    _log(player, out)
    return out


def _act_capabilities(params: dict, player) -> str:
    info = _find(str(params.get("name") or ""))
    if info is None:
        return f"سيدي، لا إضافة باسم '{C.clip(params.get('name'), 40)}'."
    params_map = info.get("parameters") or {}
    props = {}
    try:
        module = C._module_for(C.PLUGINS_DIR / str(info["file"]), f"_jarvis_pg_{Path(str(info['file'])).stem}")
        declared = getattr(module, "PLUGIN", {}) or {}
        props = ((declared.get("parameters") or {}).get("properties") or {})
    except Exception:
        props = {}
    lines = [f"سيدي، قدرات '{info['name']}':", f"  {C.clip(info.get('description', ''), 500)}"]
    if props:
        lines.append("  المعاملات:")
        for key, spec in props.items():
            desc = spec.get("description", "") if isinstance(spec, dict) else ""
            lines.append(f"    • {key}: {C.clip(desc, 160)}")
    elif params_map:
        lines.append(f"  مفاتيح المعاملات: {'، '.join(params_map)}")
    out = "\n".join(lines)
    _log(player, out)
    return out


def _act_dupes(params: dict, player) -> str:
    del params
    scan = C.scan_plugins(force=True)
    by_name: dict[str, list[str]] = {}
    for info in scan.values():
        by_name.setdefault(str(info["name"]), []).append(str(info["file"]))
    dupes = {k: v for k, v in by_name.items() if len(v) > 1}
    if not dupes:
        return "سيدي، لا أسماء مكرّرة بين الإضافات — كل اسم يخصّ ملفًا واحدًا."
    lines = ["سيدي، أسماء مكرّرة (المُحمِّل سيرفضها كلها):"]
    for name, files in dupes.items():
        lines.append(f"  ✗ {name}: {'، '.join(files)}")
    out = "\n".join(lines)
    _log(player, out)
    return out


def _act_deps(params: dict, player) -> str:
    del params
    scan = C.scan_plugins(force=True)
    needed: dict[str, list[str]] = {}
    for info in scan.values():
        for top in info.get("requires", []) or []:
            needed.setdefault(str(top), []).append(str(info["name"]))
    if not needed:
        return "سيدي، لا إضافات تطلب حزمًا خارجية."
    missing, present = [], 0
    for top in sorted(needed):
        try:
            found = importlib.util.find_spec(top) is not None
        except Exception:
            found = False
        if found:
            present += 1
        else:
            missing.append((top, needed[top]))
    lines = [f"سيدي، الحزم الخارجية: {present} موجودة، {len(missing)} ناقصة."]
    for top, who in missing:
        lines.append(f"  ⚠ {top} — تحتاجها: {'، '.join(sorted(set(who)))}  →  pip install {top}")
    if not missing:
        lines.append("  (كل ما تطلبه الإضافات مثبّت على هذا الجهاز)")
    out = "\n".join(lines)
    _log(player, out)
    return out


def _act_set_enabled(params: dict, player, enabled_value: bool) -> str:
    name = str(params.get("name") or "").strip()
    info = _find(name)
    if info is None:
        return f"سيدي، لا إضافة باسم '{C.clip(name, 40)}'."
    real = str(info["name"])
    blocked = _safety(real)
    if blocked and not enabled_value:
        return f"سيدي، {blocked}"
    ok, message = C.set_plugin_enabled(real, enabled_value)
    C.log_event("plugin_manager", "enable" if enabled_value else "disable", "ok" if ok else "failed",
                0, "" if ok else message, {"plugin": real})
    out = f"سيدي، {message}" if ok else f"سيدي، {message}"
    if ok:
        out += "\n  الحالة تُحفظ في مخزن المشروع نفسه (config/api_keys.json)، والمُحمِّل يقرؤها فورًا."
    _log(player, out)
    return out


def _act_reload(params: dict, player) -> str:
    name = str(params.get("name") or "").strip()
    info = _find(name)
    if info is None:
        return f"سيدي، لا إضافة باسم '{C.clip(name, 40)}'."
    real = str(info["name"])
    blocked = _safety(real)
    if blocked:
        return f"سيدي، {blocked} إعادة تحميلها ستُسقط ما تُشغِّله."
    path = C.PLUGINS_DIR / str(info["file"])
    if not path.exists():
        return f"سيدي، ملف '{info['file']}' غير موجود."
    key = f"_jarvis_pg_{path.stem}"
    module = sys.modules.get(key) or C._MODULE_CACHE.get(key)
    if module is not None and _holds_live_server(module):
        return f"سيدي، '{real}' تُشغّل خادمًا الآن — لن أُعيد تحميلها حتى لا أفقد المنفذ. أوقفه أولًا."
    dropped = []
    for cache_name in (key, f"plugins.{path.stem}", f"_jarvis_plugin_{path.stem}"):
        if cache_name in sys.modules:
            sys.modules.pop(cache_name, None)
            dropped.append(cache_name)
    C._MODULE_CACHE.pop(key, None)  # noqa: SLF001 — نفس الكاش الذي يستخدمه النداء
    try:
        C._module_for(path, key)  # noqa: SLF001
        loaded = True
        error = ""
    except Exception as exc:
        loaded, error = False, C.clip(exc, 160)
    C.log_event("plugin_manager", "reload", "ok" if loaded else "failed", 0, error, {"plugin": real})
    out = (f"سيدي، أعدت تحميل '{real}'" + (f" (أزلت {len(dropped)} نسخة من الذاكرة)." if dropped else ".")
           if loaded else f"سيدي، تعذّرت إعادة تحميل '{real}': {error}")
    _log(player, out)
    return out


def _act_rescan(params: dict, player) -> str:
    """يُفرغ فهرس التحليل ثم يعيده — لا يلمس أي ملف."""
    before = len(C.scan_plugins())
    scan = C.scan_plugins(force=True)
    invalid = [str(i["name"]) for i in scan.values() if not i["valid"]]
    out = (f"سيدي، أعدت فحص المجلد: {len(scan)} إضافة (كان {before}). "
           f"مرفوضة: {len(invalid)}" + (f" — {'، '.join(invalid[:10])}" if invalid else ""))
    _log(player, out)
    return out


def _act_status(params: dict, player) -> str:
    del params
    scan = C.scan_plugins()
    invalid = [i for i in scan.values() if not i["valid"]]
    disabled = [str(i["name"]) for i in scan.values() if not C.plugin_enabled(str(i["name"]))]
    loaded = C.loaded_plugin_names()
    tools = C.core_tool_names()
    lines = [
        "سيدي، حالة المدير:",
        f"  إضافات مكتشَفة: {len(scan)} · محمَّلة الآن: {len(loaded)} · مفعّلة: {len(scan) - len(disabled)}",
        f"  ملفات مرفوضة: {len(invalid)} · أدوات المشروع (actions + المدمجة): {len(tools)}",
        f"  مجلد الإضافات: {C.PLUGINS_DIR}",
    ]
    if disabled:
        lines.append(f"  معطّلة: {'، '.join(disabled[:20])}" + (" …" if len(disabled) > 20 else ""))
    out = "\n".join(lines)
    _log(player, out)
    return out


_HELP = (
    "مدير الإضافات، سيدي:\n"
    "• list (query) · status · dupes · deps — جرد كامل وحالة\n"
    "• info name=X · capabilities name=X · check name=X — تفاصيل وقدرات وفحص إعدادات\n"
    "• health (name=X) · errors — فحص تحميل حقيقي وسجل الأعطال\n"
    "• enable name=X · disable name=X — من مخزن المشروع نفسه (الإدارية محميّة)\n"
    "• reload name=X (تتوقف عن العمل لو تُشغّل خادمًا) · rescan\n"
    "لا يحذف ملفات ولا يعدّلها، ولا يمسّ المُحمِّل الحالي."
)


def run(parameters: dict, player=None, session_memory=None) -> str:
    """نقطة الدخول. لا ترمي أبدًا."""
    del session_memory
    if C is None:
        return "سيدي، ملف '_orch_core.py' المشترك غير موجود — بدونه لا يعمل المدير."
    try:
        p = dict(parameters or {})
        action = str(p.get("action") or "list").strip().lower()
        if action in ("list", "قائمة"):
            return _act_list(p, player)
        if action in ("info", "show", "تفاصيل"):
            return _act_info(p, player)
        if action in ("health", "فحص"):
            return _act_health(p, player)
        if action in ("errors", "أعطال"):
            return _act_errors(p, player)
        if action in ("check", "validate", "تحقق"):
            return _act_check(p, player)
        if action in ("capabilities", "قدرات"):
            return _act_capabilities(p, player)
        if action in ("dupes", "duplicates", "تكرار"):
            return _act_dupes(p, player)
        if action in ("deps", "packages", "حزم"):
            return _act_deps(p, player)
        if action in ("enable", "فعّل", "شغّل"):
            return _act_set_enabled(p, player, True)
        if action in ("disable", "عطّل", "أوقف"):
            return _act_set_enabled(p, player, False)
        if action in ("reload", "أعد_التحميل"):
            return _act_reload(p, player)
        if action in ("rescan", "أعد_الفحص"):
            return _act_rescan(p, player)
        if action in ("status", "حالة"):
            return _act_status(p, player)
        if action in ("help", "?"):
            return _HELP
        return f"سيدي، '{action}' ليس فعلًا أعرفه في مدير الإضافات. قل 'help'."
    except Exception as exc:  # noqa: BLE001
        return f"سيدي، مدير الإضافات واجه مشكلة: {str(exc)[:180]}"


# ── فحص ذاتي: python plugin_manager.py ────────────────────────────────────────
def _selftest() -> int:
    """الفحص الذاتي — معزولًا تمامًا عن مخزن المشروع: لا إعدادات ولا سجلات ولا أتمتات حقيقية."""
    restorer = None
    try:
        if C is not None and hasattr(C, "isolate_for_tests"):
            restorer = C.isolate_for_tests()
    except Exception:
        restorer = None
    try:
        return _selftest_body()
    finally:
        if restorer is not None:
            try:
                restorer()
            except Exception:
                pass


def _selftest_body() -> int:
    print("plugin_manager — self-test")
    problems = 0

    def check(label: str, ok: bool, extra: str = "") -> None:
        nonlocal problems
        print(("  ✓ " if ok else "  ✗ ") + label + (f"  [{extra}]" if extra and not ok else ""))
        if not ok:
            problems += 1

    if C is None:
        print("  ✗ النواة المشتركة غير موجودة")
        return 1

    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="pm_test_"))
    real_plugins = C.PLUGINS_DIR
    real_stamp, real_data = C._SCAN_CACHE.get("stamp"), C._SCAN_CACHE.get("data")
    base = C.CONFIG_DIR / "api_keys.json"
    backup = base.read_text(encoding="utf-8") if base.exists() else None
    try:
        # مجلد إضافات اختباري: إضافة سليمة، إضافة باسم مكرّر، وملف مكسور
        test_dir = tmp / "plugins"
        test_dir.mkdir(parents=True)
        (test_dir / "good_one.py").write_text(
            'PLUGIN = {"name": "good_one", "description": "اختبار", "parameters": {"type": "OBJECT", "properties": {}}}\n'
            'PLUGIN_SETTINGS = {"namespace": "good_one", "title": "ت", "fields": [{"key": "secret_key", "type": "text", "default": ""}]}\n'
            'import importlib\n'
            'def run(parameters, player=None):\n    return "ok"\n', encoding="utf-8")
        (test_dir / "dup_a.py").write_text(
            'PLUGIN = {"name": "same_name", "description": "أ"}\ndef run(parameters, player=None):\n    return "a"\n',
            encoding="utf-8")
        (test_dir / "dup_b.py").write_text(
            'PLUGIN = {"name": "same_name", "description": "ب"}\ndef run(parameters, player=None):\n    return "b"\n',
            encoding="utf-8")
        (test_dir / "broken.py").write_text('PLUGIN = {"name": "broken", "descrip\n', encoding="utf-8")
        (test_dir / "needs_pkg.py").write_text(
            'PLUGIN = {"name": "needs_pkg", "description": "يطلب حزمة غير موجودة"}\n'
            'import zzz_missing_package_xyz\ndef run(parameters, player=None):\n    return "x"\n', encoding="utf-8")
        C.PLUGINS_DIR = test_dir
        C.scan_plugins(force=True)

        out = run({"action": "list"})
        check("list يجرد كل الملفات", "good_one" in out and "broken" in out and "needs_pkg" in out, out[:200])
        check("list يوسم المكسور", "مرفوض" in out, out[:200])
        check("list يوسم الحزمة الناقصة", "zzz_missing_package_xyz" in out, out[:240])
        out = run({"action": "dupes"})
        check("يوجد ازدواج اسم", "same_name" in out and "dup_a.py" in out, out[:200])
        out = run({"action": "info", "name": "good_one"})
        check("info يعرض التفاصيل", "good_one" in out and "إعدادات" in out and "حزم خارجية" in out,
              out[:240])
        out = run({"action": "info", "name": "broken"})
        check("info يشرح سبب الرفض", "سبب الرفض" in out or "مرفوضة" in out, out[:200])
        out = run({"action": "capabilities", "name": "good_one"})
        check("capabilities يعمل", "good_one" in out, out[:160])
        out = run({"action": "check", "name": "good_one"})
        check("check ينبّه على الحقل السري الفارغ", "secret_key" in out, out[:240])
        check("check لا يكشف أي قيمة", "default" not in out, out[:200])
        out = run({"action": "health"})
        check("health يفرز السليم من المتعطل", "good_one" in out or "سليمة" in out, out[:240])
        check("health يذكر المكسور", "broken" in out, out[:300])
        out = run({"action": "deps"})
        check("deps يعرض الحزمة الناقصة", "zzz_missing_package_xyz" in out, out[:240])
        out = run({"action": "errors"})
        check("errors يجمع الأعطال", "مرفوضة" in out or "ملفات مرفوضة" in out, out[:200])
        out = run({"action": "rescan"})
        check("rescan يعمل", "أعدت فحص المجلد" in out, out[:160])
        # حماية الإدارية (نستخدم مجلد المشروع الحقيقي لهذا الفحص)
        C.PLUGINS_DIR = real_plugins
        C.scan_plugins(force=True)
        out = run({"action": "disable", "name": "plugin_manager"})
        check("لا يعطّل إضافة إدارية", "إدارية" in out, out[:160])
        out = run({"action": "reload", "name": "plugin_manager"})
        check("لا يعيد تحميل إدارية", "إدارية" in out, out[:160])
        out = run({"action": "info", "name": "لا_توجد_إضافة"})
        check("اسم غير موجود → رسالة", "لا إضافة باسم" in out, out[:140])
        out = run({"action": "zzz"})
        check("فعل مجهول", "ليس فعلًا" in out, out[:140])
        check("help يعمل", "مدير الإضافات" in run({"action": "help"}))
        out = run({"action": "status"})
        check("status يجمع الأرقام", "إضافات مكتشَفة" in out, out[:200])
        out = run({"action": "health", "name": "system_health"})
        check("فحص صحة إضافة بعينها", "system_health" in out or "سليمة" in out, out[:200])
    finally:
        C.PLUGINS_DIR = real_plugins
        C._SCAN_CACHE["stamp"], C._SCAN_CACHE["data"] = real_stamp, real_data
        if backup is not None:
            base.write_text(backup, encoding="utf-8")
        elif base.exists():
            base.unlink(missing_ok=True)
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            C.scan_plugins(force=True)
        except Exception:
            pass

    print(f"RESULT: {'PASS' if problems == 0 else str(problems) + ' problem(s)'}")
    return problems


if __name__ == "__main__":
    raise SystemExit(1 if _selftest() else 0)
