"""
research_assistant.py — looking things up properly, and saying where it came from.

IT DOES NOT INVENT A SEARCH ENGINE
    Searching is the project's own actions/web_search.py — the same code the
    assistant already calls when you ask it about the news. This plugin adds the
    part that was missing: splitting a broad request into narrower questions,
    collecting several sources, dropping duplicates, keeping the URLs, and
    marking clearly which sentences came from a source and which are my own
    arrangement of them.

WHAT "RETRIEVED" MEANS HERE
    A source appears in the report only if the search actually returned it, with
    its address attached. There is no fallback that fills a gap with something
    probable. When the search provider is missing or returns nothing, the report
    says that in one line and stops — a research report with no sources is worth
    less than an honest refusal.

YOUR OWN FILES COUNT AS SOURCES TOO
    When the Knowledge Base has something on the topic, it is searched as well
    and shown in its own section, with the file path as the citation. Local
    first, and never mixed in as if it were a web page.

SETTINGS (Plugin Manager → Research assistant)
    max_sources    8 — how many sources one study may keep
    per_query      6 — results requested per search
    save_as        none | note | document — what to do with the report
    planner        local | model — local splits the request by rules; model asks
                   Gemini for subtopics (that sends your question to Google)
"""

from __future__ import annotations

import importlib.util
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

PLUGIN = {
    "name": "research_assistant",
    "description": (
        "Researches a topic properly: breaks it into smaller questions, gathers several "
        "sources with their links, removes duplicates and summarises what they say — "
        "'research the water situation in Egypt and tell me what you find', 'look into "
        "the best time to plant tomatoes and give me sources'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "research | plan | status"},
            "topic": {"type": "STRING", "description": "What to research"},
            "max_sources": {"type": "NUMBER", "description": "How many sources to keep, default 8"},
            "depth": {"type": "NUMBER", "description": "1 = one pass per subtopic, 2 = follow-ups"},
            "save": {"type": "STRING", "description": "none | note | document"},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "research_assistant",
    "title": "Research assistant / مساعد البحث",
    "fields": [
        {"key": "max_sources", "type": "text", "label": "Most sources per study", "default": "8"},
        {"key": "per_query", "type": "text", "label": "Results per search", "default": "6"},
        {"key": "save_as", "type": "choice", "label": "Save the report as",
         "default": "none", "choices": ["none", "note", "document"]},
        {"key": "planner", "type": "choice",
         "label": "How to split the request (model sends it to Gemini)",
         "default": "local", "choices": ["local", "model"]},
    ],
}

# Words that split a request into separate questions without any help.
_SPLITTERS = re.compile(r"\s+(?:and|vs\.?|versus|compared to|plus|بالإضافة إلى|و)\s+|\s*[،,;]\s*")
_STOP = {"the", "a", "an", "of", "in", "on", "for", "about", "tell", "me", "what", "is",
         "are", "how", "to", "the", "ماذا", "عن", "لي", "قول", "ما", "هو", "هي", "في", "على"}


def _cfg() -> dict[str, Any]:
    out = {f["key"]: f.get("default") for f in PLUGIN_SETTINGS["fields"]}
    try:
        from memory.config_manager import get_plugin_config
        out.update({k: v for k, v in (get_plugin_config("research_assistant") or {}).items()
                    if v not in (None, "")})
    except Exception:
        pass
    return out


def _int_cfg(key: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(str(_cfg().get(key) or default))))
    except Exception:
        return default


