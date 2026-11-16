"""
face_gesture_control.py — Face Lock + Gesture Control + Holo Desk (Torvis 0.6)

  Face Lock: قفل بصري — لا يستجيب إلا لوجهك (يستخدم face_auth + _face_core الموجودة)
  Gesture Control: إشارات يدوية للتحكم (سلام/قبضة/إشارة) → أوامر (توقيف/تشغيل/رفع صوت)
  Holo Desk: عرض معلومات/أدوات على الشاشة كلوحة ثلاثية شفافة (overlay عبر player.show_content)

يعمل فوق الإضافات الموجودة:
  - face_auth.py + _face_core.py (التحقق)
  - sign_language.py + mediapipe/OpenCV (اليد)
  - لا يطلب كاميرا جديدة — يعيد استخدام نفس المحرك

الخصوصية: لا صور محفوظة، البصمات أرقام فقط في config/orchestration/extras/
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

PLUGIN = {
    "name": "face_gesture_control",
    "description": (
        "Control JARVIS with your face and hands. Use for: 'فعل Face Lock', 'شغّل Gesture Control', "
        "'افتح Holo Desk', 'اقفل بالوجه', 'تحكم بالإشارة', 'show holo desk', 'face lock on/off'. "
        "Face Lock = only your face unlocks JARVIS. Gesture Control = hand gestures map to commands (wave=pause, fist=mute, etc.). "
        "Holo Desk = a floating info panel."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "face_lock | gesture | holo | status | help — face_lock: on/off/check, gesture: on/off/list, holo: show/hide"},
            "mode": {"type": "STRING", "description": "on/off/toggle/check/list/show/hide"},
            "gesture_map": {"type": "STRING", "description": "لتخصيص: JSON مثل '{\"wave\":\"pause\"}'"},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "face_gesture_control",
    "title": "Face & Gesture / الوجه والإشارة",
    "fields": [
        {"key": "face_lock", "label": "قفل بالوجه (on/off)", "type": "choice", "options": ["on", "off"], "default": "off"},
        {"key": "gesture_enabled", "label": "تحكم بالإشارة (on/off)", "type": "choice", "options": ["on", "off"], "default": "off"},
        {"key": "holo_opacity", "label": "شفافية Holo Desk (0.3-1.0)", "type": "text", "default": "0.85"},
        {"key": "gesture_sensitivity", "label": "حساسية الإشارة (0.5-0.95)", "type": "text", "default": "0.8"},
    ],
}

STATE_FILE = Path(__file__).resolve().parent.parent / "memory" / "face_gesture_state.json"
DEFAULT_GESTURE_MAP = {
    "open_palm": "pause",      # كف مفتوح = إيقاف/تشغيل
    "fist": "mute",             # قبضة = كتم
    "peace": "next",            # إصبعين = التالي
    "thumbs_up": "confirm",     # إبهام = تأكيد
    "wave": "wake",             # تلويح = يا فالح
}

def _cfg() -> dict:
    defaults = {f["key"]: f.get("default") for f in PLUGIN_SETTINGS["fields"]}
    try:
        from memory.config_manager import get_plugin_config
        stored = get_plugin_config(PLUGIN_SETTINGS["namespace"]) or {}
        for k in defaults:
            if k in stored and stored[k] not in (None, ""):
                defaults[k] = str(stored[k])
    except Exception:
        pass
    return defaults

def _save_state(extra: dict | None = None) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = {"ts": time.time(), "cfg": _cfg()}
        if extra:
            data.update(extra)
        STATE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

def _log(player, text: str) -> None:
    try:
        if player and hasattr(player, "write_log"):
            player.write_log(text)
    except Exception:
        pass

def _has_face_auth() -> bool:
    try:
        import importlib.util
        return importlib.util.find_spec("plugins.face_auth") is not None or (Path(__file__).parent / "face_auth.py").exists()
    except Exception:
        return False

def _has_sign_language() -> bool:
    p = Path(__file__).parent / "sign_language.py"
    return p.exists()

# --- الأفعال ---
def _act_face_lock(params: dict, player) -> str:
    mode = str(params.get("mode") or "").strip().lower()
    cfg = _cfg()
    if mode in ("on", "فعل", "شغل"):
        if not _has_face_auth():
            return "سيدي، face_auth غير موجود — ثبّته أولاً ثم فعّل Face Lock"
        _save_state({"face_lock": "on"})
        try:
            from memory.config_manager import set_plugin_config
            set_plugin_config(PLUGIN_SETTINGS["namespace"], "face_lock", "on")
        except Exception:
            pass
        msg = "سيدي، ✅ Face Lock مفعّل — لن أستجيب إلا لوجهك (يستخدم بصمة face_auth). للإلغاء: face_lock mode=off"
        _log(player, msg)
        return msg
    if mode in ("off", "اطفئ", "عطل"):
        try:
            from memory.config_manager import set_plugin_config
            set_plugin_config(PLUGIN_SETTINGS["namespace"], "face_lock", "off")
        except Exception:
            pass
        _save_state({"face_lock": "off"})
        return "سيدي، Face Lock متوقف — أستجيب للجميع"
    if mode in ("check", "تحقق", "افحص"):
        # حاول تحقق فعلي عبر face_auth
        try:
            import plugins.face_auth as fa
            res = fa.run({"action": "verify"}, player)
            _save_state({"face_lock_check": res[:200]})
            return f"سيدي، فحص الوجه:\n{res}"
        except Exception as e:
            return f"سيدي، face_auth غير جاهز: {e} — سجّل وجهك أولاً: face_auth action=enroll"
    return "سيدي، face_lock mode=on/off/check — مثال: face_gesture_control action=face_lock mode=on"

def _act_gesture(params: dict, player) -> str:
    mode = str(params.get("mode") or "").strip().lower()
    gmap = str(params.get("gesture_map") or "").strip()
    if mode in ("on", "فعل", "شغل"):
        if not _has_sign_language():
            return "سيدي، sign_language غير موجود — ثبّتها لـ Gesture Control"
        try:
            from memory.config_manager import set_plugin_config
            set_plugin_config(PLUGIN_SETTINGS["namespace"], "gesture_enabled", "on")
        except Exception:
            pass
        if gmap:
            try:
                m = json.loads(gmap)
                if isinstance(m, dict):
                    _save_state({"gesture_map": m, "gesture": "on"})
            except Exception:
                pass
        else:
            _save_state({"gesture": "on", "map": DEFAULT_GESTURE_MAP})
        msg = (
            "سيدي، ✅ Gesture Control مفعّل — الإشارات:\n"
            "  كف مفتوح → pause | قبضة → mute | إصبعين → next | تلويح → wake\n"
            "تحتاج mediapipe لدقة عالية (pip install mediapipe) — وإلا OpenCV تقريبي. قل: gesture mode=list لرؤية الخريطة"
        )
        _log(player, msg)
        return msg
    if mode in ("off", "اطفئ"):
        try:
            from memory.config_manager import set_plugin_config
            set_plugin_config(PLUGIN_SETTINGS["namespace"], "gesture_enabled", "off")
        except Exception:
            pass
        _save_state({"gesture": "off"})
        return "سيدي، Gesture Control متوقف"
    if mode in ("list", "خريطة", "show"):
        flows = DEFAULT_GESTURE_MAP
        try:
            if STATE_FILE.exists():
                data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                flows = data.get("gesture_map") or data.get("map") or flows
        except Exception:
            pass
        lines = ["سيدي، خريطة الإشارات:"]
        for g, cmd in flows.items():
            lines.append(f"  {g} → {cmd}")
        return "\n".join(lines)
    return "سيدي، gesture mode=on/off/list — مثال: face_gesture_control action=gesture mode=on"

def _act_holo(params: dict, player) -> str:
    mode = str(params.get("mode") or "show").strip().lower()
    if mode in ("show", "افتح", "on"):
        # Holo Desk: لوحة شفافة — نستخدم player.show_content كـ overlay
        content = (
            "### Holo Desk — لوحة ثلاثية شفافة\n"
            "- **الطقس:** عبر daily_briefing\n"
            "- **المهام:** Background Tasks (core/tasks)\n"
            "- **الذاكرة:** memory/long_term.json\n"
            "- **الكاميرا:** Face Lock + Gesture أعلاه\n\n"
            "قل: screen_agent action=look لترى الشاشة، أو video_summarizer لتلخيص فيديو"
        )
        try:
            if player and hasattr(player, "show_content"):
                player.show_content("Holo Desk", content)
                return "سيدي، فتحت Holo Desk على الشاشة (overlay) — شفافية " + _cfg().get("holo_opacity", "0.85")
        except Exception:
            pass
        _log(player, content)
        return f"سيدي، Holo Desk (نصي):\n{content}"
    if mode in ("hide", "اقفل", "off"):
        try:
            if player and hasattr(player, "show_content"):
                player.show_content("", "")
        except Exception:
            pass
        return "سيدي، أغلقت Holo Desk"
    return "سيدي، holo mode=show/hide"

def _act_status(params: dict, player) -> str:
    cfg = _cfg()
    has_fa = _has_face_auth()
    has_sl = _has_sign_language()
    out = (
        f"سيدي، Face & Gesture:\n"
        f"  Face Lock: {cfg.get('face_lock')} (face_auth موجود: {has_fa})\n"
        f"  Gesture: {cfg.get('gesture_enabled')} (sign_language موجود: {has_sl})\n"
        f"  Holo opacity: {cfg.get('holo_opacity')}\n"
        f"  مصدر البصمة: config/orchestration/extras/ (لا صور محفوظة)"
    )
    _log(player, out)
    return out

_HELP = (
    "التحكم بالوجه والإيد — Torvis 0.6، سيدي:\n"
    "• face_lock mode=on/off/check — قفل لا يستجيب إلا لوجهك (يحتاج face_auth مسجّل)\n"
    "• gesture mode=on/off/list — إشارات يد: كف/قبضة/سلام → أوامر\n"
    "• holo mode=show/hide — لوحة Holo Desk الشفافة\n"
    "• status / help\n"
    "الخصوصية: البصمات أرقام فقط (0600)، لا صور، لا إرسال للخارج\n"
)

def run(parameters: dict, player=None, session_memory=None) -> str:
    del session_memory
    try:
        p = dict(parameters or {})
        action = str(p.get("action") or "status").strip().lower()
        if action in ("face_lock", "face", "وجه", "قفل"):
            return _act_face_lock(p, player)
        if action in ("gesture", "gestures", "إشارة", "ايد"):
            return _act_gesture(p, player)
        if action in ("holo", "desk", "لوحة", "holo_desk"):
            return _act_holo(p, player)
        if action in ("status", "حالة"):
            return _act_status(p, player)
        if action in ("help", "?", "مساعدة"):
            return _HELP
        return f"سيدي، '{action}' غير معروف — قل help"
    except Exception as e:
        return f"سيدي، face_gesture_control واجه مشكلة: {e}"
