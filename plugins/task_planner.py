"""task_planner.py — مخطط مهام مبسط (يغلف core/tasks) مع دعم جملة واحدة = عدة مهام."""
from __future__ import annotations
import re
PLUGIN = {
    "name": "task_planner",
    "description": "Plan multi-step tasks. Use for: 'plan a task to ...', 'ابحث ثم لخص', 'check weather and remind me'. Supports one sentence = multiple ordered tasks.",
    "parameters": {"type":"OBJECT","properties":{"action":{"type":"STRING"},"text":{"type":"STRING"},"name":{"type":"STRING"},"steps":{"type":"STRING"}},"required":["action"]},
}
def run(parameters: dict, player=None, session_memory=None) -> str:
    p = dict(parameters or {})
    action = str(p.get("action") or "help").lower()
    if action in ("help","?"):
        return "سيدي، task_planner: plan text=\"...\" → يخطط (يدعم 'و/then/and' لعدة مهام) + show/approve/run/pause/resume/cancel/list/status"
    if action in ("plan","create"):
        text = str(p.get("text") or "").strip()
        if not text: return "سيدي، ما الهدف؟ text=..."
        # تقسيم موسع (Torvis مرحلة 1)
        sep = r"\s*(?:ثم|وبعدها|بعد كده|بعد ذلك|كمان|أيضا|أيضاً|then|after that|and then|and|also|plus|،|,|؛|;|\.|\s+و\s*)\s*"
        parts = [x.strip() for x in re.split(sep, text) if x.strip() and x not in ("و","ثم")]
        if len(parts)<2: parts=[text]
        # أنشئ مهمة في core/tasks لكل جزء
        try:
            from core.tasks.store import TaskStore
            from core.tasks.engine import create_task
            ids=[]
            for part in parts:
                tid = create_task(part, agent="general")
                ids.append(tid)
            ack = f"⚡ حاضر سيدي — شغال على {len(parts)} مهام بالترتيب" if len(parts)>1 else "⚡ حاضر سيدي — شغال على مهمة واحدة"
            return f"{ack}\nسيدي، أنشأت {len(parts)} مهمة: {', '.join(ids)} — تابعه من TASKS panel أو قل task_planner action=list"
        except Exception as e:
            return f"سيدي، تعذر إنشاء المهمة: {e}"
    # أوامر أخرى تمرر لـ core/tasks
    try:
        from core.tasks.store import TaskStore
        store = TaskStore()
        if action in ("list","قائمة"):
            tasks = store.list_tasks()
            if not tasks: return "سيدي، لا مهام بعد"
            lines=[f"سيدي، مهامك ({len(tasks)}):"]
            for t in tasks[:10]: lines.append(f"  • {t.id} [{t.status}] {t.title[:40]}")
            return "\n".join(lines)
        if action in ("show","status"):
            tid = str(p.get("name") or "").strip()
            if not tid: return "سيدي، name= معرّف المهمة"
            t = store.get(tid)
            if not t: return f"سيدي، لا مهمة {tid}"
            return f"سيدي، {t.id} [{t.status}] {t.title}\n{t.goal[:200]}"
    except Exception as e:
        return f"سيدي، {e}"
    return f"سيدي، '{action}' — استخدم plan/list/show"