def _sibling(name: str):
    cached = sys.modules.get(f"_jarvis_{name}")
    if cached is not None:
        return cached
    path = Path(__file__).resolve().parent / f"{name}.py"
    if not path.exists():
        return None
    try:
        spec = importlib.util.spec_from_file_location(f"_jarvis_{name}", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    except Exception:
        sys.modules.pop(f"_jarvis_{name}", None)
        return None


def project_web_search():
    """
    The project's own web search, loaded from actions/web_search.py.

    It is imported by path rather than imported normally so this plugin works
    from anywhere the file happens to be sitting, and so a missing project file
    becomes a clear sentence instead of an ImportError at load time.
    """
    cached = sys.modules.get("_jarvis_web_search")
    if cached is not None:
        return cached
    target = Path(__file__).resolve().parent.parent / "actions" / "web_search.py"
    if not target.exists():
        return None
    try:
        spec = importlib.util.spec_from_file_location("_jarvis_web_search", target)
        module = importlib.util.module_from_spec(spec)
        sys.modules["_jarvis_web_search"] = module
        spec.loader.exec_module(module)
        return module
    except Exception:
        sys.modules.pop("_jarvis_web_search", None)
        return None


def search_provider() -> tuple[bool, str]:
    """(usable, why not) — checked before any research starts."""
    module = project_web_search()
    if module is None:
        return False, ("I cannot find the project's own web search (actions/web_search.py) "
                       "next to me")
    try:
        module._get_ddgs()
        return True, "the project's web search (ddgs)"
    except Exception:
        pass
    has_key = False
    try:
        from memory.config_manager import get_api_key
        has_key = bool(str(get_api_key() or "").strip())
    except Exception:
        pass
    if has_key:
        return True, "Gemini grounded search (the ddgs package is not installed)"
    return False, ("no search provider is available: the ddgs package is not installed and "
                   "there is no Gemini key. ddgs is in the project's requirements — "
                   "pip install ddgs")


def _web_results(query: str, count: int) -> tuple[list[dict], str]:
    """
    One search. Returns (results, note). Results are exactly what the provider
    returned — never padded, never invented.
    """
    module = project_web_search()
    if module is None:
        return [], "the project's web search file is missing"
    ok, why = search_provider()
    if not ok:
        return [], why
    try:
        results = module._ddg_search(query, max_results=count)
    except Exception:
        results = []
    if results:
        return results, ""
    # ddgs gave nothing (or is not installed): try the grounded path, which also
    # gives us prose rather than links.
    try:
        text = module._search(query)
    except Exception:
        text = ""
    if text and not str(text).lower().startswith("search failed"):
        return [{"title": f"Grounded answer: {query}", "snippet": str(text)[:1200],
                 "url": "", "grounded": True}], ""
    return [], "the search returned nothing"


def dedupe(sources: list[dict]) -> list[dict]:
    """
    Remove duplicates: same address, or same headline from the same site.

    It deliberately does NOT collapse one headline appearing on two different
    sites. That is usually the same story syndicated — but it is also sometimes
    two independent reports, and in research, quietly dropping a source is worse
    than showing two similar ones. Order is kept, so the best-ranked result
    survives.
    """
    seen_urls: set[str] = set()
    seen_keys: set[str] = set()
    out: list[dict] = []
    for s in sources:
        url = str(s.get("url") or "").strip()
        title = re.sub(r"\s+", " ", str(s.get("title") or "")).strip().lower()
        key = f"{title[:80]}|{_domain(url)}"
        norm_url = re.sub(r"^https?://(www\.)?", "", url.rstrip("/").lower())
        if url and norm_url in seen_urls:
            continue
        if key and key in seen_keys:
            continue
        if url:
            seen_urls.add(norm_url)
        if key:
            seen_keys.add(key)
        out.append(s)
    return out


def _domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().replace("www.", "") or "local"
    except Exception:
        return "local"


def break_into_subtopics(topic: str, max_parts: int = 4) -> tuple[list[str], str]:
    """
    Split a request into narrower questions. Returns (subtopics, how).

    The split is my own arrangement of your words — it is analysis, not a
    source, and the report labels it as such.
    """
    topic = (topic or "").strip()
    if not topic:
        return [], "local"
    if str(_cfg().get("planner") or "local").strip().lower() == "model":
        planned = _plan_with_model(topic, max_parts)
        if planned:
            return planned, "model"
    parts = [p.strip(" ?.!") for p in _SPLITTERS.split(topic) if p and len(p.strip()) > 3]
    if len(parts) <= 1:
        base = topic.rstrip(" ?.!")
        parts = [base, f"{base} — latest developments", f"{base} — criticism and risks",
                 f"{base} — practical details"]
    seen: list[str] = []
    for p in parts:
        if p.lower() not in [x.lower() for x in seen]:
            seen.append(p)
    return seen[:max_parts], "local"


def _plan_with_model(topic: str, max_parts: int) -> list[str]:
    """Optional: let the model propose the subtopics. Off unless chosen by name."""
    try:
        from memory.config_manager import get_api_key
        key = str(get_api_key() or "").strip()
    except Exception:
        key = ""
    if not key:
        return []
    try:
        from google import genai
        prompt = (f"Break this research request into at most {max_parts} specific, "
                  f"searchable sub-questions. Reply with one per line, no numbering, no "
                  f"explanation.\n\n{topic}")
        reply = genai.Client(api_key=key).models.generate_content(
            model="gemini-2.0-flash", contents=prompt)
        lines = [l.strip(" -•0123456789.") for l in (getattr(reply, "text", "") or "").splitlines()]
        return [l for l in lines if len(l) > 6][:max_parts]
    except Exception:
        return []


def research(topic: str, max_sources: int = 8, depth: int = 1,
             use_kb: bool = True, max_queries: int = 0) -> dict[str, Any]:
    """
    The reusable API used by deep_research. Returns a structured report:

        {ok, topic, plan, plan_source, findings, sources, local, notes,
         analysed, searched_at}

    `findings` sentences come from the sources; `analysed` is my own summary and
    is kept in a separate field so no caller can confuse the two.

    max_queries caps how many searches this call may issue (0 = no cap). It
    exists so a caller with its own budget — deep_research — can impose it,
    instead of counting the cost afterwards and discovering it overspent.
    """
    topic = str(topic or "").strip()
    if not topic:
        return {"ok": False, "reason": "there is no topic to research", "sources": []}
    ok, why = search_provider()
    started = time.time()
    subtopics, plan_source = break_into_subtopics(topic)
    per_query = _int_cfg("per_query", 6, 1, 20)
    max_sources = max(1, min(40, int(max_sources or _int_cfg("max_sources", 8, 1, 40))))

    collected: list[dict] = []
    notes: list[str] = []
    queries_done: list[str] = []
    rounds = 1 if depth < 2 else 2
    if max_queries:                       # honour the caller's ceiling up front
        allowed_subtopics = max(1, max_queries // rounds)
        subtopics = subtopics[:allowed_subtopics]

    for subtopic in subtopics:
        for r in range(rounds):
            query = subtopic if r == 0 else f"{subtopic} analysis 2026"
            if query in queries_done:
                continue
            queries_done.append(query)
            results, note = _web_results(query, per_query)
            if note and not results:
                if note not in notes:
                    notes.append(note)
                continue
            for item in results:
                collected.append({**item, "subtopic": subtopic, "query": query})
            if len(collected) >= max_sources * 2:
                break

    sources = dedupe(collected)[:max_sources]
    for i, s in enumerate(sources, 1):
        s["n"] = i

    local: list[dict] = []
    if use_kb:
        kb = _sibling("knowledge_base")
        if kb is not None and hasattr(kb, "search"):
            try:
                for hit in kb.search(topic, limit=3):
                    local.append({"title": hit.get("title", ""), "path": hit.get("source", ""),
                                  "text": hit.get("text", "")})
            except Exception:
                pass

    summarizer = _sibling("summarizer")
    analysed = ""
    if summarizer is not None and sources:
        blob = "\n\n".join(f"{s.get('title')}: {s.get('snippet', '')}" for s in sources)
        analysed = summarizer.summarize(blob, mode="medium", bullets=True)

    if not sources and not local:
        return {"ok": False, "topic": topic, "plan": subtopics, "plan_source": plan_source,
                "sources": [], "local": [], "notes": notes or [why], "findings": [],
                "analysed": "", "reason": notes[0] if notes else why,
                "searched_at": started}

    return {"ok": True, "topic": topic, "plan": subtopics, "plan_source": plan_source,
            "sources": sources, "local": local, "notes": notes,
            "findings": [{"subtopic": s["subtopic"], "title": s.get("title", ""),
                          "url": s.get("url", ""), "text": s.get("snippet", ""),
                          "n": s.get("n")} for s in sources],
            "analysed": analysed, "searched_at": started,
            "provider": why, "queries": queries_done}


def render(report: dict[str, Any], heading: str = "Research") -> str:
    """
    A report you can read. Retrieved text and my own summary sit in different
    sections, and every source carries the address it came from.
    """
    if not report.get("ok"):
        return (f"I could not research '{report.get('topic', '')}', sir: "
                f"{report.get('reason', 'unknown reason')}.")
    lines = [f"{heading}: {report['topic']}",
             f"({time.strftime('%Y-%m-%d %H:%M')} · provider: {report.get('provider', '?')})",
             ""]
    if report.get("plan"):
        how = "my own split of your question" if report.get("plan_source") == "local" \
            else "subtopics proposed by the model"
        lines.append(f"── Plan ({how} — analysis, not a source) ──")
        for i, s in enumerate(report["plan"], 1):
            lines.append(f"  {i}. {s}")
        lines.append("")

    if report.get("sources"):
        lines.append(f"── Retrieved from {len(report['sources'])} source(s) ──")
        for s in report["sources"]:
            body = " ".join(str(s.get("snippet", "")).split())
            if len(body) > 400:
                body = body[:397] + "..."
            lines.append(f"\n[{s['n']}] {s.get('title', '(no title)')}")
            if s.get("url"):
                lines.append(f"    {s['url']}")
            elif s.get("grounded"):
                lines.append("    (no link — this one came back as a grounded answer, not a "
                             "page I can point you at)")
            else:
                lines.append("    (the source gave no address)")
            lines.append(f"    {body or '(the source returned no summary text)'}")
        lines.append("")
    else:
        lines.append("── Retrieved ──\nNothing was retrieved, sir. I will not write around that.\n")

    if report.get("local"):
        lines.append("── From your own indexed files ──")
        for item in report["local"]:
            lines.append(f"  {item['title']}  →  {item['path']}")
        lines.append("")

    if report.get("notes"):
        lines.append("── Conditions I hit ──")
        for n in report["notes"]:
            lines.append(f"  ! {n}")
        lines.append("")

    if report.get("analysed"):
        lines.append("── My summary of the sources above (analysis, not retrieved text) ──")
        lines.append(report["analysed"])
        lines.append("")

    if report.get("sources"):
        lines.append("── Everything above that is not marked as analysis came from the "
                     "numbered sources. If a number you need is not in them, I do not have it. ──")
    return "\n".join(lines)


def _save_report(report: dict[str, Any], body: str, how: str, player=None) -> str:
    """Save to a note or a document. A missing saver says so; it never fails silently."""
    if how == "note":
        notes = _sibling("note_manager")
        if notes is None or not hasattr(notes, "add_note"):
            return "\n(I could not save it as a note: the note manager is not next to me.)"
        tags = ["research"] + [w for w in re.findall(r"[A-Za-z\u0600-\u06FF]{4,}",
                                                     report["topic"])[:3]]
        result = notes.add_note(f"Research: {report['topic'][:60]}", body, tags, "research",
                                source="research_assistant")
        return (f"\n(Saved as note {result['note']['id']}.)" if result.get("ok")
                else f"\n(I could not save the note: {result.get('reason')})")
    if how == "document":
        docgen = _sibling("document_generator")
        if docgen is None or not hasattr(docgen, "generate"):
            return "\n(I could not save it as a document: the document generator is not next to me.)"
        result = docgen.generate(f"Research: {report['topic'][:60]}", body, fmt="docx",
                                 filename=f"research_{report['topic'][:28]}")
        return (f"\n(Saved to {result['path']})" if result.get("ok")
                else f"\n(I could not save the document: {result.get('reason')})")
    return ""


# ── tool entry point ──────────────────────────────────────────────────────────

def run(parameters: dict, player=None, session_memory=None) -> str:
    """Dispatch one research action. Never raises."""
    try:
        p = parameters or {}
        action = str(p.get("action") or "research").strip().lower()

        if action in ("research", "look_up", "study", "find_out"):
            topic = str(p.get("topic") or p.get("query") or "").strip()
            if not topic:
                return "What should I research, sir?"
            if player:
                try:
                    player.write_log(f"[Research] {topic[:60]}")
                except Exception:
                    pass
            report = research(topic, max_sources=int(float(p.get("max_sources") or 0)) or 8,
                              depth=int(float(p.get("depth") or 1)))
            body = render(report)
            if not report.get("ok"):
                return body
            how = str(p.get("save") or _cfg().get("save_as") or "none").strip().lower()
            return body + _save_report(report, body, how, player)

        if action in ("plan", "subtopics"):
            topic = str(p.get("topic") or p.get("query") or "").strip()
            if not topic:
                return "What should I plan, sir?"
            subtopics, how = break_into_subtopics(topic)
            return (f"I would break '{topic}' into {len(subtopics)} question(s) "
                    f"({how}):\n" + "\n".join(f"  {i}. {s}" for i, s in enumerate(subtopics, 1))
                    + "\n\n(This is my own split of your words — analysis, not a source.)")

        if action in ("status", "info"):
            ok, why = search_provider()
            return (f"Search provider : {'ready' if ok else 'NOT ready'} — {why}\n"
                    f"Saved reports   : {_cfg().get('save_as')}\n"
                    f"Planner         : {_cfg().get('planner')}\n"
                    f"Honesty rule    : a source only appears if the search actually returned it")

        return (f"Sir, '{action}' is not a research action I have. I can research a topic, "
                f"plan the sub-questions, or report status.")
    except Exception as e:                                   # noqa: BLE001
        return f"Sir, the research assistant hit a problem: {str(e)[:150]}"


# ── self-test: python research_assistant.py ──────────────────────────────────

def _selftest() -> int:
    print("research_assistant — self-test")
    problems = 0

    plan, how = break_into_subtopics("the water situation in Egypt and the new plant in Mansoura")
    print(f"  split a request     : {len(plan)} part(s), planner={how}")
    for part in plan:
        print(f"      · {part[:70]}")
    problems += 0 if len(plan) >= 2 else 1

    plan2, _ = break_into_subtopics("tomatoes")
    print(f"  single word grows   : {len(plan2)} question(s) from 'tomatoes'")
    problems += 0 if len(plan2) >= 2 else 1

    dupes = dedupe([
        {"title": "Water plant opens", "url": "https://www.example.com/a", "snippet": "one"},
        {"title": "Water plant opens", "url": "https://example.com/a/", "snippet": "same"},
        {"title": "A different story", "url": "https://news.example.org/b", "snippet": "two"},
        {"title": "A different story", "url": "https://other.org/c", "snippet": "three"},
    ])
    print(f"  dedupe              : 4 → {len(dupes)} ({[d['title'][:18] for d in dupes]})")
    # The two identical URLs collapse; the identical headline on a DIFFERENT site
    # is kept on purpose (see the docstring) — dropping a source is worse.
    problems += 0 if len(dupes) == 3 else 1
    same_site = dedupe([{"title": "Same story", "url": "https://a.com/x", "snippet": "1"},
                        {"title": "Same story", "url": "https://a.com/x?utm=1", "snippet": "1"},
                        {"title": "Same story", "url": "https://a.com/y", "snippet": "1"}])
    print(f"  same title+site     : 3 → {len(same_site)} (collapsed)")
    problems += 0 if len(same_site) == 1 else 1

    ok, why = search_provider()
    print(f"  provider            : {'ready' if ok else 'NOT ready'} — {why[:64]}")
    problems += 0 if ok else 0

    if ok:
        report = research("the Aswan High Dam", max_sources=5, depth=1)
        print(f"  live research       : ok={report.get('ok')} sources={len(report.get('sources', []))}")
        if report.get("ok"):
            cited = [s for s in report["sources"] if s.get("url") or s.get("grounded")]
            grounded = [s for s in report["sources"] if s.get("grounded")]
            print(f"  every source cited  : {len(cited)}/{len(report['sources'])} have a link "
                  f"or are marked as a grounded answer ({len(grounded)} grounded)")
            problems += 0 if len(cited) == len(report["sources"]) else 1
            body_check = render(report)
            if grounded:
                problems += 0 if "no link" in body_check else 1
                print(f"  grounded labelled   : {'yes' if 'no link' in body_check else 'NO'}")
            body = render(report)
            print(f"  sections separated  : retrieved={'Retrieved' in body} "
                  f"analysis={'analysis, not retrieved' in body}")
            problems += 0 if "Retrieved" in body and "analysis, not retrieved" in body else 1
            if report.get("analysed"):
                lines = [l for l in report["analysed"].splitlines() if l.strip().startswith("-")]
                # The exact blob the summariser was given — that is the only fair
                # thing to compare against.
                blob = "\n\n".join(f"{s.get('title')}: {s.get('snippet', '')}"
                                    for s in report["sources"])
                flat = " ".join(blob.split())
                outside = [l for l in lines
                           if " ".join(l.strip("- ").split()).rstrip(".") not in flat]
                print(f"  summary stays inside: {len(lines)} bullet(s), {len(outside)} not "
                      f"traceable to the source text")
                problems += 0 if not outside else 1
        else:
            print(f"  live research reason: {report.get('reason')}")
            problems += 0 if report.get("reason") else 1
    else:
        # The honest path when there is no provider must not invent anything.
        report = research("anything at all")
        text = render(report)
        print(f"  no provider →       : refuses, mentioning the reason")
        problems += 0 if not report.get("ok") and report.get("sources") == [] else 1
        print(f"      {text[:100]}")

    print(f"  render with nothing : {render({'ok': False, 'topic': 'x', 'reason': 'no search provider'})[:88]}")
    problems += 0 if "could not research" in render({"ok": False, "topic": "x", "reason": "n"}) else 1
    print(f"  empty topic         : {run({'action': 'research', 'topic': ''})}")
    problems += 0 if "What should I research" in run({"action": "research", "topic": ""}) else 1
    print(f"  bad action          : {run({'action': 'sing'})[:70]}")
    problems += 0 if "not a research action" in run({"action": "sing"}) else 1
    print(f"  plan action         : {run({'action': 'plan', 'topic': 'solar panels'}).splitlines()[0][:70]}")
    print("  RESULT:", "PASS" if problems == 0 else f"{problems} problem(s)")
    return problems


if __name__ == "__main__":
    raise SystemExit(_selftest())
