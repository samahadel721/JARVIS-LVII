"""
workflow_scheduler.py — مجدول سير العمل وسلاسل المهام.

نظام توقيت واحد يدير كل شيء: سير عمل، أتمتة، أو إضافة مباشرة — مرة واحدة أو متكرّرًا
(يوميًا / أسبوعيًا / كل N دقيقة)، مع تفعيل وتعطيل، والموعد القادم، والتاريخ، ومعالجة
الفشل بإعادة محاولة قابلة للضبط، ومنع تشغيل نفس الجدولة مرّتين في وقت واحد.

نبضة واحدة كل 20 ثانية (قابلة للضبط) تنادي:
  1) الجدولات المستحقّة هنا، 2) محرّك الأتمتة (automation_engine check)
  ، 3) تذكيرات التقويم (calendar_events reminders) — كلٌّ بحماية مستقلة، فلا يُسقط
فشلُ أحدها الآخر. إن كانت إضافة غير موجودة يُذكر ذلك بصراحة ولا ينهار شيء.

المثال المطلوب حرفيًا: "كل صباح ٨: طقس ← تقويم ← تذكيرات ← أخبار ← موجز ← إشعار"
يُبنى بجدولة واحدة هدفها سير العمل 'full_briefing' (أو أتمتة بنفس الخطوات).
"""
from __future__ import annotations

import datetime as _dt
import importlib.util
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

PLUGIN = {
    "name": "workflow_scheduler",
    "description": (
        "The assistant's single timer: schedules workflows, automations or a single plugin to run "
        "once or on a repeating schedule (daily, weekly, every N minutes), with enable/disable, "
        "next-run time, run history, configurable retry on failure, and protection against running "
        "the same job twice at once. Use for: 'run the briefing workflow every morning at 8', "
        "'what is scheduled', 'when is the next run', 'pause the storage check', 'run that schedule "
        "now', 'delete that schedule' (asks first), 'start/stop the scheduler', 'is the scheduler on'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": ("create | list | show | run_now | enable | disable | delete | start | "
                                "stop | tick | status | history | help"),
            },
            "name": {"type": "STRING", "description": "Schedule name (or id) for show/run_now/enable/disable/delete."},
            "target_kind": {"type": "STRING", "description": "workflow | automation | plugin"},
            "target_name": {"type": "STRING", "description": "Name of the workflow / automation / plugin to run."},
            "parameters": {"type": "STRING", "description": "JSON parameters when the target is a single plugin."},
            "when": {"type": "STRING", "description": "Time phrase: 'every day at 8 am' / 'كل ٣٠ دقيقة'."},
            "schedule": {"type": "STRING", "description": "Schedule as JSON: {\"kind\":\"weekly\",\"days\":[6],\"time\":\"09:00\"}."},
            "retry": {"type": "STRING", "description": "Retry as JSON: {\"tries\":2,\"delay_min\":5}."},
            "interval_s": {"type": "STRING", "description": "For start: heartbeat interval in seconds (default 20)."},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "workflow_scheduler",
    "title": "Workflow scheduler / مجدول المهام",
    "fields": [
        {"key": "tick_interval_s", "type": "text", "label": "Heartbeat interval, seconds", "default": "20"},
        {"key": "run_timeout_s", "type": "text", "label": "Seconds allowed for one run", "default": "600"},
        {"key": "catch_up", "type": "text", "label": "Run jobs missed while the assistant was off (yes/no)", "default": "no"},
        {"key": "watch_automations", "type": "text", "label": "Automations run from this heartbeat (yes/no)", "default": "yes"},
        {"key": "watch_reminders", "type": "text", "label": "Calendar reminders fire from this heartbeat (yes/no)", "default": "yes"},
        {"key": "retry_tries", "type": "text", "label": "Default retry attempts", "default": "2"},
        {"key": "retry_delay_min", "type": "text", "label": "Default minutes between retries", "default": "5"},
    ],
}

_TICKER: dict = {"thread": None, "stop": None, "interval": 20}
_RUNNING: set[str] = set()
_LAST: dict = {}


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
    return C.cfg("workflow_scheduler", PLUGIN_SETTINGS["fields"])


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
    data = C.load_json(C.SCHEDULES_FILE, {"version": 1, "items": {}})
    if not isinstance(data, dict):
        data = {"version": 1, "items": {}}
    if not isinstance(data.get("items"), dict):
        data["items"] = {}
    return data


def _save(data: dict) -> bool:
    return C.save_json(C.SCHEDULES_FILE, data)


def _new_id(name: str) -> str:
    stamp = C.now().strftime("%Y%m%d%H%M%S")
    slug = "".join(ch for ch in str(name) if ch.isalnum() or ch in "_")[:12] or "job"
    return f"job_{slug}_{stamp}"


def _find(name: str) -> tuple[Optional[dict], Optional[str]]:
    wanted = str(name or "").strip().lower()
    if not wanted:
        return None, "أي جدولة تقصد؟"
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
        return None, f"أكثر من جدولة بهذا الاسم: {'، '.join(str(p[1].get('name')) for p in partial)}"
    return None, f"لا جدولة باسم '{C.clip(name, 40)}'."


