"""
web_panel.py — لوحة التحكم الويب (Web Control Panel).

لوحة متصفّح محلية تعرض حالة JARVIS كاملة وتتحكّم في الحزمة: الإضافات (تفعيل/تعطيل)،
الأتمتة وسير العمل والجدولات (تشغيل الآن، إيقاف/تفعيل)، الإشعارات، السجل، الذاكرة،
وحالة الواجهة البرمجية — إضافة إلى تشغيل إضافات **من قائمة سماح** فقط.

لا تستبدل لوحتك الحالية: تلك على المنفذ 8000 للتحكم من الهاتف، وهذه لوحة الإدارة
التقنية على منفذ مستقل (8788 افتراضيًا) ومربوطة بـlocalhost. صفحة واحدة بلا أي ملف
خارجي (لا CDN، لا خطوط، لا سكربتات بعيدة) فتعمل بلا إنترنت ويمكن تضمينها في أي متصفّح.

الأمان: مفتاح وصول + كوكي جلسة (HttpOnly, SameSite=Strict)، تحقّق ثابت الزمن، حدّ
معدّل، حدّ حجم، وقراءة فقط مفصولة بصريًا عن الإجراءات التي تُغيّر البيانات.
"""
from __future__ import annotations

import importlib.util
import socket
import sys
from pathlib import Path
from typing import Any

PLUGIN = {
    "name": "web_panel",
    "description": (
        "A local web control panel for JARVIS: plugin health and enable/disable, every "
        "automation, workflow and schedule with its next run, notifications, logs, "
        "memory stats and the remote API status — plus 'run now' buttons and run of "
        "approved plugins. Use for: 'open the control panel', 'start the web panel', "
        "'stop the panel', 'panel status', 'what's the panel address'. It does not "
        "replace the phone dashboard; it is the admin view, bound to localhost."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "start | stop | status | allow | token | help"},
            "name": {"type": "STRING", "description": "Plugin name for allow add/remove."},
            "allow_action": {"type": "STRING", "description": "For allow: add | remove | list | clear."},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "web_panel",
    "title": "Web control panel / لوحة التحكم",
    "fields": [
        {"key": "host", "type": "text", "label": "Bind address (127.0.0.1 = this machine only)", "default": "127.0.0.1"},
        {"key": "port", "type": "text", "label": "Port", "default": "8788"},
        {"key": "allow_lan", "type": "text", "label": "Allow other devices on the network (yes/no)", "default": "no"},
        {"key": "refresh_s", "type": "text", "label": "Page auto-refresh, seconds", "default": "15"},
        {"key": "run_allowlist", "type": "text",
         "label": "Plugins the panel may run, comma separated",
         "default": "system_health, system_diagnostics, daily_briefing, notification_center"},
        {"key": "admin_allowed", "type": "text", "label": "Allow running admin plugins from the panel (yes/no)", "default": "no"},
        {"key": "rate_per_min", "type": "text", "label": "Requests per minute per client", "default": "120"},
    ],
}

_NS = "web_panel"
_COOKIE = "jarvis_panel"
_ADMIN = frozenset({"remote_api", "web_panel", "plugin_manager", "workflow_scheduler"})
_SERVER: dict = {"http": None, "started": 0.0}


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
    return C.cfg(_NS, PLUGIN_SETTINGS["fields"])


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


def _token() -> str:
    return C.token_for(C.ORCH_DIR / "web_panel_token.json", prefix="pnl_")


def _allowlist() -> list[str]:
    raw = str(_cfg().get("run_allowlist") or "")
    return [x.strip() for x in raw.replace("؛", ",").split(",") if x.strip()]


def _save_allowlist(names: list[str]) -> bool:
    return C.store_settings(_NS, {"run_allowlist": ", ".join(dict.fromkeys(names))})


# ── بيانات اللوحة (كلها قراءة فقط) ────────────────────────────────────────────
def _data_system() -> dict:
    info: dict = {"cpu": None, "ram": None, "disk": None, "battery": None,
                  "python": sys.version.split()[0], "platform": sys.platform}
    ps = getattr(C, "_psutil", lambda: None)()
    if ps is None:
        return info
    try:
        info["cpu"] = float(ps.cpu_percent(interval=0.1))
        info["ram"] = float(ps.virtual_memory().percent)
        root = "C:\\" if sys.platform == "win32" else "/"
        info["disk"] = float(ps.disk_usage(root).percent)
        battery = ps.sensors_battery()
        info["battery"] = None if battery is None else float(battery.percent)
    except Exception:
        pass
    return info


def _data_plugins() -> dict:
    scan = C.scan_plugins(force=False)
    loaded = C.loaded_plugin_names()
    rows, rejected = [], []
    for info in sorted(scan.values(), key=lambda i: str(i["name"])):
        row = {
            "name": info["name"], "file": info["file"], "valid": bool(info["valid"]),
            "enabled": C.plugin_enabled(str(info["name"])),
            "loaded": info["name"] in loaded or info["file"] in loaded,
            "error": str(info.get("error", "")),
            "description": C.clip(info.get("description", ""), 160),
            "settings": bool(info.get("has_settings")),
            "requires": info.get("requires", []),
            "sha": info.get("sha256", ""),
        }
        rows.append(row)
        if not row["valid"]:
            rejected.append({"file": row["file"], "error": row["error"]})
    return {"total": len(rows), "enabled": sum(1 for r in rows if r["enabled"]),
            "rejected": rejected, "items": rows}


def _load_items(path: Path) -> list[dict]:
    data = C.load_json(path, {"items": {}})
    items = data.get("items", {}) if isinstance(data, dict) else {}
    return list(items.values()) if isinstance(items, dict) else []


def _data_automations() -> dict:
    items = []
    for item in _load_items(C.AUTOMATIONS_FILE):
        items.append({
            "id": item.get("id"), "name": item.get("name"), "enabled": bool(item.get("enabled", True)),
            "trigger": item.get("trigger") or {}, "trigger_text": _describe_trigger(item.get("trigger") or {}),
            "steps": [s.get("plugin") for s in (item.get("steps") or [])],
            "last_run": item.get("last_run"), "last_status": item.get("last_status"),
            "run_count": item.get("run_count", 0),
            "next_run": C.to_iso(C.next_run(item.get("trigger") or {})) if item.get("enabled", True) else "",
        })
    return {"count": len(items), "items": items}


def _data_workflows() -> dict:
    items = []
    for item in _load_items(C.WORKFLOWS_FILE):
        items.append({
            "id": item.get("id"), "name": item.get("name"), "enabled": bool(item.get("enabled", True)),
            "steps": [s.get("plugin") for s in (item.get("steps") or [])],
            "last_run": item.get("last_run"), "last_status": item.get("last_status"),
            "run_count": item.get("run_count", 0),
        })
    return {"count": len(items), "items": items}


def _data_schedules() -> dict:
    items = []
    for item in _load_items(C.SCHEDULES_FILE):
        target = item.get("target") or {}
        items.append({
            "id": item.get("id"), "name": item.get("name"), "enabled": bool(item.get("enabled", True)),
            "target": f"{target.get('kind')}:{target.get('name')}",
            "schedule_text": C.describe_schedule(item.get("schedule") or {}),
            "next_run": item.get("next_run"), "last_run": item.get("last_run"),
            "last_status": item.get("last_status"),
            "failures": item.get("consecutive_failures", 0), "missed": item.get("missed", 0),
        })
    items.sort(key=lambda i: str(i.get("next_run") or "zzz"))
    return {"count": len(items), "items": items}


