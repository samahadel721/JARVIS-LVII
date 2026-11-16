"""
_orch_core.py — النواة المشتركة لحزمة التنسيق (Automation / Workflow / Scheduler / API / Panel).

هذا ملف مشترك يبدأ بشرطة سفلية، تمامًا مثل `_net_core.py` و`_whatsapp_core.py`،
فيلتقطه مُحمِّل الإضافات ويتجاهله — لا يُسجَّل كإضافة مستقلة ولا يُنادى من الصوت.

لماذا نواة واحدة؟ لأن تسعة مكوّنات تشترك في نفس الأشياء بالضبط: قراءة الإعدادات من
`memory/config_manager`، تخزين JSON آمن، سجلات منظّمة بصيغة JSONL، نداء الإضافات
الشقيقة عبر مسارها، حساب مواعيد التنفيذ، خادم HTTP صغير موثَّق، وحجب الأسرار من
السجلات. لو كُتب كل ذلك تسع مرات لتباعدت النسخ وتسلل عيب واحد إلى تسعة ملفات.

لا اعتماديات خارجية هنا: مكتبة بايثون القياسية فقط.
"""
from __future__ import annotations

import ast
import contextlib
import datetime as _dt
import hashlib
import importlib.util
import inspect
import json
import os
import re
import secrets
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

# ── المواضع ───────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent        # جذر مشروع Mark-LV
PLUGINS_DIR = BASE_DIR / "plugins"
ACTIONS_DIR = BASE_DIR / "actions"
CONFIG_DIR = BASE_DIR / "config"
ORCH_DIR = CONFIG_DIR / "orchestration"                  # كل بيانات الحزمة هنا
LOG_DIR = ORCH_DIR / "logs"

AUTOMATIONS_FILE = ORCH_DIR / "automations.json"
WORKFLOWS_FILE = ORCH_DIR / "workflows.json"
SCHEDULES_FILE = ORCH_DIR / "schedules.json"
CALENDAR_FILE = ORCH_DIR / "calendar.json"
HOME_FILE = ORCH_DIR / "home_devices.json"
REMOTE_STATE_FILE = ORCH_DIR / "remote_api_state.json"

# الإضافات الإدارية: لا يجوز أن تستدعيها الأتمتة أو سير العمل على نفسها
ADMIN_PLUGINS = frozenset({
    "plugin_manager", "remote_api", "web_panel", "workflow_scheduler",
})


# ── أدوات عامة ────────────────────────────────────────────────────────────────
def ensure_dirs() -> None:
    """ينشئ مجلدات الحزمة إن لم تكن موجودة. لا يرمي أبدًا."""
    for d in (ORCH_DIR, LOG_DIR):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass


def load_json(path: Path, default: Any) -> Any:
    """يقرأ JSON بأمان؛ يعيد `default` عند أي فشل (ملف غائب، تالف، أو غير متوقع)."""
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return data
    except Exception:
        pass
    return default


