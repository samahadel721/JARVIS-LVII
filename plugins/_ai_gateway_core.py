"""
_ai_gateway_core.py — النواة الموحدة للذكاء (طبقة LLM)

تحول بين المحلي (Ollama/LM Studio) والسحابي (Gemini/OpenAI) تلقائياً:
  simple → محلي (127.0.0.1:11434)
  complex → سحابي إن سمح، وإلا محلي
  privacy → لا يُرسل إن كان فيه كلمة سر/مفتاح

مفصولة عن ai_gateway.py حتى تضاف محولات جديدة دون لمس البوابة.
"""

from __future__ import annotations

import re
from typing import Any, Optional

FORBIDDEN_PATTERNS = [
    re.compile(r"(password|كلمة.?سر|api.?key|token|sk-|secret)", re.I),
    re.compile(r"(private.?file|ملف.?خاص)", re.I),
]

def contains_forbidden(text: str, types: list[str]) -> bool:
    for pat in FORBIDDEN_PATTERNS:
        if pat.search(text or ""):
            return True
    return False

def route_by_complexity(text: str, allow_cloud: bool) -> str:
    """simple → local, complex → cloud if allowed."""
    txt = str(text or "")
    # طويل أو سؤال بحثي = complex
    if len(txt) > 120 or any(k in txt.lower() for k in ["ابحث", "لخص", "research", "analyze", "deep"]):
        return "cloud" if allow_cloud else "local"
    if any(k in txt.lower() for k in ["complex", "تحليل", "قارن", "compare"]):
        return "cloud" if allow_cloud else "local"
    return "local"

def call_local(text: str, local_model: str = "llama3.2", base_url: str = "http://127.0.0.1:11434") -> tuple[bool, str]:
    try:
        import httpx
        resp = httpx.post(f"{base_url}/api/generate", json={"model": local_model, "prompt": text, "stream": False}, timeout=30)
        if resp.status_code == 200:
            j = resp.json()
            return True, str(j.get("response") or j.get("text") or str(j))
        return False, f"المحلي {resp.status_code}: {resp.text[:200]}"
    except Exception as e:
        return False, f"فشل النموذج المحلي ({local_model}): تأكد أن Ollama يعمل — {e}"

def call_cloud(text: str, provider: str = "openai", model: str = "gpt-4o-mini", api_key: str = "", base_url: str = "") -> tuple[bool, str]:
    # استخدم core.gemini كبديل موحد (يحترم ladder + provider المختار)
    try:
        from core import gemini
        out = gemini.text(text, tier=gemini.SMART, timeout_ms=30_000)
        if out:
            return True, out
        return False, "لا رد من السحابة — تحقق من المفتاح أو الحصة"
    except Exception as e:
        return False, f"انقطاع الشبكة — تأكد من الاتصال: {e}"

def redact(text: str) -> str:
    # اخفاء الأسرار في السجلات
    t = re.sub(r"sk-[A-Za-z0-9]{10,}", "sk-••••", str(text))
    t = re.sub(r"password[=:]\s*\S+", "password=••••", t, flags=re.I)
    return t