def _describe_trigger(trigger: dict) -> str:
    kind = str(trigger.get("kind", ""))
    if kind in ("daily", "weekly", "interval", "once", "manual"):
        return C.describe_schedule(trigger)
    if kind == "file":
        return f"ملف {trigger.get('path')} ({trigger.get('event')})"
    if kind == "app":
        return f"تطبيق {trigger.get('process')} ({trigger.get('event')})"
    if kind == "system":
        return f"{trigger.get('metric')} {trigger.get('op')} {trigger.get('value')}%"
    if kind == "plugin":
        return f"بعد {trigger.get('plugin')} ({trigger.get('status', 'ok')})"
    return kind or "—"


def _data_notifications() -> dict:
    store = None
    for candidate in (C.CONFIG_DIR / "notifications.json", C.CONFIG_DIR / "notification_center.json"):
        if candidate.exists():
            store = candidate
            break
    if store is None:
        return {"available": False, "items": [], "count": 0}
    data = C.load_json(store, {})
    items = data.get("items") if isinstance(data, dict) else None
    items = items if isinstance(items, list) else []
    if not items and isinstance(data, dict):
        flat = []
        for value in data.values():
            if isinstance(value, list):
                flat.extend([v for v in value if isinstance(v, dict)])
        items = flat
    rows = [{"title": C.clip(n.get("title", ""), 90), "body": C.clip(n.get("body", ""), 160),
             "priority": n.get("priority", "info"), "category": n.get("category", ""),
             "ts": n.get("ts") or n.get("time") or n.get("created", "")} for n in items[-40:]]
    rows.reverse()
    return {"available": True, "count": len(rows),
            "urgent": sum(1 for r in rows if str(r["priority"]).lower() in ("urgent", "high")),
            "items": rows}


def _data_memory() -> dict:
    stats = {"memory_entries": 0, "notes": 0, "kb_documents": 0, "briefings": 0}
    try:
        from memory import memory_manager  # type: ignore
        entries = memory_manager.all_entries_for_ui()
        stats["memory_entries"] = len(entries) if isinstance(entries, list) else 0
    except Exception:
        pass
    notes = C.load_json(C.CONFIG_DIR / "notes" / "notes.json", {})
    if isinstance(notes, dict):
        items = notes.get("items") if isinstance(notes.get("items"), list) else None
        stats["notes"] = len(items) if items is not None else len([v for v in notes.values() if isinstance(v, dict)])
    index = C.load_json(C.CONFIG_DIR / "knowledge" / "index.json", {})
    if isinstance(index, dict):
        docs = index.get("documents") or index.get("files") or []
        stats["kb_documents"] = len(docs) if isinstance(docs, list) else len(docs) if isinstance(docs, dict) else 0
    folder = Path.home() / "Documents" / "JARVIS_Briefings"
    try:
        stats["briefings"] = len(list(folder.glob("briefing_*.md"))) if folder.exists() else 0
    except Exception:
        pass
    return stats


def _data_api_status() -> dict:
    state = C.load_json(C.ORCH_DIR / "remote_api_state.json", {})
    host = str(state.get("host") or "127.0.0.1")
    port = int(state.get("port") or 8787)
    alive = False
    try:
        with socket.create_connection((host, port), timeout=0.3):
            alive = True
    except Exception:
        alive = False
    return {"declared": bool(state.get("running")), "responding": alive, "host": host, "port": port,
            "updated": state.get("updated", "")}


def _data_logs(n: int = 60) -> dict:
    rows = C.tail_log("orchestration", n)
    for row in rows:
        if isinstance(row.get("detail"), dict):
            row["detail"] = {k: v for k, v in row["detail"].items() if k != "client"}
    return {"count": len(rows), "items": rows}


def _data_errors() -> dict:
    plugins = _data_plugins()
    failures = [row for row in C.tail_log("orchestration", 200)
                if row.get("status") in ("failed", "timeout", "blocked", "missed")]
    return {"rejected": plugins["rejected"], "failures": failures[:25]}


def _overview() -> dict:
    plugins = _data_plugins()
    automations = _data_automations()
    workflows = _data_workflows()
    schedules = _data_schedules()
    notifications = _data_notifications()
    return {
        "ok": True,
        "now": C.now_iso(),
        "panel": {"running": bool(_SERVER.get("http") and _SERVER["http"].running),
                  "started": _SERVER.get("started_at", ""), "requests": (C.load_json(
                      C.ORCH_DIR / "web_panel_state.json", {}).get("requests", 0))},
        "scheduler": {"running": _scheduler_running()},
        "plugins": {"total": plugins["total"], "enabled": plugins["enabled"],
                    "rejected": len(plugins["rejected"])},
        "automations": {"count": automations["count"],
                        "enabled": sum(1 for i in automations["items"] if i["enabled"])},
        "workflows": {"count": workflows["count"],
                      "enabled": sum(1 for i in workflows["items"] if i["enabled"])},
        "schedules": {"count": schedules["count"],
                      "enabled": sum(1 for i in schedules["items"] if i["enabled"]),
                      "next": [(i["name"], i["next_run"]) for i in schedules["items"][:3]]},
        "notifications": {"count": notifications["count"], "urgent": notifications.get("urgent", 0)},
        "errors": len([f for f in C.tail_log("orchestration", 100)
                       if f.get("status") in ("failed", "timeout")]),
        "system": _data_system(),
        "api": _data_api_status(),
        "allowlist": _allowlist(),
    }


def _scheduler_running() -> bool:
    try:
        module = sys.modules.get("plugins.workflow_scheduler") or sys.modules.get("_jarvis_pg_workflow_scheduler")
        ticker = getattr(module, "_TICKER", {}) if module else {}
        thread = ticker.get("thread") if isinstance(ticker, dict) else None
        return bool(thread and thread.is_alive())
    except Exception:
        return False


# ── معالجات المسارات ──────────────────────────────────────────────────────────
def _h_page(req) -> tuple[int, Any, str]:
    del req
    refresh = _int(_cfg().get("refresh_s"), 15, 5, 600)
    html = _HTML.replace("__REFRESH__", str(refresh))
    return 200, html, "text/html; charset=utf-8"


def _h_login(req) -> tuple[int, Any, str, dict]:
    payload = req.json()
    given = str((payload or {}).get("token") or "") if isinstance(payload, dict) else ""
    import secrets as _secrets
    if given and _secrets.compare_digest(given, _token()):
        cookie = f"{_COOKIE}={given}; HttpOnly; SameSite=Strict; Path=/"
        return 200, {"ok": True}, "application/json; charset=utf-8", {"Set-Cookie": cookie}
    return 401, {"ok": False, "error": "مفتاح اللوحة غير صحيح"}, "application/json; charset=utf-8", {}


def _json_ok(payload: Any, status: int = 200) -> tuple[int, Any, str]:
    return status, payload, "application/json; charset=utf-8"


def _h_overview(req) -> tuple[int, Any, str]:
    del req
    return _json_ok(_overview())


def _h_plugins(req) -> tuple[int, Any, str]:
    del req
    data = _data_plugins()
    data["ok"] = True
    return _json_ok(data)


def _h_automations(req) -> tuple[int, Any, str]:
    del req
    data = _data_automations()
    data["ok"] = True
    return _json_ok(data)


def _h_workflows(req) -> tuple[int, Any, str]:
    del req
    data = _data_workflows()
    data["ok"] = True
    return _json_ok(data)


def _h_schedules(req) -> tuple[int, Any, str]:
    del req
    data = _data_schedules()
    data["ok"] = True
    return _json_ok(data)


def _h_notifications(req) -> tuple[int, Any, str]:
    del req
    data = _data_notifications()
    data["ok"] = True
    return _json_ok(data)


def _h_memory(req) -> tuple[int, Any, str]:
    del req
    data = _data_memory()
    data["ok"] = True
    return _json_ok(data)


