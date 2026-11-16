"""
agi_learn.py — التعلم المستمر (AGI memory)

يحفظ خبرة JARVIS من كل مهمة: ماذا حاول، ماذا نجح، ماذا فشل — ويسترجعها عند مهمة مشابهة.
يستخدم memory/agi_memory.json + agi_lessons.jsonl (0600) — لا يرسل للخارج.

مع LVII: بعد كل background task، استدعِ agi_learn action=log lesson="..."

  agi_learn action=log lesson="جربت X وفشل لأن Y"
  agi_learn action=recall query="متجر" → دروس مشابهة
  agi_learn action=lessons → كل الدروس
"""

from __future__ import annotations

import json
import time
from pathlib import Path

PLUGIN = {
    "name": "agi_learn",
    "description": (
        "Continuous learning memory: log lessons from tasks and recall them for similar goals. "
        "Use for: 'احفظ ما تعلمته', 'تذكر دروس المتجر', 'agi_learn log/recall/lessons'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "log | recall | lessons | status | help"},
            "lesson": {"type": "STRING", "description": "لـ log: الدرس المستفاد"},
            "query": {"type": "STRING", "description": "لـ recall: موضوع للبحث في الدروس"},
        },
        "required": ["action"],
    },
}

MEM = Path(__file__).resolve().parent.parent / "memory" / "agi_memory.json"
LESSONS = Path(__file__).resolve().parent.parent / "memory" / "agi_lessons.jsonl"

def _load_lessons() -> list[dict]:
    if not LESSONS.exists():
        return []
    out=[]
    try:
        for line in LESSONS.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
    except Exception:
        pass
    return out

def run(parameters: dict, player=None, session_memory=None) -> str:
    del session_memory
    try:
        p = dict(parameters or {})
        action = str(p.get("action") or "help").strip().lower()
        if action in ("help","?"):
            return "سيدي، التعلم المستمر:\n• log lesson=\"...\" — احفظ درساً\n• recall query=\"متجر\" — ابحث في الدروس\n• lessons — كل الدروس\n• status — عدد الدروس والأهداف\nكلها محلية 0600 — لا تُرسل للخارج"
        if action in ("status","حالة"):
            mem = {}
            try:
                if MEM.exists():
                    mem = json.loads(MEM.read_text(encoding="utf-8"))
            except Exception:
                pass
            n_less = len(_load_lessons())
            n_goals = len(mem.get("goals",[]))
            return f"سيدي، ذاكرة التعلم: {n_goals} هدف، {n_less} درس — memory/agi_lessons.jsonl"
        if action in ("log","احفظ","lesson"):
            lesson = str(p.get("lesson") or "").strip()
            if not lesson:
                # حاول استنتاج من آخر مهمة
                return "سيدي، lesson= النص — مثال: agi_learn action=log lesson=\"التقسيم إلى 5 خطوات يقلل الأخطاء\""
            rec = {"ts": time.time(), "lesson": lesson, "date": time.strftime("%Y-%m-%d %H:%M")}
            try:
                LESSONS.parent.mkdir(parents=True, exist_ok=True)
                with LESSONS.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                import os; os.chmod(LESSONS, 0o600)
            except Exception as e:
                return f"سيدي، تعذر الحفظ: {e}"
            return f"سيدي، حفظت درساً: \"{lesson[:80]}\" — سيظهر في recall"
        if action in ("recall","تذكر","بحث"):
            q = str(p.get("query") or "").strip().lower()
            lessons = _load_lessons()
            if not lessons:
                return "سيدي، لا دروس بعد — احفظ أولاً بـ log"
            if not q:
                # أعد آخر 5
                tail = lessons[-5:]
                lines=["سيدي، آخر 5 دروس:"]
                for l in tail:
                    lines.append(f"  • {l.get('date','')} {l.get('lesson','')[:80]}")
                return "\n".join(lines)
            hits = [l for l in lessons if q in str(l.get("lesson","")).lower() or q in str(l.get("title","")).lower()]
            if not hits:
                return f"سيدي، لا دروس عن \"{q}\" — عندي {len(lessons)} درس إجمالي"
            lines=[f"سيدي، {len(hits)} درس عن \"{q}\":"]
            for l in hits[-5:]:
                lines.append(f"  • {l.get('lesson','')[:100]} — {l.get('date','')}")
            return "\n".join(lines)
        if action in ("lessons","كل","list"):
            lessons = _load_lessons()
            if not lessons:
                return "سيدي، لا دروس بعد"
            lines=[f"سيدي، {len(lessons)} درس محفوظ:"]
            for l in lessons[-10:]:
                lines.append(f"  • {l.get('date','')} {l.get('lesson','')[:90]}")
            return "\n".join(lines)
        return f"سيدي، '{action}' غير معروف — log/recall/lessons"
    except Exception as e:
        return f"سيدي، agi_learn واجه مشكلة: {e}"
