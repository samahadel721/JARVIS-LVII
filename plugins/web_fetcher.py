"""
web_fetcher.py — جلب صفحة كاملة مع التحقق (LVII fetch_page)

LVII يقرأ الصفحة كاملة لا snippet — هذه الإضافة تفعل نفس الشيء كـ plugin بسيط:
تجلب الصفحة، تحفظها، وتتحقق من كل اقتباس بالكود قبل استخدامه.

  web_fetcher action=fetch url="https://example.com"
  web_fetcher action=verify url="https://..." quote="النص المقتبس"
"""

from __future__ import annotations

import re
import time
from pathlib import Path

PLUGIN = {
    "name": "web_fetcher",
    "description": (
        "Fetch a web page in full and verify quotes against the saved HTML in code. "
        "Use for: 'اجلب الصفحة ...', 'تحقق من الاقتباس', 'web_fetcher fetch/verify url=...'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "fetch | verify | help"},
            "url": {"type": "STRING", "description": "رابط الصفحة"},
            "quote": {"type": "STRING", "description": "لـ verify: النص المراد التحقق منه"},
        },
        "required": ["action"],
    },
}

CACHE_DIR = Path(__file__).resolve().parent.parent / "memory" / "fetched_pages"

def _fetch(url: str) -> tuple[bool, str, Path | None]:
    try:
        import httpx
        r = httpx.get(url, timeout=15, follow_redirects=True, headers={"User-Agent": "JARVIS-LVII/1.0"})
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}", None
        html = r.text
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^a-zA-Z0-9_.-]", "_", url)[:80]
        path = CACHE_DIR / f"{safe}_{int(time.time())}.html"
        path.write_text(html, encoding="utf-8", errors="replace")
        return True, html, path
    except Exception as e:
        return False, str(e), None

def run(parameters: dict, player=None, session_memory=None) -> str:
    del session_memory
    try:
        p = dict(parameters or {})
        action = str(p.get("action") or "help").strip().lower()
        if action in ("help","?"):
            return "سيدي، جلب الصفحات:\n• fetch url=\"https://...\" — يجلب كاملاً ويحفظ\n• verify url=\"...\" quote=\"...\" — يتحقق أن الاقتباس موجود حرفياً في الصفحة المحفوظة"
        url = str(p.get("url") or "").strip()
        if not url:
            return "سيدي، url= الرابط — مثال: web_fetcher action=fetch url=\"https://example.com\""
        if not url.lower().startswith(("http://","https://")):
            return "سيدي، الرابط يجب أن يبدأ بـ https://"
        if action in ("fetch","اجلب","جلب"):
            ok, html, path = _fetch(url)
            if not ok:
                return f"سيدي، فشل الجلب: {html}"
            preview = re.sub(r"<[^>]+>", " ", html)[:3000]
            preview = re.sub(r"\s+", " ", preview).strip()
            try:
                if player and hasattr(player, "show_content"):
                    player.show_content(url[:40], html[:8000])
            except Exception:
                pass
            return f"سيدي، جلبت {url} ({len(html)} حرف) → {path.name if path else 'ذاكرة'}:\n{preview[:2000]}"
        if action in ("verify","تحقق","check"):
            quote = str(p.get("quote") or "").strip()
            if not quote:
                return "سيدي، quote= النص المراد التحقق منه"
            ok, html, path = _fetch(url)
            if not ok:
                return f"سيدي، فشل الجلب للتحقق: {html}"
            # تحقق حرفي (code, not LLM)
            if quote in html:
                return f"سيدي، ✅ الاقتباس موجود حرفياً في {url} (تحقق برمجي)"
            # تحقق بعد تجريد HTML
            text_only = re.sub(r"<[^>]+>", " ", html)
            text_only = re.sub(r"\s+", " ", text_only)
            if quote in text_only:
                return f"سيدي، ✅ موجود بعد تجريد HTML (تحقق برمجي)"
            # تحقق تقريبي: 90% تطابق
            import difflib
            # ابحث عن أقرب مقطع
            ratio = difflib.SequenceMatcher(None, quote, text_only[:5000]).ratio()
            if ratio > 0.85:
                return f"سيدي، ⚠️ تطابق تقريبي {ratio:.0%} لكن ليس حرفياً — راجع الصفحة المحفوظة {path.name if path else ''}"
            return f"سيدي، ❌ الاقتباس غير موجود في الصفحة (تحقق برمجي) — محفوظة في {path.name if path else 'الذاكرة'}"
        return f"سيدي، '{action}' غير معروف — fetch/verify"
    except Exception as e:
        return f"سيدي، web_fetcher واجه مشكلة: {e}"
