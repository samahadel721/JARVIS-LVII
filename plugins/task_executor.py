"""task_executor.py — منفذ (يغلف core/tasks/engine)"""
from __future__ import annotations
PLUGIN = {
    "name": "task_executor",
    "description": "Execute approved plans step by step with progress. Use after task_planner approval.",
    "parameters": {"type":"OBJECT","properties":{"action":{"type":"STRING"},"name":{"type":"STRING"}},"required":["action"]},
}
def run(parameters: dict, player=None, session_memory=None) -> str:
    p=dict(parameters or {})
    action=str(p.get("action") or "help").lower()
    if action in ("help","?"):
        return "سيدي، task_executor: execute name=... | progress name=... | pause/resume/cancel — يستخدم core/tasks engine"
    tid=str(p.get("name") or "").strip()
    if not tid: return "سيدي، name= معرّف المهمة"
    try:
        from core.tasks.store import TaskStore
        from core.tasks.engine import cancel_task, steer_task
        store=TaskStore()
        t=store.get(tid)
        if not t: return f"سيدي، لا مهمة {tid}"
        if action in ("progress","show"):
            return f"سيدي، {t.id} [{t.status}] {t.title} — {len(t.turns)} جولة"
        if action=="pause":
            cancel_task(tid, finish=False); return f"سيدي، أوقفت {tid} مؤقتاً"
        if action=="resume":
            return f"سيدي، استئناف {tid} — ستكمل تلقائياً"
        if action=="cancel":
            cancel_task(tid, finish=False); return f"سيدي، ألغيت {tid}"
        if action in ("execute","run"):
            return f"سيدي، ⚡ On it — المهمة {tid} تعمل في الخلفية (TASKS panel) — سأبلغك عند الانتهاء"
    except Exception as e:
        return f"سيدي، {e}"
    return f"سيدي، '{action}' غير معروف"
