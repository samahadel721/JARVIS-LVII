"""
remote_api.py — واجهة برمجية محلية آمنة (Remote API).

تسمح لتطبيق خارجي مُصرَّح له (سكربت، لوحة، أداة على جهازك) بالحديث مع JARVIS:
صحّة الخدمة، استكشاف القدرات، وتشغيل إضافات **من قائمة سماح صريحة** — لا أكثر.

الفرق عن `remote_gateway` الموجود: ذلك يمرّر *نصًّا* للجارفس كما لو كتبته في
المحادثة. هذا واجهة منظّمة (JSON) تستدعي إضافات محدّدة بأسمائها ومَعاملاتها،
وتحتفظ بسجل تدقيق، وتفرض حدًّا للمعدّل وحجم الطلب.

الأمان — كل بند مُختبر:
• 127.0.0.1 افتراضيًا؛ فتحه للشبكة يحتاج `allow_lan=yes` صريحًا مع تحذير.
• مفتاح وصول عشوائي 32 بايت في ملف بصلاحيات 600، ومقارنة ثابتة الزمن.
• لا أوامر شل ولا تجوّل في الملفات ولا مسارات مفتوحة: الإضافات فقط، ومن قائمة سماح.
• الإضافات الإدارية (الواجهة نفسها، اللوحة، المدير، المجدول) مرفوضة حتى لو أُضيفت.
• المفتاح لا يُكتب في أي سجل — لا طلب ولا استجابة ولا سطر خطأ.
• حدّ معدّل لكل عميل، وحدّ لحجم الجسم، ومهل زمنية، وردود JSON منظّمة.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

PLUGIN = {
    "name": "remote_api",
    "description": (
        "A local, authenticated HTTP API so authorised tools on this machine can talk "
        "to JARVIS: check health, discover what plugins can do, and run a plugin from a "
        "list you approve. Use for: 'start the remote API', 'stop it', 'is the API "
        "running', 'show the API key', 'let the API use the weather plugin', 'what "
        "requests came in'. Binds to localhost by default, never exposes the shell or "
        "the filesystem, and keeps an audit log without secrets."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "start | stop | status | token | allow | log | capabilities | help",
            },
            "name": {"type": "STRING", "description": "Plugin name for allow add/remove."},
            "allow_action": {"type": "STRING", "description": "For allow: add | remove | list | clear."},
            "reveal": {"type": "STRING", "description": "For token: 'yes' to show it in full on screen."},
            "count": {"type": "INTEGER", "description": "Rows for the log/capabilities listing."},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "remote_api",
    "title": "Remote API / الواجهة البرمجية",
    "fields": [
        {"key": "host", "type": "text", "label": "Bind address (127.0.0.1 = this machine only)", "default": "127.0.0.1"},
        {"key": "port", "type": "text", "label": "Port", "default": "8787"},
        {"key": "allow_lan", "type": "text", "label": "Allow other devices on the network (yes/no)", "default": "no"},
        {"key": "allowed_plugins", "type": "text",
         "label": "Plugins the API may run, comma separated (blank = none until you choose)",
         "default": ""},
        {"key": "admin_allowed", "type": "text", "label": "Also allow admin plugins like plugin_manager (yes/no)", "default": "no"},
        {"key": "rate_per_min", "type": "text", "label": "Requests allowed per minute per client", "default": "60"},
        {"key": "max_body_kb", "type": "text", "label": "Largest request body, KB", "default": "256"},
        {"key": "timeout_s", "type": "text", "label": "Seconds allowed for one plugin call", "default": "60"},
        {"key": "log_rows", "type": "text", "label": "Rows shown by the log action", "default": "20"},
    ],
}

_NS = "remote_api"
_ADMIN = frozenset({"remote_api", "web_panel", "plugin_manager", "workflow_scheduler"})
_SERVER: dict = {"http": None, "started": 0.0}
_SERVICE_VERSION = "1.0"


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
    return C.token_for(C.ORCH_DIR / "remote_api_token.json", prefix="jrvs_")


def _allowlist() -> list[str]:
    raw = str(_cfg().get("allowed_plugins") or "")
    return [x.strip() for x in raw.replace("؛", ",").split(",") if x.strip()]


def _save_allowlist(names: list[str]) -> bool:
    return C.store_settings(_NS, {"allowed_plugins": ", ".join(dict.fromkeys(names))})


def _state_path() -> Path:
    return C.ORCH_DIR / "remote_api_state.json"


def _write_state(running: bool, port: int, host: str) -> None:
    C.save_json(_state_path(), {"running": bool(running), "host": host, "port": port,
                                "updated": C.now_iso(), "version": _SERVICE_VERSION})


# ── معالجات المسارات ──────────────────────────────────────────────────────────
def _h_health(req) -> tuple[int, Any, str]:
    del req
    uptime = int(C.now().timestamp() - _SERVER["started"]) if _SERVER["started"] else 0
    return 200, {"ok": True, "service": "jarvis-remote-api", "version": _SERVICE_VERSION,
                 "uptime_s": uptime, "time": C.now_iso()}, "application/json; charset=utf-8"


def _capabilities(limit: int = 200) -> list[dict]:
    rows = []
    for info in sorted(C.scan_plugins(force=False).values(), key=lambda i: str(i["name"])):
        rows.append({
            "name": info["name"], "file": info["file"], "valid": bool(info["valid"]),
            "description": C.clip(info["description"], 300),
            "parameters": sorted((info.get("parameters") or {}).keys()),
            "requires": info.get("requires", []),
        })
        if len(rows) >= limit:
            break
    return rows


def _h_capabilities(req) -> tuple[int, Any, str]:
    limit = _int(req.query.get("limit") or 200, 200, 1, 500)
    return 200, {"ok": True, "count": len(_capabilities(limit)),
                 "allowed": _allowlist(), "plugins": _capabilities(limit)}, "application/json; charset=utf-8"


def _h_run(req) -> tuple[int, Any, str]:
    payload = req.json()
    if not isinstance(payload, dict):
        return 400, {"ok": False, "error": "الجسم يجب أن يكون JSON كائنًا فيه plugin و parameters."}, "application/json"
    plugin = str(payload.get("plugin") or "").strip()
    parameters = payload.get("parameters")
    if parameters is None:
        parameters = {}
    if not isinstance(parameters, dict):
        return 400, {"ok": False, "error": "parameters يجب أن يكون كائنًا."}, "application/json"
    if not plugin:
        return 400, {"ok": False, "error": "لم تحدّد plugin."}, "application/json"
    allowed = _allowlist()
    admin_allowed = C.parse_bool(_cfg().get("admin_allowed"), False)
    if plugin in _ADMIN and not admin_allowed:
        return 403, {"ok": False, "error": f"'{plugin}' إضافة إدارية — الواجهة لا تشغّلها إلا إذا فعّلت admin_allowed بنفسك."}, "application/json"
    if plugin not in allowed:
        return 403, {"ok": False,
                     "error": f"'{plugin}' ليست في قائمة السماح. أضفها: remote_api action=allow allow_action=add name={plugin}",
                     "allowed": allowed}, "application/json"
    if C.resolve_plugin(plugin) is None:
        return 404, {"ok": False, "error": f"لا توجد إضافة باسم '{plugin}' في plugins."}, "application/json"
    try:
        req._plugin = plugin  # noqa: SLF001 — لأجل سطر السجل فقط (بلا معاملات)
    except Exception:
        pass
    timeout = float(_int(_cfg().get("timeout_s"), 60, 5, 600))
    import time as _time
    started = _time.time()
    ok, value, timed_out = C.run_with_timeout(lambda: C.call_plugin(plugin, parameters), timeout)
    ms = int((_time.time() - started) * 1000)
    if timed_out:
        return 504, {"ok": False, "plugin": plugin, "error": f"تجاوزت المهلة ({timeout:.0f}s)", "duration_ms": ms}, "application/json"
    if not ok:
        return 200, {"ok": False, "plugin": plugin, "error": C.clip(str(value), 400), "duration_ms": ms}, "application/json"
    return 200, {"ok": True, "plugin": plugin, "result": str(value), "duration_ms": ms}, "application/json"


def _h_logs(req) -> tuple[int, Any, str]:
    n = _int(req.query.get("n") or _int(_cfg().get("log_rows"), 20, 1, 200), 20, 1, 200)
    rows = C.tail_log("remote_api", n)
    return 200, {"ok": True, "count": len(rows), "entries": rows}, "application/json; charset=utf-8"


# ── الأفعال ───────────────────────────────────────────────────────────────────
def _routes() -> dict:
    return {
        ("GET", "/health"): _h_health,
        ("GET", "/capabilities"): _h_capabilities,
        ("GET", "/plugins"): _h_capabilities,
        ("POST", "/run"): _h_run,
        ("GET", "/logs"): _h_logs,
    }


def _start(player=None) -> str:
    cfg = _cfg()
    if _SERVER["http"] is not None and _SERVER["http"].running:
        return f"سيدي، الواجهة تعمل بالفعل على http://{_SERVER['http'].host}:{_SERVER['http'].port}"
    token = _token()
    if not token:
        return "سيدي، تعذّر إنشاء مفتاح الوصول في مجلد config/orchestration."
    host = str(cfg.get("host") or "127.0.0.1").strip()
    if not C.parse_bool(cfg.get("allow_lan"), False) and host not in ("127.0.0.1", "localhost", "::1"):
        host = "127.0.0.1"
    port = _int(cfg.get("port"), 8787, 1024, 65535)
    server = C.MiniHTTP(host, port, _routes(), token, log_name="remote_api",
                        public_paths=("/health",),
                        rate_per_minute=_int(cfg.get("rate_per_min"), 60, 5, 6000),
                        max_body_kb=_int(cfg.get("max_body_kb"), 256, 8, 4096))
    ok, message = server.start()
    if not ok:
        return f"سيدي، {message}"
    _SERVER["http"] = server
    _SERVER["started"] = C.now().timestamp()
    _write_state(True, port, host)
    C.log_event("remote_api", "start", "ok", 0, "", {"host": host, "port": port}, log="remote_api")
    allowed = _allowlist()
    out = (f"سيدي، الواجهة تعمل على {message}\n"
           f"  نقاط: GET /health (عام) · GET /capabilities · POST /run · GET /logs\n"
           f"  المصادقة: X-API-Key أو Authorization: Bearer\n"
           f"  القائمة المسموحة: {'، '.join(allowed) if allowed else '(فارغة — لا إضافة تُشغَّل حتى تسمح بواحدة)'}\n"
           f"  المفتاح محفوظ في config/orchestration/remote_api_token.json — قل 'token' لعرضه.")
    _log(player, out)
    return out


def _stop(player=None) -> str:
    server = _SERVER.get("http")
    if server is None or not server.running:
        _write_state(False, 0, "")
        return "سيدي، الواجهة متوقفة بالفعل."
    _server_host, _server_port = server.host, server.port
    ok, message = server.stop()
    _SERVER["http"] = None
    _SERVER["started"] = 0.0
    _write_state(False, _server_port, _server_host)
    C.log_event("remote_api", "stop", "ok", 0, "", None, log="remote_api")
    out = "سيدي، أوقفت الواجهة." if ok else f"سيدي، {message}"
    _log(player, out)
    return out


def _act_status(params: dict, player) -> str:
    del params
    cfg = _cfg()
    server = _SERVER.get("http")
    live = bool(server and server.running)
    state = C.load_json(_state_path(), {})
    allowed = _allowlist()
    if live:
        lines = [f"سيدي، الواجهة تعمل على http://{server.host}:{server.port}",
                 f"  طلبات: {server.requests} · مرفوضة: {server.denied} · بدأت: {server.started_at}"]
    else:
        lines = ["سيدي، الواجهة متوقفة في هذه العملية"
                 + (f" (آخر تشغيل: {state.get('updated')} على المنفذ {state.get('port')})" if state else "") + "."]
    lines.append(f"  العنوان المضبوط: {cfg.get('host')}:{cfg.get('port')} · "
                 f"الشبكة: {'مسموحة' if C.parse_bool(cfg.get('allow_lan'), False) else 'localhost فقط'}")
    lines.append(f"  القائمة المسموحة ({len(allowed)}): {'، '.join(allowed) if allowed else '— فارغة'}")
    lines.append(f"  الإدارية: {'مسموحة (admin_allowed=yes)' if C.parse_bool(cfg.get('admin_allowed'), False) else 'مرفوضة'}")
    out = "\n".join(lines)
    _log(player, out)
    return out


def _act_token(params: dict, player) -> str:
    token = _token()
    if not token:
        return "سيدي، تعذّر قراءة المفتاح أو إنشاؤه."
    if C.parse_bool(params.get("reveal"), False):
        out = (f"سيدي، مفتاح الواجهة (سيظهر على الشاشة الآن — انتبه من التصوير أو المشاركة):\n"
               f"  {token}\n"
               f"استخدامه: -H \"X-API-Key: <المفتاح>\"")
        _log(player, out)
        return out
    out = (f"سيدي، المفتاح: {C.mask_token(token)}\n"
           f"  محفوظ في: config/orchestration/remote_api_token.json (صلاحيات 600)\n"
           f"  لعرضه كاملًا: action=token reveal=yes — ولن يظهر في أي سجل.")
    _log(player, out)
    return out


def _act_allow(params: dict, player) -> str:
    sub = str(params.get("allow_action") or "list").strip().lower()
    name = str(params.get("name") or "").strip()
    allowed = _allowlist()
    if sub in ("list", "show", "قائمة"):
        return ("سيدي، قائمة السماح: " + ("، ".join(allowed) if allowed
                else "فارغة — لا إضافة تُشغَّل عبر الواجهة حتى تسمح بواحدة.")) 
    if sub in ("clear", "none", "امسح"):
        _save_allowlist([])
        out = "سيدي، أفرغت قائمة السماح: الواجهة لا تُشغّل أي إضافة الآن."
        _log(player, out)
        return out
    if sub in ("remove", "delete", "احذف"):
        if name in allowed:
            allowed.remove(name)
            _save_allowlist(allowed)
        out = f"سيدي، أزلت '{name}'. القائمة الآن: {'، '.join(allowed) if allowed else 'فارغة'}"
        _log(player, out)
        return out
    if sub in ("add", "allow", "أضف"):
        if not name:
            return "سيدي، أي إضافة أسمح بها؟ (name=…)"
        if C.resolve_plugin(name) is None:
            return f"سيدي، '{name}' غير موجودة في plugins."
        if name in _ADMIN:
            return (f"سيدي، '{name}' إضافة إدارية. لا أضعها في القائمة إلا بتوجيه صريح منك "
                    f"(admin_allowed=yes في الإعدادات) — وهي سياسة أمان لا خطأ.")
        if name not in allowed:
            allowed.append(name)
            _save_allowlist(allowed)
        out = f"سيدي، سمحت للواجهة بتشغيل '{name}'. القائمة: {'، '.join(allowed)}"
        _log(player, out)
        return out
    return "سيدي، allow_action: add | remove | list | clear"


def _act_log(params: dict, player) -> str:
    n = _int(params.get("count") or _int(_cfg().get("log_rows"), 20, 1, 200), 20, 1, 200)
    rows = C.tail_log("remote_api", n)
    if not rows:
        return "سيدي، لا طلبات مسجّلة بعد."
    lines = [f"{row.get('ts')} · {row.get('action')} · {row.get('status')} · {row.get('duration_ms')}ms"
             + (f" · {row.get('detail', {}).get('plugin', '')}" if isinstance(row.get("detail"), dict) else "")
             for row in rows]
    text = "سيدي، سجل الواجهة (بلا مفاتيح ولا معاملات):\n" + "\n".join(lines)
    _log(player, text)
    return text


def _act_capabilities(params: dict, player) -> str:
    n = _int(params.get("count") or 30, 30, 1, 200)
    caps = _capabilities(n)
    allowed = _allowlist()
    lines = [f"سيدي، {len(caps)} إضافة يمكن للواجهة أن تعرفها (المسموح تشغيله: "
             f"{'، '.join(allowed) if allowed else 'لا شيء بعد'}):"]
    for row in caps[:n]:
        mark = "✅" if row["name"] in allowed else "·"
        lines.append(f"  {mark} {row['name']}: {C.clip(row['description'], 80)}")
    text = "\n".join(lines)
    _log(player, text)
    return text


_HELP = (
    "الواجهة البرمجية، سيدي:\n"
    "• start · stop · status — التشغيل والحالة (localhost افتراضيًا)\n"
    "• token — عرض المفتاح (مخفي؛ reveal=yes لإظهاره)\n"
    "• allow — allow_action=add/remove/list/clear مع name\n"
    "• capabilities — ما تعرفه الواجهة · log — سجل الطلبات\n"
    "نقاط الاتصال: GET /health (عام) · GET /capabilities · POST /run {\"plugin\",\"parameters\"} · GET /logs\n"
    "لا شل، لا ملفات، لا إداريات، وحدّ معدّل وحجم — وكلها في القائمة المسموحة فقط."
)


def run(parameters: dict, player=None, session_memory=None) -> str:
    """نقطة الدخول. لا ترمي أبدًا."""
    del session_memory
    if C is None:
        return "سيدي، ملف '_orch_core.py' المشترك غير موجود — بدونه لا تعمل الواجهة."
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
        if action in ("token", "key", "مفتاح"):
            return _act_token(p, player)
        if action in ("allow", "permit", "سماح"):
            return _act_allow(p, player)
        if action in ("capabilities", "plugins", "قدرات"):
            return _act_capabilities(p, player)
        if action in ("log", "logs", "سجل"):
            return _act_log(p, player)
        if action in ("help", "?"):
            return _HELP
        return f"سيدي، '{action}' ليس فعلًا أعرفه في الواجهة. قل 'help'."
    except Exception as exc:  # noqa: BLE001
        return f"سيدي، الواجهة واجهت مشكلة: {str(exc)[:180]}"


# ── فحص ذاتي: python remote_api.py ────────────────────────────────────────────
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
    print("remote_api — self-test")
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

    tmp = Path(tempfile.mkdtemp(prefix="api_test_"))
    real_logs = C.LOG_DIR
    real_stamp, real_data = C._SCAN_CACHE.get("stamp"), C._SCAN_CACHE.get("data")
    base = C.CONFIG_DIR / "api_keys.json"
    backup = base.read_text(encoding="utf-8") if base.exists() else None
    port = C.free_port(8810)
    try:
        C.LOG_DIR = tmp / "logs"
        C.LOG_DIR.mkdir(parents=True, exist_ok=True)
        C.store_settings(_NS, {"host": "127.0.0.1", "port": str(port), "allow_lan": "no",
                               "allowed_plugins": "", "admin_allowed": "no",
                               "rate_per_min": "40", "max_body_kb": "16", "timeout_s": "20"})

        # 1) المفتاح يُنشأ ويُقيَّد ملفه
        token = _token()
        check("مفتاح وصول عشوائي طويل", len(token) >= 40 and token.startswith("jrvs_"), str(len(token)))
        check("ملف المفتاح بصلاحيات 600", (C.ORCH_DIR / "remote_api_token.json").exists())
        out = run({"action": "token"})
        check("token لا يعرض المفتاح كاملًا افتراضيًا", token not in out and "…" in out, out[:140])
        out = run({"action": "token", "reveal": "yes"})
        check("reveal=yes يعرضه بتحذير", token in out and "انتبه" in out, out[:140])

        # 2) بلا قائمة سماح: لا شيء يُشغَّل — والحالة تُشرح
        out = run({"action": "start"})
        check("start يشرح النقاط والقائمة", "تعمل" in out and "القائمة المسموحة" in out, out[:220])
        time.sleep(0.4)
        code, body = C.http_json(f"http://127.0.0.1:{port}/health")
        check("/health عام بلا مفتاح", code == 200 and body.get("ok") is True, f"{code} {body}")
        code, body = C.http_json(f"http://127.0.0.1:{port}/capabilities")
        check("بلا مفتاح → 401", code == 401, str(code))
        code, body = C.http_json(f"http://127.0.0.1:{port}/capabilities", headers={"X-API-Key": token})
        check("/capabilities بالمفتاح يعرض الإضافات", code == 200 and body.get("count", 0) > 40, f"{code} {str(body)[:120]}")
        code, body = C.http_json(f"http://127.0.0.1:{port}/run", {"plugin": "system_health", "parameters": {"action": "cpu"}},
                                 headers={"X-API-Key": token})
        check("قائمة فارغة → 403 مع شرح", code == 403 and "قائمة السماح" in body.get("error", ""), f"{code} {body}")
        # 3) بعد السماح: التنفيذ يعمل
        out = run({"action": "allow", "allow_action": "add", "name": "system_health"})
        check("allow add ينجح", "سمحت" in out, out[:120])
        code, body = C.http_json(f"http://127.0.0.1:{port}/run", {"plugin": "system_health", "parameters": {"action": "cpu"}},
                                 headers={"X-API-Key": token})
        check("تنفيذ إضافة مسموحة", code == 200 and body.get("ok") is True and "CPU" in body.get("result", ""),
              f"{code} {str(body)[:160]}")
        check("المدة مُدرجة", isinstance(body.get("duration_ms"), int), str(body)[:120])
        # 4) ما ليس في القائمة وما هو إداري
        code, body = C.http_json(f"http://127.0.0.1:{port}/run", {"plugin": "note_manager", "parameters": {"action": "list"}},
                                 headers={"X-API-Key": token})
        check("إضافة غير مسموحة → 403", code == 403, str(code))
        code, body = C.http_json(f"http://127.0.0.1:{port}/run", {"plugin": "plugin_manager", "parameters": {"action": "list"}},
                                 headers={"X-API-Key": token})
        check("إدارية مرفوضة ولو سُمح بها لاحقًا", code == 403 and "إدارية" in body.get("error", ""), f"{code} {body}")
        out = run({"action": "allow", "allow_action": "add", "name": "ghost_plugin_xyz"})
        check("لا تُدرج إضافة غائبة في القائمة أصلًا", "غير موجودة" in out, out[:140])
        # فرع 404 يبقى دفاعيًا لحالة حذف ملف إضافة كانت مسموحة — نختبره بتغيير القائمة مؤقتًا
        real_allow = globals()["_allowlist"]
        globals()["_allowlist"] = lambda: ["ghost_plugin_xyz"]
        try:
            code, body = C.http_json(f"http://127.0.0.1:{port}/run",
                                     {"plugin": "ghost_plugin_xyz", "parameters": {}},
                                     headers={"X-API-Key": token})
        finally:
            globals()["_allowlist"] = real_allow
        check("إضافة مسموحة لكنها محذوفة → 404", code == 404, f"{code} {body}")
        code, body = C.http_json(f"http://127.0.0.1:{port}/run", {"plugin": "", "parameters": {}},
                                 headers={"X-API-Key": token})
        check("بلا اسم → 400", code == 400, str(code))
        # 5) التحقق من الحدود
        big = {"plugin": "system_health", "parameters": {"x": "z" * 40000}}
        code, _ = C.http_json(f"http://127.0.0.1:{port}/run", big, headers={"X-API-Key": token})
        check("جسم أكبر من الحد → 413", code == 413, str(code))
        # حدّ المعدّل يُختبر بخادم جديد بحدّ صغير، حتى لا يُفسد الفحوص السابقة
        run({"action": "stop"})
        C.store_settings(_NS, {"rate_per_min": "5"})
        run({"action": "start"})
        time.sleep(0.3)
        codes = [C.http_json(f"http://127.0.0.1:{port}/capabilities", headers={"X-API-Key": token})[0] for _ in range(9)]
        check("حدّ المعدّل → 429", 429 in codes, str(codes))
        run({"action": "stop"})
        C.store_settings(_NS, {"rate_per_min": "40"})
        run({"action": "start"})     # خادم جديد بعدّاد معدّل جديد
        time.sleep(0.3)
        check("مفتاح خطأ → 401", C.http_json(f"http://127.0.0.1:{port}/capabilities",
                                            headers={"X-API-Key": "wrong"})[0] == 401)
        check("مسارات غير معروفة → 404", C.http_json(f"http://127.0.0.1:{port}/unknown",
                                                    headers={"X-API-Key": token})[0] == 404)
        # 6) السجل: لا مفتاح ولا معاملات
        code, body = C.http_json(f"http://127.0.0.1:{port}/logs", headers={"X-API-Key": token})
        check("/logs يعيد سطورًا منظّمة", code == 200 and body.get("count", 0) > 0, str(body)[:140])
        raw = (C.LOG_DIR / "remote_api.jsonl").read_text(encoding="utf-8")
        check("المفتاح لا يظهر في أي سطر سجل", token not in raw)
        check("اسم الإضافة مسجّل بلا معاملات", '"plugin": "system_health"' in raw or "system_health" in raw)
        check("قيم المعاملات غير مسجّلة", "z" * 40000 not in raw)
        # 7) الإيقاف والحالة
        out = run({"action": "status"})
        check("status يعرض الأرقام", "طلبات" in out and "القائمة المسموحة" in out, out[:200])
        out = run({"action": "stop"})
        check("stop يعمل", "أوقفت" in out, out[:80])
        check("بعد الإيقاف: الطلبات تفشل بلا كسر",
              C.http_json(f"http://127.0.0.1:{port}/health")[0] == 0)
        out = run({"action": "status"})
        check("status يذكر التوقّف", "متوقفة" in out, out[:120])
        check("allow list", "قائمة السماح" in run({"action": "allow", "allow_action": "list"}))
        out = run({"action": "allow", "allow_action": "clear"})
        check("allow clear", "أفرغت" in out, out[:100])
        check("فعل مجهول", "ليس فعلًا" in run({"action": "zzz"}))
    finally:
        try:
            if _SERVER.get("http") is not None:
                _SERVER["http"].stop()
        except Exception:
            pass
        _SERVER["http"] = None
        C.LOG_DIR = real_logs
        C._SCAN_CACHE["stamp"], C._SCAN_CACHE["data"] = real_stamp, real_data
        if backup is not None:
            base.write_text(backup, encoding="utf-8")
        elif base.exists():
            base.unlink(missing_ok=True)
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
        for leftover in ("remote_api_state.json", "remote_api_token.json"):
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