def _parse_schedule(raw: Any) -> tuple[Optional[dict], str]:
    if isinstance(raw, dict):
        return (raw, "") if raw else (None, "schedule فارغة.")
    text = str(raw or "").strip()
    if not text:
        return None, "بلا توقيت."
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
        except Exception as exc:
            return None, f"تعذّرت قراءة schedule: {C.clip(exc, 100)}"
        if not isinstance(parsed, dict) or not parsed:
            return None, "schedule يجب أن تكون كائن JSON غير فارغ."
        return parsed, ""
    parsed = C.parse_when(text)
    if parsed is None:
        return None, (f"لم أفهم التوقيت '{C.clip(text, 40)}'. قل مثلًا: 'كل يوم ٨ صباحًا'، "
                      f"'كل ٣٠ دقيقة'، 'أسبوعيًا الأحد ٩ مساءً'.")
    return parsed, ""


def _target(state: dict) -> tuple[Optional[tuple[str, str, dict]], str]:
    """يحوّل الهدف إلى نداء قابل للتنفيذ — ويرفض ما لا وجود له بصراحة."""
    kind = str(state.get("kind") or "").strip().lower()
    name = str(state.get("name") or "").strip()
    parameters = state.get("parameters") if isinstance(state.get("parameters"), dict) else {}
    if kind == "plugin":
        if C.resolve_plugin(name) is None:
            return None, f"الإضافة '{name}' غير موجودة في plugins."
        if name == "workflow_scheduler":
            return None, "لا أجدول نفسي (منعًا للّفّ)."
        return ("plugin", name, parameters), ""
    if kind == "workflow":
        data = C.load_json(C.WORKFLOWS_FILE, {"items": {}})
        items = data.get("items", {}) if isinstance(data, dict) else {}
        if not any(str(i.get("name", "")).strip().lower() == name.lower() for i in items.values()):
            return None, f"لا سير عمل باسم '{name}' (أنشئه أولًا في workflow_engine)."
        return ("workflow", name, parameters), ""
    if kind == "automation":
        data = C.load_json(C.AUTOMATIONS_FILE, {"items": {}})
        items = data.get("items", {}) if isinstance(data, dict) else {}
        if not any(str(i.get("name", "")).strip().lower() == name.lower() for i in items.values()):
            return None, f"لا أتمتة باسم '{name}' (أنشئها أولًا في automation_engine)."
        return ("automation", name, parameters), ""
    return None, "target_kind يجب أن يكون workflow أو automation أو plugin."


# ── التنفيذ ───────────────────────────────────────────────────────────────────
def _invoke(target: tuple[str, str, dict], player=None) -> tuple[bool, str]:
    kind, name, parameters = target
    if kind == "plugin":
        ok, value, timed_out = C.run_with_timeout(
            lambda: C.call_plugin(name, parameters, player=player),
            float(_int(_cfg().get("run_timeout_s"), 600, 5, 7200)))
        if timed_out:
            return False, f"تجاوزت المهلة ({_cfg().get('run_timeout_s')}s)"
        return bool(ok), str(value)
    module = "workflow_engine" if kind == "workflow" else "automation_engine"
    if C.resolve_plugin(module) is None:
        return False, f"'{module}' غير موجود — لا أستطيع تنفيذ {kind}."
    ok, value, timed_out = C.run_with_timeout(
        lambda: C.call_plugin(module, {"action": "run", "name": name, **parameters}, player=player),
        float(_int(_cfg().get("run_timeout_s"), 600, 5, 7200)))
    if timed_out:
        return False, f"تجاوزت المهلة ({_cfg().get('run_timeout_s')}s)"
    return bool(ok), str(value)


def _execute(item: dict, key: str, *, reason: str, player=None) -> dict:
    """ينفّذ جدولة واحدة — بمنع تداخل حقيقي (ذاكرة + ملف) وبتحديث فوري للملف."""
    if key in _RUNNING:
        return {"ok": False, "output": "تعمل الآن بالفعل — لن أُشغّلها مرّتين.", "skipped": True}
    target, why = _target(item.get("target") or {})
    if target is None:
        return {"ok": False, "output": why}
    data = _load()
    live = data["items"].get(str(key))
    if live is not None:
        if live.get("running"):
            return {"ok": False, "output": "الجدولة مُعلَّمة كقيد التشغيل — تجاهلتها.", "skipped": True}
        live["running"] = True
        _save(data)                      # يُحفظ فورًا، لا في نهاية الدفعة (تفاديًا لفقدان التحديث)
    _RUNNING.add(key)
    started = time.time()
    try:
        ok, output = _invoke(target, player)
    except Exception as exc:  # noqa: BLE001 — المجدول لا يُسقط المساعد
        ok, output = False, str(exc)
    finally:
        _RUNNING.discard(key)
    ms = int((time.time() - started) * 1000)

    data = _load()
    live = data["items"].get(str(key))
    if live is None:
        return {"ok": bool(ok), "output": str(output), "ms": ms}
    live["running"] = False
    live["last_run"] = C.now_iso()
    live["last_status"] = "ok" if ok else "failed"
    live["last_output"] = C.clip(output, 300)
    live["run_count"] = int(live.get("run_count", 0)) + 1
    live["consecutive_failures"] = 0 if ok else int(live.get("consecutive_failures", 0)) + 1
    retry = live.get("retry") if isinstance(live.get("retry"), dict) else {}
    tries_used = int(live.get("retry_used", 0))
    if not ok and retry and tries_used < _int(retry.get("tries"), 0, 0, 10):
        delay = _int(retry.get("delay_min"), 5, 1, 1440)
        live["retry_used"] = tries_used + 1
        live["retry_pending"] = True
        live["next_run"] = C.to_iso(C.now() + _dt.timedelta(minutes=delay))
    else:
        live["retry_used"] = 0
        live["retry_pending"] = False
        schedule = live.get("schedule") or {}
        if str(schedule.get("kind")) == "once":
            live["enabled"] = False
            live["next_run"] = ""
        else:
            nxt = C.next_run(schedule, after=C.now())
            live["next_run"] = C.to_iso(nxt) if nxt else ""
    history = live.get("history") if isinstance(live.get("history"), list) else []
    history.append({"ts": live["last_run"], "reason": reason, "status": live["last_status"], "ms": ms,
                    "output": C.clip(output, 160)})
    live["history"] = history[-15:]
    _save(data)
    C.log_event("workflow_scheduler", f"run:{live.get('name')}", live["last_status"], ms,
                "" if ok else C.clip(output, 200),
                {"target": f"{target[0]}:{target[1]}", "reason": reason})
    out = (f"سيدي، جدولة '{live.get('name')}': {'تمّ' if ok else 'فشلت'} تنفيذ "
           f"{target[0]} '{target[1]}' في {ms}ms" + ("" if ok else f" — {C.clip(output, 180)}"))
    if not ok and live.get("retry_pending"):
        out += f"\n  سأعيد المحاولة بعد {retry.get('delay_min')} دقيقة (المحاولة {live.get('retry_used')})."
    _log(player, out)
    return {"ok": bool(ok), "output": str(output), "ms": ms, "text": out}


