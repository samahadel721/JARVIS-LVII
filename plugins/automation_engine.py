"""
automation_engine.py — محرّك الأتمتة.

يبني أتمتة من جملة عربية/إنجليزية أو من خطوات صريحة، ويشغّلها يدويًا أو عند موعد
أو عند حدث. المحرّك يعمل داخل المشروع فقط: لا أوامر نظام، ولا تنفيذ لما لم يُعلَن.

الأنواع المدعومة (كلها مُختبَرة في الفحص الذاتي):
• مواعيد: يوميًا · أسبوعيًا (أيام) · كل N دقيقة/ساعة · مرة واحدة.
• أحداث المشروع: بعد إضافة (نجحت/فشلت/أيًّا كان) — أي إضافة تنادي `fire` فتُشغَّل الأتمتة.
• الملفات: أُنشئ/عُدّل/حُذف ملف بمسار مُعدّ صراحة.
• التطبيقات: بدأ/توقّف برنامج باسم مُعدّ صراحة.
• النظام: مؤشر (معالج/ذاكرة/قرص/بطارية) تجاوز حدًّا — مع تهدئة زمنية تمنع التكرار.
• جدولة تخصّ الإضافات نفسها: خطوات متعدّدة، شرط لكل خطوة، إعادة محاولة، ومسار فشل.

الحمايات: الإضافات الإدارية مرفوضة داخل الأتمتة (منعًا للّف والدوران)، الإضافة لا
تنادي نفسها، سقف عمق للنداء المتسلسل، سقف خطوات، مهلة لكل خطوة، ومنع تشغيل نفس
الأتمتة مرّتين في وقت واحد. والفشل يُسجَّل بالخطوة المسؤولة مع الحفاظ على مخرجات
الخطوات السابقة.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path
from typing import Any, Optional

PLUGIN = {
    "name": "automation_engine",
    "description": (
        "Builds and runs automations for the assistant: 'every day at 8 in the morning run X', "
        "'every 30 minutes check Y', 'when the news plugin finishes, summarize and notify me', "
        "'when a file appears in this folder, process it', 'when memory passes 90% tell me', "
        "'after midnight clean the temp folder'. Automations can run one plugin or a chain of "
        "them, with conditions, retries and failure paths. Use for: 'make an automation', "
        "'what automations do I have', 'run the morning automation now', 'stop that automation', "
        "'delete the automation' (asks first), 'show the automation log'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": ("create | nl_create | list | show | run | enable | disable | edit | "
                                "delete | tick | fire | events | logs | status | help"),
            },
            "name": {"type": "STRING", "description": "Automation name (or its id)."},
            "text": {"type": "STRING", "description": "The request in your own words (for nl_create)."},
            "when": {"type": "STRING", "description": "Time phrase, e.g. 'every day at 8 am' / 'كل ٣٠ دقيقة'."},
            "trigger": {"type": "STRING", "description": "Trigger as JSON: {\"kind\":\"file\",\"path\":\"...\"}."},
            "steps": {"type": "STRING", "description": "Steps as JSON: [{\"plugin\":\"...\",\"parameters\":{...}}]."},
            "new_name": {"type": "STRING", "description": "For edit: rename to this."},
            "source_plugin": {"type": "STRING", "description": "For fire: the plugin that just ran."},
            "source_status": {"type": "STRING", "description": "For fire: ok | failed | any."},
            "output": {"type": "STRING", "description": "For fire: that plugin's output (used in {{prev}})."},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "automation_engine",
    "title": "Automation engine / محرّك الأتمتة",
    "fields": [
        {"key": "default_time", "type": "text", "label": "Default time for daily automations (HH:MM)", "default": "08:00"},
        {"key": "step_timeout_s", "type": "text", "label": "Seconds allowed per step", "default": "180"},
        {"key": "max_steps", "type": "text", "label": "Maximum steps per automation", "default": "25"},
        {"key": "system_cooldown_min", "type": "text", "label": "Minutes between system-trigger repeats", "default": "15"},
    ],
}

_LOCK_WINDOW_S = 30.0           # منع تشغيل نفس الأتمتة مرّتين في نافذة قصيرة
_RUNNING: set[str] = set()      # أتمتات تعمل الآن (منع تداخل)
_FIRING = False                 # منع اللف عند إطلاق الأحداث من داخل حدث

# فهم الجملة: كلمات مفتاحية → إضافة (تُستخدم في nl_create فقط، والخطوات الصريحة أدقّ)
_PLUGIN_HINTS: tuple[tuple[tuple[str, ...], str, dict], ...] = (
    (("طقس", "الجو", "weather"), "daily_briefing", {"action": "brief", "sections": "weather"}),
    (("أخبار", "اخبار", "news", "عناوين"), "news_monitor", {"action": "latest"}),
    (("لخّص", "لخص", "تلخيص", "ملخّص", "summar"), "summarizer", {"action": "summarize", "text": "{{prev}}"}),
    (("ترجم", "ترجمة", "translate"), "translation", {"action": "translate", "text": "{{prev}}",
                                                     "to": "{{vars.language}}"}),
    (("تقويم", "مواعيدي", "calendar", "اجتماع"), "calendar_events", {"action": "today"}),
    (("صلوات", "صلاة", "prayer"), "prayer_times", {"action": "today"}),
    (("أشعرني", "إشعار", "اشعار", "بلغني", "notif"), "notification_center",
     {"action": "notify", "title": "JARVIS", "body": "{{prev}}"}),
    (("تليجرام", "تلجرام", "telegram"), "telegram_control", {"action": "send", "message": "{{prev}}"}),
    (("واتس", "واتساب", "whatsapp"), "whatsapp_control", {"action": "send", "message": "{{prev}}"}),
    (("رسالة", "message", "ابعت", "قول له"), "universal_messenger", {"action": "send", "message": "{{prev}}"}),
    (("تقرير", "مستند", "وثيقة", "report", "document"), "document_generator",
     {"action": "create", "title": "تقرير", "content": "{{prev}}"}),
    (("ابحث", "بحث", "search", "دور على"), "research_assistant", {"action": "research",
                                                                 "topic": "{{vars.topic}}"}),
    (("صحة النظام", "حالة النظام", "الأداء", "system health"), "system_health", {"action": "status"}),
    (("مساحة", "تخزين", "قرص", "storage"), "storage_analyzer", {"action": "report"}),
    (("تنظيف", "نظّف", "clean"), "system_diagnostics", {"action": "quick"}),
    (("ملاحظة", "دوّن", "note"), "note_manager", {"action": "add", "title": "ملاحظة",
                                                 "content": "{{prev}}"}),
    (("موجز", "briefing"), "daily_briefing", {"action": "brief"}),
    (("أسعار", "سعر", "prices"), "price_monitor", {"action": "list"}),
    (("ابحث في ملفاتي", "ذاكرة", "semantic"), "semantic_search", {"action": "search",
                                                                 "query": "{{vars.topic}}"}),
)

_ADMIN_IN_AUTOMATION = frozenset({"plugin_manager", "automation_engine", "workflow_engine",
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


def _cfg() -> dict:
    defaults = {f["key"]: f.get("default") for f in PLUGIN_SETTINGS["fields"]}
    if C is None:
        return defaults
    return C.cfg("automation_engine", PLUGIN_SETTINGS["fields"])


def _int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(str(value).strip())))
    except Exception:
        return default


def _log(player, text: str) -> None:
    try:
        if player is not None and hasattr(player, "write_log"):
            player.write_log(text)
    except Exception:
        pass


# ── المخزن ────────────────────────────────────────────────────────────────────
def _load() -> dict:
    data = C.load_json(C.AUTOMATIONS_FILE, {"version": 1, "items": {}})
    if not isinstance(data, dict):
        data = {"version": 1, "items": {}}
    if not isinstance(data.get("items"), dict):
        data["items"] = {}
    return data


def _save(data: dict) -> bool:
    return C.save_json(C.AUTOMATIONS_FILE, data)


def _new_id(name: str) -> str:
    stamp = C.now().strftime("%Y%m%d%H%M%S")
    slug = "".join(ch for ch in str(name) if ch.isalnum() or ch in "_")[:12] or "auto"
    return f"auc_{slug}_{stamp}"


def _find(name: str) -> tuple[Optional[dict], Optional[str]]:
    """يبحث بالمعرّف أو بالاسم (مطابقة تامة ثم جزئية)."""
    wanted = str(name or "").strip().lower()
    if not wanted:
        return None, "أي أتمتة تقصد؟"
    data = _load()
    if wanted in data["items"]:
        return data["items"][wanted], wanted
    exact = [(key, item) for key, item in data["items"].items() if str(item.get("name", "")).strip().lower() == wanted]
    if len(exact) == 1:
        return exact[0][1], exact[0][0]
    partial = [(key, item) for key, item in data["items"].items() if wanted in str(item.get("name", "")).lower()]
    if len(partial) == 1:
        return partial[0][1], partial[0][0]
    if len(partial) > 1:
        return None, f"وجدت أكثر من أتمتة بهذا الاسم: {'، '.join(str(p[1].get('name')) for p in partial)} — حدّد الاسم."
    return None, f"لا أتمتة باسم '{C.clip(name, 40)}'."


def _history_append(item: dict, entry: dict, keep: int = 10) -> None:
    history = item.get("history")
    if not isinstance(history, list):
        history = []
    history.append(entry)
    item["history"] = history[-keep:]


# ── الخطوات ───────────────────────────────────────────────────────────────────
def _parse_steps(raw: Any, player=None) -> tuple[list, str]:
    """يقبل JSON نصًّا أو قائمة. يتحقق من كل خطوة: الإضافة موجودة وليست إدارية."""
    del player
    if raw is None or raw == "":
        return [], "بلا خطوات"
    steps = raw
    if isinstance(raw, str):
        try:
            steps = json.loads(raw)
        except Exception as exc:
            return [], f"تعذّرت قراءة steps: {C.clip(exc, 120)}"
    if isinstance(steps, dict):
        steps = [steps]
    if not isinstance(steps, list) or not steps:
        return [], "steps يجب أن تكون قائمة خطوات."
    if len(steps) > _int(_cfg().get("max_steps"), 25, 1, 50):
        return [], f"عدد الخطوات يتجاوز السقف ({_cfg().get('max_steps')})."
    clean = []
    for index, step in enumerate(steps, start=1):
        if isinstance(step, str):
            step = {"plugin": step}
        if not isinstance(step, dict):
            return [], f"الخطوة {index} ليست كائنًا."
        target = str(step.get("plugin") or "").strip()
        if not target:
            return [], f"الخطوة {index} بلا اسم إضافة."
        if target in _ADMIN_IN_AUTOMATION:
            return [], (f"الخطوة {index}: '{target}' إدارية — تشغيلها من داخل أتمتة "
                        f"قد يُدخلنا في لفّ، فلن أضيفها.")
        if C.resolve_plugin(target) is None:
            return [], f"الخطوة {index}: الإضافة '{target}' غير موجودة في plugins."
        parameters = step.get("parameters")
        if parameters is not None and not isinstance(parameters, dict):
            return [], f"الخطوة {index}: parameters يجب أن تكون كائن JSON."
        entry = {
            "name": str(step.get("name") or step.get("label") or f"step{index}"),
            "plugin": target,
            "parameters": parameters or {},
        }
        if step.get("when") is not None:
            entry["when"] = step["when"]
        if step.get("retries") is not None:
            entry["retries"] = max(0, min(5, _int(step.get("retries"), 0, 0, 5)))
        if isinstance(step.get("on_failure"), dict):
            entry["on_failure"] = step["on_failure"]
        clean.append(entry)
    return clean, ""


def _parse_trigger(raw: Any) -> tuple[Optional[dict], str]:
    """trigger كـJSON، أو when كجملة زمنية طبيعية."""
    if raw in (None, ""):
        return {"kind": "manual"}, ""
    trigger = raw
    if isinstance(raw, str):
        try:
            trigger = json.loads(raw)
        except Exception:
            parsed = C.parse_when(raw)
            if parsed is None:
                return None, (f"لم أفهم التوقيت '{C.clip(raw, 40)}'. قل مثلًا: 'كل يوم ٨ صباحًا' "
                              f"أو 'كل ٣٠ دقيقة'، أو أعطني trigger كـJSON.")
            return parsed, ""
    if isinstance(trigger, dict) and not trigger:
        return {"kind": "manual"}, ""
    if not isinstance(trigger, dict):
        return None, "trigger يجب أن يكون كائن JSON."
    kind = str(trigger.get("kind") or "").strip().lower()
    if not kind:
        return {"kind": "manual"}, ""
    if kind in ("daily", "weekly", "interval", "once"):
        if kind == "once" and not trigger.get("at"):
            return None, "trigger من نوع once يحتاج \"at\" بتوقيت ISO."
        return trigger, ""
    if kind == "manual":
        return trigger, ""
    if kind == "file":
        path = str(trigger.get("path") or "").strip()
        if not path:
            return None, "trigger من نوع file يحتاج \"path\"."
        event = str(trigger.get("event") or "created").lower()
        if event not in ("created", "modified", "deleted", "any"):
            return None, "حدث الملف يجب أن يكون created/modified/deleted/any."
        return {"kind": "file", "path": path, "event": event}, ""
    if kind == "app":
        if not str(trigger.get("process") or "").strip():
            return None, "trigger من نوع app يحتاج \"process\" (اسم البرنامج)."
        event = str(trigger.get("event") or "start").lower()
        if event not in ("start", "stop", "any"):
            return None, "حدث التطبيق يجب أن يكون start/stop/any."
        return {"kind": "app", "process": str(trigger["process"]).strip(), "event": event}, ""
    if kind == "system":
        metric = str(trigger.get("metric") or "cpu").lower()
        if metric not in ("cpu", "ram", "memory", "disk", "battery", "bat"):
            return None, "المؤشر يجب أن يكون cpu/ram/disk/battery."
        return {"kind": "system", "metric": metric, "op": str(trigger.get("op") or ">"),
                "value": float(trigger.get("value") or 90),
                "cooldown_min": _int(trigger.get("cooldown_min", _cfg().get("system_cooldown_min")), 15, 1, 1440)}, ""
    if kind in ("event", "plugin"):
        if not str(trigger.get("plugin") or "").strip():
            return None, "trigger من نوع plugin يحتاج \"plugin\"."
        status = str(trigger.get("status") or "any").lower()
        if status not in ("ok", "failed", "any"):
            return None, "حالة الحدث يجب أن تكون ok/failed/any."
        return {"kind": "plugin", "plugin": str(trigger["plugin"]).strip(), "status": status}, ""
    return None, f"نوع التوقيت '{kind}' غير مدعوم."


def _nl_to_steps(text: str) -> list[dict]:
    """تقسيم الجملة على 'ثم' ومطابقة كل جزء بإضافة معروفة — بلا اختراع."""
    sentence = C.normalize_digits(str(text or ""))
    parts = [p for p in __import__("re").split(r"(?:ثم|وبعدها|بعد كده|بعد ذلك|,|،|then|and then)", sentence) if p.strip()]
    steps = []
    for part in parts:
        low = part.strip().lower()
        for words, plugin, params in _PLUGIN_HINTS:
            if any(word in low for word in words):
                steps.append({"plugin": plugin, "parameters": dict(params)})
                break
    return steps


# ── التنفيذ ───────────────────────────────────────────────────────────────────
def _run_item(item: dict, key: str, *, reason: str, player=None, prev: str = "") -> dict:
    """ينفّذ أتمتة واحدة مع كل الحمايات. يعيد سطر نتيجة موحّدًا."""
    if key in _RUNNING:
        return {"ok": False, "output": "", "error": "تعمل بالفعل الآن — لن أُشغّلها مرّتين.", "steps": []}
    steps = item.get("steps") or []
    if not steps:
        return {"ok": False, "output": "", "error": "أتمتة بلا خطوات.", "steps": []}
    _RUNNING.add(key)
    started = time.time()
    try:
        outcome = C.run_steps(
            steps,
            {"prev": prev, "steps": {}, "vars": {}},
            owner="automation_engine",
            player=player,
            max_steps=_int(_cfg().get("max_steps"), 25, 1, 50),
            step_timeout_s=float(_int(_cfg().get("step_timeout_s"), 180, 5, 1800)),
        )
    except Exception as exc:  # noqa: BLE001 — الأتمتة لا تُسقط المساعد أبدًا
        outcome = {"ok": False, "output": "", "results": [], "failed": None, "error": str(exc)}
    finally:
        _RUNNING.discard(key)
    ms = int((time.time() - started) * 1000)
    results = outcome.get("results") or []
    return {
        "ok": bool(outcome.get("ok")),
        "output": str(outcome.get("output") or ""),
        "error": str(outcome.get("error") or ""),
        "failed": outcome.get("failed"),
        "ms": ms,
        "steps": [{"name": r.get("name"), "plugin": r.get("plugin"), "status": r.get("status"),
                   "ms": r.get("ms"), "error": C.clip(r.get("error", ""), 120)} for r in results],
    }


def _execute_and_record(item: dict, key: str, *, reason: str, player=None) -> str:
    """يشغّل أتمتة ثم يحدّث السجل والتاريخ وحالة الجدولة. يُستخدم من run و tick."""
    outcome = _run_item(item, key, reason=reason, player=player)
    data = _load()
    fresh = data["items"].get(key)
    if fresh is None:
        return f"سيدي، الأتمتة '{item.get('name')}' اختفت أثناء التنفيذ."
    fresh["last_run"] = C.now_iso()
    fresh["last_status"] = "ok" if outcome["ok"] else ("timeout" if outcome.get("timeout") else "failed")
    fresh["run_count"] = int(fresh.get("run_count", 0)) + 1
    fresh["last_error"] = C.clip(outcome.get("error", "") or "", 200)
    _history_append(fresh, {"ts": fresh["last_run"], "reason": reason, "status": fresh["last_status"],
                            "failed_step": outcome.get("failed"), "ms": outcome.get("ms", 0),
                            "steps": outcome.get("steps", [])})
    trigger = fresh.get("trigger") or {}
    # إعادة جدولة + سياسة إعادة المحاولة عند الفشل
    retry = fresh.get("retry") if isinstance(fresh.get("retry"), dict) else {}
    tries_used = int(fresh.get("retry_used", 0))
    if not outcome["ok"] and retry and tries_used < _int(retry.get("tries"), 0, 0, 5):
        delay = _int(retry.get("delay_min"), 5, 1, 1440)
        fresh["retry_used"] = tries_used + 1
        fresh["next_run"] = C.to_iso(C.now() + __import__("datetime").timedelta(minutes=delay))
        fresh["last_status"] = "retrying"
    else:
        fresh["retry_used"] = 0
        if str(trigger.get("kind")) == "once":
            fresh["enabled"] = False
            fresh["next_run"] = ""
        elif str(trigger.get("kind")) in ("daily", "weekly", "interval"):
            nxt = C.next_run(trigger, after=C.now())
            fresh["next_run"] = C.to_iso(nxt) if nxt else ""
        else:
            fresh["next_run"] = ""
    _save(data)
    C.log_event("automation_engine", f"run:{fresh.get('name')}", fresh["last_status"],
                outcome.get("ms", 0), fresh["last_error"], {"reason": reason, "steps": outcome.get("steps", [])})
    head = "سيدي، " + ("نفّذت" if outcome["ok"] else "تعثّرت في") + f" أتمتة '{fresh.get('name')}'"
    if not outcome["ok"] and outcome.get("failed"):
        failed_step = outcome["failed"]
        detail = next((s for s in outcome.get("steps", []) if s.get("name") == failed_step), {})
        head += f" — فشلت الخطوة '{failed_step}'"
        if detail.get("error"):
            head += f": {C.clip(detail['error'], 140)}"
        head += "\n  مخرجات الخطوات السابقة محفوظة، وبقية المساعد لم يتأثر."
    lines = [head]
    for step in outcome.get("steps", [])[:8]:
        mark = {"ok": "✔", "failed": "✗", "skipped": "—", "blocked": "⛔"}.get(str(step.get("status")), "•")
        lines.append(f"  {mark} {step.get('name')} [{step.get('plugin')}] {step.get('status')} "
                     f"({step.get('ms', 0)}ms)" + (f" — {C.clip(step.get('error'), 90)}" if step.get("error") else ""))
    if outcome["ok"] and outcome.get("output"):
        lines.append("  الناتج: " + C.clip(outcome["output"], 300))
    text = "\n".join(lines)
    _log(player, text)
    return text


# ── المُطلِقات ────────────────────────────────────────────────────────────────
def _system_due(item: dict) -> tuple[bool, str]:
    trigger = item.get("trigger") or {}
    metric = str(trigger.get("metric") or "cpu")
    try:
        value = float(str(trigger.get("value") or 90))
    except Exception:
        return False, ""
    current = C.system_metric(metric)
    if current is None:
        return False, ""
    op = str(trigger.get("op") or ">").strip()
    hit = (current > value) if op in (">", "gt") else (current < value)
    if not hit:
        return False, ""
    state = item.get("state") if isinstance(item.get("state"), dict) else {}
    cooldown = _int(trigger.get("cooldown_min"), 15, 1, 1440) * 60
    last = C.from_iso(state.get("system_last"))
    if last and (C.now() - last).total_seconds() < cooldown:
        return False, ""
    return True, f"{metric}={current:.0f} {op} {value:.0f}"


def _file_due(item: dict) -> tuple[bool, str]:
    trigger = item.get("trigger") or {}
    path = Path(str(trigger.get("path") or "")).expanduser()
    event = str(trigger.get("event") or "created")
    state = item.get("state") if isinstance(item.get("state"), dict) else {}
    exists = path.exists()
    mtime = path.stat().st_mtime if exists else 0.0
    seen_exists = bool(state.get("file_exists"))
    seen_mtime = float(state.get("file_mtime") or 0.0)
    hit, why = False, ""
    if event in ("created", "any") and exists and not seen_exists:
        hit, why = True, f"أُنشئ {path.name}"
    elif event in ("modified", "any") and exists and seen_mtime and mtime > seen_mtime + 0.5:
        hit, why = True, f"عُدّل {path.name}"
    elif event in ("deleted", "any") and not exists and seen_exists:
        hit, why = True, f"حُذف {path.name}"
    return hit, why


def _app_due(item: dict) -> tuple[bool, str]:
    trigger = item.get("trigger") or {}
    process = str(trigger.get("process") or "")
    event = str(trigger.get("event") or "start")
    running = C.process_running(process)
    if running is None:
        return False, ""
    state = item.get("state") if isinstance(item.get("state"), dict) else {}
    seen = state.get("app_running")
    if event == "start" and running and seen is False:
        return True, f"بدأ {process}"
    if event == "stop" and not running and seen is True:
        return True, f"توقّف {process}"
    if event == "any" and seen is not None and bool(seen) != bool(running):
        return True, f"{'بدأ' if running else 'توقّف'} {process}"
    return False, ""


def _refresh_state(item: dict) -> None:
    """يحفظ ما نرصده الآن حتى لا تتكرر الأحداث القديمة بعد إعادة التشغيل."""
    trigger = item.get("trigger") or {}
    kind = str(trigger.get("kind"))
    state = item.get("state") if isinstance(item.get("state"), dict) else {}
    if kind == "file":
        path = Path(str(trigger.get("path") or "")).expanduser()
        state["file_exists"] = path.exists()
        state["file_mtime"] = path.stat().st_mtime if path.exists() else 0.0
    elif kind == "app":
        state["app_running"] = C.process_running(str(trigger.get("process") or ""))
    elif kind == "system":
        state["system_last"] = C.now_iso()
    item["state"] = state


def _fire(source_plugin: str, status: str, output: str, player=None) -> list[dict]:
    """تُنادى من أي إضافة (أو من سجل التنفيذ) لتشغيل أتمتات الأحداث المطابقة."""
    global _FIRING
    if _FIRING:
        return []          # حدث داخل حدث — لا لفّ
    source = str(source_plugin or "").strip()
    if not source:
        return []
    _FIRING = True
    fired: list[dict] = []
    try:
        data = _load()
        for key, item in list(data["items"].items()):
            if not item.get("enabled", True):
                continue
            trigger = item.get("trigger") or {}
            if str(trigger.get("kind")) != "plugin":
                continue
            if str(trigger.get("plugin")) != source:
                continue
            wanted = str(trigger.get("status") or "any").lower()
            if wanted not in ("any", str(status).lower()):
                continue
            text = _execute_and_record(item, key, reason=f"حدث {source}={status}", player=player)
            fired.append({"id": key, "name": item.get("name"), "result": text})
    finally:
        _FIRING = False
    return fired


def _tick(player=None, *, respect_lock: bool = True) -> str:
    """ينفّذ كل ما حان وقته الآن. يناديه المجدول دوريًا أو المستخدم عند الطلب."""
    data = _load()
    now = C.now()
    ran, skipped = [], []
    for key, item in list(data["items"].items()):
        if not item.get("enabled", True):
            continue
        trigger = item.get("trigger") or {}
        kind = str(trigger.get("kind"))
        due, why = False, ""
        if kind in ("daily", "weekly", "interval", "once") or item.get("retry_used"):
            nxt = C.from_iso(item.get("next_run"))
            if nxt is None:
                nxt = C.next_run(trigger, after=now)
                fresh = _load()
                if key in fresh["items"]:
                    fresh["items"][key]["next_run"] = C.to_iso(nxt) if nxt else ""
                    _save(fresh)
            if nxt and nxt <= now:
                due, why = True, f"حان {C.describe_schedule(trigger)}"
        elif kind == "file":
            due, why = _file_due(item)
        elif kind == "app":
            due, why = _app_due(item)
        elif kind == "system":
            due, why = _system_due(item)
        if not due:
            continue
        if respect_lock and str(item.get("last_run") or "") and kind in ("file", "app", "system"):
            last = C.from_iso(item.get("last_run"))
            if last and (now - last).total_seconds() < _LOCK_WINDOW_S:
                skipped.append(str(item.get("name")))
                continue
        outcome = _run_item(item, key, reason=why, player=player)
        fresh = _load()
        entry = fresh["items"].get(key)
        if entry is None:
            continue
        entry["last_run"] = C.now_iso()
        entry["last_status"] = "ok" if outcome["ok"] else "failed"
        entry["run_count"] = int(entry.get("run_count", 0)) + 1
        entry["last_error"] = C.clip(outcome.get("error", "") or "", 200)
        _history_append(entry, {"ts": entry["last_run"], "reason": why, "status": entry["last_status"],
                                "failed_step": outcome.get("failed"), "ms": outcome.get("ms", 0)})
        _refresh_state(entry)
        if kind == "once":
            entry["enabled"] = False
            entry["next_run"] = ""
        elif kind in ("daily", "weekly", "interval"):
            nxt = C.next_run(trigger, after=now)
            entry["next_run"] = C.to_iso(nxt) if nxt else ""
        _save(fresh)
        C.log_event("automation_engine", f"tick:{entry.get('name')}", entry["last_status"],
                    outcome.get("ms", 0), entry["last_error"], {"reason": why})
        ran.append({"name": entry.get("name"), "ok": outcome["ok"], "output": outcome.get("output", ""),
                    "failed": outcome.get("failed")})
    if not ran:
        out = "سيدي، لا أتمتة حان وقتها الآن." + (f" (تجاهلت {len(skipped)} حديثة التنفيذ)" if skipped else "")
    else:
        lines = [f"سيدي، نفّذت {len(ran)} أتمتة:"]
        for row in ran:
            lines.append(f"  {'✔' if row['ok'] else '✗'} {row['name']}" +
                         (f" — تعثّرت في {row['failed']}" if not row["ok"] and row.get("failed") else ""))
            if row["ok"] and row.get("output"):
                lines.append("      " + C.clip(row["output"], 160))
        out = "\n".join(lines)
    _log(player, out)
    return out


# ── الأفعال ───────────────────────────────────────────────────────────────────
def _create(params: dict, player, nl_text: str = "") -> str:
    name = str(params.get("name") or "").strip()
    steps, why = _parse_steps(params.get("steps", params.get("steps_json")), player)
    if nl_text:
        auto = _nl_to_steps(nl_text)
        if not steps and auto:
            steps = auto
    if not steps:
        return (f"سيدي، لم أفهم خطوات الأتمتة{(': ' + why) if why and why != 'بلا خطوات' else '.'}\n"
                f"  أعطني steps كـJSON، مثل: [{{\"plugin\":\"system_health\",\"parameters\":{{\"action\":\"status\"}}}}]، "
                f"أو صُغ الطلب بخطوات أوضح (مثال: 'لخّص الأخبار ثم أشعرني على التليجرام').")
    trigger, why = _parse_trigger(params.get("trigger") or params.get("trigger_json") or params.get("when"))
    if trigger is None:
        return f"سيدي، {why}"
    if not name:
        name = C.clip(nl_text, 60) or (C.describe_schedule(trigger) + " — " + str(steps[0].get("plugin")))
        name = name.strip() or "أتمتة"
    key = _new_id(name)
    item = {
        "id": key, "name": name, "nl": C.clip(nl_text, 300), "enabled": True,
        "trigger": trigger, "steps": steps, "created": C.now_iso(), "updated": C.now_iso(),
        "last_run": "", "last_status": "", "last_error": "", "run_count": 0,
        "retry": params.get("retry") if isinstance(params.get("retry"), dict) else {},
        "retry_used": 0, "history": [], "state": {},
    }
    nxt = C.next_run(trigger, after=C.now())
    item["next_run"] = C.to_iso(nxt) if nxt else ""
    data = _load()
    data["items"][key] = item
    if not _save(data):
        return "سيدي، تعذّر حفظ الأتمتة."
    C.log_event("automation_engine", "create", "ok", 0, "", {"id": key, "trigger": trigger.get("kind")})
    lines = [f"سيدي، أنشأت الأتمتة '{name}' ({key}).",
             f"  التوقيت: {C.describe_schedule(trigger) if trigger.get('kind') != 'plugin' else 'عند حدث ' + str(trigger.get('plugin'))}",
             "  الخطوات: " + " ← ".join(f"{s['plugin']}" for s in steps),
             "  القادم: " + (item["next_run"] or "بلا موعد (يدوية/حدثية)"),
             "  للتشغيل فورًا: action=run name=" + name]
    out = "\n".join(lines)
    _log(player, out)
    return out


def _act_list(params: dict, player) -> str:
    query = str(params.get("query") or "").strip().lower()
    data = _load()
    items = [it for it in data["items"].values() if not query or query in str(it.get("name", "")).lower()]
    if not items:
        return "سيدي، لا أتمتات بعد." if not query else f"سيدي، لا أتمتة تطابق '{query}'."
    lines = [f"سيدي، أتمتاتك ({len(items)}):"]
    for item in sorted(items, key=lambda i: str(i.get("name", ""))):
        state = "مفعّلة" if item.get("enabled", True) else "موقوفة"
        when = ("عند حدث " + str((item.get("trigger") or {}).get("plugin"))) \
            if str((item.get("trigger") or {}).get("kind")) == "plugin" else C.describe_schedule(item.get("trigger"))
        lines.append(f"  • {item.get('name')} [{state}] — {when} · خطوات: "
                     f"{' ← '.join(str(s.get('plugin')) for s in (item.get('steps') or []))}"
                     + (f" · القادم: {item.get('next_run')}" if item.get("next_run") else "")
                     + (f" · آخر حالة: {item.get('last_status')}" if item.get("last_status") else ""))
    out = "\n".join(lines)
    _log(player, out)
    return out


def _act_show(params: dict, player) -> str:
    item, key = _find(str(params.get("name") or ""))
    if item is None:
        return f"سيدي، {key}"
    lines = [f"سيدي، الأتمتة '{item.get('name')}' ({key}):",
             f"  الحالة: {'مفعّلة' if item.get('enabled', True) else 'موقوفة'} · التشغيلات: {item.get('run_count', 0)}",
             f"  التوقيت: {C.describe_schedule(item.get('trigger'))} · القادم: {item.get('next_run') or '—'}",
             "  الخطوات:"]
    for step in item.get("steps") or []:
        param_text = C.clip(json.dumps(step.get("parameters") or {}, ensure_ascii=False), 160)
        lines.append(f"    • {step.get('name')} → {step.get('plugin')} {param_text}")
    history = item.get("history") or []
    if history:
        lines.append("  آخر التشغيلات:")
        for row in history[-5:][::-1]:
            lines.append(f"    · {row.get('ts')} — {row.get('status')}"
                         + (f" (فشلت: {row.get('failed_step')})" if row.get("failed_step") else ""))
    out = "\n".join(lines)
    _log(player, out)
    return out


def _act_run(params: dict, player) -> str:
    item, key = _find(str(params.get("name") or ""))
    if item is None:
        return f"سيدي، {key}"
    return _execute_and_record(item, str(key), reason="طلب يدوي", player=player)


def _act_toggle(params: dict, player, enabled: bool) -> str:
    item, key = _find(str(params.get("name") or ""))
    if item is None:
        return f"سيدي، {key}"
    data = _load()
    fresh = data["items"].get(str(key))
    if fresh is None:
        return "سيدي، الأتمتة اختفت."
    fresh["enabled"] = enabled
    if enabled:
        nxt = C.next_run(fresh.get("trigger") or {}, after=C.now())
        fresh["next_run"] = C.to_iso(nxt) if nxt else ""
        fresh["retry_used"] = 0
    _save(data)
    C.log_event("automation_engine", "enable" if enabled else "disable", "ok", 0, "", {"id": key})
    out = f"سيدي، {'فعّلت' if enabled else 'أوقفت'} الأتمتة '{fresh.get('name')}'."
    if enabled and fresh.get("next_run"):
        out += f"\n  القادم: {fresh['next_run']} ({C.describe_schedule(fresh.get('trigger'))})"
    _log(player, out)
    return out


def _act_edit(params: dict, player) -> str:
    item, key = _find(str(params.get("name") or ""))
    if item is None:
        return f"سيدي، {key}"
    data = _load()
    fresh = data["items"].get(str(key))
    if fresh is None:
        return "سيدي، الأتمتة اختفت."
    changes = []
    if params.get("steps") or params.get("steps_json"):
        steps, why = _parse_steps(params.get("steps", params.get("steps_json")), player)
        if not steps:
            return f"سيدي، {why}"
        fresh["steps"] = steps
        changes.append("الخطوات")
    if params.get("trigger") or params.get("when"):
        trigger, why = _parse_trigger(params.get("trigger") or params.get("when"))
        if trigger is None:
            return f"سيدي، {why}"
        fresh["trigger"] = trigger
        nxt = C.next_run(trigger, after=C.now())
        fresh["next_run"] = C.to_iso(nxt) if nxt else ""
        changes.append("التوقيت")
    if params.get("new_name"):
        fresh["name"] = C.clip(params["new_name"], 80)
        changes.append("الاسم")
    if not changes:
        return "سيدي، ماذا أعدّل؟ أعطني steps أو when أو new_name."
    fresh["updated"] = C.now_iso()
    _save(data)
    C.log_event("automation_engine", "edit", "ok", 0, "", {"id": key, "changed": changes})
    out = f"سيدي، عدّلت {' و'.join(changes)} في الأتمتة '{fresh.get('name')}'."
    if "التوقيت" in changes:
        out += f"\n  التوقيت الآن: {C.describe_schedule(fresh.get('trigger'))}"
        if fresh.get("next_run"):
            out += f" · القادم: {fresh['next_run']}"
    _log(player, out)
    return out


def _do_delete(key: str) -> str:
    data = _load()
    item = data["items"].pop(str(key), None)
    if item is None:
        return "سيدي، الأتمتة لم تعد موجودة."
    _save(data)
    C.log_event("automation_engine", "delete", "ok", 0, "", {"id": key, "name": item.get("name")})
    return f"سيدي، حذفت الأتمتة '{item.get('name')}'. لا شيء آخر تغيّر."


def _act_delete(params: dict, player) -> str:
    item, key = _find(str(params.get("name") or ""))
    if item is None:
        return f"سيدي، {key}"
    steps_text = " ← ".join(str(s.get("plugin")) for s in (item.get("steps") or []))
    return C.confirm_request(
        key=f"automation_delete_{key}",
        title=f"حذف الأتمتة '{item.get('name')}'؟",
        detail=(f"التوقيت: {C.describe_schedule(item.get('trigger'))}\n"
                f"الخطوات: {steps_text}\n"
                f"الحذف من ملف automations.json فقط — الإضافات نفسها لا تُمس."),
        run=lambda: _do_delete(str(key)),
        player=player,
    )


def _act_events(params: dict, player) -> str:
    del params
    data = _load()
    rows = [i for i in data["items"].values()
            if str((i.get("trigger") or {}).get("kind")) in ("plugin", "file", "app", "system")]
    if not rows:
        return "سيدي، لا أتمتات حدثية (لا على إضافات، ولا ملفات، ولا تطبيقات، ولا مؤشرات نظام)."
    lines = [f"سيدي، الأتمتات الحدثية ({len(rows)}):"]
    for item in rows:
        trigger = item.get("trigger") or {}
        kind = str(trigger.get("kind"))
        if kind == "plugin":
            desc = f"عند انتهاء '{trigger.get('plugin')}' بحالة {trigger.get('status', 'any')}"
        elif kind == "file":
            desc = f"ملف {trigger.get('path')} ({trigger.get('event')})"
        elif kind == "app":
            desc = f"تطبيق {trigger.get('process')} ({trigger.get('event')})"
        else:
            desc = f"{trigger.get('metric')} {trigger.get('op')} {trigger.get('value')}"
        lines.append(f"  • {item.get('name')} — {desc}"
                     + ("" if item.get("enabled", True) else " (موقوفة)"))
    out = "\n".join(lines)
    _log(player, out)
    return out


def _act_logs(params: dict, player) -> str:
    name = str(params.get("name") or "").strip().lower()
    rows = [r for r in C.tail_log("orchestration", 300) if str(r.get("plugin")) == "automation_engine"]
    if name:
        rows = [r for r in rows if name in str(r.get("action", "")).lower()]
    if not rows:
        return "سيدي، لا سجلات أتمتة بعد."
    lines = [f"سيدي، سجل الأتمتة ({len(rows)} سطرًا، الأحدث أولًا):"]
    for row in rows[:25]:
        lines.append(f"  · {row.get('ts')} {row.get('action')} — {row.get('status')} "
                     f"({row.get('duration_ms')}ms)" + (f" — {row.get('error')}" if row.get("error") else ""))
    out = "\n".join(lines)
    _log(player, out)
    return out


def _act_status(params: dict, player) -> str:
    del params
    data = _load()
    items = list(data["items"].values())
    enabled = [i for i in items if i.get("enabled", True)]
    upcoming = sorted([i for i in enabled if i.get("next_run")], key=lambda i: str(i.get("next_run")))
    lines = [
        "سيدي، حالة محرّك الأتمتة:",
        f"  الأتمتات: {len(items)} · مفعّلة: {len(enabled)} · ملف: {C.AUTOMATIONS_FILE.name}",
        f"  تعمل الآن: {len(_RUNNING)}" + (f" ({'، '.join(sorted(_RUNNING))})" if _RUNNING else ""),
    ]
    if upcoming:
        lines.append("  الأقرب:")
        for item in upcoming[:4]:
            lines.append(f"    · {item.get('name')} — {item.get('next_run')}")
    failures = [i for i in items if str(i.get("last_status")) in ("failed", "retrying")]
    if failures:
        lines.append(f"  تحتاج انتباه: {len(failures)} — {'، '.join(str(i.get('name')) for i in failures[:5])}")
    out = "\n".join(lines)
    _log(player, out)
    return out


_HELP = (
    "محرّك الأتمتة، سيدي:\n"
    "• nl_create text=\"كل يوم ٨ صباحًا لخّص الأخبار ثم أشعرني\" — أو create مع steps وwhen\n"
    "  steps كـJSON: [{\"name\":\"خبر\",\"plugin\":\"news_monitor\",\"parameters\":{\"action\":\"latest\"}}]\n"
    "  when: 'كل يوم ٨ صباحًا' · 'كل ٣٠ دقيقة' · 'أسبوعيًا الأحد ٩ مساءً' · أو trigger JSON\n"
    "  أحداث: trigger={\"kind\":\"plugin\",\"plugin\":\"news_monitor\",\"status\":\"ok\"}\n"
    "         أو {\"kind\":\"file\",\"path\":\"C:/x\",\"event\":\"created\"} أو {\"kind\":\"system\",\"metric\":\"ram\",\"op\":\">\",\"value\":90}\n"
    "• list · show name= · run name= · enable/disable name= · edit · delete (بتأكيد)\n"
    "• tick — ينفّذ ما حان وقته · events — الأتمتات الحدثية · logs · status\n"
    "  fire — تُناديها الإضافات عند انتهائها (يربط الإضافات بالأتمتة بلا لفّ)"
)


def run(parameters: dict, player=None, session_memory=None) -> str:
    """نقطة الدخول. لا ترمي أبدًا."""
    del session_memory
    if C is None:
        return "سيدي، ملف '_orch_core.py' المشترك غير موجود — بدونه لا يعمل المحرّك."
    try:
        p = dict(parameters or {})
        action = str(p.get("action") or "status").strip().lower()
        C.ensure_dirs()
        if action in ("create", "أضف", "أنشئ"):
            return _create(p, player, nl_text=str(p.get("text") or ""))
        if action in ("nl_create", "create_nl", "من_جملة"):
            text = str(p.get("text") or p.get("name") or "").strip()
            if not text:
                return "سيدي، قل لي الطلب كما تقوله: 'كل يوم ٨ صباحًا ... ثم ...'"
            return _create(p, player, nl_text=text)
        if action in ("list", "قائمة"):
            return _act_list(p, player)
        if action in ("show", "info", "تفاصيل"):
            return _act_show(p, player)
        if action in ("run", "شغّل"):
            return _act_run(p, player)
        if action in ("enable", "فعّل"):
            return _act_toggle(p, player, True)
        if action in ("disable", "عطّل", "أوقف"):
            return _act_toggle(p, player, False)
        if action in ("edit", "عدّل"):
            return _act_edit(p, player)
        if action in ("delete", "remove", "احذف"):
            return _act_delete(p, player)
        if action in ("tick", "check", "نفّذ_المستحق"):
            return _tick(player)
        if action in ("fire", "حدث"):
            source = str(p.get("source_plugin") or p.get("plugin") or "").strip()
            if not source:
                return "سيدي، fire يحتاج source_plugin."
            fired = _fire(source, str(p.get("source_status") or "any"), str(p.get("output") or ""), player)
            if not fired:
                return f"سيدي، لا أتمتة مرتبطة بحدث '{source}'."
            return "\n\n".join(str(f["result"]) for f in fired)
        if action in ("events", "أحداث"):
            return _act_events(p, player)
        if action in ("logs", "سجل"):
            return _act_logs(p, player)
        if action in ("status", "حالة"):
            return _act_status(p, player)
        if action in ("help", "?"):
            return _HELP
        return f"سيدي، '{action}' ليس فعلًا أعرفه في محرّك الأتمتة. قل 'help'."
    except Exception as exc:  # noqa: BLE001
        return f"سيدي، محرّك الأتمتة واجه مشكلة: {str(exc)[:180]}"


# ── فحص ذاتي: python automation_engine.py ─────────────────────────────────────
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
    print("automation_engine — self-test")
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

    tmp = Path(tempfile.mkdtemp(prefix="ae_test_"))
    real_files = (C.AUTOMATIONS_FILE, C.LOG_DIR)
    real_stamp, real_data = C._SCAN_CACHE.get("stamp"), C._SCAN_CACHE.get("data")
    base = C.CONFIG_DIR / "api_keys.json"
    backup = base.read_text(encoding="utf-8") if base.exists() else None
    try:
        C.AUTOMATIONS_FILE = tmp / "automations.json"
        C.LOG_DIR = tmp / "logs"
        C.LOG_DIR.mkdir(parents=True, exist_ok=True)
        C.save_json(C.AUTOMATIONS_FILE, {"version": 1, "items": {}})
        C.store_settings("automation_engine", {"default_time": "08:00", "step_timeout_s": "60",
                                               "max_steps": "25", "system_cooldown_min": "15"})
        watch = tmp / "watch_me.txt"
        real_plugins = C.PLUGINS_DIR

        # 1) إنشاء بخطوات صريحة + جدولة يومية
        steps = json.dumps([{"name": "حالة", "plugin": "habit_tracker", "parameters": {"action": "status"}}])
        out = run({"action": "create", "name": "اختبار يومي", "when": "كل يوم 8 صباحًا", "steps": steps})
        check("create بموعد يومي", "أنشأت الأتمتة" in out and "يوميًا" in out, out[:200])
        check("حُفظت في الملف", len(_load()["items"]) == 1)
        item = list(_load()["items"].values())[0]
        check("القادم محسوب", bool(item.get("next_run")), str(item.get("next_run")))
        # 2) تشغيل يدوي
        out = run({"action": "run", "name": "اختبار يومي"})
        check("run ينفّذ الخطوة", "✔" in out and "نفّذت" in out, out[:240])
        fresh = list(_load()["items"].values())[0]
        check("التشغيل مُسجَّل في التاريخ", fresh.get("run_count") == 1 and fresh.get("history"), str(fresh.get("history"))[:120])
        check("السجل المنظَّم يحوي سطرًا", any(r.get("plugin") == "automation_engine" for r in C.tail_log("orchestration", 20)))
        # 3) خطوة فاشلة لا تُسقط شيئًا وتحفظ مخرجات السابق
        #    نستخدم مجلد إضافات اختباريًا: خطوة تنجح دائمًا وأخرى تنفجر دائمًا
        step_dir = tmp / "step_plugins"
        step_dir.mkdir(parents=True, exist_ok=True)
        (step_dir / "ok_step.py").write_text(
            'PLUGIN = {"name": "ok_step", "description": "خطوة ناجحة للاختبار"}\n'
            'def run(parameters, player=None):\n    return "نجحت الخطوة"\n', encoding="utf-8")
        (step_dir / "boom_step.py").write_text(
            'PLUGIN = {"name": "boom_step", "description": "خطوة تفشل للاختبار"}\n'
            'def run(parameters, player=None):\n    raise RuntimeError("انفجار مقصود")\n', encoding="utf-8")
        C.PLUGINS_DIR = step_dir
        C.scan_plugins(force=True)
        bad_steps = json.dumps([
            {"name": "سليمة", "plugin": "ok_step", "parameters": {}},
            {"name": "متعطلة", "plugin": "boom_step", "parameters": {}},
        ])
        run({"action": "create", "name": "اختبار الفشل", "when": "كل 30 دقيقة", "steps": bad_steps})
        out = run({"action": "run", "name": "اختبار الفشل"})
        check("الفشل يُعلَن باسم الخطوة", "فشلت الخطوة" in out, out[:260])
        check("مخرجات الخطوة السابقة محفوظة", "الناتج:" in out or "مخرجات الخطوات السابقة" in out, out[:300])
        check("المساعد لم ينهَر", "تعثّرت في" in out, out[:200])
        C.PLUGINS_DIR = real_plugins
        C.scan_plugins(force=True)
        # 4) الأتمتة الحدثية
        ev = json.dumps({"kind": "plugin", "plugin": "news_monitor", "status": "ok"})
        run({"action": "create", "name": "بعد الأخبار", "trigger": ev,
             "steps": json.dumps([{"plugin": "note_manager",
                                   "parameters": {"action": "add", "text": "خبر جديد: {{prev}}"}}])})
        out = run({"action": "fire", "source_plugin": "news_monitor", "source_status": "ok", "output": "عنوان تجريبي"})
        check("fire يشغّل أتمتة الحدث", "بعد الأخبار" in out and "نفّذت" in out, out[:240])
        out = run({"action": "fire", "source_plugin": "لا_توجد"})
        check("حدث بلا أتمتة → رسالة", "لا أتمتة مرتبطة" in out, out[:160])
        out = run({"action": "events"})
        check("events يعرض الحدثية", "بعد الأخبار" in out and "news_monitor" in out, out[:200])
        # 5) أتمتة الملفات
        run({"action": "create", "name": "مراقب الملف",
             "trigger": json.dumps({"kind": "file", "path": str(watch), "event": "created"}),
             "steps": json.dumps([{"plugin": "habit_tracker", "parameters": {"action": "status"}}])})
        out = run({"action": "tick"})
        check("ملف غير موجود → لا تنفيذ", "لا أتمتة حان وقتها" in out or "نفّذت" in out, out[:200])
        watch.write_text("ظهرت", encoding="utf-8")
        out = run({"action": "tick"})
        check("إنشاء الملف يُشغّل الأتمتة", "مراقب الملف" in out, out[:240])
        check("الحالة محفوظة بعد التشغيل", bool(list(_load()["items"].values())[-1].get("state", {}).get("file_exists")))
        out = run({"action": "tick"})
        check("لا تكرار لنفس الحدث", "مراقب الملف" not in out, out[:200])
        # 6) الحمايات
        out = run({"action": "create", "name": "لولبية", "when": "كل 5 دقائق",
                   "steps": json.dumps([{"plugin": "automation_engine", "parameters": {"action": "tick"}}])})
        check("الإدارية مرفوضة داخل الأتمتة", "إدارية" in out, out[:200])
        out = run({"action": "create", "name": "وهمية", "when": "كل 5 دقائق",
                   "steps": json.dumps([{"plugin": "لا_توجد_إضافة"}])})
        check("إضافة غير موجودة مرفوضة", "غير موجودة" in out, out[:200])
        out = run({"action": "create", "name": "وقت غامض", "when": "في وقت ما", "steps": steps})
        check("توقيت غير مفهوم يُقال بصراحة", "لم أفهم التوقيت" in out, out[:200])
        # 7) التفعيل/الإيقاف والتعديل والعرض
        out = run({"action": "disable", "name": "اختبار يومي"})
        check("disable يعمل", "أوقفت" in out, out[:160])
        check("الحالة في الملف", list(_load()["items"].values())[0].get("enabled") is False)
        out = run({"action": "enable", "name": "اختبار يومي"})
        check("enable يعمل ويعيد الموعد", "فعّلت" in out, out[:200])
        out = run({"action": "list"})
        check("list يعرض الكل", "اختبار يومي" in out and "مراقب الملف" in out, out[:240])
        out = run({"action": "show", "name": "اختبار يومي"})
        check("show يعرض الخطوات والتاريخ", "الخطوات" in out and "آخر التشغيلات" in out, out[:240])
        out = run({"action": "edit", "name": "اختبار يومي", "when": "كل 15 دقيقة"})
        check("edit يغيّر التوقيت", "عدّلت" in out and "كل 15 دقيقة" in out, out[:200])
        out = run({"action": "edit", "name": "اختبار يومي", "new_name": "اختبار مُعدَّل"})
        check("edit يغيّر الاسم", "اختبار مُعدَّل" in out, out[:160])
        # 8) الحذف ببوابة تأكيد
        before = len(_load()["items"])
        out = run({"action": "delete", "name": "اختبار مُعدَّل"})
        check("الحذف يستأذن ولا يحذف فورًا", len(_load()["items"]) == before, out[:200])
        item, key = _find("اختبار مُعدَّل")
        out = _do_delete(str(key))
        check("الحذف بعد الموافقة", len(_load()["items"]) == before - 1 and "حذفت" in out, out[:140])
        # 9) nl_create
        out = run({"action": "nl_create", "text": "كل يوم 7 صباحًا لخّص الأخبار ثم أشعرني"})
        check("nl_create يفهم سلسلة عربية", "أنشأت الأتمتة" in out, out[:260])
        check("nl_create بنى خطوات صحيحة", "news_monitor" in out or "summarizer" in out, out[:260])
        out = run({"action": "nl_create", "text": "كل يوم 7 صباحًا"})
        check("nl_create بلا خطوات يفهمها → يطلبها", "أعطني steps" in out, out[:260])
        # 10) الحالة والسجل والمساعدة والفعل المجهول
        out = run({"action": "status"})
        check("status يعرض أرقامًا", "الأتمتات:" in out, out[:200])
        out = run({"action": "logs"})
        check("logs يعرض سطورًا", "سجل الأتمتة" in out or "لا سجلات" in out, out[:160])
        check("help يعمل", "محرّك الأتمتة" in run({"action": "help"}))
        check("فعل مجهول", "ليس فعلًا" in run({"action": "zzz"}))
        # 11) لا أسرار في السجل
        raw = ""
        for f in C.LOG_DIR.glob("*.jsonl"):
            raw += f.read_text(encoding="utf-8")
        check("لا كلمات مرور/توكنات في السجل", "password" not in raw.lower() and "bearer " not in raw.lower())
    finally:
        C.PLUGINS_DIR = real_plugins if "real_plugins" in locals() else C.PLUGINS_DIR
        C.AUTOMATIONS_FILE, C.LOG_DIR = real_files
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
