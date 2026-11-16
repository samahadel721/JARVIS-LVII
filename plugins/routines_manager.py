"""
routines_manager.py — إدارة الروتينات + Quiet Hours (LVII)

LVII وحّد 4 جداول قديمة (briefing/check-in/news/reminders) في محرك routines واحد.
هذه الإضافة تتحكم فيه من الشات — الروتينات محفوظة في core/routines/ وتعرض في TASKS/ROUTINES panel.

  routines_manager action=list → كل الروتينات
  routines_manager action=create when="every Friday 17:00" what="ذكّرني أبعت التقرير"
  routines_manager action=toggle name=... enabled=no
  routines_manager action=quiet start="23:00" end="07:00"
"""

from __future__ import annotations

import json
from pathlib import Path

PLUGIN = {
    "name": "routines_manager",
    "description": (
        "Manage routines that run unasked: once, weekly, monthly, every N minutes, at startup or after quiet. "
        "Use for: 'اعرض الروتينات', 'كل جمعة 17:00 ذكرني...', 'وقت الهدوء من 11 ل 7', 'routines list/create/toggle/quiet'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "list | create | toggle | quiet | help"},
            "name": {"type": "STRING", "description": "اسم الروتين للـ toggle"},
            "when": {"type": "STRING", "description": "لـ create: when مثل 'every weekday 08:30' / 'weekly Mon 17:00' / 'once 2026-11-15 09:00'"},
            "what": {"type": "STRING", "description": "لـ create: ماذا يفعل — نص أو تعليمات لمهمة"},
            "enabled": {"type": "STRING", "description": "لـ toggle: yes/no"},
            "start": {"type": "STRING", "description": "لـ quiet: بداية الهدوء HH:MM"},
            "end": {"type": "STRING", "description": "لـ quiet: نهاية الهدوء HH:MM"},
        },
        "required": ["action"],
    },
}

def _try_import():
    try:
        from core.routines.engine import RoutineStore
        return RoutineStore
    except Exception:
        return None

def run(parameters: dict, player=None, session_memory=None) -> str:
    del session_memory
    try:
        p = dict(parameters or {})
        action = str(p.get("action") or "list").strip().lower()
        if action in ("help","?","مساعدة"):
            return "سيدي، الروتينات:\n• list — كل الروتينات (من TASKS/ROUTINES panel أيضاً)\n• create when=\"...\" what=\"...\" — أنشئ روتين\n• toggle name=... enabled=yes/no — فعّل/عطّل\n• quiet start=\"23:00\" end=\"07:00\" — وقت الهدوء (لا يزعجك إلا التذكير المحدد لوقت)\n"
        Store = _try_import()
        if not Store:
            return "سيدي، محرك الروتينات غير متاح في هذه النسخة — حدّث إلى LVII الكامل"
        store = Store()
        if action in ("list","قائمة"):
            items = store.list() if hasattr(store, "list") else []
            if not items:
                return "سيدي، لا روتينات — أنشئ واحد: routines_manager action=create when=\"every weekday 08:30\" what=\"قلّي الطقس ومواعيدي\""
            lines=[f"سيدي، روتينات ({len(items)}):"]
            for r in items[:10]:
                try:
                    lines.append(f"  • {getattr(r,'name',r.get('name','?'))} — {getattr(r,'when',r.get('when',''))} — {'on' if getattr(r,'enabled',True) else 'off'}: {str(getattr(r,'what',r.get('what','')))[:40]}")
                except Exception:
                    lines.append(f"  • {r}")
            return "\n".join(lines)
        if action in ("create","أنشئ","add"):
            when = str(p.get("when") or "").strip()
            what = str(p.get("what") or p.get("text") or "").strip()
            if not when or not what:
                return "سيدي، when= و what= مطلوبان — مثال: when=\"every Friday 17:00\" what=\"ذكرني أبعت التقرير\""
            try:
                r = store.create(when=when, what=what) if hasattr(store, "create") else None
                return f"سيدي، أنشأت روتين: {when} → {what[:60]} — {getattr(r,'id',r) if r else 'تم'}"
            except Exception as e:
                return f"سيدي، تعذر إنشاء الروتين: {e}"
        if action in ("toggle","تفعيل","switch"):
            name = str(p.get("name") or "").strip()
            enabled = str(p.get("enabled") or "yes").strip().lower() in ("yes","true","1","on","مفعل")
            if not name:
                return "سيدي، name= اسم الروتين"
            try:
                store.set_enabled(name, enabled)
                return f"سيدي، {'فعّلت' if enabled else 'عطّلت'} روتين {name}"
            except Exception as e:
                return f"سيدي، {e}"
        if action in ("quiet","هدوء","silence"):
            start = str(p.get("start") or "23:00").strip()
            end = str(p.get("end") or "07:00").strip()
            try:
                if hasattr(store, "set_quiet_hours"):
                    store.set_quiet_hours(start, end)
                return f"سيدي، وقت الهدوء: {start} → {end} — التذكيرات المحددة لوقت ستظل ترن، الباقي ينتظر"
            except Exception as e:
                return f"سيدي، {e}"
        return f"سيدي، '{action}' غير معروف — قل help"
    except Exception as e:
        return f"سيدي، routines_manager واجه مشكلة: {e}"