def save_json(path: Path, data: Any, private: bool = False) -> bool:
    """كتابة ذرّية: ملف مؤقت ثم استبدال. `private=True` يضبط الصلاحيات على 600."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        if private:
            try:
                os.chmod(path, 0o600)
            except Exception:
                pass
        return True
    except Exception:
        return False


def now() -> _dt.datetime:
    """الوقت المحلي الحالي (بلا منطقة زمنية — كل الجدولة بالتوقيت المحلي للجهاز)."""
    return _dt.datetime.now()


def now_iso() -> str:
    return now().replace(microsecond=0).isoformat(sep=" ")


def to_iso(dt: Optional[_dt.datetime]) -> str:
    return dt.replace(microsecond=0).isoformat(sep=" ") if dt else ""


def from_iso(text: Any) -> Optional[_dt.datetime]:
    """يحوّل نصًّا إلى datetime؛ يتقبّل الصيغة التي نكتبها وأي ISO قياسي."""
    if not text:
        return None
    if isinstance(text, _dt.datetime):
        return text.replace(microsecond=0, tzinfo=None)
    try:
        dt = _dt.datetime.fromisoformat(str(text).strip())
        return dt.replace(microsecond=0, tzinfo=None)
    except Exception:
        return None


def parse_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on", "نعم", "صحيح", "مفعّل", "مفعل")


def clip(text: Any, limit: int = 600) -> str:
    s = "" if text is None else str(text)
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= limit else s[: max(0, limit - 1)].rstrip() + "…"


# ── الإعدادات (نفس مخزن المشروع: config/api_keys.json عبر config_manager) ──────
def settings_defaults(fields: list[dict]) -> dict:
    return {f.get("key", ""): f.get("default") for f in fields if f.get("key")}


def store_settings(namespace: str, values: dict) -> bool:
    """يحفظ قيم إعدادات إضافة في المخزن الرسمي للمشروع. لا يرمي أبدًا."""
    try:
        from memory.config_manager import save_plugin_config
        save_plugin_config(namespace, values)
        return True
    except Exception:
        return False


def stored_settings(namespace: str) -> dict:
    try:
        from memory.config_manager import get_plugin_config
        cfg = get_plugin_config(namespace)
        return dict(cfg) if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def cfg(namespace: str, fields: list[dict]) -> dict:
    """القيم الافتراضية + ما حفظه المستخدم (القيم الفارغة لا تلغي الافتراضي)."""
    out = settings_defaults(fields)
    for k, v in stored_settings(namespace).items():
        if v not in (None, ""):
            out[k] = v
    return out


def plugin_enabled(name: str) -> bool:
    """حالة الإضافة من مخزن المشروع (مفعّلة افتراضيًا — نموذج opt-out في Mark-LV)."""
    try:
        from memory.config_manager import get_plugin_enabled
        return bool(get_plugin_enabled(name))
    except Exception:
        return True


def set_plugin_enabled(name: str, enabled: bool) -> tuple[bool, str]:
    """تفعيل/تعطيل إضافة — الواجهة الرسمية الموجودة في المشروع
    (`save_plugin_enabled`، سطر 371). المُحمِّل يقرأ الحالة عند كل نداء، فلا حاجة لإعادة تشغيل."""
    try:
        from memory.config_manager import save_plugin_enabled
        save_plugin_enabled(name, bool(enabled))
        return True, ("تم تفعيل" if enabled else "تم تعطيل") + f" الإضافة '{name}'."
    except Exception as exc:
        return False, f"تعذّر تغيير حالة '{name}': {exc}"


# ── الأسرار: لا سطر في أي سجل يحتوي مفتاحًا ────────────────────────────────────
_SECRET_KEY_RE = re.compile(
    r"(token|api[_-]?key|apikey|secret|password|passwd|pwd|authorization|auth|cookie|bearer|credential|refresh)",
    re.I,
)
_REDACT_RULES = (
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]{8,}"), "Bearer ••••"),
    (re.compile(r"(?i)\b(token|api[_-]?key|apikey|key|password|passwd|secret)=([^\s&\"']+)"), r"\1=••••"),
    (re.compile(r"\bsk-[A-Za-z0-9]{12,}\b"), "••••"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}\b"), "••••"),
)


def looks_secret(key: Any) -> bool:
    """هل اسم هذا المفتاح يشير إلى سرّ؟ (يُستخدم لحجب القيم قبل تسجيلها)"""
    return bool(_SECRET_KEY_RE.search(str(key)))


def redact_text(text: Any, limit: int = 500) -> str:
    """يحجب الأشكال الشائعة للأسرار داخل نص قبل كتابته في سجل."""
    s = "" if text is None else str(text)
    for rx, replacement in _REDACT_RULES:
        s = rx.sub(replacement, s)
    return clip(s, limit)


def safe_params(parameters: Any, limit: int = 300) -> dict:
    """نسخة آمنة للسجل: يبقى اسم المفتاح، ويُحجب أي قيمة يبدو اسمها سرًّا."""
    out: dict = {}
    if not isinstance(parameters, dict):
        return out
    for k, v in parameters.items():
        if looks_secret(k):
            out[str(k)] = "••••"
        else:
            out[str(k)] = redact_text(v, 120) if isinstance(v, str) else v
    _ = limit
    return out


def token_for(path: Path, prefix: str = "") -> str:
    """يقرأ مفتاح وصول من ملف (صلاحيات 600) أو ينشئه مرة واحدة بـsecrets.

    الملف JSON فيه الحقل `token`؛ ويُقبل أيضًا ملف نصّي عادي فيه المفتاح وحده (توافقًا
    مع نسخ سابقة). عطل سابق: كان يُكتب JSON ويُقرأ كنصّ، فيُقرأ الملف كله بما فيه من
    علامات الترقيم ويُقارَن — وهو ما يكسر `compare_digest` ويُسقط كل طلب موثَّق.
    المفتاح نفسه لا يظهر في أي سجل — فقط عند طلب صريح من المستخدم.
    """
    try:
        if path.exists():
            raw = path.read_text(encoding="utf-8").strip()
            tok = ""
            if raw.startswith("{"):
                try:
                    data = json.loads(raw)
                    if isinstance(data, dict):
                        tok = str(data.get("token") or "").strip()
                except Exception:
                    tok = ""
            else:
                tok = raw
            if len(tok) >= 24:
                return tok
        fresh = prefix + secrets.token_urlsafe(32)
        save_json(path, {"protocol": "jarvis-local-key-v1", "token": fresh}, private=True)
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass
        return fresh
    except Exception:
        return ""


def mask_token(tok: str) -> str:
    if not tok:
        return "(لا يوجد مفتاح بعد)"
    return f"{tok[:4]}…{tok[-4:]} ({len(tok)} حرفًا)"


# ── سجل منظَّم (JSONL) بكل الحقول المطلوبة ─────────────────────────────────────
_LOG_LOCK = threading.Lock()
_LOG_MAX_BYTES = 1_500_000


def isolate_for_tests(root: Optional[Path] = None) -> Callable[[], None]:
    """يعزل مخزن الحزمة أثناء الفحص الذاتي: كل الملفات تذهب إلى مجلد مؤقت.

    الغرض: تشغيل فحص إضافة (vault · أتمتة · تقويم · سجلات) **لا يلمس** بيانات المستخدم
    الحقيقية في `config/orchestration`، ولا يخلق أتمتة حقيقية أو كاشًا حقيقيًا.
    يعيد دالة استرجاع تُعيد كل شيء إلى أصله وتحذف المجلد المؤقت.
    لا يرمي أبدًا.
    """
    global ORCH_DIR, LOG_DIR, AUTOMATIONS_FILE, WORKFLOWS_FILE, SCHEDULES_FILE
    global CALENDAR_FILE, HOME_FILE, REMOTE_STATE_FILE
    try:
        tmp = Path(root) if root is not None else Path(tempfile.mkdtemp(prefix="orch_selftest_"))
        try:
            tmp.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        saved_paths = (ORCH_DIR, LOG_DIR, AUTOMATIONS_FILE, WORKFLOWS_FILE,
                       SCHEDULES_FILE, CALENDAR_FILE, HOME_FILE, REMOTE_STATE_FILE)
        saved_store = globals().get("store_settings")
        saved_read = globals().get("stored_settings")
        memory_store: dict = {}

        ORCH_DIR = tmp
        LOG_DIR = tmp / "logs"
        AUTOMATIONS_FILE = tmp / "automations.json"
        WORKFLOWS_FILE = tmp / "workflows.json"
        SCHEDULES_FILE = tmp / "schedules.json"
        CALENDAR_FILE = tmp / "calendar.json"
        HOME_FILE = tmp / "home_devices.json"
        REMOTE_STATE_FILE = tmp / "remote_api_state.json"
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

        def _test_store(namespace: str, values: dict) -> bool:      # إعدادات في الذاكرة فقط
            try:
                memory_store.setdefault(str(namespace), {}).update(dict(values or {}))
            except Exception:
                pass
            return True

        def _test_read(namespace: str) -> dict:                     # لا read-modify-write على القرص
            try:
                return dict(memory_store.get(str(namespace), {}))
            except Exception:
                return {}

        globals()["store_settings"] = _test_store
        globals()["stored_settings"] = _test_read

        def _restore() -> None:
            global ORCH_DIR, LOG_DIR, AUTOMATIONS_FILE, WORKFLOWS_FILE, SCHEDULES_FILE
            global CALENDAR_FILE, HOME_FILE, REMOTE_STATE_FILE
            try:
                (ORCH_DIR, LOG_DIR, AUTOMATIONS_FILE, WORKFLOWS_FILE,
                 SCHEDULES_FILE, CALENDAR_FILE, HOME_FILE, REMOTE_STATE_FILE) = saved_paths
            except Exception:
                pass
            if saved_store is not None:
                globals()["store_settings"] = saved_store
            if saved_read is not None:
                globals()["stored_settings"] = saved_read
            shutil.rmtree(tmp, ignore_errors=True)

        return _restore
    except Exception:
        return lambda: None


def log_event(plugin: str, action: str, status: str, duration_ms: int = 0,
              error: str = "", detail: Any = None, log: str = "orchestration",
              extra: Optional[dict] = None) -> None:
    """سطر واحد لكل حدث: الوقت، الإضافة، الفعل، الحالة، المدة، والخطأ إن وُجد.

    كل قيمة تمرّ عبر حجب الأسرار، والمفاتيح التي يبدو اسمها سرًّا تُحجب قيمها —
    فالتوكنات وكلمات المرور لا تصل إلى القرص هنا أبدًا.
    """
    ensure_dirs()
    record = {
        "ts": now_iso(),
        "plugin": plugin,
        "action": clip(action, 60),
        "status": status,
        "duration_ms": int(duration_ms),
    }
    if error:
        record["error"] = redact_text(error, 300)
    if detail is not None:
        record["detail"] = safe_params(detail) if isinstance(detail, dict) else redact_text(detail, 300)
    if extra:
        record["extra"] = safe_params(extra)
    path = LOG_DIR / f"{log}.jsonl"
    try:
        with _LOG_LOCK:
            if path.exists() and path.stat().st_size > _LOG_MAX_BYTES:
                try:
                    os.replace(path, LOG_DIR / f"{log}.jsonl.1")
                except Exception:
                    pass
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def tail_log(log: str = "orchestration", n: int = 50) -> list[dict]:
    """آخر `n` سطرًا من سجل منظَّم، من الأحدث إلى الأقدم."""
    path = LOG_DIR / f"{log}.jsonl"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return []
    out = []
    for line in lines[-max(1, n):]:
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                out.append(obj)
        except Exception:
            continue
    out.reverse()
    return out


# ── نداء الإضافات والأدوات الشقيقة ────────────────────────────────────────────
_MODULE_CACHE: dict[str, Any] = {}
_SCAN_CACHE: dict[str, Any] = {"stamp": 0.0, "data": {}}


def _dir_stamp() -> float:
    try:
        return PLUGINS_DIR.stat().st_mtime
    except Exception:
        return 0.0


def _module_for(path: Path, cache_key: str):
    """يستورد ملفًا بالمسار (نفس أسلوب `api_connector` مع `_net_core`) ويخزّنه مؤقتًا."""
    if cache_key in _MODULE_CACHE:
        return _MODULE_CACHE[cache_key]
    spec = importlib.util.spec_from_file_location(cache_key, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not build import spec for {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[cache_key] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(cache_key, None)
        raise
    _MODULE_CACHE[cache_key] = module
    return module


def scan_plugins(force: bool = False) -> dict[str, dict]:
    """فهرس الإضافات بقراءة ثابتة (AST) بلا تنفيذ أي كود منها.

    لكل ملف: اسم PLUGIN، الوصف، المعاملات، هل يعلن إعدادات، المتطلبات الخارجية،
    وهل يوجد خطأ نحوي أو ازدواج في الاسم. الاستيراد لا يحدث هنا إطلاقًا — أمان أولًا،
    وهذا ما يجعل الفحص ممكنًا حتى لو كانت إضافة سليمة الشكل لكنها تفشل عند التشغيل.
    """
    stamp = _dir_stamp()
    if not force and _SCAN_CACHE["data"] and abs(stamp - _SCAN_CACHE["stamp"]) < 0.001:
        return _SCAN_CACHE["data"]
    ensure_dirs()
    result: dict[str, dict] = {}
    try:
        files = sorted(PLUGINS_DIR.glob("*.py"), key=lambda p: p.name)
    except Exception:
        files = []
    for path in files:
        if path.name.startswith("_"):
            continue
        info: dict[str, Any] = {
            "file": path.name, "name": path.stem, "description": "", "parameters": {},
            "has_settings": False, "namespace": "", "version": "", "requires": [],
            "valid": False, "error": "", "size": 0, "mtime": 0.0, "sha256": "",
        }
        try:
            info["size"] = path.stat().st_size
            info["mtime"] = path.stat().st_mtime
            raw = path.read_bytes()
            info["sha256"] = hashlib.sha256(raw).hexdigest()[:16]
            tree = ast.parse(raw.decode("utf-8", errors="replace"))
        except SyntaxError as exc:
            info["error"] = f"خطأ نحوي في السطر {exc.lineno}: {exc.msg}"
            result[path.stem] = info
            continue
        except Exception as exc:
            info["error"] = f"تعذّرت قراءة الملف: {exc}"
            result[path.stem] = info
            continue

        meta: dict = {}
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "PLUGIN" and isinstance(node.value, ast.Dict):
                        for k, v in zip(node.value.keys, node.value.values):
                            if isinstance(k, ast.Constant):
                                meta[k.value] = v
                    if isinstance(target, ast.Name) and target.id == "__version__" and isinstance(node.value, ast.Constant):
                        info["version"] = str(node.value.value)
        if not meta:
            info["error"] = "لا يوجد ثابت PLUGIN"
            result[path.stem] = info
            continue
        name_val = meta.get("name")
        info["name"] = name_val.value if isinstance(name_val, ast.Constant) else path.stem
        desc_val = meta.get("description")
        info["description"] = desc_val.value if isinstance(desc_val, ast.Constant) else ""
        ver = meta.get("version")
        if isinstance(ver, ast.Constant):
            info["version"] = str(ver.value)
        params = meta.get("parameters")
        if isinstance(params, ast.Dict):
            info["parameters"] = {getattr(k, "value", "?"): True for k in params.keys}
        # PLUGIN_SETTINGS
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "PLUGIN_SETTINGS" for t in node.targets):
                info["has_settings"] = True
                if isinstance(node.value, ast.Dict):
                    for k, v in zip(node.value.keys, node.value.values):
                        if isinstance(k, ast.Constant) and k.value == "namespace" and isinstance(v, ast.Constant):
                            info["namespace"] = str(v.value)
        # المتطلبات الخارجية (بلا تنفيذ)
        stdlib = getattr(sys, "stdlib_module_names", frozenset())
        local = {"memory", "core", "plugins", "actions", "dashboard"}
        reqs: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    if top and top not in stdlib and top not in local and not alias.name.startswith("_"):
                        reqs.add(top)
            elif isinstance(node, ast.ImportFrom):
                top = (node.module or "").split(".")[0]
                if top and node.level == 0 and top not in stdlib and top not in local:
                    reqs.add(top)
        info["requires"] = sorted(reqs)
        info["valid"] = bool(info["name"]) and bool(str(info["description"]).strip())
        if not info["valid"] and not info["error"]:
            info["error"] = "PLUGIN ناقص: الاسم أو الوصف"
        result[path.stem] = info

    # ازدواج الأسماء: نفس اسم PLUGIN في أكثر من ملف
    by_name: dict[str, list[str]] = {}
    for info in result.values():
        by_name.setdefault(str(info["name"]), []).append(info["file"])
    for info in result.values():
        dupes = by_name.get(str(info["name"]), [])
        if len(dupes) > 1:
            info["valid"] = False
            info["error"] = f"اسم مكرّر: {info['name']} موجود في {', '.join(sorted(dupes))}"
    _SCAN_CACHE["stamp"] = stamp
    _SCAN_CACHE["data"] = result
    return result


def resolve_plugin(name: str) -> Optional[Path]:
    """يحوّل اسم إضافة (أو اسم ملف) إلى مسار ملفه."""
    if not name:
        return None
    candidate = str(name).strip()
    direct = PLUGINS_DIR / f"{candidate}.py"
    if direct.exists():
        return direct
    for info in scan_plugins().values():
        if str(info["name"]) == candidate:
            return PLUGINS_DIR / str(info["file"])
    return None


def call_plugin(name: str, parameters: Optional[dict] = None, player: Any = None,
                session_memory: Any = None) -> tuple[bool, str]:
    """ينادي إضافة شقيقة كما يناديها المُحمِّل تمامًا: تمرير الوسائط المعلَنة فقط.

    يعيد دائمًا `(ok, output)` — لا يرمي أبدًا، لأن كل الأتمتة تُبنى فوقه.
    """
    path = resolve_plugin(name)
    if path is None:
        return False, f"الإضافة '{name}' غير موجودة في مجلد plugins."
    key = f"_jarvis_pg_{path.stem}"
    try:
        module = _module_for(path, key)
    except Exception as exc:
        return False, f"تعذّر تحميل '{path.name}': {clip(exc, 200)}"
    fn = getattr(module, "run", None)
    if not callable(fn):
        return False, f"'{path.name}' لا يحتوي دالة run() قابلة للنداء."
    try:
        sig = inspect.signature(fn)
        has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
        kwargs = {}
        if has_var_kw or "player" in sig.parameters:
            kwargs["player"] = player
        if has_var_kw or "session_memory" in sig.parameters:
            kwargs["session_memory"] = session_memory
        return True, str(fn(dict(parameters or {}), **kwargs))
    except Exception as exc:
        return False, f"انهارت '{name}' أثناء التنفيذ: {clip(exc, 200)}"


def call_action(name: str, parameters: Optional[dict] = None, player: Any = None,
                session_memory: Any = None) -> tuple[bool, str]:
    """ينادي أداة من `actions/` المشروع (مثل reminder أو weather_report) بنفس الأسلوب.

    الأدوات في المشروع تُعلن TOOL ومعاملاتها أعلى الملف، وتُصدَّر عادةً بدالة
    تحمل اسم الأداة نفسها. نبحث عن أول دالة عامة قابلة للنداء تأخذ dict.
    """
    path = ACTIONS_DIR / f"{name}.py"
    if not path.exists():
        return False, f"الأداة '{name}' غير موجودة في مجلد actions."
    key = f"_jarvis_act_{path.stem}"
    try:
        module = _module_for(path, key)
    except Exception as exc:
        return False, f"تعذّر تحميل الأداة '{path.name}': {clip(exc, 200)}"
    fn = getattr(module, name, None)
    if not callable(fn):
        for attr in dir(module):
            cand = getattr(module, attr, None)
            if attr.startswith("_") or not callable(cand) or getattr(cand, "__module__", "") != module.__name__:
                continue
            try:
                params = list(inspect.signature(cand).parameters.values())
            except Exception:
                continue
            if params and params[0].name in ("parameters", "params"):
                fn = cand
                break
    if not callable(fn):
        return False, f"لم أجد في '{path.name}' دالة تقبل معاملات."
    try:
        sig = inspect.signature(fn)
        kwargs = {}
        if "player" in sig.parameters:
            kwargs["player"] = player
        if "session_memory" in sig.parameters:
            kwargs["session_memory"] = session_memory
        return True, str(fn(dict(parameters or {}), **kwargs))
    except Exception as exc:
        return False, f"انهارت الأداة '{name}': {clip(exc, 200)}"


def run_with_timeout(fn: Callable[[], Any], timeout_s: float) -> tuple[bool, Any, bool]:
    """ينفّذ دالة في خيط منفصل مع مهلة. يعيد `(تمّ, الناتج, انتهت المهلة)`.

    لا نقتل الخيط — قتل الخيوط غير آمن في بايثون — لكننا لا نُعلّق المجدول:
    يُسجَّل انتهاء المهلة ويُترك الخيط يعمل حتى ينتهي وحده.
    """
    box: dict = {}

    def _runner() -> None:
        try:
            box["value"] = fn()
        except Exception as exc:  # noqa: BLE001 — أي فشل يُعاد كقيمة
            box["error"] = exc

    thread = threading.Thread(target=_runner, daemon=True, name="jarvis-orch-task")
    thread.start()
    thread.join(max(0.1, float(timeout_s)))
    if thread.is_alive():
        return False, None, True
    if "error" in box:
        return False, box["error"], False
    value = box.get("value")
    # نتيجة call_plugin عقدها (نجح، نص) — نفكّها هنا، وإلا بدت *فشلًا* نجاحًا في كل
    # مَن يستعمل المهلة (المجدول، الواجهة، اللوحة). هذا العطل وقع فعلًا مرة، ولا يقع ثانية.
    if isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], bool):
        return bool(value[0]), value[1], False
    return True, value, False


def loaded_plugin_names() -> set[str]:
    """أسماء الإضافات المحمَّلة فعليًا في هذه العملية (يعرفها المُحمِّل بـplugins.<stem>)."""
    out = set()
    for key, module in list(sys.modules.items()):
        if key.startswith("plugins.") and module is not None:
            meta = getattr(module, "PLUGIN", None)
            if isinstance(meta, dict) and isinstance(meta.get("name"), str):
                out.add(meta["name"])
    return out


def core_tool_names() -> set[str]:
    """أسماء الأدوات التي يحجزها المشروع: أدوات actions/*.py + أدوات main.py المضمّنة."""
    names: set[str] = set()
    try:
        for path in sorted(ACTIONS_DIR.glob("*.py")):
            if path.name.startswith("_"):
                continue
            try:
                tree = ast.parse(path.read_bytes().decode("utf-8", errors="replace"))
            except Exception:
                continue
            for node in tree.body:
                if isinstance(node, ast.Assign) and any(
                        isinstance(t, ast.Name) and t.id == "TOOL" for t in node.targets):
                    if isinstance(node.value, ast.Dict):
                        for k, v in zip(node.value.keys, node.value.values):
                            if isinstance(k, ast.Constant) and k.value == "name" and isinstance(v, ast.Constant):
                                names.add(str(v.value))
    except Exception:
        pass
    try:
        main_path = BASE_DIR / "main.py"
        tree = ast.parse(main_path.read_bytes().decode("utf-8", errors="replace"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "TOOL_DECLARATIONS" for t in node.targets):
                for sub in ast.walk(node.value):
                    if isinstance(sub, ast.Dict):
                        for k, v in zip(sub.keys, sub.values):
                            if isinstance(k, ast.Constant) and k.value == "name" and isinstance(v, ast.Constant):
                                names.add(str(v.value))
    except Exception:
        pass
    return names


# ── الجدولة: تفسير الوقت وحساب الموعد القادم ───────────────────────────────────
_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
_WEEKDAYS = {
    "monday": 0, "mon": 0, "الاثنين": 0, "الاتنين": 0,
    "tuesday": 1, "tue": 1, "الثلاثاء": 1,
    "wednesday": 2, "wed": 2, "الأربعاء": 2, "الاربعاء": 2,
    "thursday": 3, "thu": 3, "الخميس": 3,
    "friday": 4, "fri": 4, "الجمعة": 4,
    "saturday": 5, "sat": 5, "السبت": 5,
    "sunday": 6, "sun": 6, "الأحد": 6, "الاحد": 6,
}
_WEEKDAY_AR = ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]


def normalize_digits(text: str) -> str:
    return str(text or "").translate(_AR_DIGITS)


def parse_hhmm(text: str) -> Optional[tuple[int, int]]:
    """يفهم: 08:30 · 8:00 · 8 · 8 ص · 8 مساءً · 8pm · 20:15."""
    s = normalize_digits(text or "").strip().lower()
    if not s:
        return None
    pm = bool(re.search(r"(pm|p\.m|م\b|مساء|مساءً|بعد الظهر)", s))
    am = bool(re.search(r"(am|a\.m|ص\b|صباح|صباحًا|الصبح|فجر)", s))
    m = re.search(r"(\d{1,2})(?:\s*[:.]\s*(\d{2}))?", s)
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2)) if m.group(2) else 0
    if hour > 23 or minute > 59:
        return None
    if pm and hour < 12:
        hour += 12
    if am and hour == 12:
        hour = 0
    return hour, minute


def parse_when(text: str) -> Optional[dict]:
    """يفهم عبارات التوقيت الطبيعية (عربي/إنجليزي) ويعيد وصفًا للجدولة.

    الأنواع: daily · weekly · interval · once. لا يفهم → None (ونقول ذلك بصراحة).
    """
    s = normalize_digits(str(text or "").strip().lower())
    if not s:
        return None
    # كل N دقيقة/ساعة
    m = re.search(r"(?:every|each|كل)\s*(\d{1,4})\s*"
                  r"(min|mins|minute|minutes|دقيقة|دقائق|دقيقه|ساعة|ساعات|ساعه|h|hour|hours)", s)
    if m:
        amount = int(m.group(1))
        unit = m.group(2)
        minutes = amount * 60 if unit.startswith(("h", "hour", "ساعة", "ساعات", "ساعه")) else amount
        if minutes >= 1:
            return {"kind": "interval", "minutes": minutes}
    # كل يوم / يوميًا
    daily = bool(re.search(r"(every\s*day|daily|كل\s*يوم|يوميًا|يوميا|كل\s*ص\b|daily\s*at)", s))
    morning = bool(re.search(r"(morning|صباح|الصبح)", s))
    evening = bool(re.search(r"(evening|night|مساء|بالليل|الليل)", s))
    if daily or morning or evening:
        hm = parse_hhmm(s) or ((8, 0) if morning else (20, 0) if evening else None)
        if hm:
            return {"kind": "daily", "time": f"{hm[0]:02d}:{hm[1]:02d}"}
    # أيام محددة
    days = sorted({idx for word, idx in _WEEKDAYS.items() if re.search(rf"(?<!\w){re.escape(word)}(?!\w)", s)})
    if days and re.search(r"(every|each|كل|في\s+يوم)", s):
        hm = parse_hhmm(s) or (8, 0)
        return {"kind": "weekly", "days": days, "time": f"{hm[0]:02d}:{hm[1]:02d}"}
    # مرة واحدة: غدًا/اليوم + وقت
    if re.search(r"(tomorrow|غد[ًا]?|بكرة)", s) or re.search(r"(today|اليوم|النهاردة|النهارده)", s):
        hm = parse_hhmm(s)
        if hm:
            base = now() + _dt.timedelta(days=1 if re.search(r"(tomorrow|غد[ًا]?|بكرة)", s) else 0)
            at = base.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
            if at <= now():
                at += _dt.timedelta(days=1)
            return {"kind": "once", "at": to_iso(at)}
    return None


def describe_schedule(sched: Optional[dict]) -> str:
    """وصف عربي قصير للجدولة، ليُقال للمستخدم كما هو."""
    if not isinstance(sched, dict) or not sched:
        return "بلا جدولة"
    kind = sched.get("kind")
    if kind == "interval":
        minutes = int(sched.get("minutes", 1))
        if minutes % 60 == 0:
            return f"كل {minutes // 60} ساعة"
        return f"كل {minutes} دقيقة"
    if kind == "daily":
        return f"يوميًا الساعة {sched.get('time', '08:00')}"
    if kind == "weekly":
        days = sched.get("days") or []
        names = "، ".join(_WEEKDAY_AR[d] for d in sorted(days) if 0 <= int(d) <= 6)
        return f"أسبوعيًا ({names}) الساعة {sched.get('time', '08:00')}"
    if kind == "once":
        dt = from_iso(sched.get("at"))
        return f"مرة واحدة — {to_iso(dt)}" if dt else "مرة واحدة"
    if kind == "manual":
        return "يدوي فقط"
    return "بلا جدولة"


def next_run(sched: Optional[dict], after: Optional[_dt.datetime] = None) -> Optional[_dt.datetime]:
    """الموعد القادم لأي نوع جدولة. None إذا كانت manual أو غير صالحة."""
    if not isinstance(sched, dict):
        return None
    base = (after or now()).replace(microsecond=0)
    kind = sched.get("kind")
    if kind == "interval":
        minutes = max(1, int(sched.get("minutes", 1) or 1))
        return base + _dt.timedelta(minutes=minutes)
    if kind in ("daily", "weekly"):
        hm = parse_hhmm(str(sched.get("time", "08:00"))) or (8, 0)
        if kind == "daily":
            cand = base.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
            return cand if cand > base else cand + _dt.timedelta(days=1)
        days = [int(d) for d in (sched.get("days") or []) if str(d).isdigit() or isinstance(d, int)]
        days = sorted({d for d in days if 0 <= d <= 6}) or [base.weekday()]
        for offset in range(0, 8):
            cand = base + _dt.timedelta(days=offset)
            if cand.weekday() in days:
                cand = cand.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
                if cand > base:
                    return cand
        return None
    if kind == "once":
        dt = from_iso(sched.get("at"))
        return dt if dt and dt > base else None
    return None


# ── قوالب سير العمل: {{prev}} · {{steps.NAME.output}} · {{vars.KEY}} ───────────
_TEMPLATE_RE = re.compile(r"\{\{\s*([a-zA-Z_][\w.\-]*)\s*\}\}")


def render(template: str, ctx: dict) -> str:
    """يستبدل متغيّرات القالب من سياق التنفيذ؛ المجهول يبقى كما هو ليراه المستخدم."""
    if not isinstance(template, str):
        return template

    def _sub(match: re.Match) -> str:
        key = match.group(1)
        if key == "prev":
            return str(ctx.get("prev", ""))
        if key.startswith("steps."):
            parts = key.split(".")
            if len(parts) >= 3:
                step = (ctx.get("steps") or {}).get(parts[1]) or {}
                return str(step.get(parts[2], ""))
            return match.group(0)
        if key.startswith("vars."):
            return str((ctx.get("vars") or {}).get(key[5:], ""))
        if key in (ctx.get("vars") or {}):
            return str(ctx["vars"][key])
        return match.group(0)

    return _TEMPLATE_RE.sub(_sub, template)


def render_params(parameters: Any, ctx: dict) -> dict:
    """يطبّق القوالب على كل قيم المعاملات نصًّا بنصّ (يتعامل مع dict متداخل)."""
    if isinstance(parameters, dict):
        return {k: render_params(v, ctx) for k, v in parameters.items()}
    if isinstance(parameters, list):
        return [render_params(v, ctx) for v in parameters]
    if isinstance(parameters, str):
        return render(parameters, ctx)
    return parameters


def eval_condition(cond: Any, ctx: dict) -> tuple[bool, str]:
    """شرط فرع: {"left": "...", "op": "contains|==|!=|>|<|empty|not_empty", "right": "..."}.

    الطرفان يُحلّان من السياق أولًا، حتى تعمل الشروط على مخرجات الخطوات السابقة.
    """
    if not cond:
        return True, ""
    if isinstance(cond, str):
        cond = {"left": cond, "op": "not_empty", "right": ""}
    if not isinstance(cond, dict):
        return True, ""
    left = render(str(cond.get("left", "")), ctx)
    right = render(str(cond.get("right", "")), ctx)
    op = str(cond.get("op", "not_empty")).strip().lower()
    try:
        if op in ("empty", "فارغ"):
            ok = not left.strip()
        elif op in ("not_empty", "non_empty", "غير_فارغ", "موجود"):
            ok = bool(left.strip())
        elif op in ("==", "=", "equals", "is", "يساوي"):
            ok = left.strip().lower() == right.strip().lower()
        elif op in ("!=", "<>", "not_equals", "لا_يساوي"):
            ok = left.strip().lower() != right.strip().lower()
        elif op in ("contains", "يحتوي"):
            ok = right.strip().lower() in left.lower()
        elif op in ("not_contains", "لا_يحتوي"):
            ok = right.strip().lower() not in left.lower()
        elif op in (">", "gt", "أكبر"):
            ok = float(left.strip() or 0) > float(right.strip() or 0)
        elif op in ("<", "lt", "أصغر"):
            ok = float(left.strip() or 0) < float(right.strip() or 0)
        elif op in (">=", "gte"):
            ok = float(left.strip() or 0) >= float(right.strip() or 0)
        elif op in ("<=", "lte"):
            ok = float(left.strip() or 0) <= float(right.strip() or 0)
        else:
            return True, f"عامل مقارنة غير معروف '{op}' — تُجوهز"
    except Exception as exc:
        return False, f"تعذّر تقييم الشرط: {clip(exc, 80)}"
    return ok, ""


# ── بوابة التأكيد: ملف واحد يتعامل معها دائمًا، وبالفشل المُغلَق ────────────────
def confirm_request(key: str, title: str, detail: str, run: Callable[[], Any],
                    player: Any = None) -> str:
    """يسأل عبر `core/confirm` الحقيقي، و**العمل المؤثر يبقى داخل `run=`**.

    درس مهم: `confirm.request` لا تعيد نتيجة التنفيذ — تُعيد جملة وتُعلّق العمل
    حتى يضغط المستخدم CONFIRM. لذلك أي فعل غير قابل للاستدراك يجب أن يكون داخل
    `run`. وإذا تعذّر الوصول إلى البوابة (نسخة قديمة أو خطأ) فإننا **نرفض** ولا ننفّذ.
    """
    try:
        from core import confirm as _confirm  # type: ignore
        return str(_confirm.request(key=key, title=title, detail=detail, run=run, player=player))
    except TypeError:
        try:
            from core import confirm as _confirm2  # type: ignore
            return str(_confirm2.request(key, title, detail, run))
        except Exception as exc:
            return f"لم أستطع عرض تأكيد '{title}'، ولم أنفّذ شيئًا. ({clip(exc, 120)})"
    except Exception as exc:
        return f"لم أستطع عرض تأكيد '{title}'، ولم أنفّذ شيئًا. ({clip(exc, 120)})"


# ── HTTP: خادم صغير موحّد للواجهة البرمجية واللوحة ─────────────────────────────
@dataclass
class Req:
    """طلب وارد، بعد التحقق من الحدود الأساسية."""
    method: str
    path: str
    query: dict = field(default_factory=dict)
    headers: dict = field(default_factory=dict)
    body: bytes = b""
    client: str = ""
    authorized: bool = False

    def json(self) -> Any:
        if not self.body:
            return {}
        try:
            return json.loads(self.body.decode("utf-8", errors="replace"))
        except Exception:
            return None


class RateGate:
    """حدّ معدّل بسيط لكل عميل (نافذة زمنية). لا يمنع أحدًا أبدًا أكثر من الحد."""

    def __init__(self, per_minute: int, burst: int = 0):
        self.per_minute = max(1, int(per_minute))
        self.capacity = max(self.per_minute, int(burst) or self.per_minute)
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        stamp = time.time()
        with self._lock:
            hits = [t for t in self._hits.get(key, []) if stamp - t < 60.0]
            if len(hits) >= self.capacity:
                self._hits[key] = hits
                return False
            hits.append(stamp)
            self._hits[key] = hits
            return True


class MiniHTTP:
    """خادم HTTP صغير آمن: توكن إجباري، حدّ معدّل، حدّ حجم، وسجل بلا أسرار.

    الاستخدام: تُبنى الخرائط كـ`{(method, path): callable(req) -> (status, body, ctype)}`،
    و`public_paths` بلا توكن (مثل /health). كل شيء آخر يتطلب مفتاحًا صحيحًا.
    """

    def __init__(self, host: str, port: int, routes: dict, token: str, *,
                 log_name: str = "http", public_paths: tuple[str, ...] = ("/health",),
                 rate_per_minute: int = 60, max_body_kb: int = 256,
                 accept_key_param: bool = False, cookie_name: str = ""):
        self.host = str(host or "127.0.0.1")
        self.port = int(port)
        self.routes = routes
        self.token = str(token or "")
        self.log_name = log_name
        self.public_paths = tuple(public_paths)
        self.rate = RateGate(rate_per_minute)
        self.max_body = max(1024, int(max_body_kb) * 1024)
        self.accept_key_param = bool(accept_key_param)
        # كوكي جلسة للوحة الويب: HttpOnly + SameSite=Strict، فلا يظهر المفتاح في العنوان
        # ولا في أي سجل، ويُتحقّق منه بنفس المقارنة الثابتة الزمن.
        self.cookie_name = str(cookie_name or "")
        self._httpd = None
        self._thread: Optional[threading.Thread] = None
        self.started_at: Optional[str] = None
        self.requests = 0
        self.denied = 0

    # -- دورة الحياة --
    @property
    def running(self) -> bool:
        return bool(self._httpd) and bool(self._thread and self._thread.is_alive())

    def start(self) -> tuple[bool, str]:
        if self.running:
            return True, f"يعمل بالفعل على {self.host}:{self.port}"
        if not self.token:
            return False, "لا يوجد مفتاح وصول — لم أبدأ الخادم."
        import http.server  # noqa: PLC0415 — كسول عن قصد: لا كلفة إن لم نبدأ

        outer = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "JarvisLocal/1.0"

            def log_message(self, fmt, *args):  # noqa: A003 — نكتب سجلنا بنفسنا
                return

            def _reply(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                if self.command != "HEAD":
                    try:
                        self.wfile.write(body)
                    except Exception:
                        pass

            def _deny(self, status: int, message: str) -> None:
                outer.denied += 1
                payload = json.dumps({"ok": False, "error": message}, ensure_ascii=False).encode("utf-8")
                self._reply(status, payload, "application/json; charset=utf-8")

            def _serve(self, method: str) -> None:
                outer.requests += 1
                started = time.time()
                parsed = urllib.parse.urlparse(self.path)
                path = parsed.path.rstrip("/") or "/"
                query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
                client = self.client_address[0] if self.client_address else "?"
                headers = {k.lower(): v for k, v in self.headers.items()}
                status, note, plugin_used = 500, "", ""
                try:
                    length = int(headers.get("content-length") or 0)
                    if length > outer.max_body:
                        status = 413
                        self._deny(413, "الطلب أكبر من الحد المسموح.")
                        return
                    body = self.rfile.read(length) if length > 0 else b""
                    if not outer.rate.allow(client):
                        status = 429
                        self._deny(429, "طلبات كثيرة جدًا — انتظر قليلًا.")
                        return
                    given = ""
                    auth = headers.get("authorization", "")
                    if auth.lower().startswith("bearer "):
                        given = auth[7:].strip()
                    given = given or headers.get("x-api-key", "").strip()
                    if not given and outer.accept_key_param:
                        given = str(query.get("key", "")).strip()
                    if not given and outer.cookie_name:
                        for chunk in headers.get("cookie", "").split(";"):
                            if "=" in chunk:
                                name, value = chunk.split("=", 1)
                                if name.strip() == outer.cookie_name:
                                    given = value.strip()
                                    break
                    authorized = bool(given) and secrets.compare_digest(given, outer.token)
                    if not authorized and path not in outer.public_paths:
                        status = 401
                        self._deny(401, "مفتاح الوصول غير صحيح.")
                        return
                    handler = outer.routes.get((method, path))
                    if handler is None:
                        status = 404 if any(k[1] == path for k in outer.routes) else 404
                        self._deny(status, "المسار غير موجود.")
                        return
                    req = Req(method=method, path=path, query=query, headers=headers,
                              body=body, client=client, authorized=authorized)
                    result = handler(req) or (500, {"ok": False, "error": "لا استجابة"}, "application/json")
                    extra_headers = {}
                    if isinstance(result, tuple) and len(result) == 4:
                        status, payload, ctype, extra_headers = result
                    else:
                        status, payload, ctype = result
                    if isinstance(payload, (dict, list)):
                        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                        ctype = ctype or "application/json; charset=utf-8"
                    elif isinstance(payload, bytes):
                        raw, ctype = payload, ctype or "application/octet-stream"
                    else:
                        raw = str(payload).encode("utf-8")
                        ctype = ctype or "text/plain; charset=utf-8"
                    plugin_used = str(getattr(req, "_plugin", ""))
                    self._reply(status, raw, ctype, extra_headers)
                except Exception as exc:  # noqa: BLE001 — لا نُسقط الخادم أبدًا
                    status = 500
                    try:
                        self._deny(500, f"خطأ داخلي: {clip(exc, 120)}")
                    except Exception:
                        pass
                    note = f"exception: {exc}"
                finally:
                    log_event(outer.log_name, f"{method} {path}", str(status),
                              duration_ms=int((time.time() - started) * 1000),
                              error=note, detail={"client": client, "plugin": plugin_used},
                              log=outer.log_name, extra={"query_keys": sorted(query.keys())})

            def do_GET(self):
                self._serve("GET")

            def do_POST(self):
                self._serve("POST")

            def do_PUT(self):
                self._serve("PUT")

            def do_HEAD(self):
                self._serve("GET")

            def do_DELETE(self):
                self._serve("DELETE")

        try:
            httpd = http.server.ThreadingHTTPServer((self.host, self.port), _Handler)
            httpd.daemon_threads = True
            self._httpd = httpd
        except OSError as exc:
            self._httpd = None
            return False, f"تعذّر فتح المنفذ {self.host}:{self.port} — {exc}"
        self.started_at = now_iso()
        self._thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.4},
                                        daemon=True, name=f"jarvis-{self.log_name}")
        self._thread.start()
        warn = ""
        if self.host not in ("127.0.0.1", "localhost", "::1"):
            warn = f" (تحذير: مرتبط بـ{self.host} — متاح من الشبكة)"
        return True, f"الخادم يعمل على http://{self.host}:{self.port}{warn}"

    def stop(self) -> tuple[bool, str]:
        if not self._httpd:
            return False, "الخادم متوقف بالفعل."
        try:
            self._httpd.shutdown()
            self._httpd.server_close()
        except Exception:
            pass
        self._httpd = None
        self._thread = None
        return True, "تم إيقاف الخادم."

    def status(self) -> dict:
        return {
            "running": self.running, "host": self.host, "port": self.port,
            "started": self.started_at or "", "requests": self.requests, "denied": self.denied,
            "url": f"http://{self.host}:{self.port}",
        }


def free_port(start: int, tries: int = 50) -> int:
    """أول منفذ حرّ ≥ start (نستخدمه في الفحوص لتجنّب التعارض)."""
    for port in range(int(start), int(start) + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return int(start)


def http_json(url: str, payload: Any = None, headers: Optional[dict] = None,
              timeout: float = 10.0, method: Optional[str] = None) -> tuple[int, Any]:
    """طلبان صغيران بـurllib للفحوص وانتظار الخدمات المحلية. لا يرمي أبدًا."""
    data = None
    hdrs = dict(headers or {})
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — عناوين محلية/مُعدّة
            raw = resp.read(2_000_000)
            ctype = resp.headers.get("Content-Type", "")
            if "json" in ctype:
                try:
                    return resp.status, json.loads(raw.decode("utf-8", errors="replace"))
                except Exception:
                    return resp.status, raw.decode("utf-8", errors="replace")
            return resp.status, raw.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read(200_000).decode("utf-8", errors="replace")
            try:
                return exc.code, json.loads(raw)
            except Exception:
                return exc.code, raw
        except Exception:
            return exc.code, ""
    except Exception as exc:
        return 0, f"{type(exc).__name__}: {clip(exc, 160)}"


def fetch_text(url: str, timeout: float = 8.0, limit_kb: int = 256,
               headers: Optional[dict] = None) -> tuple[int, str]:
    """جلب نصّي بسيط (للطقس والتكاملات النصّية). لا يرمي أبدًا."""
    req = urllib.request.Request(url, headers=dict(headers or {"User-Agent": "JARVIS-local/1.0"}))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            raw = resp.read(limit_kb * 1024)
            return resp.status, raw.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, ""
    except Exception as exc:
        return 0, f"{type(exc).__name__}: {clip(exc, 120)}"


# ── استشعار النظام (psutil موجود في requirements المشروع) ──────────────────────
def _psutil():
    try:
        import psutil  # noqa: PLC0415
        return psutil
    except Exception:
        return None


def process_running(needle: str) -> Optional[bool]:
    """هل توجد عملية يطابق اسمها جزئيًا هذا النص؟ None إذا تعذّرت المعرفة."""
    ps = _psutil()
    needle = str(needle or "").strip().lower()
    if ps is None or not needle:
        return None
    try:
        for proc in ps.process_iter(["name"]):
            name = str(proc.info.get("name") or "").lower()
            if needle in name:
                return True
        return False
    except Exception:
        return None


def system_metric(metric: str) -> Optional[float]:
    """قراءة مقياس نظام: cpu · ram · disk · battery. None إذا لم يتوفر."""
    ps = _psutil()
    if ps is None:
        return None
    metric = str(metric or "cpu").strip().lower()
    try:
        if metric in ("cpu", "processor"):
            return float(ps.cpu_percent(interval=0.15))
        if metric in ("ram", "memory"):
            return float(ps.virtual_memory().percent)
        if metric in ("disk", "storage"):
            root = "C:\\" if os.name == "nt" else "/"
            return float(ps.disk_usage(root).percent)
        if metric in ("battery", "bat"):
            battery = ps.sensors_battery()
            return None if battery is None else float(battery.percent)
    except Exception:
        return None
    return None


# ── عمق التنفيذ: سقف عالمي يمنع الأتمتة التي تنادي نفسها ───────────────────────
_RUN_LOCAL = threading.local()


def run_depth() -> int:
    return int(getattr(_RUN_LOCAL, "depth", 0))


@contextlib.contextmanager
def run_session():
    """يزيد عدّاد العمق حول أي تنفيذ متسلسل، ثم ينقصه دائمًا (try/finally)."""
    _RUN_LOCAL.depth = run_depth() + 1
    try:
        yield
    finally:
        _RUN_LOCAL.depth = max(0, run_depth() - 1)


# ── منفّذ الخطوات الموحّد: يستخدمه محرّك الأتمتة وسير العمل معًا ───────────────
def run_steps(steps: Sequence[Any], ctx: Optional[dict] = None, *, owner: str = "orchestration",
              log: str = "orchestration", player: Any = None, max_steps: int = 25,
              max_depth: int = 3, step_timeout_s: float = 180.0,
              allow_admin: bool = False) -> dict:
    """ينفّذ سلسلة خطوات إضافات: قوالب، شروط، إعادة محاولة، ومسارات فشل.

    كل خطوة: {"name": "...", "plugin": "...", "parameters": {...}, "when": {...},
              "retries": n, "on_failure": {"action": "stop|continue|jump", "step": "..."}}

    الحماية: تُرفض الإضافات الإدارية (وشملها منع المناداة الذاتية)، ويُسجَّل كل خطوة
    بسطر منظَّم فيه المدة والحالة، وتُحفظ مخرجات الخطوات الناجحة في السياق حتى لو
    فشل ما بعدها — فلا تضيع نتيجة خطوة بسبب خطوة تالية.

    يعيد: {"ok", "output" (آخر مخرَج ناجح), "results": [...], "failed": اسم الخطوة}
    """
    ctx = dict(ctx or {})
    ctx.setdefault("prev", "")
    ctx.setdefault("steps", {})
    ctx.setdefault("vars", {})
    steps = list(steps or [])
    results: list[dict] = []
    failed: Optional[str] = None
    stopped = False

    if run_depth() >= max_depth:
        return {"ok": False, "output": "", "results": [],
                "failed": None, "steps_run": 0,
                "error": f"رفضت التنفيذ: تجاوز عمق النداء الحد ({max_depth}) — منعًا للّف."}

    with run_session():
        index = 0
        jumps = 0
        executed = 0
        while index < len(steps) and executed < max_steps:
            step = steps[index]
            index += 1
            if not isinstance(step, dict):
                continue
            label = str(step.get("name") or f"step{index}")
            target = str(step.get("plugin") or "").strip()
            entry = {"name": label, "plugin": target, "status": "", "output": "", "error": "", "ms": 0}
            if not target:
                entry["status"], entry["error"] = "failed", "خطوة بلا اسم إضافة"
            elif target in ADMIN_PLUGINS and not allow_admin:
                entry["status"], entry["error"] = "blocked", f"'{target}' إضافة إدارية — لا تُشغَّل من داخل أتمتة"
            elif target == owner:
                entry["status"], entry["error"] = "blocked", "الشيء لا ينادي نفسه"
            else:
                condition_ok, why = eval_condition(step.get("when"), ctx)
                if not condition_ok:
                    entry["status"], entry["error"] = "skipped", why
                else:
                    attempts = max(1, min(5, int(step.get("retries", 0) or 0) + 1))
                    for attempt in range(attempts):
                        params = render_params(step.get("parameters") or {}, ctx)
                        started = time.time()
                        try:
                            ok, value, timed_out = run_with_timeout(
                                lambda t=target, p=params: call_plugin(t, p, player=player),
                                float(step_timeout_s))
                        except Exception as exc:  # noqa: BLE001
                            ok, value, timed_out = False, str(exc), False
                        entry["ms"] += int((time.time() - started) * 1000)
                        # call_plugin يعيد (نجح، الناتج) — نفكّ الصفّ هنا، وإلا بدا الفشل نجاحًا.
                        if isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], bool):
                            ok, value = value[0], value[1]
                        if timed_out:
                            entry["status"], entry["error"] = "timeout", f"تجاوزت المهلة ({step_timeout_s:.0f}s)"
                            ok = False
                        elif ok:
                            entry["status"], entry["output"] = "ok", str(value)
                            entry["error"] = ""
                            break
                        else:
                            entry["status"], entry["error"] = "failed", str(value)
                            _ = attempt
            executed += 1
            if entry["status"] == "ok":
                ctx["prev"] = entry["output"]
                ctx["steps"][label] = {"output": entry["output"], "status": "ok"}
            else:
                ctx["steps"][label] = {"output": "", "status": entry["status"], "error": entry["error"]}
            results.append(entry)
            log_event(owner, f"step:{label}", entry["status"], entry["ms"], entry["error"],
                      {"plugin": target}, log=log)
            if entry["status"] in ("failed", "blocked", "timeout"):
                failed = label
                policy = step.get("on_failure") if isinstance(step.get("on_failure"), dict) else {}
                action = str(policy.get("action", "stop")).strip().lower()
                if action in ("continue", "skip", "استمر"):
                    continue
                if action in ("jump", "goto", "اقفز") and jumps < 5:
                    jump_to = str(policy.get("step") or policy.get("to") or "").strip()
                    target_index = next((k for k, s in enumerate(steps)
                                         if isinstance(s, dict) and str(s.get("name")) == jump_to), None)
                    if target_index is not None:
                        index = target_index
                        jumps += 1
                        continue
                stopped = True
                break

    return {
        "ok": failed is None and not stopped,
        "output": str(ctx.get("prev") or ""),
        "results": results,
        "failed": failed,
        "steps_run": executed,
    }


if __name__ == "__main__":
    print("_orch_core — ملف مشترك، لا يعمل وحده.")
    print("BASE_DIR:", BASE_DIR)
    print("plugins:", len(scan_plugins()), "| core tools:", len(core_tool_names()))
    for probe in ("every day at 8 am", "كل 30 دقيقة", "غدًا الساعة 5 م", "every sunday at 20:00"):
        print(f"  {probe!r:32} → {parse_when(probe)}")