# ── النبضة ────────────────────────────────────────────────────────────────────
def _tick_once(player=None, *, dry: bool = False, now_text: str = "") -> str:
    """ينفّذ كل ما حان وقته. dry=yes يعرض ما سيُنفَّذ بلا تنفيذ."""
    if now_text:
        moment = C.from_iso(now_text) or C.now()
    else:
        moment = C.now()
    data = _load()
    ran, waiting, missed = [], [], []
    catch_up = C.parse_bool(_cfg().get("catch_up"), False)
    for key, item in list(data["items"].items()):
        if not item.get("enabled", True):
            continue
        if item.get("running"):
            waiting.append(str(item.get("name")))
            continue
        nxt = C.from_iso(item.get("next_run"))
        schedule = item.get("schedule") or {}
        if nxt is None and str(schedule.get("kind")) != "once":
            nxt = C.next_run(schedule, after=moment)
            fresh = _load()
            if key in fresh["items"]:
                fresh["items"][key]["next_run"] = C.to_iso(nxt) if nxt else ""
                _save(fresh)
        if nxt is None or nxt > moment:
            continue
        overdue_min = (moment - nxt).total_seconds() / 60.0
        slack = _int(schedule.get("minutes"), 1, 1, 10080) if str(schedule.get("kind")) == "interval" else 60
        if overdue_min > max(120, slack * 3) and not catch_up and not item.get("retry_pending"):
            missed.append(f"{item.get('name')} (فائتة بمقدار {int(overdue_min)} دقيقة)")
            fresh = _load()
            live = fresh["items"].get(key)
            if live is not None:
                nxt2 = C.next_run(schedule, after=moment)
                live["next_run"] = C.to_iso(nxt2) if nxt2 else ""
                live["missed"] = int(live.get("missed", 0)) + 1
                _save(fresh)
                C.log_event("workflow_scheduler", f"missed:{live.get('name')}", "missed", 0,
                            f"فائتة {int(overdue_min)} دقيقة")
            continue
        if dry:
            ran.append(f"{item.get('name')} — كان سيُنفَّذ الآن ({C.describe_schedule(schedule)})")
            continue
        outcome = _execute(item, key, reason="حان الموعد", player=player)
        line = str(outcome.get("text") or outcome.get("output") or "").replace("\n", " · ")
        ran.append(C.clip(line, 200))
    lines = []
    if dry:
        lines.append(f"سيدي، فحص جاف: {len(ran)} جدولة حان وقتها."
                     if ran else "سيدي، فحص جاف: لا جدولة حان وقتها الآن.")
        lines.extend("  • " + row for row in ran)
    else:
        lines.append(f"سيدي، نفّذت {len(ran)} جدولة." if ran else "سيدي، لا جدولة حان وقتها.")
        lines.extend("  • " + row for row in ran)
    if waiting:
        lines.append(f"  (تُركت {len(waiting)} لأنها تعمل الآن: {'، '.join(waiting)})")
    if missed:
        lines.append(f"  (فائتة أثناء إغلاق المساعد: {'، '.join(missed)} — لن أُشغّلها متأخّرة إلا إذا طلبت)")
    _log(player, "\n".join(lines))
    return "\n".join(lines)


def _heartbeat() -> None:
    """يربط المجدول بالأتمتة والتذكيرات — كلٌّ بحماية مستقلة."""
    try:
        if C.parse_bool(_cfg().get("watch_automations"), True) and C.resolve_plugin("automation_engine"):
            C.call_plugin("automation_engine", {"action": "check"})
    except Exception as exc:  # noqa: BLE001
        C.log_event("workflow_scheduler", "tick:automations", "failed", 0, C.clip(exc, 150))
    try:
        if C.parse_bool(_cfg().get("watch_reminders"), True) and C.resolve_plugin("calendar_events"):
            C.call_plugin("calendar_events", {"action": "reminders", "fire": "yes"})
    except Exception as exc:  # noqa: BLE001
        C.log_event("workflow_scheduler", "tick:reminders", "failed", 0, C.clip(exc, 150))