def _h_system(req) -> tuple[int, Any, str]:
    del req
    data = _data_system()
    data["ok"] = True
    return _json_ok(data)


def _h_logs(req) -> tuple[int, Any, str]:
    n = _int(req.query.get("n") or 60, 60, 1, 500)
    data = _data_logs(n)
    data["ok"] = True
    return _json_ok(data)


def _h_errors(req) -> tuple[int, Any, str]:
    del req
    data = _data_errors()
    data["ok"] = True
    return _json_ok(data)


def _h_plugin_toggle(req) -> tuple[int, Any, str]:
    payload = req.json()
    if not isinstance(payload, dict):
        return _json_ok({"ok": False, "error": "جسم JSON مطلوب"}, 400)
    name = str(payload.get("name") or "").strip()
    enabled = bool(payload.get("enabled"))
    if not name:
        return _json_ok({"ok": False, "error": "بلا اسم إضافة"}, 400)
    scan = C.scan_plugins(force=False)
    info = next((i for i in scan.values() if str(i["name"]) == name or i["file"] == f"{name}.py"), None)
    if info is None:
        return _json_ok({"ok": False, "error": f"لا توجد إضافة '{name}'"}, 404)
    if not enabled and str(info["name"]) in _ADMIN:
        return _json_ok({"ok": False, "error": "لا يُعطّل شيءٌ نفسه/نظيره من اللوحة"}, 403)
    ok, message = C.set_plugin_enabled(str(info["name"]), enabled)
    return _json_ok({"ok": ok, "message": message}, 200 if ok else 500)


def _h_run(req) -> tuple[int, Any, str]:
    payload = req.json()
    if not isinstance(payload, dict):
        return _json_ok({"ok": False, "error": "جسم JSON مطلوب"}, 400)
    name = str(payload.get("name") or "").strip()
    parameters = payload.get("parameters") or {}
    if not name:
        return _json_ok({"ok": False, "error": "بلا اسم إضافة"}, 400)
    if name in _ADMIN and not C.parse_bool(_cfg().get("admin_allowed"), False):
        return _json_ok({"ok": False, "error": f"'{name}' إضافة إدارية — تشغيلها من اللوحة يحتاج admin_allowed=yes"}, 403)
    if name not in _allowlist():
        return _json_ok({"ok": False, "error": f"'{name}' ليست في قائمة تشغيل اللوحة"}, 403)
    try:
        req._plugin = name  # noqa: SLF001 — للسجل فقط
    except Exception:
        pass
    ok, value, timed_out = C.run_with_timeout(lambda: C.call_plugin(name, parameters), 120.0)
    if timed_out:
        return _json_ok({"ok": False, "error": "تجاوزت المهلة"}, 504)
    return _json_ok({"ok": bool(ok), "result": str(value), "plugin": name})


def _h_toggle(req) -> tuple[int, Any, str]:
    payload = req.json()
    if not isinstance(payload, dict):
        return _json_ok({"ok": False, "error": "جسم JSON مطلوب"}, 400)
    kind = str(payload.get("kind") or "").strip().lower()
    item_id = str(payload.get("id") or "").strip()
    enabled = bool(payload.get("enabled"))
    files = {"automation": C.AUTOMATIONS_FILE, "workflow": C.WORKFLOWS_FILE, "schedule": C.SCHEDULES_FILE}
    if kind not in files or not item_id:
        return _json_ok({"ok": False, "error": "kind يجب أن يكون automation/workflow/schedule مع id"}, 400)
    data = C.load_json(files[kind], {"items": {}})
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, dict) or item_id not in items:
        return _json_ok({"ok": False, "error": "لم أجد العنصر"}, 404)
    items[item_id]["enabled"] = enabled
    if kind == "schedule" and enabled:
        items[item_id]["next_run"] = C.to_iso(C.next_run(items[item_id].get("schedule") or {}))
    C.save_json(files[kind], data)
    C.log_event("web_panel", f"toggle:{kind}", "ok", 0, "", {"id": item_id, "enabled": enabled})
    return _json_ok({"ok": True, "message": f"{'فُعّل' if enabled else 'أُوقف'} {kind} {item_id}"})


def _h_run_now(req) -> tuple[int, Any, str]:
    payload = req.json()
    if not isinstance(payload, dict):
        return _json_ok({"ok": False, "error": "جسم JSON مطلوب"}, 400)
    kind = str(payload.get("kind") or "").strip().lower()
    key = str(payload.get("id") or payload.get("name") or "").strip()
    if not kind or not key:
        return _json_ok({"ok": False, "error": "kind مع id/name مطلوبان"}, 400)
    try:
        req._plugin = kind  # noqa: SLF001 — للسجل فقط
    except Exception:
        pass
    if kind == "schedule":
        call = ("workflow_scheduler", {"action": "run_now", "id": key})
    elif kind == "workflow":
        call = ("workflow_engine", {"action": "run", "name": key})
    elif kind == "automation":
        call = ("automation_engine", {"action": "run", "name": key})
    else:
        return _json_ok({"ok": False, "error": "kind غير معروف"}, 400)
    ok, value, timed_out = C.run_with_timeout(lambda: C.call_plugin(call[0], call[1]), 180.0)
    if timed_out:
        return _json_ok({"ok": False, "error": "تجاوزت المهلة"}, 504)
    return _json_ok({"ok": bool(ok), "result": str(value), "kind": kind})


def _routes() -> dict:
    return {
        ("GET", "/"): _h_page,
        ("GET", "/health"): lambda req: _json_ok({"ok": True, "service": "jarvis-web-panel"}),
        ("POST", "/login"): _h_login,
        ("GET", "/api/overview"): _h_overview,
        ("GET", "/api/plugins"): _h_plugins,
        ("GET", "/api/automations"): _h_automations,
        ("GET", "/api/workflows"): _h_workflows,
        ("GET", "/api/schedules"): _h_schedules,
        ("GET", "/api/notifications"): _h_notifications,
        ("GET", "/api/memory"): _h_memory,
        ("GET", "/api/system"): _h_system,
        ("GET", "/api/logs"): _h_logs,
        ("GET", "/api/errors"): _h_errors,
        ("POST", "/api/plugin"): _h_plugin_toggle,
        ("POST", "/api/run"): _h_run,
        ("POST", "/api/toggle"): _h_toggle,
        ("POST", "/api/run-now"): _h_run_now,
    }


# ── الأفعال ───────────────────────────────────────────────────────────────────
def _start(player=None) -> str:
    cfg = _cfg()
    if _SERVER["http"] is not None and _SERVER["http"].running:
        return f"سيدي، اللوحة تعمل بالفعل على http://{_SERVER['http'].host}:{_SERVER['http'].port}"
    token = _token()
    if not token:
        return "سيدي، تعذّر إنشاء مفتاح اللوحة."
    host = str(cfg.get("host") or "127.0.0.1").strip()
    if not C.parse_bool(cfg.get("allow_lan"), False) and host not in ("127.0.0.1", "localhost", "::1"):
        host = "127.0.0.1"
    port = _int(cfg.get("port"), 8788, 1024, 65535)
    server = C.MiniHTTP(host, port, _routes(), token, log_name="web_panel",
                        public_paths=("/", "/health", "/login"),
                        rate_per_minute=_int(cfg.get("rate_per_min"), 120, 10, 6000),
                        max_body_kb=64, cookie_name=_COOKIE)
    ok, message = server.start()
    if not ok:
        return f"سيدي، {message}"
    _SERVER["http"] = server
    _SERVER["started"] = C.now().timestamp()
    _SERVER["started_at"] = C.now_iso()
    C.save_json(C.ORCH_DIR / "web_panel_state.json",
                {"running": True, "host": host, "port": port, "updated": C.now_iso()})
    C.log_event("web_panel", "start", "ok", 0, "", {"host": host, "port": port})
    allowed = _allowlist()
    out = (f"سيدي، اللوحة تعمل على {message}\n"
           f"  افتح: http://{host}:{port}\n"
           f"  الدخول بمفتاح اللوحة (محفوظ في config/orchestration/web_panel_token.json)\n"
           f"  تُشغِّل من اللوحة: {'، '.join(allowed) if allowed else 'لا إضافات (القائمة فارغة)'}\n"
           f"  لوحتك الأخرى للهاتف تبقى كما هي على منفذها — هذه لوحة إدارة تقنية.")
    _log(player, out)
    return out


