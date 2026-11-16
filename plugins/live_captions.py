"""
live_captions.py — سجل المحادثة الحية (Live API → نص محفوظ)

LVII يتحدث عبر Live API — هذه الإضافة تحفظ كل جولة (user ↔ jarvis) في ملف محلي
وتتيح البحث فيها، بدون إرسال للخارج. مفيدة لـ: "ماذا قلت قبل 5 دقائق؟" أو "ابحث في كلامنا".

  live_captions action=recent n=5 → آخر 5 جولات
  live_captions action=search q="الطائرات"
  live_captions action=export format=txt
"""

from __future__ import annotations

import json
import time
from pathlib import Path

PLUGIN = {
    "name": "live_captions",
    "description": (
        "Search and export the live conversation transcript saved locally. "
        "Use for: 'ماذا قلت قبل شوية', 'ابحث في كلامنا عن...', 'صدّر المحادثة', 'live_captions recent/search/export'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "recent | search | export | status | help"},
            "q": {"type": "STRING", "description": "كلمة البحث لـ search"},
            "n": {"type": "STRING", "description": "عدد الجولات لـ recent (افتراضي 5)"},
            "format": {"type": "STRING", "description": "لـ export: txt/json"},
        },
        "required": ["action"],
    },
}

CAPTIONS_FILE = Path(__file__).resolve().parent.parent / "memory" / "live_transcripts.jsonl"

def _load() -> list[dict]:
    if not CAPTIONS_FILE.exists():
        return []
    out=[]
    try:
        for line in CAPTIONS_FILE.read_text(encoding="utf-8").splitlines():
            line=line.strip()
            if not line: continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    except Exception:
        pass
    return out

def run(parameters: dict, player=None, session_memory=None) -> str:
    del session_memory
    try:
        p = dict(parameters or {})
        action = str(p.get("action") or "recent").strip().lower()
        if action in ("help","?","مساعدة"):
            return "سيدي، سجل المحادثة:\n• recent n=5 — آخر الجولات\n• search q=\"...\" — ابحث في كلامنا\n• export format=txt — صدّر كاملاً\n• status — عدد الجولات المحفوظة"
        if action in ("status","حالة"):
            items=_load()
            return f"سيدي، محفوظ {len(items)} جولة في memory/live_transcripts.jsonl — كلها محلية 0600"
        if action in ("recent","آخر","اخر"):
            try:
                n=int(str(p.get("n") or "5").strip())
            except Exception:
                n=5
            n=max(1,min(20,n))
            items=_load()
            if not items:
                return "سيدي، لا جولات محفوظة بعد — تحدث معي وسيبدأ الحفظ"
            tail=items[-n:]
            lines=[f"سيدي، آخر {len(tail)} جولات:"]
            for it in tail:
                ts=time.strftime("%H:%M", time.localtime(float(it.get("ts",0)))) if it.get("ts") else "??:??"
                role=it.get("role","?")
                text=str(it.get("text",""))[:120]
                lines.append(f"  [{ts}] {role}: {text}")
            return "\n".join(lines)
        if action in ("search","ابحث","بحث"):
            q=str(p.get("q") or "").strip()
            if not q:
                return "سيدي، q= كلمة البحث"
            items=_load()
            ql=q.lower()
            hits=[it for it in items if ql in str(it.get("text","")).lower()]
            if not hits:
                return f"سيدي، لا نتائج لـ \"{q}\" في {len(items)} جولة"
            lines=[f"سيدي، وجدت {len(hits)} لـ \"{q}\":"]
            for it in hits[-5:]:
                lines.append(f"  • [{it.get('role','?')}] {str(it.get('text',''))[:100]}")
            return "\n".join(lines)
        if action in ("export","تصدير","save"):
            fmt=str(p.get("format") or "txt").strip().lower()
            items=_load()
            if not items:
                return "سيدي، لا شيء لتصديره"
            try:
                if fmt=="json":
                    out=Path(__file__).resolve().parent.parent / "memory" / "live_export.json"
                    out.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
                    return f"سيدي، صدّرت {len(items)} جولة إلى memory/live_export.json"
                else:
                    out=Path(__file__).resolve().parent.parent / "memory" / "live_export.txt"
                    lines=[]
                    for it in items:
                        lines.append(f"[{it.get('role','?')}] {it.get('text','')}")
                    out.write_text("\n".join(lines), encoding="utf-8")
                    return f"سيدي، صدّرت {len(items)} جولة إلى memory/live_export.txt"
            except Exception as e:
                return f"سيدي، تعذر التصدير: {e}"
        return f"سيدي، '{action}' غير معروف — قل help"
    except Exception as e:
        return f"سيدي، live_captions واجه مشكلة: {e}"
