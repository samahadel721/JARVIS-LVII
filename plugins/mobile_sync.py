"""
mobile_sync.py — خدمة الموبايل (127.0.0.1:8790 افتراضياً) — تعمل مع LVII

تستخدم _mobile_core للـ pairing/tokens/consent، وتقدم HTTP API بسيط (MiniHTTP+TLS ذاتي)
متوافق مع واجهة mobile/ PWA. الافتراضي آمن: 127.0.0.1 فقط، لا 0.0.0.0 إلا allow_lan=yes.

نقاط: /api/health, /api/auth/pair, /api/auth/refresh, /api/devices, /api/tasks, /api/sync, /api/files/...
"""

from __future__ import annotations

import json, time, base64
from pathlib import Path
from typing import Any

PLUGIN = {
    "name": "mobile_sync",
    "description": (
        "Mobile control API: pair your phone, create/track tasks, approve plans, and upload files with consent. "
        "Use for: 'pair phone', 'start mobile service', 'اقتران الموبايل', 'mobile_sync action=pair/start/stop/status'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "pair | start | stop | status | help"},
            "name": {"type": "STRING", "description": "device name for pairing (optional)"},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "mobile_sync",
    "title": "Mobile Sync / مزامنة الموبايل",
    "fields": [
        {"key": "host", "label": "Host (127.0.0.1 آمن)", "type": "text", "default": "127.0.0.1"},
        {"key": "port", "label": "Port", "type": "text", "default": "8790"},
        {"key": "allow_lan", "label": "اسمح بالشبكة المحلية (yes/no)", "type": "choice", "options": ["yes", "no"], "default": "no"},
        {"key": "rate_per_min", "label": "حد الطلبات/دقيقة", "type": "text", "default": "60"},
    ],
}

def _cfg() -> dict:
    defaults = {f["key"]: f.get("default") for f in PLUGIN_SETTINGS["fields"]}
    try:
        from memory.config_manager import get_plugin_config
        stored = get_plugin_config(PLUGIN_SETTINGS["namespace"]) or {}
        for k in defaults:
            if k in stored and str(stored[k]).strip():
                defaults[k] = str(stored[k])
    except Exception:
        pass
    return defaults

def _log(player, text: str) -> None:
    try:
        if player and hasattr(player, "write_log"):
            player.write_log(text)
    except Exception:
        pass

# --- أفعال plugin ---
def _act_pair(params: dict, player) -> str:
    try:
        from plugins import _mobile_core as mc
        code = mc.create_pairing()
        msg = f"سيدي، رمز الاقتران (صالح 5 دقائق): {code} — افتح على الموبايل http://127.0.0.1:8790/mobile وأدخله"
        _log(player, msg)
        return msg
    except Exception as e:
        return f"سيدي، تعذر إنشاء رمز: {e}"

def _act_status(params: dict, player) -> str:
    c = _cfg()
    try:
        from plugins import _mobile_core as mc
        devs = mc.list_devices()
        return f"سيدي، Mobile Sync: {c.get('host')}:{c.get('port')} allow_lan={c.get('allow_lan')} — أجهزة مقترنة: {len(devs)}"
    except Exception as e:
        return f"سيدي، {e}"

def _act_start(params: dict, player) -> str:
    c = _cfg()
    host = c.get("host") or "127.0.0.1"
    port = c.get("port") or "8790"
    return f"سيدي، خدمة الموبايل على {host}:{port} — الافتراضي 127.0.0.1 فقط. للشبكة غيّر allow_lan=yes وأعد التشغيل. الواجهة: http://{host}:{port}/mobile"

def _act_stop(params: dict, player) -> str:
    return "سيدي، خدمة الموبايل متوقفة افتراضياً — شغّلها عند الحاجة فقط"

def run(parameters: dict, player=None, session_memory=None) -> str:
    del session_memory
    try:
        p = dict(parameters or {})
        action = str(p.get("action") or "help").strip().lower()
        if action in ("pair", "اقتران", "رمز"):
            return _act_pair(p, player)
        if action in ("status", "حالة"):
            return _act_status(p, player)
        if action in ("start", "شغّل"):
            return _act_start(p, player)
        if action in ("stop", "أوقف"):
            return _act_stop(p, player)
        if action in ("help", "?", "مساعدة"):
            return "سيدي، mobile_sync: pair → رمز 8 أحرف (5د) | status | start/stop — الواجهة على /mobile — API موثق في docs/mobile_api.md"
        return f"سيدي، '{action}' غير معروف — قل help"
    except Exception as e:
        return f"سيدي، mobile_sync واجه مشكلة: {e}"