def _stop(player=None) -> str:
    server = _SERVER.get("http")
    if server is None or not server.running:
        C.save_json(C.ORCH_DIR / "web_panel_state.json", {"running": False, "updated": C.now_iso()})
        return "سيدي، اللوحة متوقفة بالفعل."
    ok, message = server.stop()
    _SERVER["http"] = None
    _SERVER["started"] = 0.0
    C.save_json(C.ORCH_DIR / "web_panel_state.json", {"running": False, "updated": C.now_iso()})
    C.log_event("web_panel", "stop", "ok", 0, "", None)
    out = "سيدي، أوقفت اللوحة." if ok else f"سيدي، {message}"
    _log(player, out)
    return out


def _act_status(params: dict, player) -> str:
    del params
    cfg = _cfg()
    server = _SERVER.get("http")
    if server and server.running:
        lines = [f"سيدي، اللوحة تعمل على http://{server.host}:{server.port}",
                 f"  طلبات: {server.requests} · مرفوضة: {server.denied} · بدأت: {server.started_at}"]
    else:
        lines = ["سيدي، اللوحة متوقفة في هذه العملية."]
    lines.append(f"  الإعداد: {cfg.get('host')}:{cfg.get('port')} · "
                 f"الشبكة: {'مسموحة' if C.parse_bool(cfg.get('allow_lan'), False) else 'localhost فقط'}")
    lines.append("  قائمة التشغيل: " + ("، ".join(_allowlist()) or "فارغة"))
    lines.append("  التحديث التلقائي للصفحة: كل " + str(_int(cfg.get("refresh_s"), 15, 5, 600)) + " ثانية")
    out = "\n".join(lines)
    _log(player, out)
    return out


def _act_allow(params: dict, player) -> str:
    sub = str(params.get("allow_action") or "list").strip().lower()
    name = str(params.get("name") or "").strip()
    allowed = _allowlist()
    if sub in ("list", "show"):
        return "سيدي، تُشغِّل اللوحة: " + ("، ".join(allowed) or "لا شيء (القائمة فارغة).")
    if sub in ("clear",):
        _save_allowlist([])
        return "سيدي، أفرغت قائمة تشغيل اللوحة."
    if sub in ("remove", "delete"):
        if name in allowed:
            allowed.remove(name)
            _save_allowlist(allowed)
        return f"سيدي، أزلت '{name}'. القائمة: {'، '.join(allowed) or 'فارغة'}"
    if sub in ("add", "allow"):
        if not name:
            return "سيدي، أي إضافة؟"
        if C.resolve_plugin(name) is None:
            return f"سيدي، '{name}' غير موجودة في plugins."
        if name in _ADMIN:
            return f"سيدي، '{name}' إدارية — لا أضيفها إلا بضبط admin_allowed بنفسك."
        if name not in allowed:
            allowed.append(name)
            _save_allowlist(allowed)
        return f"سيدي، صارت اللوحة تُشغّل '{name}'. القائمة: {'، '.join(allowed)}"
    _ = player
    return "سيدي، allow_action: add | remove | list | clear"


def _act_token(params: dict, player) -> str:
    del params
    token = _token()
    if not token:
        return "سيدي، تعذّر قراءة مفتاح اللوحة."
    out = (f"سيدي، مفتاح اللوحة: {C.mask_token(token)}\n"
           f"  محفوظ في config/orchestration/web_panel_token.json (صلاحيات 600) ولا يظهر في أي سجل.\n"
           f"  أدخله في صفحة اللوحة مرة واحدة، وسيحفظها المتصفّح في كوكي جلسة.")
    _log(player, out)
    return out


_HELP = (
    "لوحة التحكم، سيدي:\n"
    "• start — تشغيلها على 127.0.0.1:8788 (أو ما تضبطه) · stop · status\n"
    "• allow — ما يُشغَّل من اللوحة (add/remove/list/clear مع name)\n"
    "• token — مفتاح الدخول (مخفي)\n"
    "تعرض: حالة JARVIS والنظام · الإضافات وصحتها (وتفعيل/تعطيل) · الأتمتة · سير العمل · "
    "الجدولات ومواعيدها القادمة · الإشعارات · السجل · الذاكرة · حالة الواجهة البرمجية.\n"
    "وتُنفّذ: تشغيل الآن لأي جدولة/سير عمل/أتمتة، وتشغيل إضافة من القائمة المسموحة.\n"
    "لا تستبدل لوحة الهاتف (منفذ 8000) — هذه إضافة إدارية مستقلة."
)


def run(parameters: dict, player=None, session_memory=None) -> str:
    """نقطة الدخول. لا ترمي أبدًا."""
    del session_memory
    if C is None:
        return "سيدي، ملف '_orch_core.py' المشترك غير موجود — بدونه لا تعمل اللوحة."
    try:
        p = dict(parameters or {})
        action = str(p.get("action") or "status").strip().lower()
        C.ensure_dirs()
        if action in ("start", "serve", "شغّل"):
            return _start(player)
        if action in ("stop", "أوقف"):
            return _stop(player)
        if action in ("status", "حالة"):
            return _act_status(p, player)
        if action in ("allow", "سماح"):
            return _act_allow(p, player)
        if action in ("token", "مفتاح"):
            return _act_token(p, player)
        if action in ("help", "?"):
            return _HELP
        return f"سيدي، '{action}' ليس فعلًا أعرفه في اللوحة. قل 'help'."
    except Exception as exc:  # noqa: BLE001
        return f"سيدي، اللوحة واجهت مشكلة: {str(exc)[:180]}"