def _loop(interval_s: int) -> None:
    stop = _TICKER.get("stop")
    while not (stop is not None and stop.is_set()):
        try:
            _tick_once(None)
            _heartbeat()
        except Exception as exc:  # noqa: BLE001
            C.log_event("workflow_scheduler", "tick", "failed", 0, C.clip(exc, 200))
        for _ in range(int(max(1, interval_s))):
            if stop is not None and stop.is_set():
                break
            time.sleep(1.0)


# ── الأفعال ───────────────────────────────────────────────────────────────────
def _act_create(params: dict, player) -> str:
    name = str(params.get("name") or "").strip()
    kind = str(params.get("target_kind") or "").strip().lower()
    target_name = str(params.get("target_name") or params.get("plugin") or "").strip()
    if kind not in ("plugin", "workflow", "automation"):
        return "سيدي، target_kind يجب أن يكون workflow أو automation أو plugin."
    if not target_name:
        return "سيدي، ما الذي أجدوله؟ أعطني target_name."
    parameters: dict = {}
    raw_params = params.get("parameters")
    if isinstance(raw_params, str) and raw_params.strip():
        try:
            parsed = json.loads(raw_params)
            if isinstance(parsed, dict):
                parameters = parsed
        except Exception:
            return "سيدي، parameters يجب أن تكون JSON."
    elif isinstance(raw_params, dict):
        parameters = raw_params
    target_state = {"kind": kind, "name": target_name, "parameters": parameters}
    target, why = _target(target_state)
    if target is None:
        return f"سيدي، {why}"
    schedule, why = _parse_schedule(params.get("schedule") or params.get("when"))
    if schedule is None:
        return f"سيدي، {why}"
    kind_of_schedule = str(schedule.get("kind"))
    if kind_of_schedule not in ("daily", "weekly", "interval", "once", "manual"):
        return f"سيدي، نوع التوقيت '{kind_of_schedule}' غير مدعوم في المجدول."
    retry = {"tries": _int(_cfg().get("retry_tries"), 2, 0, 10),
             "delay_min": _int(_cfg().get("retry_delay_min"), 5, 1, 1440)}
    raw_retry = params.get("retry")
    if isinstance(raw_retry, str) and raw_retry.strip():
        try:
            parsed = json.loads(raw_retry)
            if isinstance(parsed, dict):
                retry = {"tries": _int(parsed.get("tries"), retry["tries"], 0, 10),
                         "delay_min": _int(parsed.get("delay_min"), retry["delay_min"], 1, 1440)}
        except Exception:
            return "سيدي، retry يجب أن تكون JSON."
    elif isinstance(raw_retry, dict):
        retry = {"tries": _int(raw_retry.get("tries"), retry["tries"], 0, 10),
                 "delay_min": _int(raw_retry.get("delay_min"), retry["delay_min"], 1, 1440)}
    if not name:
        name = f"{kind}:{target_name}"
    key = _new_id(name)
    nxt = C.next_run(schedule, after=C.now())
    item = {"id": key, "name": name.strip(), "enabled": True, "target": target_state, "schedule": schedule,
            "retry": retry, "retry_used": 0, "retry_pending": False, "running": False,
            "next_run": C.to_iso(nxt) if nxt else "", "last_run": "", "last_status": "", "last_output": "",
            "run_count": 0, "missed": 0, "consecutive_failures": 0, "created": C.now_iso(), "history": []}
    data = _load()
    data["items"][key] = item
    if not _save(data):
        return "سيدي، تعذّر حفظ الجدولة."
    C.log_event("workflow_scheduler", "create", "ok", 0, "", {"id": key, "target": f"{kind}:{target_name}"})
    lines = [f"سيدي، جدولت {kind} '{target_name}' باسم '{item['name']}' ({key}).",
             f"  التوقيت: {C.describe_schedule(schedule)} · القادم: {item['next_run'] or '—'}",
             f"  عند الفشل: {'إعادة محاولة ' + str(retry['tries']) + ' بعد ' + str(retry['delay_min']) + ' دقيقة' if retry['tries'] else 'تُسجَّل بلا إعادة'}"
             ]
    if _TICKER.get("thread") and _TICKER["thread"].is_alive():
        lines.append("  المجدول يعمل الآن، فستُنفَّذ تلقائيًا.")
    else:
        lines.append("  المجدول متوقف — شغّله بـ action=start لتُنفَّذ تلقائيًا.")
    out = "\n".join(lines)
    _log(player, out)
    return out


def _act_list(params: dict, player) -> str:
    del params
    data = _load()
    items = list(data["items"].values())
    if not items:
        return "سيدي، لا جدولات بعد. أنشئ واحدة: action=create target_kind=plugin target_name=... when='كل يوم 8 صباحًا'"
    lines = [f"سيدي، جدولاتك ({len(items)}):"]
    for item in sorted(items, key=lambda i: str(i.get("next_run") or "zzzz")):
        target = item.get("target") or {}
        lines.append(f"  • {item.get('name')} [{'مفعّلة' if item.get('enabled', True) else 'موقوفة'}] — "
                     f"{target.get('kind')}:{target.get('name')} · {C.describe_schedule(item.get('schedule'))}"
                     f" · القادم: {item.get('next_run') or '—'}"
                     + (f" · آخر حالة: {item.get('last_status')}" if item.get("last_status") else "")
                     + (f" · فشل متتالٍ: {item.get('consecutive_failures')}" if item.get("consecutive_failures") else "")
                     + (f" · فائتة: {item.get('missed')}" if item.get("missed") else ""))
    out = "\n".join(lines)
    _log(player, out)
    return out


