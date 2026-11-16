"""
panels_hub.py — مركز اللوحات (TASKS/ROUTINES/LIVE)

LVII عنده 3 لوحات في الهود: TASKS (المهام الخلفية) + ROUTINES (الروتينات) + LIVE (البث).
هذه الإضافة تتحكم فيها من الشات.

  panels_hub action=show panel=tasks/routines/live/all
  panels_hub action=hide panel=live
"""

from __future__ import annotations

PLUGIN = {
    "name": "panels_hub",
    "description": (
        "Show/hide the HUD panels: TASKS, ROUTINES and LIVE stream. "
        "Use for: 'اعرض لوحة المهام', 'اخف البث', 'panels_hub show/hide panel=tasks/routines/live/all'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "show | hide | status | help"},
            "panel": {"type": "STRING", "description": "tasks/routines/live/all (افتراضي all)"},
        },
        "required": ["action"],
    },
}

def run(parameters: dict, player=None, session_memory=None) -> str:
    del session_memory
    try:
        p = dict(parameters or {})
        action = str(p.get("action") or "help").strip().lower()
        panel = str(p.get("panel") or "all").strip().lower()
        if action in ("help","?"):
            return "سيدي، اللوحات:\n• show panel=tasks/routines/live/all — اعرض\n• hide panel=live — اخف\n• status — حالة اللوحات\nTASKS=المهام الخلفية، ROUTINES=الروتينات، LIVE=البث الحي"
        if action in ("status","حالة"):
            # حاول قراءة المهام/الروتينات الفعلية
            try:
                from core.tasks.store import TaskStore
                n_tasks = len(TaskStore().list_tasks())
            except Exception:
                n_tasks = "?"
            try:
                from core.routines.engine import RoutineStore
                n_rout = len(RoutineStore().list())
            except Exception:
                n_rout = "?"
            return f"سيدي، اللوحات: TASKS={n_tasks} مهمة، ROUTINES={n_rout} روتين، LIVE=يعمل — الهود يعرضها في الشريط الجانبي"
        if action in ("show","اعرض","افتح"):
            try:
                if player and hasattr(player, "show_panel"):
                    player.show_panel(panel, True)
            except Exception:
                pass
            # fallback: اعرض كمحتوى
            try:
                if panel in ("tasks","all"):
                    from core.tasks.store import TaskStore
                    tasks = TaskStore().list_tasks()
                    txt = "\n".join(f"• {t.id} [{t.status}] {t.title[:40]}" for t in tasks[:8]) or "لا مهام"
                    if player and hasattr(player, "show_content"):
                        player.show_content("TASKS", txt)
            except Exception:
                pass
            return f"سيدي، عرضت لوحة {panel}"
        if action in ("hide","اخف","اقفل"):
            try:
                if player and hasattr(player, "show_panel"):
                    player.show_panel(panel, False)
            except Exception:
                pass
            return f"سيدي، أخفيت لوحة {panel}"
        return f"سيدي، '{action}' غير معروف — show/hide/status"
    except Exception as e:
        return f"سيدي، panels_hub واجه مشكلة: {e}"