# ── الصفحة (HTML واحد، بلا أي ملف خارجي) ──────────────────────────────────────
_HTML = """<!DOCTYPE html>
<html lang="ar" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>JARVIS — لوحة التحكم</title>
<style>
:root{--bg:#0b1020;--card:#141a2e;--line:#243049;--txt:#e8ecf6;--dim:#93a0bd;--ok:#31c48d;--warn:#f0b429;--bad:#ef5b5b;--act:#5b8def}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);
font-family:system-ui,"Segoe UI",Tahoma,Arial,sans-serif;line-height:1.6}
header{display:flex;align-items:center;gap:14px;padding:14px 18px;background:#0e1426;border-bottom:1px solid var(--line);flex-wrap:wrap}
h1{font-size:18px;margin:0}
.pill{padding:3px 10px;border-radius:999px;font-size:12px;border:1px solid var(--line);color:var(--dim)}
.pill.on{color:var(--ok);border-color:#1d5c45}.pill.off{color:var(--bad);border-color:#5c1d1d}
nav{display:flex;gap:6px;flex-wrap:wrap;padding:10px 18px;border-bottom:1px solid var(--line);background:#0e1426}
nav button{background:transparent;color:var(--dim);border:1px solid var(--line);padding:6px 12px;border-radius:8px;cursor:pointer;font-size:13px}
nav button.sel{color:var(--txt);background:#1b2440;border-color:var(--act)}
main{padding:18px;max-width:1180px;margin:0 auto}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px}
.card h3{margin:0 0 8px;font-size:14px;color:var(--dim);font-weight:600}
.big{font-size:24px;font-weight:700}
.row{display:flex;justify-content:space-between;gap:10px;padding:7px 0;border-bottom:1px dashed #1e2740;font-size:13.5px}
.row:last-child{border-bottom:0}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:right;padding:8px 6px;border-bottom:1px solid #1e2740;vertical-align:top}
th{color:var(--dim);font-weight:600;font-size:12px}
button.act{background:var(--act);color:#fff;border:0;border-radius:7px;padding:5px 10px;cursor:pointer;font-size:12px}
button.act.ghost{background:#22304f}
button.act.danger{background:#7a2b2b}
.badge{font-size:11px;padding:2px 7px;border-radius:999px;border:1px solid var(--line);color:var(--dim)}
.badge.ro{border-color:#2c3a58}.badge.act{border-color:var(--act);color:#9dbaff}
.ok{color:var(--ok)}.bad{color:var(--bad)}.warn{color:var(--warn)}.dim{color:var(--dim)}
.mono{font-family:ui-monospace,Consolas,monospace;font-size:12px}
#login{max-width:380px;margin:12vh auto;text-align:center}
input{width:100%;padding:10px;border-radius:8px;border:1px solid var(--line);background:#0e1426;color:var(--txt);margin:10px 0}
.bar{height:8px;background:#1b2440;border-radius:6px;overflow:hidden;margin-top:6px}
.bar i{display:block;height:100%;background:linear-gradient(90deg,#31c48d,#5b8def)}
footer{color:var(--dim);font-size:12px;text-align:center;padding:18px}
</style></head><body>
<header>
  <h1>⚙️ JARVIS — لوحة التحكم</h1>
  <span id="pillJarvis" class="pill">…</span>
  <span id="pillSched" class="pill">…</span>
  <span id="pillApi" class="pill">…</span>
  <span id="stamp" class="dim" style="margin-inline-start:auto;font-size:12px"></span>
</header>
<div id="app" style="display:none">
<nav id="tabs"></nav>
<main id="view"></main>
<footer>لوحة إدارية محلية — لا تستبدل لوحة الهاتف. <span id="foot"></span></footer>
</div>
<div id="login" style="display:none" class="card">
  <h3>دخول لوحة JARVIS</h3>
  <p class="dim" style="font-size:13px">أدخل مفتاح اللوحة من <span class="mono">config/orchestration/web_panel_token.json</span>
  أو قل لجارفس: <span class="mono">web_panel token</span>.</p>
  <input id="tok" type="password" placeholder="مفتاح اللوحة" autocomplete="off">
  <button class="act" style="width:100%" onclick="login()">دخول</button>
  <p id="err" class="bad" style="font-size:13px"></p>
</div>
<script>
const REFRESH = __REFRESH__ * 1000;
let TAB = 'overview', TIMER = null;

async function api(path, method, body){
  const opt = {method: method||'GET', headers:{}, credentials:'same-origin'};
  if (body !== undefined){ opt.headers['Content-Type']='application/json'; opt.body = JSON.stringify(body); }
  const r = await fetch(path, opt);
  if (r.status === 401){ showLogin(); throw new Error('401'); }
  return await r.json();
}
function esc(s){ return String(s==null?'':s).replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function pct(v){ return v==null ? '—' : (Math.round(v*10)/10) + '%'; }

function showLogin(){ document.getElementById('login').style.display='block'; document.getElementById('app').style.display='none'; }
function showApp(){ document.getElementById('login').style.display='none'; document.getElementById('app').style.display='block'; }
async function login(){
  const t = document.getElementById('tok').value.trim();
  try{
    const r = await fetch('/login', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({token:t})});
    if (r.ok){ showApp(); boot(); } else { document.getElementById('err').textContent='مفتاح غير صحيح'; }
  }catch(e){ document.getElementById('err').textContent='تعذّر الاتصال بالخادم'; }
}
async function act(path, body, label){
  if (!confirm('تأكيد الإجراء: ' + label + '؟')) return;
  try{
    const r = await api(path, 'POST', body);
    alert(r.ok ? (r.message || r.result || 'تم') : ('لم يتم: ' + (r.error||'')));
    refresh();
  }catch(e){}
}
const TABS = [['overview','نظرة عامة'],['plugins','الإضافات'],['automations','الأتمتة'],['workflows','سير العمل'],
  ['schedules','الجدولات'],['notifications','الإشعارات'],['errors','الأخطاء'],['logs','السجل'],
  ['memory','الذاكرة'],['system','النظام']];
function tabs(){
  document.getElementById('tabs').innerHTML = TABS.map(([k,l])=>
    `<button class="${k===TAB?'sel':''}" onclick="TAB='${k}';tabs();refresh()">${l}</button>`).join('');
}
async function refresh(){
  if (TAB==='overview') await viewOverview();
  else if (TAB==='plugins') await viewPlugins();
  else if (TAB==='automations') await viewAutomations();
  else if (TAB==='workflows') await viewWorkflows();
  else if (TAB==='schedules') await viewSchedules();
  else if (TAB==='notifications') await viewNotifications();
  else if (TAB==='errors') await viewErrors();
  else if (TAB==='logs') await viewLogs();
  else if (TAB==='memory') await viewMemory();
  else if (TAB==='system') await viewSystem();
}
async function viewOverview(){
  const d = await api('/api/overview');
  document.getElementById('pillJarvis').textContent = 'JARVIS: نشِط';
  document.getElementById('pillJarvis').className = 'pill on';
  const sch = d.scheduler.running;
  document.getElementById('pillSched').textContent = 'المجدول: ' + (sch?'يعمل':'متوقف');
  document.getElementById('pillSched').className = 'pill ' + (sch?'on':'off');
  const ap = d.api.responding;
  document.getElementById('pillApi').textContent = 'الواجهة: ' + (ap?'تستجيب':'مغلقة');
  document.getElementById('pillApi').className = 'pill ' + (ap?'on':'off');
  document.getElementById('stamp').textContent = 'آخر تحديث: ' + d.now;
  const sys = d.system;
  document.getElementById('view').innerHTML = `
  <div class="grid">
    <div class="card"><h3>الإضافات <span class="badge ro">قراءة فقط</span></h3>
      <div class="big">${d.plugins.total}</div>
      <div class="row"><span>مفعّلة</span><span class="ok">${d.plugins.enabled}</span></div>
      <div class="row"><span>غير صالحة</span><span class="${d.plugins.rejected?'bad':'ok'}">${d.plugins.rejected}</span></div></div>
    <div class="card"><h3>الأتمتة <span class="badge ro">قراءة فقط</span></h3>
      <div class="big">${d.automations.count}</div>
      <div class="row"><span>مفعّلة</span><span>${d.automations.enabled}</span></div>
      <div class="row"><span>سير عمل</span><span>${d.workflows.count} (${d.workflows.enabled} مفعّل)</span></div></div>
    <div class="card"><h3>الجدولات <span class="badge ro">قراءة فقط</span></h3>
      <div class="big">${d.schedules.count}</div>
      <div class="row"><span>مفعّلة</span><span>${d.schedules.enabled}</span></div>
      ${d.schedules.next.map(([n,t])=>`<div class="row"><span>${esc(n)}</span><span class="mono">${esc(t||'—')}</span></div>`).join('')}</div>
    <div class="card"><h3>الإشعارات</h3>
      <div class="big">${d.notifications.count}</div>
      <div class="row"><span>مهمة/عاجلة</span><span class="${d.notifications.urgent?'warn':'ok'}">${d.notifications.urgent}</span></div>
      <div class="row"><span>أعطال حديثة في السجل</span><span class="${d.errors?'bad':'ok'}">${d.errors}</span></div></div>
    <div class="card"><h3>النظام <span class="badge ro">قراءة فقط</span></h3>
      <div class="row"><span>المعالج</span><span>${pct(sys.cpu)}</span></div><div class="bar"><i style="width:${sys.cpu||0}%"></i></div>
      <div class="row"><span>الذاكرة</span><span>${pct(sys.ram)}</span></div><div class="bar"><i style="width:${sys.ram||0}%"></i></div>
      <div class="row"><span>القرص</span><span>${pct(sys.disk)}</span></div><div class="bar"><i style="width:${sys.disk||0}%"></i></div>
      <div class="row"><span>البطارية</span><span>${sys.battery==null?'—':pct(sys.battery)}</span></div></div>
    <div class="card"><h3>الواجهة البرمجية (remote_api)</h3>
      <div class="row"><span>تعمل الآن</span><span class="${ap?'ok':'dim'}">${ap?'نعم':'لا'}</span></div>
      <div class="row"><span>العنوان المسجّل</span><span class="mono">${esc(d.api.host)}:${esc(d.api.port)}</span></div>
      <div class="row"><span>آخر تحديث للحالة</span><span class="mono">${esc(d.api.updated||'—')}</span></div></div>
  </div>
  <div class="card" style="margin-top:12px"><h3>تشغيل إضافات من اللوحة <span class="badge act">إجراء</span></h3>
    <p class="dim" style="font-size:13px">المسموح تشغيله الآن: ${d.allowlist.length?esc(d.allowlist.join('، ')):'لا شيء — أضف بقولك لجارفس: web_panel allow'}</p>
    <div style="display:flex;gap:8px;flex-wrap:wrap">
      ${d.allowlist.map(n=>`<button class="act ghost" onclick="act('/api/run',{name:'${esc(n)}'},'تشغيل ${esc(n)}')">تشغيل ${esc(n)}</button>`).join('')}
    </div>
    <pre id="runOut" class="mono dim" style="white-space:pre-wrap;margin-top:10px"></pre></div>`;
}
async function viewPlugins(){
  const d = await api('/api/plugins');
  document.getElementById('view').innerHTML = `<div class="card"><h3>الإضافات (${d.total}) — التفعيل إجراء</h3>
  <table><tr><th>الاسم</th><th>الملف</th><th>الحالة</th><th>التحميل</th><th>حزم ناقصة</th><th>إجراء</th></tr>
  ${d.items.map(p=>`<tr><td class="mono">${esc(p.name)}</td><td class="mono dim">${esc(p.file)}</td>
  <td>${p.valid?(p.enabled?'<span class="ok">مفعّلة</span>':'<span class="warn">معطّلة</span>'):'<span class="bad">مرفوضة</span>'}
  ${p.error?`<div class="dim" style="font-size:11px">${esc(p.error)}</div>`:''}</td>
  <td>${p.loaded?'<span class="ok">✔</span>':'<span class="dim">—</span>'}</td>
  <td class="dim" style="font-size:11px">${esc((p.requires&&p.requires.length)?p.requires.join(', '):'—')}</td>
  <td>${p.valid?`<span class="badge act">إجراء</span> <button class="act ${p.enabled?'danger':'ghost'}" onclick="act('/api/plugin',{name:'${esc(p.name)}',enabled:${p.enabled?'false':'true'}},'${p.enabled?'تعطيل':'تفعيل'} ${esc(p.name)}')">${p.enabled?'تعطيل':'تفعيل'}</button>`:''}</td></tr>`).join('')}
  </table></div>`;
}
async function viewAutomations(){
  const d = await api('/api/automations');
  document.getElementById('view').innerHTML = `<div class="card"><h3>الأتمتة (${d.count}) — التشغيل إجراء</h3>
  ${d.count?`<table><tr><th>الاسم</th><th>التوقيت</th><th>الخطوات</th><th>القادم</th><th>آخر تشغيل</th><th>إجراء</th></tr>
  ${d.items.map(a=>`<tr><td>${esc(a.name)}<div class="dim mono" style="font-size:11px">${esc(a.id)}</div></td>
  <td>${esc(a.trigger_text)}</td><td class="dim">${esc((a.steps||[]).join(' ← '))}</td>
  <td class="mono">${esc(a.next_run||'—')}</td><td>${esc(a.last_run||'—')} <span class="${a.last_status==='ok'?'ok':(a.last_status?'bad':'dim')}">${esc(a.last_status||'')}</span></td>
  <td><span class="badge act">إجراء</span> <button class="act" onclick="act('/api/run-now',{kind:'automation',id:'${esc(a.id)}'},'تشغيل أتمتة ${esc(a.name)}')">تشغيل الآن</button>
  <button class="act ${a.enabled?'danger':'ghost'}" onclick="act('/api/toggle',{kind:'automation',id:'${esc(a.id)}',enabled:${a.enabled?'false':'true'}},'${a.enabled?'إيقاف':'تفعيل'} ${esc(a.name)}')">${a.enabled?'إيقاف':'تفعيل'}</button></td></tr>`).join('')}</table>`:'<p class="dim">لا أتمتة بعد.</p>'}</div>`;
}
async function viewWorkflows(){
  const d = await api('/api/workflows');
  document.getElementById('view').innerHTML = `<div class="card"><h3>سير العمل (${d.count})</h3>
  ${d.count?`<table><tr><th>الاسم</th><th>السلسلة</th><th>آخر تشغيل</th><th>إجراء</th></tr>
  ${d.items.map(w=>`<tr><td>${esc(w.name)}<div class="dim mono" style="font-size:11px">${esc(w.id)}</div></td>
  <td class="dim">${esc((w.steps||[]).join(' ← '))}</td>
  <td>${esc(w.last_run||'—')} <span class="${w.last_status==='ok'?'ok':(w.last_status?'bad':'dim')}">${esc(w.last_status||'')}</span></td>
  <td><span class="badge act">إجراء</span> <button class="act" onclick="act('/api/run-now',{kind:'workflow',id:'${esc(w.id)}'},'تشغيل سير العمل ${esc(w.name)}')">تشغيل الآن</button>
  <button class="act ${w.enabled?'danger':'ghost'}" onclick="act('/api/toggle',{kind:'workflow',id:'${esc(w.id)}',enabled:${w.enabled?'false':'true'}},'${w.enabled?'إيقاف':'تفعيل'}')">${w.enabled?'إيقاف':'تفعيل'}</button></td></tr>`).join('')}</table>`:'<p class="dim">لا سير عمل بعد.</p>'}</div>`;
}
async function viewSchedules(){
  const d = await api('/api/schedules');
  document.getElementById('view').innerHTML = `<div class="card"><h3>الجدولات (${d.count})</h3>
  ${d.count?`<table><tr><th>الاسم</th><th>الهدف</th><th>التوقيت</th><th>القادم</th><th>فشل/فائت</th><th>إجراء</th></tr>
  ${d.items.map(s=>`<tr><td>${esc(s.name)}<div class="dim mono" style="font-size:11px">${esc(s.id)}</div></td>
  <td class="mono">${esc(s.target)}</td><td>${esc(s.schedule_text)}</td><td class="mono">${esc(s.next_run||'—')}</td>
  <td><span class="${s.failures?'bad':'dim'}">${s.failures}</span> / <span class="${s.missed?'warn':'dim'}">${s.missed}</span></td>
  <td><span class="badge act">إجراء</span> <button class="act" onclick="act('/api/run-now',{kind:'schedule',id:'${esc(s.id)}'},'تشغيل ${esc(s.name)} الآن')">تشغيل الآن</button>
  <button class="act ${s.enabled?'danger':'ghost'}" onclick="act('/api/toggle',{kind:'schedule',id:'${esc(s.id)}',enabled:${s.enabled?'false':'true'}},'${s.enabled?'إيقاف':'تفعيل'}')">${s.enabled?'إيقاف':'تفعيل'}</button></td></tr>`).join('')}</table>`:'<p class="dim">لا جدولات بعد.</p>'}</div>`;
}
async function viewNotifications(){
  const d = await api('/api/notifications');
  document.getElementById('view').innerHTML = `<div class="card"><h3>الإشعارات <span class="badge ro">قراءة فقط</span> (${d.count||0})</h3>
  ${d.available? (d.items.length? d.items.map(n=>`<div class="row"><div><b>${esc(n.title)}</b><div class="dim" style="font-size:12px">${esc(n.body)}</div></div>
  <div style="text-align:left"><span class="badge">${esc(n.priority)}</span><div class="dim mono" style="font-size:11px">${esc(n.ts)}</div></div></div>`).join('') : '<p class="dim">لا إشعارات.</p>')
  : '<p class="dim">لم أجد مخزن إشعارات (notification_center غير مستخدم بعد).</p>'}</div>`;
}
async function viewErrors(){
  const d = await api('/api/errors');
  document.getElementById('view').innerHTML = `<div class="grid">
  <div class="card"><h3>إضافات مرفوضة (${d.rejected.length})</h3>${d.rejected.length?d.rejected.map(r=>`<div class="row"><span class="mono">${esc(r.file)}</span><span class="bad" style="font-size:12px">${esc(r.error)}</span></div>`).join(''):'<p class="ok">لا شيء</p>'}</div>
  <div class="card"><h3>أعطال تنفيذ حديثة (${d.failures.length})</h3>${d.failures.length?d.failures.map(f=>`<div class="row"><span class="mono">${esc(f.plugin)} · ${esc(f.action)}</span><span class="bad" style="font-size:12px">${esc(f.status)}</span></div>`).join(''):'<p class="ok">لا شيء</p>'}</div></div>`;
}
async function viewLogs(){
  const d = await api('/api/logs?n=80');
  document.getElementById('view').innerHTML = `<div class="card"><h3>السجل المنظَّم (${d.count}) <span class="badge ro">قراءة فقط</span></h3>
  <table><tr><th>الوقت</th><th>الإضافة</th><th>الفعل</th><th>الحالة</th><th>المدة</th><th>خطأ</th></tr>
  ${d.items.map(r=>`<tr><td class="mono">${esc(r.ts)}</td><td class="mono">${esc(r.plugin)}</td><td>${esc(r.action)}</td>
  <td class="${r.status==='ok'?'ok':(r.status==='skipped'?'dim':'bad')}">${esc(r.status)}</td><td class="mono">${esc(r.duration_ms)}ms</td>
  <td class="dim" style="font-size:11px">${esc(r.error||'')}</td></tr>`).join('')}</table></div>`;
}
async function viewMemory(){
  const d = await api('/api/memory');
  document.getElementById('view').innerHTML = `<div class="card"><h3>الذاكرة والملفات <span class="badge ro">قراءة فقط</span></h3>
  <div class="row"><span>مدخلات ذاكرة JARVIS</span><span>${d.memory_entries}</span></div>
  <div class="row"><span>ملاحظات note_manager</span><span>${d.notes}</span></div>
  <div class="row"><span>مستندات قاعدة المعرفة</span><span>${d.kb_documents}</span></div>
  <div class="row"><span>موجزات محفوظة</span><span>${d.briefings}</span></div></div>`;
}
async function viewSystem(){
  const d = await api('/api/system');
  document.getElementById('view').innerHTML = `<div class="card"><h3>النظام <span class="badge ro">قراءة فقط</span></h3>
  <div class="row"><span>المعالج</span><span>${pct(d.cpu)}</span></div><div class="bar"><i style="width:${d.cpu||0}%"></i></div>
  <div class="row"><span>الذاكرة</span><span>${pct(d.ram)}</span></div><div class="bar"><i style="width:${d.ram||0}%"></i></div>
  <div class="row"><span>القرص</span><span>${pct(d.disk)}</span></div><div class="bar"><i style="width:${d.disk||0}%"></i></div>
  <div class="row"><span>البطارية</span><span>${d.battery==null?'—':pct(d.battery)}</span></div>
  <div class="row"><span>منصة التشغيل</span><span class="mono">${esc(d.platform)} · Python ${esc(d.python)}</span></div></div>`;
}
function boot(){
  tabs();
  document.getElementById('foot').textContent = 'التحديث التلقائي كل ' + (REFRESH/1000) + ' ثانية.';
  refresh();
  if (TIMER) clearInterval(TIMER);
  TIMER = setInterval(refresh, REFRESH);
}
(async function start(){
  try{ await api('/api/overview'); showApp(); boot(); }
  catch(e){ showLogin(); }
})();
</script></body></html>"""