def _act_show(params: dict, player) -> str:
    item, key = _find(str(params.get("name") or ""))
    if item is None:
        return f"سيدي، {key}"
    target = item.get("target") or {}
    retry = item.get("retry") or {}
    lines = [f"سيدي، الجدولة '{item.get('name')}' ({key}):",
             f"  الهدف: {target.get('kind')}:{target.get('name')}"
             + (f" · معاملات: {C.clip(json.dumps(target.get('parameters') or {}, ensure_ascii=False), 120)}"
                if target.get("parameters") else ""),
             f"  التوقيت: {C.describe_schedule(item.get('schedule'))} · القادم: {item.get('next_run') or '—'}",
             f"  الحالة: {'مفعّلة' if item.get('enabled', True) else 'موقوفة'}"
             f" · تشغيلات: {item.get('run_count', 0)} · فشل متتالٍ: {item.get('consecutive_failures', 0)}",
             f"  عند الفشل: {'إعادة ' + str(retry.get('tries')) + ' مرة بعد ' + str(retry.get('delay_min')) + ' دقيقة' if retry.get('tries') else 'تسجيل فقط'}"
             + (" · إعادة معلّقة الآن" if item.get("retry_pending") else "")]
    for row in (item.get("history") or [])[-5:][::-1]:
        lines.append(f"    · {row.get('ts')} — {row.get('status')} ({row.get('ms')}ms)"
                     + (f" — {C.clip(row.get('output'), 90)}" if row.get("output") else ""))
    out = "\n".join(lines)
    _log(player, out)
    return out


def _act_run_now(params: dict, player) -> str:
    item, key = _find(str(params.get("name") or ""))
    if item is None:
        return f"سيدي، {key}"
    outcome = _execute(item, str(key), reason="طلب يدوي", player=player)
    return str(outcome.get("text") or outcome.get("output") or "تمّ.")


def _act_toggle(params: dict, player, enabled: bool) -> str:
    item, key = _find(str(params.get("name") or ""))
    if item is None:
        return f"سيدي، {key}"
    data = _load()
    fresh = data["items"].get(str(key))
    if fresh is None:
        return "سيدي، الجدولة اختفت."
    fresh["enabled"] = enabled
    if enabled:
        nxt = C.next_run(fresh.get("schedule") or {}, after=C.now())
        fresh["next_run"] = C.to_iso(nxt) if nxt else ""
        fresh["retry_used"] = 0
        fresh["retry_pending"] = False
    _save(data)
    C.log_event("workflow_scheduler", "enable" if enabled else "disable", "ok", 0, "", {"id": key})
    out = f"سيدي، {'فعّلت' if enabled else 'أوقفت'} الجدولة '{fresh.get('name')}'."
    if enabled and fresh.get("next_run"):
        out += f" · القادم: {fresh['next_run']}"
    _log(player, out)
    return out


def _do_delete(key: str) -> str:
    data = _load()
    item = data["items"].pop(str(key), None)
    if item is None:
        return "سيدي، الجدولة لم تعد موجودة."
    _save(data)
    C.log_event("workflow_scheduler", "delete", "ok", 0, "", {"id": key})
    return f"سيدي، حذفت الجدولة '{item.get('name')}'. الهدف نفسه لم يُمس."


def _act_delete(params: dict, player) -> str:
    item, key = _find(str(params.get("name") or ""))
    if item is None:
        return f"سيدي، {key}"
    target = item.get("target") or {}
    return C.confirm_request(
        key=f"scheduler_delete_{key}",
        title=f"حذف الجدولة '{item.get('name')}'؟",
        detail=(f"الهدف: {target.get('kind')}:{target.get('name')}\n"
                f"التوقيت: {C.describe_schedule(item.get('schedule'))}\n"
                f"الحذف من schedules.json فقط — لا يحذف سير العمل ولا الأتمتة."),
        run=lambda: _do_delete(str(key)),
        player=player,
    )


def _act_start(params: dict, player) -> str:
    interval = _int(params.get("interval_s") or _cfg().get("tick_interval_s"), 20, 5, 3600)
    if _TICKER.get("thread") and _TICKER["thread"].is_alive():
        return f"سيدي، المجدول يعمل بالفعل (كل {_TICKER.get('interval')} ثانية)."
    # تنظيف علامات التشغيل القديمة من عملية سابقة (لئلا تبقى جدولة معلَّقة إلى الأبد)
    data = _load()
    stale = 0
    for item in data["items"].values():
        if item.get("running"):
            item["running"] = False
            stale += 1
    if stale:
        _save(data)
    _TICKER["stop"] = threading.Event()
    _TICKER["interval"] = interval
    _TICKER["thread"] = threading.Thread(target=_loop, args=(interval,), daemon=True, name="jarvis-scheduler")
    _TICKER["thread"].start()
    C.log_event("workflow_scheduler", "start", "ok", 0, "", {"interval_s": interval, "stale_cleared": stale})
    out = (f"سيدي، شغّلت المجدول: نبضة كل {interval} ثانية.\n"
           f"  يشغّل الجدولات المستحقّة + الأتمتة (automation_engine) + تذكيرات التقويم.\n"
           f"  يتوقف مع إغلاق المساعد (خيط خفي) — وهذا طبيعي، أو أوقفه بـ action=stop."
           + (f"\n  (نظّفت {stale} علامة تشغيل قديمة)" if stale else ""))
    _log(player, out)
    return out


