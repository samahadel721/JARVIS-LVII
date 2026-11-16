"""
workflow_engine.py — محرّك سير العمل (سلاسل الإضافات).

يسلسل إضافاتك الموجودة: A → B → C مع تمرير المخرجات، فروع شرطية، مسارات نجاح/فشل،
متغيّرات قابلة لإعادة الاستخدام — وكل ذلك من جملة عربية أو من خطوات صريحة.

المتغيّرات المدعومة في أي قيمة نصّية:
  {{prev}}                 مخرَج الخطوة السابقة
  {{steps.اسم_الخطوة.output}}  مخرَج خطوة بعينها
  {{vars.اسم_المتغيّر}}     متغيّر تمرّره عند التشغيل

الشروط لكل خطوة: {"left": "...", "op": "contains|==|!=|>|<|empty|not_empty", "right": "..."}
مسار الفشل لكل خطوة: {"action": "continue|stop|jump", "step": "اسم خطوة"}

قوالب جاهزة تُنشئ السلاسل المطلوبة في أمر واحد (كلها إضافاتك الموجودة):
  بحث → تلخيص → مستند → إشعار · أخبار → تلخيص → ترجمة → تليجرام · طقس+تقويم+تذكير → إشعار · موجز صباحي.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path
from typing import Any, Optional

PLUGIN = {
    "name": "workflow_engine",
    "description": (
        "Builds and runs multi-plugin workflows: chain A then B then C, pass each output to the "
        "next step, branch on a condition, define success and failure paths, and reuse variables. "
        "Example chains already in the assistant: web search → summarize → document → notify, and "
        "news → summarize → translate → Telegram. Use for: 'make a workflow', 'search for X then "
        "summarize it and write a report then notify me', 'run the research workflow', 'what "
        "workflows do I have', 'stop the daily workflow', 'delete that workflow' (asks first)."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": ("create | nl_create | templates | use_template | list | show | run | "
                                "enable | disable | edit | delete | logs | status | help"),
            },
            "name": {"type": "STRING", "description": "Workflow name (or id) / template name."},
            "text": {"type": "STRING", "description": "Your request in your own words (nl_create)."},
            "steps": {"type": "STRING", "description": "Steps as JSON: [{\"plugin\":\"...\",\"parameters\":{...}}]."},
            "vars": {"type": "STRING", "description": "Variables as JSON, e.g. {\"topic\":\"الطاقة الشمسية\"}."},
            "new_name": {"type": "STRING", "description": "For edit: rename to this."},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "workflow_engine",
    "title": "Workflow engine / محرّك سير العمل",
    "fields": [
        {"key": "step_timeout_s", "type": "text", "label": "Seconds allowed per step", "default": "240"},
        {"key": "max_steps", "type": "text", "label": "Maximum steps per workflow", "default": "25"},
        {"key": "keep_history", "type": "text", "label": "Runs kept per workflow", "default": "10"},
    ],
}

_ADMIN_IN_WORKFLOW = frozenset({"plugin_manager", "automation_engine", "workflow_engine",
                                "workflow_scheduler", "remote_api", "web_panel"})

# القوالب الجاهزة: سلاسل من إضافات موجودة فعلًا (لا شيء مخترع)
_TEMPLATES: dict[str, dict] = {
    "research_report": {
        "title": "بحث → تلخيص → مستند → إشعار",
        "about": "يبحث عن موضوع عبر مساعد البحث، يلخّص النتائج، يكتب تقريرًا في ملف، ثم يُشعرك.",
        "vars": ["topic"],
        "steps": [
            {"name": "بحث", "plugin": "research_assistant",
             "parameters": {"action": "research", "topic": "{{vars.topic}}"}},
            {"name": "تلخيص", "plugin": "summarizer",
             "parameters": {"action": "summarize", "text": "{{steps.بحث.output}}"}},
            {"name": "مستند", "plugin": "document_generator",
             "parameters": {"action": "create", "title": "تقرير: {{vars.topic}}",
                            "content": "{{steps.تلخيص.output}}"}},
            {"name": "إشعار", "plugin": "notification_center",
             "parameters": {"action": "notify", "title": "تقرير جاهز",
                            "body": "{{steps.مستند.output}}", "category": "workflow", "priority": "info"}},
        ],
    },
    "news_digest": {
        "title": "أخبار → تلخيص → ترجمة → تليجرام",
        "about": "يجلب آخر أخبار موضوع، يلخّصها، يترجمها، ثم يرسلها على تليجرام.",
        "vars": ["topic", "language"],
        "steps": [
            {"name": "أخبار", "plugin": "news_monitor",
             "parameters": {"action": "latest", "keyword": "{{vars.topic}}"}},
            {"name": "تلخيص", "plugin": "summarizer",
             "parameters": {"action": "summarize", "text": "{{steps.أخبار.output}}"}},
            {"name": "ترجمة", "plugin": "translation",
             "parameters": {"action": "translate", "text": "{{steps.تلخيص.output}}",
                            "to": "{{vars.language}}"}},
            {"name": "تليجرام", "plugin": "telegram_control",
             "parameters": {"action": "send", "message": "{{steps.ترجمة.output}}"}},
        ],
    },
    "morning_pulse": {
        "title": "صلاة + تقويم → تلخيص → إشعار واحد",
        "about": "يجمع مواقيت الصلاة ومواعيد اليوم في إشعار واحد قصير.",
        "vars": [],
        "steps": [
            {"name": "صلوات", "plugin": "prayer_times", "parameters": {"action": "today"}},
            {"name": "تقويم", "plugin": "calendar_events", "parameters": {"action": "today"}},
            {"name": "ملخّص", "plugin": "summarizer",
             "parameters": {"action": "summarize",
                            "text": "الصلاة: {{steps.صلوات.output}} · اليوم: {{steps.تقويم.output}}"}},
            {"name": "إشعار", "plugin": "notification_center",
             "parameters": {"action": "notify", "title": "نبض الصباح", "body": "{{steps.ملخّص.output}}"}},
        ],
    },
    "full_briefing": {
        "title": "الموجز الصباحي الكامل (يجمع كل شيء)",
        "about": "يشغّل مُجمِّع الموجز (طقس، تقويم، تذكيرات، أخبار، نظام) ثم يُشعرك.",
        "vars": [],
        "steps": [
            {"name": "موجز", "plugin": "daily_briefing", "parameters": {"action": "brief"}},
            {"name": "إشعار", "plugin": "notification_center",
             "parameters": {"action": "notify", "title": "الموجز الصباحي", "body": "{{steps.موجز.output}}",
                            "category": "briefing", "priority": "info"}},
        ],
    },
}

_NL_MAP: tuple[tuple[tuple[str, ...], str, dict], ...] = (
    # (كلمات, إضافة, معاملات) — إن وُجد متغيّر {{vars.topic}} يُملأ من الجملة أو من vars
    (("ابحث في ملفاتي", "ذاكرة", "semantic"), "semantic_search", {"action": "search", "query": "{{vars.topic}}"}),
    (("ابحث", "بحث", "دور", "search"), "research_assistant", {"action": "research", "topic": "{{vars.topic}}"}),
    (("أخبار", "اخبار", "news"), "news_monitor", {"action": "latest", "keyword": "{{vars.topic}}"}),
    (("لخّص", "لخص", "ملخّص", "تلخيص", "summar"), "summarizer", {"action": "summarize", "text": "{{prev}}"}),
    (("ترجم", "ترجمة", "translate"), "translation", {"action": "translate", "text": "{{prev}}",
                                                     "to": "{{vars.language}}"}),
    (("مستند", "تقرير", "وثيقة", "document", "report"), "document_generator",
     {"action": "create", "title": "تقرير: {{vars.topic}}", "content": "{{prev}}"}),
    (("أشعرني", "إشعار", "اشعار", "بلغني", "notif"), "notification_center",
     {"action": "notify", "title": "JARVIS", "body": "{{prev}}"}),
    (("تليجرام", "تلجرام", "telegram", "ارسل على"), "telegram_control", {"action": "send", "message": "{{prev}}"}),
    (("واتس", "واتساب", "whatsapp"), "whatsapp_control", {"action": "send", "message": "{{prev}}"}),
    (("رسالة", "message", "قول له"), "universal_messenger", {"action": "send", "message": "{{prev}}"}),
    (("احفظ", "ملاحظة", "دوّن", "note"), "note_manager", {"action": "add", "title": "ملاحظة",
                                                         "content": "{{prev}}"}),
    (("طقس", "الجو", "weather"), "daily_briefing", {"action": "brief", "sections": "weather"}),
    (("تقويم", "مواعيدي", "calendar"), "calendar_events", {"action": "today"}),
    (("صلوات", "صلاة", "prayer"), "prayer_times", {"action": "today"}),
    (("موجز", "briefing"), "daily_briefing", {"action": "brief"}),
    (("صحة النظام", "حالة النظام", "system"), "system_health", {"action": "status"}),
    (("سعر", "أسعار", "price"), "price_monitor", {"action": "list"}),
)


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
    return C.cfg("workflow_engine", PLUGIN_SETTINGS["fields"])


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
    data = C.load_json(C.WORKFLOWS_FILE, {"version": 1, "items": {}})
    if not isinstance(data, dict):
        data = {"version": 1, "items": {}}
    if not isinstance(data.get("items"), dict):
        data["items"] = {}
    return data


def _save(data: dict) -> bool:
    return C.save_json(C.WORKFLOWS_FILE, data)


def _new_id(name: str) -> str:
    stamp = C.now().strftime("%Y%m%d%H%M%S")
    slug = "".join(ch for ch in str(name) if ch.isalnum() or ch in "_")[:12] or "wf"
    return f"wf_{slug}_{stamp}"


def _find(name: str) -> tuple[Optional[dict], Optional[str]]:
    wanted = str(name or "").strip().lower()
    if not wanted:
        return None, "أي سير عمل تقصد؟"
    data = _load()
    if wanted in data["items"]:
        return data["items"][wanted], wanted
    exact = [(k, i) for k, i in data["items"].items() if str(i.get("name", "")).strip().lower() == wanted]
    if len(exact) == 1:
        return exact[0][1], exact[0][0]
    partial = [(k, i) for k, i in data["items"].items() if wanted in str(i.get("name", "")).lower()]
    if len(partial) == 1:
        return partial[0][1], partial[0][0]
    if len(partial) > 1:
        return None, f"أكثر من سير عمل بهذا الاسم: {'، '.join(str(p[1].get('name')) for p in partial)}"
    return None, f"لا سير عمل باسم '{C.clip(name, 40)}'."


def _validate_steps(raw: Any) -> tuple[list, str]:
    if raw in (None, ""):
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
        return [], "steps يجب أن تكون قائمة."
    if len(steps) > _int(_cfg().get("max_steps"), 25, 1, 50):
        return [], "عدد الخطوات يتجاوز السقف."
    clean = []
    for index, step in enumerate(steps, start=1):
        if isinstance(step, str):
            step = {"plugin": step}
        if not isinstance(step, dict):
            return [], f"الخطوة {index} ليست كائنًا."
        target = str(step.get("plugin") or "").strip()
        if not target:
            return [], f"الخطوة {index} بلا اسم إضافة."
        if target in _ADMIN_IN_WORKFLOW:
            return [], f"الخطوة {index}: '{target}' إدارية — لا تُستدعى من داخل سير عمل."
        if C.resolve_plugin(target) is None:
            return [], f"الخطوة {index}: الإضافة '{target}' غير موجودة في plugins."
        entry = {"name": str(step.get("name") or f"step{index}"), "plugin": target,
                 "parameters": step.get("parameters") if isinstance(step.get("parameters"), dict) else {}}
        if step.get("when") is not None:
            entry["when"] = step["when"]
        if step.get("retries") is not None:
            entry["retries"] = _int(step.get("retries"), 0, 0, 5)
        if isinstance(step.get("on_failure"), dict):
            entry["on_failure"] = step["on_failure"]
        clean.append(entry)
    return clean, ""


def _nl_steps(text: str) -> list[dict]:
    import re as _re
    sentence = C.normalize_digits(str(text or ""))
    parts = [p for p in _re.split(r"(?:ثم|وبعدها|بعد كده|after that|then|,|،)", sentence) if p.strip()]
    steps = []
    for part in parts:
        low = part.strip().lower()
        for words, plugin, params in _NL_MAP:
            if any(word in low for word in words):
                steps.append({"plugin": plugin, "parameters": dict(params)})
                break
    return steps


def _guess_vars(text: str) -> dict:
    """يستخرج موضوعًا من الجملة (bين علامتَي اقتباس، أو بعد 'عن'/'about')."""
    import re as _re
    sentence = str(text or "")
    quoted = _re.search(r"[«\"']([^»\"']{2,60})[»\"']", sentence)
    if quoted:
        return {"topic": quoted.group(1).strip()}
    about = _re.search(r"(?:عن|حول|about)\s+([\w\s\u0600-\u06FF]{2,60})", sentence)
    if about:
        topic = about.group(1).strip()
        topic = _re.split(r"\s+(?:ثم|وبعدها|after|then)\b", topic)[0].strip()
        if topic:
            return {"topic": topic}
    return {}


# ── التنفيذ ───────────────────────────────────────────────────────────────────
def _run_item(item: dict, key: str, variables: dict, player=None) -> dict:
    steps = item.get("steps") or []
    if not steps:
        return {"ok": False, "output": "", "error": "سير عمل بلا خطوات.", "steps": []}
    ctx_vars = dict(item.get("vars") or {})
    ctx_vars.update(variables or {})
    started = time.time()
    try:
        outcome = C.run_steps(
            steps,
            {"prev": "", "steps": {}, "vars": ctx_vars},
            owner="workflow_engine", player=player,
            max_steps=_int(_cfg().get("max_steps"), 25, 1, 50),
            step_timeout_s=float(_int(_cfg().get("step_timeout_s"), 240, 5, 3600)),
        )
    except Exception as exc:  # noqa: BLE001
        outcome = {"ok": False, "output": "", "results": [], "failed": None, "error": str(exc)}
    results = outcome.get("results") or []
    return {
        "ok": bool(outcome.get("ok")), "output": str(outcome.get("output") or ""),
        "error": str(outcome.get("error") or ""), "failed": outcome.get("failed"),
        "ms": int((time.time() - started) * 1000),
        "steps": [{"name": r.get("name"), "plugin": r.get("plugin"), "status": r.get("status"),
                   "ms": r.get("ms"), "error": C.clip(r.get("error", ""), 120),
                   "output": C.clip(r.get("output", ""), 200)} for r in results],
    }


def _render_outcome(name: str, outcome: dict) -> str:
    if outcome["ok"]:
        lines = [f"سيدي، سير العمل '{name}' تمّ بالكامل."]
    else:
        lines = [f"سيدي، سير العمل '{name}' تعثّر" +
                 (f" في الخطوة '{outcome['failed']}'" if outcome.get("failed") else "") + "."]
        if outcome.get("error"):
            lines.append(f"  السبب: {C.clip(outcome['error'], 200)}")
        lines.append("  مخرجات الخطوات التي نجحت محفوظة ولم يُفقد شيء، وبقية المساعد لم يتأثر.")
    for step in outcome.get("steps", []):
        mark = {"ok": "✔", "failed": "✗", "skipped": "—", "blocked": "⛔"}.get(str(step.get("status")), "•")
        line = f"  {mark} {step.get('name')} [{step.get('plugin')}] {step.get('status')} ({step.get('ms', 0)}ms)"
        if step.get("error"):
            line += f" — {C.clip(step['error'], 110)}"
        lines.append(line)
    if outcome["ok"] and outcome.get("output"):
        lines.append("  الناتج النهائي: " + C.clip(outcome["output"], 400))
    return "\n".join(lines)


def _execute_and_record(item: dict, key: str, variables: dict, *, reason: str, player=None) -> str:
    outcome = _run_item(item, key, variables, player=player)
    data = _load()
    fresh = data["items"].get(str(key))
    if fresh is None:
        return f"سيدي، سير العمل '{item.get('name')}' اختفى أثناء التنفيذ."
    fresh["last_run"] = C.now_iso()
    fresh["last_status"] = "ok" if outcome["ok"] else "failed"
    fresh["run_count"] = int(fresh.get("run_count", 0)) + 1
    fresh["last_error"] = C.clip(outcome.get("error", "") or "", 200)
    history = fresh.get("history") if isinstance(fresh.get("history"), list) else []
    history.append({"ts": fresh["last_run"], "reason": reason, "status": fresh["last_status"],
                    "failed_step": outcome.get("failed"), "ms": outcome.get("ms", 0),
                    "steps": outcome.get("steps", [])})
    fresh["history"] = history[-_int(_cfg().get("keep_history"), 10, 1, 50):]
    _save(data)
    C.log_event("workflow_engine", f"run:{fresh.get('name')}", fresh["last_status"],
                outcome.get("ms", 0), fresh["last_error"], {"reason": reason, "failed": outcome.get("failed")})
    text = _render_outcome(str(fresh.get("name")), outcome)
    _log(player, text)
    return text


# ── الأفعال ───────────────────────────────────────────────────────────────────
def _create(params: dict, player, nl_text: str = "") -> str:
    name = str(params.get("name") or "").strip()
    steps, why = _validate_steps(params.get("steps"))
    if nl_text and not steps:
        steps = _nl_steps(nl_text)
        if not steps:
            return (f"سيدي، لم أفهم خطوات سير العمل{(': ' + why) if why != 'بلا خطوات' else '.'}\n"
                    f"  أعطني steps كـJSON، أو صُغها: 'ابحث عن X ثم لخّص ثم اكتب تقرير ثم أشعرني'.")
    if not steps:
        return f"سيدي، {why} — أعطني steps كـJSON."
    variables: dict = {}
    if isinstance(params.get("vars"), str) and params["vars"].strip():
        try:
            parsed = json.loads(params["vars"])
            if isinstance(parsed, dict):
                variables = parsed
        except Exception:
            return "سيدي، vars يجب أن تكون JSON مثل {\"topic\":\"كذا\"}."
    elif isinstance(params.get("vars"), dict):
        variables = params["vars"]
    if nl_text:
        for var, value in _guess_vars(nl_text).items():
            variables.setdefault(var, value)
    if not name:
        name = C.clip(nl_text, 60) or ("سلسلة " + " → ".join(str(s["plugin"]) for s in steps[:3]))
    key = _new_id(name)
    item = {"id": key, "name": name.strip() or "سير عمل", "nl": C.clip(nl_text, 300), "enabled": True,
            "steps": steps, "vars": variables, "created": C.now_iso(), "updated": C.now_iso(),
            "last_run": "", "last_status": "", "last_error": "", "run_count": 0, "history": []}
    data = _load()
    data["items"][key] = item
    if not _save(data):
        return "سيدي، تعذّر حفظ سير العمل."
    C.log_event("workflow_engine", "create", "ok", 0, "", {"id": key, "steps": len(steps)})
    lines = [f"سيدي، أنشأت سير العمل '{item['name']}' ({key}).",
             "  السلسلة: " + " ← ".join(str(s["plugin"]) for s in steps)]
    if variables:
        lines.append("  المتغيّرات: " + "، ".join(f"{k}={C.clip(v, 40)}" for k, v in variables.items()))
    lines.append("  للتشغيل: action=run name=" + item["name"])
    out = "\n".join(lines)
    _log(player, out)
    return out


def _act_templates(params: dict, player) -> str:
    del params
    lines = ["سيدي، القوالب الجاهزة (كلها من إضافاتك الموجودة):"]
    for key, tpl in _TEMPLATES.items():
        lines.append(f"  • {key} — {tpl['title']}\n      {tpl['about']}"
                     + (f"\n      متغيّرات: {'، '.join(tpl['vars'])}" if tpl.get("vars") else ""))
    lines.append("  لإنشاء واحد منها: action=use_template name=المفتاح [vars={\"...\":\"...\"}]")
    out = "\n".join(lines)
    _log(player, out)
    return out


def _act_use_template(params: dict, player) -> str:
    key_name = str(params.get("name") or "").strip()
    tpl = _TEMPLATES.get(key_name)
    if tpl is None:
        return f"سيدي، لا قالب باسم '{C.clip(key_name, 40)}'. اعرض القوالب بـ action=templates."
    missing = [p for p in {str(s["plugin"]) for s in tpl["steps"]} if C.resolve_plugin(p) is None]
    if missing:
        return f"سيدي، القالب يحتاج إضافات غير موجودة عندك: {'، '.join(missing)} — لن أنشئه ناقصًا."
    suffix = str(params.get("new_name") or "").strip()
    name = (tpl["title"] + (" — " + suffix if suffix else ""))
    return _create({"name": name, "steps": json.dumps(tpl["steps"], ensure_ascii=False),
                    "vars": params.get("vars") or {}}, player)


def _act_list(params: dict, player) -> str:
    query = str(params.get("query") or "").strip().lower()
    data = _load()
    items = [i for i in data["items"].values() if not query or query in str(i.get("name", "")).lower()]
    if not items:
        return "سيدي، لا سلاسل بعد. القوالب الجاهزة: action=templates"
    lines = [f"سيدي، سلاسل سير العمل ({len(items)}):"]
    for item in sorted(items, key=lambda i: str(i.get("name"))):
        lines.append(f"  • {item.get('name')} [{'مفعّل' if item.get('enabled', True) else 'موقوف'}] — "
                     + " ← ".join(str(s.get("plugin")) for s in (item.get("steps") or []))
                     + (f" · تشغيلات: {item.get('run_count', 0)}" if item.get("run_count") else ""))
    out = "\n".join(lines)
    _log(player, out)
    return out


def _act_show(params: dict, player) -> str:
    item, key = _find(str(params.get("name") or ""))
    if item is None:
        return f"سيدي، {key}"
    lines = [f"سيدي، سير العمل '{item.get('name')}' ({key}):",
             f"  الحالة: {'مفعّل' if item.get('enabled', True) else 'موقوف'} · التشغيلات: {item.get('run_count', 0)}"]
    if item.get("vars"):
        lines.append("  المتغيّرات: " + "، ".join(f"{k}={C.clip(v, 40)}" for k, v in item["vars"].items()))
    lines.append("  الخطوات:")
    for step in item.get("steps") or []:
        lines.append(f"    • {step.get('name')} → {step.get('plugin')} "
                     + C.clip(json.dumps(step.get("parameters") or {}, ensure_ascii=False), 140)
                     + (f" [شرط: {json.dumps(step['when'], ensure_ascii=False)}]" if step.get("when") else "")
                     + (f" [فشل: {json.dumps(step['on_failure'], ensure_ascii=False)}]" if step.get("on_failure") else ""))
    history = item.get("history") or []
    if history:
        lines.append("  آخر التشغيلات:")
        for row in history[-5:][::-1]:
            lines.append(f"    · {row.get('ts')} — {row.get('status')}"
                         + (f" (فشل: {row.get('failed_step')})" if row.get("failed_step") else ""))
    out = "\n".join(lines)
    _log(player, out)
    return out


def _act_run(params: dict, player) -> str:
    item, key = _find(str(params.get("name") or ""))
    if item is None:
        return f"سيدي، {key}"
    if not item.get("enabled", True):
        return f"سيدي، سير العمل '{item.get('name')}' موقوف — فعّله أولًا (action=enable)."
    variables: dict = {}
    if isinstance(params.get("vars"), str) and params["vars"].strip():
        try:
            parsed = json.loads(params["vars"])
            if isinstance(parsed, dict):
                variables = parsed
        except Exception:
            return "سيدي، vars يجب أن تكون JSON."
    elif isinstance(params.get("vars"), dict):
        variables = params["vars"]
    return _execute_and_record(item, str(key), variables, reason="طلب يدوي", player=player)


def _act_toggle(params: dict, player, enabled: bool) -> str:
    item, key = _find(str(params.get("name") or ""))
    if item is None:
        return f"سيدي، {key}"
    data = _load()
    fresh = data["items"].get(str(key))
    if fresh is None:
        return "سيدي، سير العمل اختفى."
    fresh["enabled"] = enabled
    _save(data)
    C.log_event("workflow_engine", "enable" if enabled else "disable", "ok", 0, "", {"id": key})
    out = f"سيدي، {'فعّلت' if enabled else 'أوقفت'} سير العمل '{fresh.get('name')}'."
    _log(player, out)
    return out


def _act_edit(params: dict, player) -> str:
    item, key = _find(str(params.get("name") or ""))
    if item is None:
        return f"سيدي، {key}"
    data = _load()
    fresh = data["items"].get(str(key))
    if fresh is None:
        return "سيدي، سير العمل اختفى."
    changes = []
    if params.get("steps"):
        steps, why = _validate_steps(params.get("steps"))
        if not steps:
            return f"سيدي، {why}"
        fresh["steps"] = steps
        changes.append("الخطوات")
    if params.get("new_name"):
        fresh["name"] = C.clip(params["new_name"], 80)
        changes.append("الاسم")
    if isinstance(params.get("vars"), (str, dict)) and params.get("vars"):
        raw = params["vars"]
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            return "سيدي، vars يجب أن تكون JSON."
        if isinstance(parsed, dict):
            fresh["vars"] = parsed
            changes.append("المتغيّرات")
    if not changes:
        return "سيدي، ماذا أعدّل؟ steps أو vars أو new_name."
    fresh["updated"] = C.now_iso()
    _save(data)
    C.log_event("workflow_engine", "edit", "ok", 0, "", {"id": key, "changed": changes})
    out = f"سيدي، عدّلت {' و'.join(changes)} في '{fresh.get('name')}'."
    _log(player, out)
    return out


def _do_delete(key: str) -> str:
    data = _load()
    item = data["items"].pop(str(key), None)
    if item is None:
        return "سيدي، سير العمل لم يعد موجودًا."
    _save(data)
    C.log_event("workflow_engine", "delete", "ok", 0, "", {"id": key})
    return f"سيدي، حذفت سير العمل '{item.get('name')}'. الإضافات نفسها لم تُمس."


def _act_delete(params: dict, player) -> str:
    item, key = _find(str(params.get("name") or ""))
    if item is None:
        return f"سيدي، {key}"
    chain = " ← ".join(str(s.get("plugin")) for s in (item.get("steps") or []))
    return C.confirm_request(
        key=f"workflow_delete_{key}",
        title=f"حذف سير العمل '{item.get('name')}'؟",
        detail=f"السلسلة: {chain}\nالحذف من workflows.json فقط.",
        run=lambda: _do_delete(str(key)),
        player=player,
    )


def _act_logs(params: dict, player) -> str:
    name = str(params.get("name") or "").strip().lower()
    rows = [r for r in C.tail_log("orchestration", 300) if str(r.get("plugin")) == "workflow_engine"]
    if name:
        rows = [r for r in rows if name in str(r.get("action", "")).lower()]
    if not rows:
        return "سيدي، لا سجلات لسير العمل بعد."
    lines = [f"سيدي، سجل سير العمل ({len(rows)} سطرًا، الأحدث أولًا):"]
    for row in rows[:25]:
        lines.append(f"  · {row.get('ts')} {row.get('action')} — {row.get('status')} ({row.get('duration_ms')}ms)"
                     + (f" — {row.get('error')}" if row.get("error") else ""))
    out = "\n".join(lines)
    _log(player, out)
    return out


def _act_status(params: dict, player) -> str:
    del params
    data = _load()
    items = list(data["items"].values())
    failed = [i for i in items if str(i.get("last_status")) == "failed"]
    lines = [
        "سيدي، حالة محرّك سير العمل:",
        f"  السلاسل: {len(items)} · مفعّلة: {len([i for i in items if i.get('enabled', True)])} "
        f"· ملف: {C.WORKFLOWS_FILE.name}",
        f"  إجمالي التشغيلات: {sum(int(i.get('run_count', 0)) for i in items)}",
    ]
    if failed:
        lines.append(f"  سلاسل آخر تشغيل لها فشل: {'، '.join(str(i.get('name')) for i in failed[:6])}")
    lines.append("  القوالب الجاهزة: " + "، ".join(_TEMPLATES))
    out = "\n".join(lines)
    _log(player, out)
    return out


_HELP = (
    "محرّك سير العمل، سيدي:\n"
    "• templates — القوالب الجاهزة · use_template name=research_report vars={\"topic\":\"...\"}\n"
    "• nl_create text=\"ابحث عن الطاقة الشمسية ثم لخّص ثم اكتب تقرير ثم أشعرني\"\n"
    "• create name=X steps='[{\"plugin\":\"research_assistant\",\"parameters\":{\"topic\":\"{{vars.topic}}\"}}]' vars='{...}'\n"
    "  في أي قيمة: {{prev}} · {{steps.اسم.output}} · {{vars.اسم}} — ولكل خطوة when وon_failure\n"
    "• list · show · run name= vars='{...}' · enable/disable · edit · delete (بتأكيد) · logs · status"
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
        if action in ("create", "أنشئ"):
            return _create(p, player, nl_text=str(p.get("text") or ""))
        if action in ("nl_create", "من_جملة"):
            text = str(p.get("text") or p.get("name") or "").strip()
            if not text:
                return "سيدي، قل لي السلسلة كما تقولها."
            return _create(p, player, nl_text=text)
        if action in ("templates", "قوالب"):
            return _act_templates(p, player)
        if action in ("use_template", "استخدم_قالب"):
            return _act_use_template(p, player)
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
        if action in ("logs", "سجل"):
            return _act_logs(p, player)
        if action in ("status", "حالة"):
            return _act_status(p, player)
        if action in ("help", "?"):
            return _HELP
        return f"سيدي، '{action}' ليس فعلًا أعرفه في سير العمل. قل 'help'."
    except Exception as exc:  # noqa: BLE001
        return f"سيدي، محرّك سير العمل واجه مشكلة: {str(exc)[:180]}"


# ── فحص ذاتي: python workflow_engine.py ───────────────────────────────────────
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
    print("workflow_engine — self-test")
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

    tmp = Path(tempfile.mkdtemp(prefix="wf_test_"))
    real_files = (C.WORKFLOWS_FILE, C.LOG_DIR)
    real_stamp, real_data = C._SCAN_CACHE.get("stamp"), C._SCAN_CACHE.get("data")
    base = C.CONFIG_DIR / "api_keys.json"
    backup = base.read_text(encoding="utf-8") if base.exists() else None
    try:
        C.WORKFLOWS_FILE = tmp / "workflows.json"
        C.LOG_DIR = tmp / "logs"
        C.LOG_DIR.mkdir(parents=True, exist_ok=True)
        C.save_json(C.WORKFLOWS_FILE, {"version": 1, "items": {}})
        real_plugins = C.PLUGINS_DIR
        C.store_settings("workflow_engine", {"step_timeout_s": "120", "max_steps": "25", "keep_history": "10"})

        # 1) سلسلة بتمرير المخرجات
        steps = json.dumps([
            {"name": "أول", "plugin": "habit_tracker", "parameters": {"action": "status"}},
            {"name": "ثاني", "plugin": "summarizer", "parameters": {"text": "{{steps.أول.output}}"}},
        ], ensure_ascii=False)
        out = run({"action": "create", "name": "سلسلة تمرير", "steps": steps})
        check("create يعرض السلسلة", "أنشأت سير العمل" in out and "habit_tracker" in out, out[:220])
        out = run({"action": "run", "name": "سلسلة تمرير"})
        check("run ينفّذ الخطوتين", out.count("✔") == 2, out[:260])
        fresh = list(_load()["items"].values())[0]
        check("التاريخ محفوظ", fresh.get("run_count") == 1 and fresh.get("history"), str(fresh.get("history"))[:100])
        # 2) قالب جاهز (إضافات موجودة فعلًا)
        out = run({"action": "templates"})
        check("templates يعرض القوالب", "research_report" in out and "news_digest" in out, out[:200])
        out = run({"action": "use_template", "name": "morning_pulse", "vars": json.dumps({"city": "المنصورة"}),
                   "new_name": "صباحي"})
        check("use_template ينشئ السلسلة", "أنشأت سير العمل" in out, out[:260])
        check("القالب يحفظ المتغيّرات", "المنصورة" in out or "city" in out, out[:240])
        # 3) شرط يوقف خطوة
        cond_steps = json.dumps([
            {"name": "أول", "plugin": "habit_tracker", "parameters": {"action": "status"}},
            {"name": "مشروط", "plugin": "note_manager", "parameters": {"action": "list"},
             "when": {"left": "{{steps.أول.output}}", "op": "contains", "right": "كلمة_لن_توجد"}},
        ], ensure_ascii=False)
        run({"action": "create", "name": "سلسلة شرط", "steps": cond_steps})
        out = run({"action": "run", "name": "سلسلة شرط"})
        check("الشرط غير المتحقق يُتخطّى", "— مشروط" in out or "skipped" in out, out[:260])
        check("السلسلة تُعدّ ناجحة رغم التخطي", "تمّ بالكامل" in out, out[:200])
        # 4) مسار الفشل: continue يمضي — بمجلد إضافات اختباري فيه خطوة تنفجر فعلًا
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
        fail_steps = json.dumps([
            {"name": "معطوبة", "plugin": "boom_step", "parameters": {},
             "on_failure": {"action": "continue"}},
            {"name": "تالية", "plugin": "ok_step", "parameters": {}},
        ], ensure_ascii=False)
        run({"action": "create", "name": "سلسلة فشل", "steps": fail_steps})
        out = run({"action": "run", "name": "سلسلة فشل"})
        check("on_failure=continue يمضي للخطوة التالية", "✔ تالية" in out, out[:300])
        C.PLUGINS_DIR = real_plugins
        C.scan_plugins(force=True)
        check("الفشل مُعلَن في الشرح", "تعثّر" in out or "فشلت" in out or "✗" in out, out[:300])
        # 5) الحمايات
        out = run({"action": "create", "name": "إدارية", "steps": json.dumps(
            [{"plugin": "remote_api", "parameters": {}}])})
        check("إضافة إدارية مرفوضة", "إدارية" in out, out[:200])
        out = run({"action": "create", "name": "وهمية", "steps": json.dumps([{"plugin": "لا_توجد"}])})
        check("إضافة غير موجودة مرفوضة", "غير موجودة" in out, out[:200])
        out = run({"action": "create", "name": "بلا خطوات"})
        check("بلا خطوات → رسالة", "بلا خطوات" in out or "أعطني steps" in out, out[:200])
        # 6) الإدارة
        out = run({"action": "list"})
        check("list يعرض الكل", "سلسلة تمرير" in out and "سلسلة شرط" in out, out[:240])
        out = run({"action": "show", "name": "سلسلة شرط"})
        check("show يعرض الشرط", "شرط" in out and "when" in out or "الخطوات" in out, out[:240])
        out = run({"action": "disable", "name": "سلسلة شرط"})
        check("disable يعمل", "أوقفت" in out, out[:140])
        out = run({"action": "run", "name": "سلسلة شرط"})
        check("الموقوف لا يُشغَّل", "موقوف" in out, out[:140])
        out = run({"action": "enable", "name": "سلسلة شرط"})
        check("enable يعمل", "فعّلت" in out, out[:140])
        out = run({"action": "edit", "name": "سلسلة شرط", "new_name": "سلسلة معدّلة"})
        check("edit يعمل", "سلسلة معدّلة" in out, out[:160])
        before = len(_load()["items"])
        out = run({"action": "delete", "name": "سلسلة معدّلة"})
        check("الحذف يستأذن", len(_load()["items"]) == before, out[:160])
        item, key = _find("سلسلة معدّلة")
        out = _do_delete(str(key))
        check("الحذف بعد الموافقة", len(_load()["items"]) == before - 1, out[:100])
        # 7) nl_create
        out = run({"action": "nl_create", "text": "ابحث عن الطاقة الشمسية ثم لخّص ثم اكتب تقرير ثم أشعرني"})
        check("nl_create يبني السلسلة", "أنشأت سير العمل" in out and "research_assistant" in out, out[:280])
        check("nl_create يستخرج الموضوع", "الطاقة الشمسية" in out, out[:280])
        # 8) الإحصاء والسجل والمساعدة
        out = run({"action": "status"})
        check("status يعمل", "السلاسل:" in out, out[:160])
        out = run({"action": "logs"})
        check("logs يعمل", "سجل سير العمل" in out or "لا سجلات" in out, out[:160])
        check("help يعمل", "محرّك سير العمل" in run({"action": "help"}))
        check("فعل مجهول", "ليس فعلًا" in run({"action": "zzz"}))
    finally:
        C.PLUGINS_DIR = real_plugins if "real_plugins" in locals() else C.PLUGINS_DIR
        C.WORKFLOWS_FILE, C.LOG_DIR = real_files
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
