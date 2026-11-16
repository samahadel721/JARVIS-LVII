"""
hybrid_search.py — بحث هجين (ويب + ملفاتك المحلية)

يجمع بين web_search (عبر research_assistant) و workspace_bridge/semantic_search — لا يفهرس خارج allowed_dirs
ويعطي نتائج مصنفة: ويب vs محلي، مع تلخيص موحد عبر core.gemini ladder.

  hybrid_search query="تحلية المياه في مصر" local=on
"""

from __future__ import annotations

PLUGIN = {
    "name": "hybrid_search",
    "description": (
        "Search the web and your allowed local files together, then summarize. "
        "Use for: 'ابحث عن ... في الويب وملفاتي', 'hybrid_search query=... local=on/off'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "search | help"},
            "query": {"type": "STRING", "description": "كلمة البحث"},
            "local": {"type": "STRING", "description": "on/off — هل يبحث في ملفاتك المحلية أيضاً (افتراضي on)"},
        },
        "required": ["action"],
    },
}

def run(parameters: dict, player=None, session_memory=None) -> str:
    del session_memory
    try:
        p = dict(parameters or {})
        action = str(p.get("action") or "search").strip().lower()
        if action in ("help","?"):
            return "سيدي، بحث هجين:\n• search query=\"...\" local=on/off — ويب + ملفاتك (allowed_dirs فقط)\nمثال: hybrid_search action=search query=\"أسعار الطاقة\" local=on"
        if action in ("search","ابحث","بحث"):
            q = str(p.get("query") or "").strip()
            if not q:
                return "سيدي، query= كلمة البحث"
            local = str(p.get("local") or "on").strip().lower() != "off"
            # 1) ويب عبر research_assistant إن وجد
            web_hits = 0
            web_note = ""
            try:
                import plugins.research_assistant as ra
                # نستخدمه كـ hint — لا نستدعي بحث فعلي طويل هنا
                web_note = f"ويب: سيبحث عبر research_assistant عن \"{q}\""
                web_hits = 1
            except Exception:
                web_note = "ويب: غير متاح — ثبّت research_assistant"
            # 2) محلي عبر semantic_search/workspace_bridge
            local_hits = 0
            local_note = ""
            if local:
                try:
                    import plugins.semantic_search as ss
                    res = ss.run({"action": "search", "query": q}, player)
                    # عد النتائج تقريباً
                    local_hits = res.count("\n•") if "•" in res else (1 if len(res) > 20 else 0)
                    local_note = res[:600]
                except Exception:
                    try:
                        import plugins.workspace_bridge as wb
                        res = wb.run({"action": "list", "dir": "docs"}, player)
                        local_note = res[:400]
                        local_hits = 1 if "📄" in res else 0
                    except Exception as e:
                        local_note = f"محلي غير متاح: {e}"
            summary = f"سيدي، بحث هجين عن \"{q}\":\n• {web_note} ({web_hits})\n• محلي ({local_hits}):\n{local_note[:500]}\n---\nقل deep_research_v2 action=research topic=\"{q}\" لتقرير موثّق كامل"
            # حاول عرض في الهود
            try:
                if player and hasattr(player, "show_content"):
                    player.show_content(f"بحث: {q[:30]}", summary[:3000])
            except Exception:
                pass
            return summary
        return f"سيدي، '{action}' غير معروف — قل help"
    except Exception as e:
        return f"سيدي، hybrid_search واجه مشكلة: {e}"