def _act_stop(params: dict, player) -> str:
    del params
    stop = _TICKER.get("stop")
    thread = _TICKER.get("thread")
    if not (thread and thread.is_alive()):
        _TICKER["thread"] = None
        return "سيدي، المجدول متوقف بالفعل."
    if stop is not None:
        stop.set()
    thread.join(timeout=3.0)
    alive = thread.is_alive()
    _TICKER["thread"] = None
    C.log_event("workflow_scheduler", "stop", "ok", 0, "", {"stopped": not alive})
    out = "سيدي، أوقفت المجدول." if not alive else "سيدي، أشرت له بالتوقف وسيتوقف بعد النبضة الحالية."
    _log(player, out)
    return out


def _act_status(params: dict, player) -> str:
    del params
    data = _load()
    items = list(data["items"].values())
    thread = _TICKER.get("thread")
    running = bool(thread and thread.is_alive())
    enabled = [i for i in items if i.get("enabled", True)]
    upcoming = sorted([i for i in enabled if i.get("next_run")], key=lambda i: str(i.get("next_run")))
    lines = [
        "سيدي، حالة المجدول:",
        f"  المجدول: {'يعمل — نبضة كل ' + str(_TICKER.get('interval')) + ' ثانية' if running else 'متوقف'}"
        f" · تنفيذات جارية: {len(_RUNNING)}",
        f"  الجدولات: {len(items)} · مفعّلة: {len(enabled)} · ملف: {C.SCHEDULES_FILE.name}",
        "  يتابع أيضًا: "
        + ("الأتمتة " if C.parse_bool(_cfg().get("watch_automations"), True) else "")
        + ("تذكيرات التقويم" if C.parse_bool(_cfg().get("watch_reminders"), True) else "")
        + ("" if (C.parse_bool(_cfg().get("watch_automations"), True)
                 or C.parse_bool(_cfg().get("watch_reminders"), True)) else "لا شيء غير الجدولات"),
        f"  catch_up (تشغيل الفائت): {'نعم' if C.parse_bool(_cfg().get('catch_up'), False) else 'لا'}",
    ]
    if upcoming:
        lines.append("  الأقرب:")
        for item in upcoming[:5]:
            lines.append(f"    · {item.get('name')} — {item.get('next_run')}")
    failing = [i for i in items if int(i.get("consecutive_failures", 0)) > 0]
    if failing:
        lines.append("  جدولات تفشل: " + "، ".join(f"{i.get('name')} ({i.get('consecutive_failures')})"
                                               for i in failing[:6]))
    out = "\n".join(lines)
    _log(player, out)
    return out


def _act_history(params: dict, player) -> str:
    item, key = _find(str(params.get("name") or ""))
    if item is None:
        return f"سيدي، {key}"
    rows = item.get("history") or []
    if not rows:
        return f"سيدي، لا تاريخ للجدولة '{item.get('name')}' بعد."
    lines = [f"سيدي، تاريخ '{item.get('name')}' ({len(rows)} سطرًا):"]
    for row in rows[::-1]:
        lines.append(f"  · {row.get('ts')} — {row.get('status')} ({row.get('ms')}ms) · {row.get('reason')}"
                     + (f" — {C.clip(row.get('output'), 100)}" if row.get("output") else ""))
    out = "\n".join(lines)
    _log(player, out)
    return out


_HELP = (
    "المجدول، سيدي:\n"
    "• create target_kind=workflow|automation|plugin target_name=الاسم when='كل يوم 8 صباحًا'\n"
    "   مثال الموجز الصباحي: target_kind=workflow target_name='الموجز الصباحي الكامل — ' when='كل يوم 8 صباحًا'\n"
    "   retry='{\"tries\":2,\"delay_min\":5}' · parameters='{\"action\":\"status\"}' للهدف المفرد\n"
    "• list · show name= · run_now name= · enable/disable name= · delete (بتأكيد) · history name=\n"
    "• start [interval_s=20] · stop · tick (نبضة واحدة فورًا) · status\n"
    "ينفّذ الفائت؟ catch_up في الإعدادات · ومنع التشغيل المتزامن مضمون لكل جدولة."
)


def run(parameters: dict, player=None, session_memory=None) -> str:
    """نقطة الدخول. لا ترمي أبدًا."""
    del session_memory
    if C is None:
        return "سيدي، ملف '_orch_core.py' المشترك غير موجود — بدونه لا يعمل المجدول."
    try:
        p = dict(parameters or {})
        action = str(p.get("action") or "status").strip().lower()
        C.ensure_dirs()
        if action in ("create", "schedule", "جدول"):
            return _act_create(p, player)
        if action in ("list", "قائمة"):
            return _act_list(p, player)
        if action in ("show", "info", "تفاصيل"):
            return _act_show(p, player)
        if action in ("run_now", "run", "شغّل_الآن"):
            return _act_run_now(p, player)
        if action in ("enable", "فعّل"):
            return _act_toggle(p, player, True)
        if action in ("disable", "عطّل", "أوقف"):
            return _act_toggle(p, player, False)
        if action in ("delete", "remove", "احذف"):
            return _act_delete(p, player)
        if action in ("start", "شغّل_المجدول"):
            return _act_start(p, player)
        if action in ("stop", "أوقف_المجدول"):
            return _act_stop(p, player)
        if action in ("tick", "نبضة"):
            return _tick_once(player, dry=C.parse_bool(p.get("dry"), False), now_text=str(p.get("at") or ""))
        if action in ("status", "حالة"):
            return _act_status(p, player)
        if action in ("history", "تاريخ"):
            return _act_history(p, player)
        if action in ("help", "?"):
            return _HELP
        return f"سيدي، '{action}' ليس فعلًا أعرفه في المجدول. قل 'help'."
    except Exception as exc:  # noqa: BLE001
        return f"سيدي، المجدول واجه مشكلة: {str(exc)[:180]}"


