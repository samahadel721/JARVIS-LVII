"""
hud_control.py — تحكم Command Center (LVII)

LVII أضاف HUD مركزي: HoloAvatar + Reactor 3D + metrics + overlays.
هذه الإضافة تتحكم فيه من الشات/الصوت بدون لمس الكود الأصلي.

  hud_control action=status → حالة الـ HUD
  hud_control action=theme name=dark/light/holo
  hud_control action=reactor mode=on/off
  hud_control action=stats → CPU/RAM/GPU
"""

from __future__ import annotations

PLUGIN = {
    "name": "hud_control",
    "description": (
        "Control the Command Center HUD: avatar, reactor, theme and live metrics. "
        "Use for: 'غير ثيم الهود', 'شغّل المفاعل', 'اعرض حالة الهود', 'hud theme=dark', 'hud stats'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "status | theme | reactor | stats | help"},
            "name": {"type": "STRING", "description": "لـ theme: dark/light/holo/midnight"},
            "mode": {"type": "STRING", "description": "لـ reactor: on/off"},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "hud_control",
    "title": "HUD Control / لوحة القيادة",
    "fields": [
        {"key": "theme", "label": "الثيم (dark/light/holo)", "type": "choice", "options": ["dark", "light", "holo", "midnight"], "default": "holo"},
        {"key": "reactor_enabled", "label": "المفاعل ثلاثي الأبعاد (on/off)", "type": "choice", "options": ["on", "off"], "default": "on"},
        {"key": "show_metrics", "label": "إظهار المقاييس (on/off)", "type": "choice", "options": ["on", "off"], "default": "on"},
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

def _stats() -> str:
    try:
        import psutil
        cpu = psutil.cpu_percent(interval=0.3)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        return f"CPU {cpu:.0f}% • RAM {mem.percent:.0f}% ({mem.used//1024//1024}MB/{mem.total//1024//1024}MB) • Disk {disk.percent:.0f}%"
    except Exception as e:
        return f"المقاييس غير متاحة: {e}"

def run(parameters: dict, player=None, session_memory=None) -> str:
    del session_memory
    try:
        p = dict(parameters or {})
        action = str(p.get("action") or "status").strip().lower()
        if action in ("help","?","مساعدة"):
            return "سيدي، HUD:\n• status — حالة الهود والمفاعل\n• theme name=dark/holo — غيّر الثيم\n• reactor mode=on/off — المفاعل 3D\n• stats — CPU/RAM/Disk مباشر"
        if action in ("status","حالة"):
            c = _cfg()
            has_reactor = False
            try:
                from gui.reactor import Reactor3D
                has_reactor = Reactor3D is not None
            except Exception:
                pass
            return f"سيدي، HUD: theme={c.get('theme')} reactor={c.get('reactor_enabled')} (متاح: {has_reactor}) metrics={c.get('show_metrics')} — {_stats()}"
        if action in ("theme","ثيم","color"):
            name = str(p.get("name") or "").strip().lower()
            if not name:
                return "سيدي، name= dark/light/holo/midnight"
            if name not in ("dark","light","holo","midnight"):
                return f"سيدي، '{name}' غير معروف — dark/light/holo/midnight"
            try:
                from memory.config_manager import set_plugin_config
                set_plugin_config(PLUGIN_SETTINGS["namespace"], "theme", name)
            except Exception:
                pass
            # حاول تطبيق مباشر إن كان HUD مفتوح
            try:
                if player and hasattr(player, "set_theme"):
                    player.set_theme(name)
            except Exception:
                pass
            return f"سيدي، غيّرت ثيم الهود إلى {name}"
        if action in ("reactor","مفاعل","core"):
            mode = str(p.get("mode") or "on").strip().lower()
            on = mode in ("on","1","true","شغل","فعل")
            try:
                from memory.config_manager import set_plugin_config
                set_plugin_config(PLUGIN_SETTINGS["namespace"], "reactor_enabled", "on" if on else "off")
            except Exception:
                pass
            return f"سيدي، المفاعل {'مفعّل' if on else 'متوقف'} — سيظهر عند إعادة فتح الهود"
        if action in ("stats","مقاييس","metrics"):
            return f"سيدي، {_stats()}"
        return f"سيدي، '{action}' غير معروف — قل help"
    except Exception as e:
        return f"سيدي، hud_control واجه مشكلة: {e}"