# ── فحص ذاتي: python web_panel.py ─────────────────────────────────────────────
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
    print("web_panel — self-test")
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
    import time

    tmp = Path(tempfile.mkdtemp(prefix="wp_test_"))
    real_logs, real_auto, real_wf, real_sched = C.LOG_DIR, C.AUTOMATIONS_FILE, C.WORKFLOWS_FILE, C.SCHEDULES_FILE
    real_stamp, real_data = C._SCAN_CACHE.get("stamp"), C._SCAN_CACHE.get("data")
    base = C.CONFIG_DIR / "api_keys.json"
    backup = base.read_text(encoding="utf-8") if base.exists() else None
    port = C.free_port(8830)
    try:
        C.LOG_DIR = tmp / "logs"
        C.LOG_DIR.mkdir(parents=True, exist_ok=True)
        C.AUTOMATIONS_FILE = tmp / "automations.json"
        C.WORKFLOWS_FILE = tmp / "workflows.json"
        C.SCHEDULES_FILE = tmp / "schedules.json"
        C.save_json(C.AUTOMATIONS_FILE, {"items": {"a1": {"id": "a1", "name": "أتمتة الاختبار", "enabled": True,
                                                          "trigger": {"kind": "daily", "time": "08:00"},
                                                          "steps": [{"plugin": "habit_tracker", "parameters": {"action": "status"}}],
                                                          "run_count": 0}}})
        C.save_json(C.WORKFLOWS_FILE, {"items": {"w1": {"id": "w1", "name": "سلسلة الاختبار", "enabled": True,
                                                         "steps": [{"plugin": "habit_tracker"}]}}})
        C.save_json(C.SCHEDULES_FILE, {"items": {"s1": {"id": "s1", "name": "جدولة الاختبار", "enabled": True,
                                                         "target": {"kind": "plugin", "name": "habit_tracker"},
                                                         "schedule": {"kind": "daily", "time": "09:00"},
                                                         "next_run": C.to_iso(C.now()), "run_count": 0}}})
        C.store_settings(_NS, {"host": "127.0.0.1", "port": str(port), "allow_lan": "no",
                               "refresh_s": "15", "rate_per_min": "300"})

        token = _token()
        check("مفتاح اللوحة أُنشئ", len(token) >= 40 and token.startswith("pnl_"), str(len(token)))
        out = run({"action": "token"})
        check("token لا يكشفه افتراضيًا", token not in out and "…" in out, out[:140])
        check("HTML بلا أي ملف خارجي", "https://" not in _HTML and "http://" not in _HTML and "cdn" not in _HTML.lower())
        check("HTML يُذكر فيه التأخير من الإعدادات", "__REFRESH__" not in _HTML.replace("__REFRESH__", "15"))

        out = run({"action": "start"})
        check("start يعمل ويشرح العنوان", "تعمل" in out and str(port) in out, out[:160])
        time.sleep(0.4)
        # 1) الصفحة عامة، والبيانات محميّة
        code, body = C.http_json(f"http://127.0.0.1:{port}/")
        check("الصفحة تُفتح بلا مفتاح (هيكل فقط)", code == 200 and "لوحة التحكم" in str(body), f"{code}")
        check("الصفحة فيها قسم الدخول", "دخول لوحة JARVIS" in str(body))
        check("بلا جلسة: البيانات 401", C.http_json(f"http://127.0.0.1:{port}/api/overview")[0] == 401)
        code, _ = C.http_json(f"http://127.0.0.1:{port}/login", {"token": "wrong"})
        check("دخول بمفتاح خطأ → 401", code == 401, str(code))
        code, body = C.http_json(f"http://127.0.0.1:{port}/login", {"token": token})
        check("دخول صحيح → كوكي جلسة", code == 200 and body.get("ok") is True, str(body))
        cookie = f"{_COOKIE}={token}"
        # 2) البيانات بالكوكي
        code, body = C.http_json(f"http://127.0.0.1:{port}/api/overview", headers={"Cookie": cookie})
        check("نظرة عامة تعمل بالكوكي", code == 200 and body.get("ok") is True, f"{code} {str(body)[:120]}")
        check("النظرة فيها الإضافات والنظام والواجهة",
              all(k in body for k in ("plugins", "system", "api", "automations", "schedules")), str(list(body))[:160])
        check("عدّ الإضافات الحقيقي", body["plugins"]["total"] > 40, str(body["plugins"]))
        code, body = C.http_json(f"http://127.0.0.1:{port}/api/plugins", headers={"Cookie": cookie})
        check("قائمة الإضافات كاملة", code == 200 and body.get("total", 0) > 40 and body.get("items"), str(code))
        code, body = C.http_json(f"http://127.0.0.1:{port}/api/schedules", headers={"Cookie": cookie})
        check("الجدولات تُعرض بموعدها القادم", code == 200 and body["count"] == 1 and body["items"][0]["next_run"], str(body)[:160])
        code, body = C.http_json(f"http://127.0.0.1:{port}/api/automations", headers={"Cookie": cookie})
        check("الأتمتة تُعرض بتوقيتها", code == 200 and "يوميًا" in body["items"][0]["trigger_text"], str(body["items"])[:160])
        code, body = C.http_json(f"http://127.0.0.1:{port}/api/logs", headers={"Cookie": cookie})
        check("السجل يعمل", code == 200 and "items" in body, str(code))
        code, body = C.http_json(f"http://127.0.0.1:{port}/api/memory", headers={"Cookie": cookie})
        check("الذاكرة تُقرأ", code == 200 and "notes" in body, str(code))
        # 3) الإجراءات
        code, body = C.http_json(f"http://127.0.0.1:{port}/api/toggle",
                                 {"kind": "automation", "id": "a1", "enabled": False},
                                 headers={"Cookie": cookie})
        check("إيقاف أتمتة من اللوحة", code == 200 and body.get("ok"), str(body))
        check("التغيير وصل للملف فعلًا", C.load_json(C.AUTOMATIONS_FILE, {})["items"]["a1"]["enabled"] is False)
        code, body = C.http_json(f"http://127.0.0.1:{port}/api/run-now", {"kind": "automation", "id": "a1"},
                                 headers={"Cookie": cookie})
        check("تشغيل الآن ينفّذ الأتمتة", code == 200 and body.get("ok"), f"{code} {str(body)[:160]}")
        check("الأتمتة سُجّلت تنفيذها", C.load_json(C.AUTOMATIONS_FILE, {})["items"]["a1"]["run_count"] == 1)
        code, body = C.http_json(f"http://127.0.0.1:{port}/api/plugin",
                                 {"name": "system_health", "enabled": False}, headers={"Cookie": cookie})
        check("تعطيل إضافة من اللوحة", code == 200 and body.get("ok") and C.plugin_enabled("system_health") is False, str(body))
        code, _ = C.http_json(f"http://127.0.0.1:{port}/api/plugin",
                              {"name": "system_health", "enabled": True}, headers={"Cookie": cookie})
        check("إعادة تفعيلها", C.plugin_enabled("system_health") is True)
        code, body = C.http_json(f"http://127.0.0.1:{port}/api/plugin",
                                 {"name": "web_panel", "enabled": False}, headers={"Cookie": cookie})
        check("لا تُعطّل اللوحة نظيرها الإداري", code == 403, f"{code} {body}")
        # 4) قائمة التشغيل
        code, body = C.http_json(f"http://127.0.0.1:{port}/api/run", {"name": "note_manager", "parameters": {"action": "list"}},
                                 headers={"Cookie": cookie})
        check("إضافة خارج القائمة مرفوضة", code == 403, f"{code} {body}")
        code, body = C.http_json(f"http://127.0.0.1:{port}/api/run", {"name": "system_health", "parameters": {"action": "cpu"}},
                                 headers={"Cookie": cookie})
        check("إضافة في القائمة تُشغَّل", code == 200 and body.get("ok") and "CPU" in body.get("result", ""),
              f"{code} {str(body)[:160]}")
        code, body = C.http_json(f"http://127.0.0.1:{port}/api/run", {"name": "plugin_manager", "parameters": {"action": "list"}},
                                 headers={"Cookie": cookie})
        check("الإدارية مرفوضة في اللوحة أيضًا", code == 403, f"{code} {body}")
        out = run({"action": "allow", "allow_action": "add", "name": "note_manager"})
        check("allow add يضيف", "صارت اللوحة" in out, out[:120])
        out = run({"action": "allow", "allow_action": "list"})
        check("allow list يعرض", "note_manager" in out, out[:140])
        # 5) الأخطاء والإيقاف
        code, body = C.http_json(f"http://127.0.0.1:{port}/api/errors", headers={"Cookie": cookie})
        check("صفحة الأخطاء تعمل", code == 200 and "rejected" in body, str(code))
        out = run({"action": "status"})
        check("status يشرح الحالة", "تعمل" in out and "قائمة التشغيل" in out, out[:200])
        out = run({"action": "stop"})
        check("stop يعمل", "أوقفت" in out, out[:80])
        check("بعد الإيقاف الصفحة لا تستجيب", C.http_json(f"http://127.0.0.1:{port}/")[0] == 0)
        check("فعل مجهول", "ليس فعلًا" in run({"action": "zzz"}))
    finally:
        try:
            if _SERVER.get("http") is not None:
                _SERVER["http"].stop()
        except Exception:
            pass
        _SERVER["http"] = None
        C.LOG_DIR, C.AUTOMATIONS_FILE, C.WORKFLOWS_FILE, C.SCHEDULES_FILE = real_logs, real_auto, real_wf, real_sched
        C._SCAN_CACHE["stamp"], C._SCAN_CACHE["data"] = real_stamp, real_data
        if backup is not None:
            base.write_text(backup, encoding="utf-8")
        elif base.exists():
            base.unlink(missing_ok=True)
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
        for leftover in ("web_panel_state.json", "web_panel_token.json"):
            try:
                (C.ORCH_DIR / leftover).unlink(missing_ok=True)
            except Exception:
                pass
        try:
            C.scan_plugins(force=True)
        except Exception:
            pass

    print(f"RESULT: {'PASS' if problems == 0 else str(problems) + ' problem(s)'}")
    return problems


if __name__ == "__main__":
    raise SystemExit(1 if _selftest() else 0)