# ── نقطة نداء للمشروع: نبضة واحدة بلا خيط (لو أراد أحد تشغيلها من تلقاء نفسه) ──
def tick(player=None) -> str:
    """نبضة واحدة — مفيدة للمُحمِّل أو للاختبارات."""
    return _tick_once(player)


# ── فحص ذاتي: python workflow_scheduler.py ────────────────────────────────────
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
    print("workflow_scheduler — self-test")
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

    tmp = Path(tempfile.mkdtemp(prefix="ws_test_"))
    real_files = (C.SCHEDULES_FILE, C.WORKFLOWS_FILE, C.AUTOMATIONS_FILE, C.LOG_DIR)
    real_stamp, real_data = C._SCAN_CACHE.get("stamp"), C._SCAN_CACHE.get("data")
    real_plugins = C.PLUGINS_DIR
    base = C.CONFIG_DIR / "api_keys.json"
    backup = base.read_text(encoding="utf-8") if base.exists() else None
    try:
        C.SCHEDULES_FILE = tmp / "schedules.json"
        C.WORKFLOWS_FILE = tmp / "workflows.json"
        C.AUTOMATIONS_FILE = tmp / "automations.json"
        C.LOG_DIR = tmp / "logs"
        C.LOG_DIR.mkdir(parents=True, exist_ok=True)
        C.save_json(C.SCHEDULES_FILE, {"version": 1, "items": {}})
        C.save_json(C.WORKFLOWS_FILE, {"version": 1, "items": {"wf1": {
            "id": "wf1", "name": "موجز الاختبار", "enabled": True,
            "steps": [{"name": "طقس", "plugin": "habit_tracker", "parameters": {"action": "status"}}],
            "vars": {}, "run_count": 0, "history": []}}})
        C.save_json(C.AUTOMATIONS_FILE, {"version": 1, "items": {}})
        C.store_settings("workflow_scheduler", {"tick_interval_s": "5", "run_timeout_s": "120", "catch_up": "no",
                                                "watch_automations": "yes", "watch_reminders": "yes",
                                                "retry_tries": "1", "retry_delay_min": "1"})

        # 1) إنشاء جدولات
        out = run({"action": "create", "name": "حالة النظام صباحًا", "target_kind": "plugin",
                   "target_name": "habit_tracker", "parameters": json.dumps({"action": "status"}),
                   "when": "كل يوم 8 صباحًا"})
        check("create لهدف إضافة", "جدولت plugin" in out and "يوميًا" in out, out[:240])
        check("تحذير أن المجدول متوقف", "المجدول متوقف" in out, out[:300])
        out = run({"action": "create", "name": "كل نصف ساعة", "target_kind": "workflow",
                   "target_name": "موجز الاختبار", "when": "كل 30 دقيقة"})
        check("create لهدف سير عمل", "جدولت workflow" in out and "كل 30 دقيقة" in out, out[:240])
        out = run({"action": "create", "name": "هدف وهمي", "target_kind": "plugin", "target_name": "لا_توجد",
                   "when": "كل ساعة"})
        check("هدف غير موجود → رفض", "غير موجودة" in out, out[:200])
        out = run({"action": "create", "name": "وقت غامض", "target_kind": "plugin", "target_name": "habit_tracker",
                   "when": "في وقت ما"})
        check("توقيت غير مفهوم → رفض بصراحة", "لم أفهم التوقيت" in out, out[:200])
        out = run({"action": "create", "name": "سير عمل وهمي", "target_kind": "workflow",
                   "target_name": "لا_يوجد_سير", "when": "كل ساعة"})
        check("سير عمل غير موجود → رفض", "لا سير عمل" in out, out[:200])
        out = run({"action": "create", "name": "نفسي", "target_kind": "plugin", "target_name": "workflow_scheduler",
                   "when": "كل ساعة"})
        check("لا يجدول نفسه (منعًا للّف)", "نفسي" in out or "أجدول" in out, out[:200])
        # 2) العرض والقوائم
        out = run({"action": "list"})
        check("list يعرض الاسم والهدف والموعد", "حالة النظام صباحًا" in out and "plugin:habit_tracker" in out,
              out[:260])
        out = run({"action": "show", "name": "كل نصف ساعة"})
        check("show يعرض التفاصيل", "workflow:موجز الاختبار" in out and "التوقيت" in out, out[:260])
        # 3) النبضة الجافة والتنفيذ الفعلي
        out = run({"action": "tick", "dry": "yes"})
        check("tick جاف لا ينفّذ", "فحص جاف" in out, out[:200])
        # نُقدّم موعد الجدولة الأولى إلى الماضي ثم ننبض
        data = _load()
        key = next(k for k, i in data["items"].items() if i["name"] == "حالة النظام صباحًا")
        data["items"][key]["next_run"] = C.to_iso(C.now() - _dt.timedelta(minutes=1))
        _save(data)
        out = run({"action": "tick"})
        check("tick ينفّذ المستحقّ", "حالة النظام صباحًا" in out and "نفّذت" in out, out[:240])
        fresh = _load()["items"][key]
        check("سُجّل التشغيل والحالة", fresh.get("run_count") == 1 and fresh.get("last_status") == "ok",
              json.dumps(fresh.get("last_status")))
        check("الموعد القادم أُعيد حسابه", bool(fresh.get("next_run")) and C.from_iso(fresh["next_run"]) > C.now(),
              str(fresh.get("next_run")))
        check("التاريخ يحفظ سطرًا", bool(fresh.get("history")), str(fresh.get("history"))[:120])
        # 4) منع التشغيل المتزامن
        data = _load()
        data["items"][key]["running"] = True
        data["items"][key]["next_run"] = C.to_iso(C.now() - _dt.timedelta(minutes=1))
        _save(data)
        out = run({"action": "tick"})
        check("لا يُشغّل ما هو قيد التشغيل", "تعمل الآن" in out or "تجاهلتها" in out, out[:240])
        data = _load()
        data["items"][key]["running"] = False
        _save(data)
        # 5) الفشل وإعادة المحاولة — نؤجّل الجدولة اليومية أولًا لعزل الفحص
        data = _load()
        data["items"][key]["next_run"] = C.to_iso(C.now() + _dt.timedelta(days=1))
        _save(data)
        step_dir = tmp / "step_plugins"
        step_dir.mkdir(parents=True, exist_ok=True)
        (step_dir / "boom_step.py").write_text(
            'PLUGIN = {"name": "boom_step", "description": "تفشل دائمًا"}\n'
            'def run(parameters, player=None):\n    raise RuntimeError("انفجار مقصود")\n', encoding="utf-8")
        C.PLUGINS_DIR = step_dir
        C.scan_plugins(force=True)
        run({"action": "create", "name": "تفشل", "target_kind": "plugin", "target_name": "boom_step",
             "when": "كل 10 دقائق", "retry": json.dumps({"tries": 1, "delay_min": 1})})
        data = _load()
        fkey = next(k for k, i in data["items"].items() if i["name"] == "تفشل")
        data["items"][fkey]["next_run"] = C.to_iso(C.now() - _dt.timedelta(minutes=1))
        _save(data)
        out = run({"action": "tick"})
        check("الفشل مُعلَن بلا انهيار", "فشلت" in out, out[:260])
        fresh = _load()["items"][fkey]
        check("إعادة محاولة معلّقة", fresh.get("retry_pending") is True and fresh.get("retry_used") == 1,
              json.dumps({k: fresh.get(k) for k in ("retry_pending", "retry_used")}))
        check("الموعد القادم = موعد الإعادة", bool(fresh.get("next_run")) and C.from_iso(fresh["next_run"]) > C.now(),
              str(fresh.get("next_run")))
        out = run({"action": "tick"})
        check("لا يكرّر الإعادة فورًا", "لا جدولة حان وقتها" in out, out[:200])
        C.PLUGINS_DIR = real_plugins
        C.scan_plugins(force=True)
        # 6) تفعيل/إيقاف
        out = run({"action": "disable", "name": "كل نصف ساعة"})
        check("disable يعمل", "أوقفت" in out, out[:160])
        out = run({"action": "enable", "name": "كل نصف ساعة"})
        check("enable يعمل ويعيد الموعد", "فعّلت" in out, out[:200])
        # 7) الحذف ببوابة التأكيد
        before = len(_load()["items"])
        out = run({"action": "delete", "name": "كل نصف ساعة"})
        check("الحذف يستأذن", len(_load()["items"]) == before, out[:200])
        item, key2 = _find("كل نصف ساعة")
        out = _do_delete(str(key2))
        check("الحذف بعد الموافقة", len(_load()["items"]) == before - 1 and "حذفت" in out, out[:140])
        # 8) التشغيل اليدوي + الخيط الحي + الإيقاف
        out = run({"action": "run_now", "name": "حالة النظام صباحًا"})
        check("run_now ينفّذ فورًا", "تمّ" in out or "فشلت" in out, out[:200])
        out = run({"action": "start", "interval_s": "5"})
        check("start يشغّل الخيط", "شغّلت المجدول" in out, out[:200])
        time.sleep(0.5)
        out = run({"action": "status"})
        check("status يرى الخيط حيًّا", "يعمل" in out and "نبضة" in out, out[:240])
        out = run({"action": "stop"})
        check("stop يوقف الخيط", "أوقفت المجدول" in out, out[:160])
        out = run({"action": "status"})
        check("status بعد الإيقاف", "متوقف" in out, out[:200])
        # 9) تاريخ + فعل مجهول + مساعدة + لا أسرار
        out = run({"action": "history", "name": "حالة النظام صباحًا"})
        check("history يعرض الأسطر", "تاريخ" in out and "ok" in out, out[:200])
        check("help يعمل", "المجدول" in run({"action": "help"}))
        check("فعل مجهول", "ليس فعلًا" in run({"action": "zzz"}))
        raw = ""
        for f in C.LOG_DIR.glob("*.jsonl"):
            raw += f.read_text(encoding="utf-8")
        check("لا أسرار في السجل", "password" not in raw.lower() and "bearer " not in raw.lower())
    finally:
        try:
            stop_event = _TICKER.get("stop")
            if stop_event is not None:
                stop_event.set()
        except Exception:
            pass
        C.PLUGINS_DIR = real_plugins
        C.SCHEDULES_FILE, C.WORKFLOWS_FILE, C.AUTOMATIONS_FILE, C.LOG_DIR = real_files
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
