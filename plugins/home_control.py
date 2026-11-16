"""
home_control.py — أتمتة المنزل (Home Automation).

تصميم مهايئات (adapters) لا يربط النظام بمزوّد واحد:
• `hass`  — Home Assistant عبر REST: قراءة الحالات واستدعاء الخدمات.
• `mqtt`  — أي نظام يفهم MQTT (paho-mqtt موجود في requirements المشروع).
وأي مزوّد آخر يُضاف بدالة واحدة في `_ADAPTERS` بلا مساس بالبقية.

الأمان — كل بند مُختبر في الفحص الذاتي:
• لا أوامر شبكة عامة: فقط مسارات Home Assistant المعروفة، ودومينات مسموحة صريحة
  (light/switch/climate/cover/lock/fan/media_player/scene/script/input_boolean) —
  ورفض قاطع لدومينات التنفيذ مثل shell_command وcommand_line وpython_script.
• التحكّم فقط في الأجهزة **المُعدّة عندك** باسم مستعار؛ الاكتشاف يعرض، لا يتحكّم.
• الأجهزة المؤثّرة (أقفال · أبواب · إنذار) تطلب تأكيدًا قبل التنفيذ.
• الأخطاء كلها مغلقة: خدمة غائبة = رسالة واضحة، ولا تنفيذ على المجهول.
• التوكنات وكلمات المرور لا تظهر في أي سجل (تمرّ عبر حجب الأسرار الموحّد).
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any, Optional

PLUGIN = {
    "name": "home_control",
    "description": (
        "Talks to your home automation through adapters — Home Assistant (REST) and "
        "MQTT — to read device states, control configured devices, run scenes and watch "
        "sensors. Use for: 'turn on the bedroom light', 'what is the temperature', "
        "'activate movie mode', 'list my devices', 'add a device for the kitchen lamp', "
        "'which doors are open', 'create a scene'. Only devices you configured can be "
        "controlled, execution-type Home Assistant domains are refused outright, and "
        "locks or covers always ask before acting."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": ("status | discover | devices | add | remove | state | set | sensors | "
                                "monitor | scene | scenes | scene_delete | adapters | help"),
            },
            "device": {"type": "STRING", "description": "Device alias (as you named it)."},
            "entity": {"type": "STRING", "description": "Home Assistant entity id, e.g. light.bedroom."},
            "value": {"type": "STRING", "description": "For set: on | off | a number (brightness/temperature)."},
            "scene": {"type": "STRING", "description": "Scene name."},
            "steps": {"type": "STRING", "description": "For scene create: JSON [{\"device\",\"action\",\"value\"}]."},
            "query": {"type": "STRING", "description": "Filter for discover/sensors."},
            "disruptive": {"type": "STRING", "description": "add: mark as disruptive (yes/no)."},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "home_control",
    "title": "Home automation / أتمتة المنزل",
    "fields": [
        {"key": "adapters", "type": "text", "label": "Enabled adapters: hass, mqtt (comma separated)", "default": ""},
        {"key": "hass_url", "type": "text", "label": "Home Assistant URL (e.g. http://192.168.1.10:8123)", "default": ""},
        {"key": "hass_token", "type": "text", "label": "Home Assistant long-lived token", "default": ""},
        {"key": "mqtt_host", "type": "text", "label": "MQTT broker host", "default": ""},
        {"key": "mqtt_port", "type": "text", "label": "MQTT port", "default": "1883"},
        {"key": "mqtt_user", "type": "text", "label": "MQTT username (optional)", "default": ""},
        {"key": "mqtt_pass", "type": "text", "label": "MQTT password (optional)", "default": ""},
        {"key": "mqtt_prefix", "type": "text", "label": "MQTT topic prefix", "default": "jarvis"},
        {"key": "timeout_s", "type": "text", "label": "Seconds allowed for a request", "default": "10"},
        {"key": "confirm_disruptive", "type": "text", "label": "Ask before locks/covers/alarms (yes/no)", "default": "yes"},
    ],
}

_NS = "home_control"
_ALLOWED_DOMAINS = ("light", "switch", "fan", "media_player", "climate", "cover",
                    "lock", "scene", "script", "input_boolean", "number", "select")
_FORBIDDEN_DOMAINS = ("shell_command", "command_line", "python_script", "pyscript",
                      "homeassistant", "hassio", "hassio_", "rest_command", "notify", "recorder")
_DISRUPTIVE_DOMAINS = ("lock", "cover", "alarm_control_panel", "garage_door", "siren")
_ENTITY_RE = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")
_SENSOR_DOMAINS = ("sensor", "binary_sensor", "weather", "sun")


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


def _adapters_on() -> list[str]:
    raw = str(_cfg().get("adapters") or "").lower()
    return [a.strip() for a in raw.replace("؛", ",").split(",") if a.strip()]


# ── المخزن ────────────────────────────────────────────────────────────────────
def _load() -> dict:
    data = C.load_json(C.HOME_FILE, {"version": 1, "devices": {}, "scenes": {}, "state": {}})
    if not isinstance(data, dict):
        data = {"version": 1, "devices": {}, "scenes": {}, "state": {}}
    for key in ("devices", "scenes", "state"):
        if not isinstance(data.get(key), dict):
            data[key] = {}
    return data


def _save(data: dict) -> bool:
    return C.save_json(C.HOME_FILE, data)


# ── المهايئات ─────────────────────────────────────────────────────────────────
def _hass_headers() -> dict:
    token = str(_cfg().get("hass_token") or "").strip()
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"} if token else {}


def _hass_base() -> str:
    return str(_cfg().get("hass_url") or "").rstrip("/")


def _hass_states() -> tuple[bool, Any]:
    base = _hass_base()
    if not base:
        return False, "لم تضبط عنوان Home Assistant (hass_url) في الإعدادات."
    timeout = _int(_cfg().get("timeout_s"), 10, 3, 60)
    code, body = C.http_json(f"{base}/api/states", headers=_hass_headers(), timeout=timeout)
    if code == 200 and isinstance(body, list):
        return True, body
    if code == 401:
        return False, "رفض Home Assistant التوكن (401) — راجع hass_token."
    if code == 0:
        return False, f"تعذّر الوصول إلى Home Assistant على {C.clip(base, 60)}: {C.clip(body, 80)}"
    return False, f"Home Assistant ردّ بحالة {code}."


def _hass_service(domain: str, service: str, entity_id: str, data: Optional[dict] = None) -> tuple[bool, str]:
    """استدعاء خدمة — بدومين مسموح فقط، وبمعرّف كيان صحيح الشكل."""
    if domain in _FORBIDDEN_DOMAINS:
        return False, f"دومين '{domain}' ممنوع: لا أنفّذ أوامر من هذا النوع إطلاقًا."
    if domain not in _ALLOWED_DOMAINS:
        return False, f"دومين '{domain}' خارج القائمة المسموحة ({'، '.join(_ALLOWED_DOMAINS)})."
    if not _ENTITY_RE.match(str(entity_id or "")):
        return False, f"معرّف كيان غير صالح: {C.clip(entity_id, 40)}"
    base = _hass_base()
    if not base:
        return False, "لم تضبط عنوان Home Assistant."
    payload = dict(data or {})
    payload["entity_id"] = entity_id
    timeout = _int(_cfg().get("timeout_s"), 10, 3, 60)
    code, body = C.http_json(f"{base}/api/services/{domain}/{service}", payload,
                             headers=_hass_headers(), timeout=timeout, method="POST")
    if code in (200, 201):
        return True, f"تم: {domain}.{service} على {entity_id}"
    if code == 0:
        return False, f"تعذّر الوصول إلى Home Assistant: {C.clip(body, 80)}"
    return False, f"الخدمة ردّت بحالة {code}: {C.clip(body, 100)}"


def _mqtt_publish(topic: str, payload: dict) -> tuple[bool, str]:
    if "mqtt" not in _adapters_on():
        return False, "مهايئ MQTT غير مفعّل (adapters في الإعدادات)."
    try:
        import paho.mqtt.publish as publish  # noqa: PLC0415
    except Exception:
        return False, "حزمة paho-mqtt غير مثبّتة:  pip install paho-mqtt"
    host = str(_cfg().get("mqtt_host") or "").strip()
    if not host:
        return False, "لم تضبط وسيط MQTT (mqtt_host)."
    auth = None
    user = str(_cfg().get("mqtt_user") or "").strip()
    if user:
        auth = {"username": user, "password": str(_cfg().get("mqtt_pass") or "")}
    port = _int(_cfg().get("mqtt_port"), 1883, 1, 65535)
    try:
        publish.single(topic, payload=json.dumps(payload, ensure_ascii=False), qos=1,
                       hostname=host, port=port, auth=auth, keepalive=20)
        return True, f"نُشر على {topic}"
    except Exception as exc:
        return False, f"تعذّر النشر على MQTT: {C.clip(exc, 120)}"


_ADAPTERS = {"hass": _hass_states, "mqtt": lambda: (True, "MQTT جاهز للفحص عند النشر")}


# ── الأجهزة ───────────────────────────────────────────────────────────────────
def _device(alias: str) -> tuple[Optional[dict], str]:
    data = _load()
    dev = data["devices"].get(str(alias or "").strip())
    if not dev:
        return None, f"لا يوجد جهاز باسم '{alias}' — أضفه أولًا (add) أو اعرض القائمة (devices)."
    return dev, ""


def _entity_state(entity: str) -> tuple[Optional[dict], str]:
    ok, body = _hass_states()
    if not ok:
        return None, str(body)
    for row in body:
        if str(row.get("entity_id")) == entity:
            return row, ""
    return None, f"الكيان '{entity}' غير موجود في Home Assistant."


def _friendly(row: dict) -> str:
    attrs = row.get("attributes") or {}
    name = attrs.get("friendly_name") or row.get("entity_id")
    unit = attrs.get("unit_of_measurement", "")
    return f"{name}: {row.get('state')}{(' ' + unit) if unit else ''}"


# ── الأفعال ───────────────────────────────────────────────────────────────────
def _act_status(params: dict, player) -> str:
    del params
    on = _adapters_on()
    lines = ["سيدي، حالة أتمتة المنزل:"]
    if not on:
        lines.append("  لا مهايئ مفعّل بعد. اختر في الإعدادات: adapters = hass أو mqtt، "
                     "واملأ عنوان الخادم والتوكن.")
    for adapter in on:
        if adapter == "hass":
            ok, body = _hass_states()
            count = len(body) if ok and isinstance(body, list) else 0
            lines.append(f"  • Home Assistant: {'متصل — ' + str(count) + ' كيان' if ok else 'غير متصل — ' + str(body)}")
        elif adapter == "mqtt":
            try:
                available = importlib.util.find_spec("paho.mqtt.publish") is not None
                lines.append(("  • MQTT: الحزمة موجودة. الوسيط: " if available else
                              "  • MQTT: الحزمة paho-mqtt غير مثبّتة (pip install paho-mqtt). الوسيط: ")
                             + (str(_cfg().get('mqtt_host')) or 'لم يُضبط'))
            except Exception:
                lines.append("  • MQTT: الحزمة paho-mqtt غير مثبّتة (pip install paho-mqtt)")
        else:
            lines.append(f"  • {adapter}: مهايئ غير معروف")
    data = _load()
    lines.append(f"  الأجهزة المُعدّة: {len(data['devices'])} · المشاهد: {len(data['scenes'])}")
    out = "\n".join(lines)
    _log(player, out)
    return out


def _act_discover(params: dict, player) -> str:
    query = str(params.get("query") or "").strip().lower()
    ok, body = _hass_states()
    if not ok:
        return f"سيدي، {body}"
    rows = []
    for row in body:
        entity = str(row.get("entity_id", ""))
        domain = entity.split(".")[0]
        if query and query not in entity.lower() and query not in str(
                (row.get("attributes") or {}).get("friendly_name", "")).lower():
            continue
        if domain in _SENSOR_DOMAINS:
            continue
        rows.append(f"  • {entity} — {_friendly(row)}" + (" ⚠ حساس" if domain in _DISRUPTIVE_DOMAINS else ""))
    if not rows:
        return "سيدي، لا أجهزة مطابقة. (جرّب sensors لعرض الحسّاسات.)"
    text = (f"سيدي، وجدت {len(rows)} جهازًا في Home Assistant. "
            f"للتحكّم أضِف الجهاز باسم مستعار أولًا:\n"
            f"action=add device=اسمي entity=المعرّف\n" + "\n".join(rows[:40]))
    _log(player, text)
    return text


def _act_devices(params: dict, player) -> str:
    del params
    data = _load()
    if not data["devices"]:
        return "سيدي، لا أجهزة مُعدّة بعد. أضف واحدًا: action=add device=مصباح الغرفة entity=light.bedroom"
    lines = ["سيدي، أجهزتك المُعدّة:"]
    for alias, dev in sorted(data["devices"].items()):
        extra = ""
        if dev.get("adapter") == "hass":
            row, err = _entity_state(str(dev.get("entity", "")))
            extra = (" — " + _friendly(row)) if row else f" — ({C.clip(err, 60)})"
        lines.append(f"  • {alias} [{dev.get('adapter')}] {dev.get('entity') or dev.get('topic')}"
                     f"{' ⚠ حساس' if dev.get('disruptive') else ''}{extra}")
    out = "\n".join(lines)
    _log(player, out)
    return out


def _act_add(params: dict, player) -> str:
    alias = str(params.get("device") or "").strip()
    if not alias or " " in alias:
        return "سيدي، أعطني اسمًا مستعارًا بلا مسافات (مثل: bedroom_light)."
    entity = str(params.get("entity") or "").strip()
    if entity and not _ENTITY_RE.match(entity):
        return f"سيدي، معرّف الكيان غير صالح: {C.clip(entity, 40)} (الشكل: domain.object_id)"
    if entity:
        domain = entity.split(".")[0]
        if domain in _FORBIDDEN_DOMAINS:
            return f"سيدي، لن أضيف كيانًا من دومين '{domain}' — دومين تنفيذ أوامر."
        if domain not in _ALLOWED_DOMAINS:
            return f"سيدي، دومين '{domain}' خارج المسموح ({'، '.join(_ALLOWED_DOMAINS)})."
    data = _load()
    adapter = "hass" if entity else "mqtt"
    if adapter == "mqtt" and "mqtt" not in _adapters_on():
        # مسموح بالإعداد، لكن نقول إن المهايئ غير مفعّل
        pass
    data["devices"][alias] = {
        "adapter": adapter, "entity": entity,
        "topic": str(params.get("topic") or f"{_cfg().get('mqtt_prefix') or 'jarvis'}/{alias}/set"),
        "disruptive": C.parse_bool(params.get("disruptive"), entity.split(".")[0] in _DISRUPTIVE_DOMAINS if entity else False),
        "added": C.now_iso(),
    }
    if not _save(data):
        return "سيدي، تعذّر حفظ الجهاز."
    dev = data["devices"][alias]
    out = (f"سيدي، أضفت '{alias}' ({dev['adapter']}) → {dev.get('entity') or dev.get('topic')}"
           + (" ⚠ سيطلب تأكيدًا قبل كل أمر" if dev.get("disruptive") else ""))
    C.log_event("home_control", "add", "ok", 0, "", {"alias": alias, "adapter": adapter})
    _log(player, out)
    return out


def _do_remove(alias: str) -> str:
    data = _load()
    dev = data["devices"].pop(str(alias), None)
    if dev is None:
        return "سيدي، الجهاز لم يعد موجودًا."
    _save(data)
    C.log_event("home_control", "remove", "ok", 0, "", {"alias": alias})
    return f"سيدي، حذفت '{alias}' من قائمة أجهزتي. الجهاز نفسه في نظامك لم يُمس."


def _act_remove(params: dict, player) -> str:
    alias = str(params.get("device") or "").strip()
    dev, why = _device(alias)
    if dev is None:
        return f"سيدي، {why}"
    return C.confirm_request(
        key=f"home_remove_{alias}",
        title=f"حذف '{alias}' من أجهزة جارفس؟",
        detail=f"المهايئ: {dev.get('adapter')} · {dev.get('entity') or dev.get('topic')}\n"
               f"الحذف من ملف {C.HOME_FILE.name} فقط — لا يغيّر شيئًا في نظام منزلك.",
        run=lambda: _do_remove(alias),
        player=player,
    )


def _act_state(params: dict, player) -> str:
    alias = str(params.get("device") or "").strip()
    dev, why = _device(alias)
    if dev is None:
        return f"سيدي، {why}"
    if dev.get("adapter") != "hass":
        return f"سيدي، '{alias}' جهاز MQTT — لا أقرأ حالته (MQTT بلا حالة هنا). استخدم set للإرسال."
    row, err = _entity_state(str(dev.get("entity")))
    if row is None:
        return f"سيدي، {err}"
    attrs = row.get("attributes") or {}
    extras = []
    for key, label in (("brightness", "السطوع"), ("current_temperature", "الحرارة"),
                       ("temperature", "المطلوب"), ("battery_level", "البطارية")):
        if attrs.get(key) is not None:
            extras.append(f"{label}: {attrs[key]}")
    out = f"سيدي، {alias}: {_friendly(row)}" + ((" · " + " · ".join(extras)) if extras else "")
    _log(player, out)
    return out


def _apply_set(alias: str, dev: dict, value: str) -> tuple[bool, str]:
    """التنفيذ الفعلي للأمر (يُستدعى مباشرة أو من داخل بوابة التأكيد)."""
    adapter = str(dev.get("adapter"))
    if adapter == "mqtt":
        payload = {"state": "ON" if str(value).lower() in ("on", "1", "true", "شغل", "تشغيل") else
                   ("OFF" if str(value).lower() in ("off", "0", "false", "اطفي", "إطفاء", "أطفئ") else value),
                   "source": "jarvis"}
        ok, msg = _mqtt_publish(str(dev.get("topic")), payload)
        C.log_event("home_control", "set", "ok" if ok else "failed", 0, "" if ok else msg,
                    {"alias": alias, "value": value})
        return ok, (f"سيدي، أرسلت إلى '{alias}': {payload['state']}" if ok else f"سيدي، {msg}")
    entity = str(dev.get("entity"))
    domain = entity.split(".")[0]
    raw = str(value).strip().lower()
    if domain in ("light", "fan", "switch", "input_boolean"):
        if raw in ("on", "1", "true", "شغل", "تشغيل", "افتح"):
            ok, msg = _hass_service(domain, "turn_on", entity)
        elif raw in ("off", "0", "false", "اطفي", "إطفاء", "أطفئ", "اقفل"):
            ok, msg = _hass_service(domain, "turn_off", entity)
        elif raw.isdigit():
            ok, msg = _hass_service(domain, "turn_on", entity, {"brightness_pct": int(raw)})
        else:
            return False, f"سيدي، القيمة '{value}' غير مفهومة لهذا الجهاز (on/off أو رقم للسطوع)."
    elif domain == "lock":
        ok, msg = _hass_service("lock", "unlock" if raw in ("on", "open", "افتح") else "lock", entity)
    elif domain == "cover":
        if raw in ("on", "open", "افتح"):
            ok, msg = _hass_service("cover", "open_cover", entity)
        elif raw in ("off", "close", "اقفل", "أغلق"):
            ok, msg = _hass_service("cover", "close_cover", entity)
        elif raw in ("stop", "قف"):
            ok, msg = _hass_service("cover", "stop_cover", entity)
        else:
            return False, "سيدي، لهذا الباب: open أو close أو stop."
    elif domain == "media_player":
        if raw in ("on", "شغل"):
            ok, msg = _hass_service("media_player", "turn_on", entity)
        elif raw in ("off", "اطفي", "أوقف"):
            ok, msg = _hass_service("media_player", "turn_off", entity)
        else:
            return False, "سيدي، للمشغّل: on أو off."
    elif domain == "climate":
        if raw.isdigit():
            ok, msg = _hass_service("climate", "set_temperature", entity, {"temperature": int(raw)})
        elif raw in ("on", "شغل"):
            ok, msg = _hass_service("climate", "turn_on", entity)
        elif raw in ("off", "اطفي"):
            ok, msg = _hass_service("climate", "turn_off", entity)
        else:
            return False, "سيدي، للتكييف: رقم للحرارة أو on/off."
    elif domain == "scene":
        ok, msg = _hass_service("scene", "turn_on", entity)
    elif domain == "script":
        ok, msg = _hass_service("script", "turn_on", entity)
    else:
        return False, f"سيدي، لا أعرف كيف أتحكّم بدومين '{domain}'."
    C.log_event("home_control", "set", "ok" if ok else "failed", 0, "" if ok else msg,
                {"alias": alias, "value": value})
    return ok, (f"سيدي، {msg} ({alias} = {value})" if ok else f"سيدي، {msg}")


def _act_set(params: dict, player) -> str:
    alias = str(params.get("device") or "").strip()
    value = str(params.get("value") or "").strip()
    dev, why = _device(alias)
    if dev is None:
        return f"سيدي، {why}"
    if not value:
        return "سيدي، ما القيمة؟ (on أو off أو رقم)"
    domain = str(dev.get("entity") or "").split(".")[0]
    disruptive = bool(dev.get("disruptive")) or domain in _DISRUPTIVE_DOMAINS
    if disruptive and C.parse_bool(_cfg().get("confirm_disruptive"), True):
        return C.confirm_request(
            key=f"home_set_{alias}_{value}",
            title=f"تنفيذ أمر حسّاس على '{alias}'؟",
            detail=f"الجهاز: {dev.get('entity') or dev.get('topic')} · الأمر: {value}\n"
                   f"هذا النوع من الأجهزة (أقفال/أبواب/إنذار) يتطلب تأكيدك.",
            run=lambda: _apply_set(alias, dev, value)[1],
            player=player,
        )
    ok, msg = _apply_set(alias, dev, value)
    _log(player, msg)
    return msg


def _act_sensors(params: dict, player) -> str:
    query = str(params.get("query") or "").strip().lower()
    ok, body = _hass_states()
    if not ok:
        return f"سيدي، {body}"
    rows = []
    for row in body:
        entity = str(row.get("entity_id", ""))
        if entity.split(".")[0] not in _SENSOR_DOMAINS:
            continue
        if query and query not in entity.lower() and query not in str(
                (row.get("attributes") or {}).get("friendly_name", "")).lower():
            continue
        rows.append(f"  • {_friendly(row)}")
    if not rows:
        return "سيدي، لا حسّاسات مطابقة."
    text = f"سيدي، حسّاساتك ({len(rows)}):\n" + "\n".join(rows[:40])
    _log(player, text)
    return text


def _act_monitor(params: dict, player) -> str:
    del params
    data = _load()
    if not data["devices"]:
        return "سيدي، لا أجهزة مُعدّة لأراقبها."
    changed, unchanged, problems = [], 0, []
    for alias, dev in sorted(data["devices"].items()):
        if dev.get("adapter") != "hass":
            continue
        row, err = _entity_state(str(dev.get("entity")))
        if row is None:
            problems.append(f"{alias}: {C.clip(err, 60)}")
            continue
        state = str(row.get("state"))
        previous = (data["state"].get(alias) or {}).get("state")
        if previous is not None and previous != state:
            changed.append(f"  • {alias}: {previous} → {state}")
        else:
            unchanged += 1
        data["state"][alias] = {"state": state, "ts": C.now_iso()}
    _save(data)
    C.log_event("home_control", "monitor", "ok", 0, "", {"changed": len(changed), "same": unchanged})
    if changed and C.resolve_plugin("notification_center"):
        C.call_plugin("notification_center", {"action": "notify", "title": "تغيّر في المنزل",
                                              "body": "\n".join(changed), "category": "home", "priority": "info"})
    lines = [f"سيدي، راقبت {unchanged + len(changed)} جهازًا."]
    if changed:
        lines.append("تغيّر:")
        lines.extend(changed)
    else:
        lines.append("لا تغيّر منذ آخر مراقبة.")
    for problem in problems:
        lines.append(f"  (تعذّر: {problem})")
    out = "\n".join(lines)
    _log(player, out)
    return out


def _act_scene(params: dict, player) -> str:
    name = str(params.get("scene") or "").strip()
    raw = str(params.get("steps") or "").strip()
    if not name or not raw:
        return ("سيدي، أنشئ مشهدًا هكذا: action=scene scene=سينما "
                "steps='[{\"device\":\"tv\",\"action\":\"on\"},{\"device\":\"lamp\",\"action\":\"off\"}]'")
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, list) or not parsed:
            return "سيدي، steps يجب أن تكون قائمة خطوات."
    except Exception as exc:
        return f"سيدي، تعذّرت قراءة steps: {exc}"
    data = _load()
    clean = []
    unknown = []
    for step in parsed:
        if not isinstance(step, dict):
            continue
        alias = str(step.get("device") or "").strip()
        if alias not in data["devices"]:
            unknown.append(alias)
            continue
        clean.append({"device": alias, "value": str(step.get("value") or step.get("action") or "on")})
    if not clean:
        return f"سيدي، لا خطوات صالحة. أجهزة غير معروفة: {'، '.join(unknown) or '—'}"
    data["scenes"][name] = {"steps": clean, "created": C.now_iso()}
    _save(data)
    out = (f"سيدي، أنشأت مشهد '{name}' ب{len(clean)} خطوة: "
           + "، ".join(f"{s['device']}→{s['value']}" for s in clean)
           + (f" (تجاهلت: {'، '.join(unknown)})" if unknown else "")
           + "\nللتشغيل: action=scene scene=" + name + " — وللحذف: action=scene_delete (بتأكيد).")
    _log(player, out)
    return out


def _act_scene_trigger(params: dict, player) -> str:
    name = str(params.get("scene") or "").strip()
    data = _load()
    scene = data["scenes"].get(name)
    if not scene:
        return f"سيدي، لا مشهد باسم '{name}'. المشاهد: {'، '.join(data['scenes']) or 'لا شيء'}"
    results, ok_count = [], 0
    for step in scene.get("steps", []):
        dev = data["devices"].get(str(step.get("device")))
        if not dev:
            results.append(f"  ✗ {step.get('device')}: الجهاز لم يعد مُعدًّا")
            continue
        ok, msg = _apply_set(str(step.get("device")), dev, str(step.get("value")))
        ok_count += int(ok)
        results.append(("  ✔ " if ok else "  ✗ ") + C.clip(msg, 100))
    out = f"سيدي، مشهد '{name}': {ok_count}/{len(scene.get('steps', []))} خطوة نجحت\n" + "\n".join(results)
    _log(player, out)
    return out


def _do_scene_delete(name: str) -> str:
    data = _load()
    scene = data["scenes"].pop(str(name), None)
    if scene is None:
        return "سيدي، المشهد لم يعد موجودًا."
    _save(data)
    return f"سيدي، حذفت مشهد '{name}'. الأجهزة لم تُمس."


def _act_scene_delete(params: dict, player) -> str:
    name = str(params.get("scene") or "").strip()
    data = _load()
    scene = data["scenes"].get(name)
    if not scene:
        return f"سيدي، لا مشهد باسم '{name}'."
    return C.confirm_request(
        key=f"home_scene_del_{name}",
        title=f"حذف مشهد '{name}'؟",
        detail="الخطوات: " + "، ".join(f"{s['device']}→{s['value']}" for s in scene.get("steps", [])),
        run=lambda: _do_scene_delete(name),
        player=player,
    )


def _act_scenes(params: dict, player) -> str:
    del params
    data = _load()
    if not data["scenes"]:
        return "سيدي، لا مشاهد بعد. أنشئ واحدًا بـ action=scene."
    lines = ["سيدي، مشاهدك:"]
    for name, scene in data["scenes"].items():
        lines.append(f"  • {name}: " + "، ".join(f"{s['device']}→{s['value']}" for s in scene.get("steps", [])))
    out = "\n".join(lines)
    _log(player, out)
    return out


def _act_adapters(params: dict, player) -> str:
    del params
    out = ("سيدي، المهايئات:\n"
           "  • hass (Home Assistant REST): متاح في هذا الملف — يحتاج hass_url وhass_token.\n"
           "  • mqtt (paho-mqtt): متاح — يحتاج mqtt_host، ويُنشر JSON على موضوع كل جهاز.\n"
           "  • إضافة مزوّد آخر: دالة واحدة في _ADAPTERS بنفس العقد (بلا مساس بالبقية).\n"
           "المفعّل عندك الآن: " + ("، ".join(_adapters_on()) or "لا شيء"))
    _log(player, out)
    return out


_HELP = (
    "أتمتة المنزل، سيدي:\n"
    "• status · adapters — الحالة والمهايئات المتاحة\n"
    "• discover (query) — ما في Home Assistant · sensors — الحسّاسات\n"
    "• add device=اسم entity=light.x [disruptive=yes] · devices · remove (بتأكيد)\n"
    "• state device=اسم — الحالة الحالية · set device=اسم value=on|off|رقم\n"
    "• scene scene=اسم steps='[{...}]' · scene scene=اسم (تشغيل) · scene_delete (بتأكيد) · scenes\n"
    "• monitor — يقارن بحالة آخر مراقبة ويُشعرك بالتغيّر\n"
    "الأمان: التحكّم في أجهزتك المُعدّة فقط، دومينات التنفيذ ممنوعة، والأقفال/الأبواب تطلب تأكيدًا."
)


def run(parameters: dict, player=None, session_memory=None) -> str:
    """نقطة الدخول. لا ترمي أبدًا."""
    del session_memory
    if C is None:
        return "سيدي، ملف '_orch_core.py' المشترك غير موجود — بدونه لا تعمل أتمتة المنزل."
    try:
        p = dict(parameters or {})
        action = str(p.get("action") or "status").strip().lower()
        C.ensure_dirs()
        if action in ("status", "حالة"):
            return _act_status(p, player)
        if action in ("discover", "scan", "اكشف"):
            return _act_discover(p, player)
        if action in ("devices", "list", "أجهزة"):
            return _act_devices(p, player)
        if action in ("add", "أضف"):
            return _act_add(p, player)
        if action in ("remove", "delete", "احذف"):
            return _act_remove(p, player)
        if action in ("state", "حالة_جهاز", "read"):
            return _act_state(p, player)
        if action in ("set", "control", "شغّل"):
            return _act_set(p, player)
        if action in ("sensors", "حسّاسات", "حساسات"):
            return _act_sensors(p, player)
        if action in ("monitor", "راقب"):
            return _act_monitor(p, player)
        if action in ("scene", "مشهد"):
            if p.get("steps"):
                return _act_scene(p, player)
            if p.get("scene"):
                return _act_scene_trigger(p, player)
            return _HELP
        if action in ("scenes", "مشاهد"):
            return _act_scenes(p, player)
        if action in ("scene_delete", "scene_remove"):
            return _act_scene_delete(p, player)
        if action in ("adapters", "مهايئات"):
            return _act_adapters(p, player)
        if action in ("help", "?"):
            return _HELP
        return f"سيدي، '{action}' ليس فعلًا أعرفه في أتمتة المنزل. قل 'help'."
    except Exception as exc:  # noqa: BLE001
        return f"سيدي، أتمتة المنزل واجهت مشكلة: {str(exc)[:180]}"


# ── فحص ذاتي: python home_control.py ──────────────────────────────────────────
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
    print("home_control — self-test")
    problems = 0

    def check(label: str, ok: bool, extra: str = "") -> None:
        nonlocal problems
        print(("  ✓ " if ok else "  ✗ ") + label + (f"  [{extra}]" if extra and not ok else ""))
        if not ok:
            problems += 1

    if C is None:
        print("  ✗ النواة المشتركة غير موجودة")
        return 1

    import http.server
    import json as _json
    import tempfile
    import threading

    TOKEN = "ha_test_token_1234567890_abcdef"
    calls: list[dict] = []
    states = [
        {"entity_id": "light.bedroom", "state": "off", "attributes": {"friendly_name": "Bedroom light"}},
        {"entity_id": "switch.kitchen", "state": "off", "attributes": {"friendly_name": "Kitchen switch"}},
        {"entity_id": "lock.front_door", "state": "locked", "attributes": {"friendly_name": "Front door"}},
        {"entity_id": "sensor.living_temp", "state": "23.5", "attributes": {"unit_of_measurement": "°C",
                                                                           "friendly_name": "Living temp"}},
        {"entity_id": "cover.garage", "state": "closed", "attributes": {"friendly_name": "Garage"}},
    ]

    class _Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # noqa: A003
            return

        def _send(self, code: int, payload) -> None:
            raw = _json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):  # noqa: N802
            if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                self._send(401, {"error": "unauthorized"})
                return
            if self.path == "/api/states":
                self._send(200, states)
                return
            self._send(404, {"error": "not found"})

        def do_POST(self):  # noqa: N802
            if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                self._send(401, {"error": "unauthorized"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b"{}"
            parts = self.path.strip("/").split("/")
            if len(parts) == 4 and parts[:2] == ["api", "services"]:
                calls.append({"domain": parts[2], "service": parts[3],
                              "body": _json.loads(body.decode("utf-8") or "{}")})
                self._send(200, [])
                return
            self._send(404, {"error": "not found"})

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    ha_port = server.server_address[1]
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True).start()

    tmp = Path(tempfile.mkdtemp(prefix="hc_test_"))
    real_file, real_logs = C.HOME_FILE, C.LOG_DIR
    real_stamp, real_data = C._SCAN_CACHE.get("stamp"), C._SCAN_CACHE.get("data")
    base = C.CONFIG_DIR / "api_keys.json"
    backup = base.read_text(encoding="utf-8") if base.exists() else None
    try:
        C.HOME_FILE = tmp / "home.json"
        C.LOG_DIR = tmp / "logs"
        C.LOG_DIR.mkdir(parents=True, exist_ok=True)
        C.store_settings(_NS, {"adapters": "hass", "hass_url": f"http://127.0.0.1:{ha_port}",
                               "hass_token": TOKEN, "confirm_disruptive": "yes", "timeout_s": "5"})

        # 1) الحالة والاكتشاف
        out = run({"action": "status"})
        check("status يرى Home Assistant", "متصل" in out and "5 كيان" in out, out[:200])
        out = run({"action": "discover"})
        check("discover يعرض الأجهزة", "light.bedroom" in out and "lock.front_door" in out, out[:240])
        check("discover يوسم الحسّاس", "⚠" in out, out[:240])
        out = run({"action": "discover", "query": "bedroom"})
        check("discover يفلتر", "light.bedroom" in out and "switch.kitchen" not in out, out[:200])
        out = run({"action": "sensors"})
        check("الحسّاسات تُعرض بقيمتها", "23.5" in out and "°C" in out, out[:200])
        # 2) إضافة أجهزة والتحكّم
        out = run({"action": "add", "device": "bedroom_light", "entity": "light.bedroom"})
        check("إضافة جهاز بمعرّف صحيح", "أضفت" in out, out[:140])
        out = run({"action": "add", "device": "bad", "entity": "shell_command.x"})
        check("رفض دومين تنفيذ", "ممنوع" in out or "تنفيذ" in out, out[:140])
        out = run({"action": "add", "device": "bad2", "entity": "not-an-entity"})
        check("رفض معرّف مشوّه", "غير صالح" in out, out[:140])
        calls.clear()
        out = run({"action": "set", "device": "bedroom_light", "value": "on"})
        check("تشغيل مصباح → خدمة فعلية", any(c["domain"] == "light" and c["service"] == "turn_on"
                                        and c["body"].get("entity_id") == "light.bedroom" for c in calls),
              f"{out[:120]} {calls}")
        check("الرد يؤكد التنفيذ", "تم:" in out or "turn_on" in out, out[:140])
        calls.clear()
        out = run({"action": "set", "device": "bedroom_light", "value": "70"})
        check("سطوع برقم → brightness_pct", any(c["body"].get("brightness_pct") == 70 for c in calls), str(calls))
        out = run({"action": "state", "device": "bedroom_light"})
        check("قراءة الحالة", "Bedroom light" in out, out[:140])
        out = run({"action": "set", "device": "ghost", "value": "on"})
        check("جهاز غير مُعدّ → رسالة", "لا يوجد جهاز" in out, out[:120])
        # 3) الحساس يحتاج تأكيدًا
        run({"action": "add", "device": "front_door", "entity": "lock.front_door"})
        calls.clear()
        out = run({"action": "set", "device": "front_door", "value": "on"})
        check("القفل يطلب تأكيدًا ولا ينفّذ بلا واجهة", len(calls) == 0
              and ("confirm" in out.lower() or "أستطع" in out), f"{out[:120]} calls={calls}")
        out = run({"action": "devices"})
        check("devices يعرض الحالة والوسم", "front_door" in out and "حساس" in out, out[:240])
        # 4) المهايئات المفقودة تُعلن بلا كسر
        out = run({"action": "adapters"})
        check("adapters يشرح الثلاثة", "hass" in out and "mqtt" in out and "مزوّد آخر" in out, out[:240])
        run({"action": "add", "device": "mqtt_lamp", "entity": ""})
        out = run({"action": "set", "device": "mqtt_lamp", "value": "on"})
        check("MQTT غير مفعّل → رسالة واضحة", "غير مفعّل" in out or "غير مثبّتة" in out, out[:160])
        # 5) المشاهد
        calls.clear()
        out = run({"action": "scene", "scene": "movie", "steps": _json.dumps(
            [{"device": "bedroom_light", "value": "on"}, {"device": "front_door", "value": "off"},
             {"device": "ghost_device", "value": "on"}])})
        check("إنشاء مشهد يتجاهل المجهول", "أنشأت مشهد" in out and "ghost_device" in out, out[:220])
        out = run({"action": "scene", "scene": "movie"})
        check("تشغيل المشهد ينفّذ خطواته", "2/2" in out, out[:200])
        check("نُفّذت خدمتان فعليًا", len(calls) == 2, str(calls))
        out = run({"action": "scenes"})
        check("scenes يعرضها", "movie" in out, out[:140])
        before_scenes = len(_load()["scenes"])
        out = run({"action": "scene_delete", "scene": "movie"})
        check("حذف المشهد يستأذن", len(_load()["scenes"]) == before_scenes
              and ("confirm" in out.lower() or "أستطع" in out), out[:140])
        out = _do_scene_delete("movie")
        check("الحذف بعد الموافقة", len(_load()["scenes"]) == before_scenes - 1, out[:80])
        # 6) الحذف ببوابة
        out = run({"action": "remove", "device": "bedroom_light"})
        check("حذف جهاز يستأذن", "confirm" in out.lower() or "أستطع" in out, out[:140])
        # 7) المراقبة تكشف التغيّر
        run({"action": "monitor"})
        states[0]["state"] = "on"          # تغيّر خارجي
        out = run({"action": "monitor"})
        check("المراقبة تكشف التغيّر وتُشعر", "off → on" in out, out[:200])
        # 8) خدمة غائبة تُعالج بلطف
        C.store_settings(_NS, {"hass_url": "http://127.0.0.1:9"})
        out = run({"action": "status"})
        check("خادم غائب → رسالة بلا انهيار", "غير متصل" in out or "تعذّر" in out, out[:200])
        out = run({"action": "set", "device": "bedroom_light", "value": "on"})
        check("أمر على خدمة غائبة لا ينهار", isinstance(out, str) and len(out) > 10, out[:160])
        # 9) لا أسرار في السجل
        raw = ""
        for f in C.LOG_DIR.glob("*.jsonl"):
            raw += f.read_text(encoding="utf-8")
        check("التوكن لا يظهر في أي سجل", TOKEN not in raw)
        check("فعل مجهول", "ليس فعلًا" in run({"action": "zzz"}))
    finally:
        try:
            server.shutdown()
            server.server_close()
        except Exception:
            pass
        C.HOME_FILE, C.LOG_DIR = real_file, real_logs
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
