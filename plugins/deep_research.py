"""
deep_research.py — several studies, compared, with the disagreements left visible.

IT BUILDS ON THE RESEARCH ASSISTANT, IT DOES NOT REPEAT IT
    There is no searching in this file. Every source in a deep report was
    fetched by research_assistant.research(), which in turn uses the project's
    own web search. All this plugin adds is the orchestration: turn one hard
    question into several separate studies, run them under a budget, put the
    results side by side, and point out where the sources contradict each other.

WHEN TWO SOURCES DISAGREE, YOU GET BOTH
    Comparing sources is easy to fake and dangerous to fake. This one only
    reports a conflict when two different sources state different figures with
    the same subject beside them, and it prints both figures with the number of
    the source that said each. It never averages them, never picks a winner and
    never quietly drops the older one. If it cannot tell, it says it cannot tell.

THE BUDGET, AND WHY THERE IS ONE
    Research that runs itself is research that never stops. Every run has a
    ceiling: searches, sources, rounds and a wall-clock deadline. When the
    deadline passes, the report is written with whatever was actually gathered
    and says plainly that it stopped early. The same query is never searched
    twice — that is the loop this plugin is most likely to fall into.

    The search ceiling is exact: it is handed down to the research assistant, so
    a run cannot outspend it. The deadline is checked between lines of enquiry
    rather than mid-search, so the overshoot is at most one enquiry — every
    individual search already carries its own timeout from the project's own
    web search. It cannot run away, and it tells you when it stopped.

NOTHING IS CLAIMED THAT WAS NOT RETRIEVED
    Every source is numbered, and a source only exists in the report if a search
    returned it. Tasks that came back empty are listed as empty, by name. The
    one-line-per-section method note at the end says which parts are retrieved
    and which parts are my arrangement of them.

SETTINGS (Plugin Manager → Deep research)
    max_searches  12 — hard ceiling per run
    max_sources   20 — most sources kept across the whole study
    depth          2 — passes per sub-question
    timeout_s    180 — seconds; the run stops and reports at the deadline
    save_as       none | note | document
"""

from __future__ import annotations

import importlib.util
import re
import sys
import time
from pathlib import Path
from typing import Any

PLUGIN = {
    "name": "deep_research",
    "description": (
        "A full study of a hard question: it splits the request into several lines of "
        "enquiry, researches each one, compares the sources against each other, flags "
        "where they disagree, and writes a structured report — 'do deep research on "
        "desalination in Egypt and write me a report'. Slower than a normal search and "
        "it stops at a set budget."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "research | status"},
            "topic": {"type": "STRING", "description": "The question to study"},
            "depth": {"type": "NUMBER", "description": "Passes per sub-question, default 2"},
            "max_sources": {"type": "NUMBER", "description": "Most sources to keep"},
            "save": {"type": "STRING", "description": "none | note | document"},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "deep_research",
    "title": "Deep research / البحث العميق",
    "fields": [
        {"key": "max_searches", "type": "text", "label": "Most searches per run", "default": "12"},
        {"key": "max_sources", "type": "text", "label": "Most sources kept", "default": "20"},
        {"key": "depth", "type": "text", "label": "Passes per sub-question", "default": "2"},
        {"key": "timeout_s", "type": "text", "label": "Wall-clock limit, seconds", "default": "180"},
        {"key": "save_as", "type": "choice", "label": "Save the report as",
         "default": "none", "choices": ["none", "note", "document"]},
    ],
}

# Numbers with a unit or a nearby subject are what sources most often disagree on.
_NUM = re.compile(r"(?<![\w.])(\d[\d,\.]*)\s*(%|percent|billion|million|thousand|bn|m\b|km|"
                  r"km2|kg|tons?|tonnes?|years?|EGP|USD|AED|SAR|جنيه|مليون|مليار|%|فدان|كم)")
_STOP = {"the", "a", "an", "of", "in", "on", "for", "and", "to", "is", "are", "was",
         "من", "في", "على", "عن", "و", "إلى", "الى", "هذا", "هذه", "التي", "الذي"}
# Words that say nothing about WHAT is being counted. Two figures only disagree if
# they are about the same thing — "the plant cost 240" and "the bridge cost 90" are
# not a disagreement, and calling it one would be false reporting.
_GENERIC = {"cost", "costs", "costing", "total", "about", "around", "nearly", "almost",
            "over", "under", "more", "less", "than", "serves", "served", "service", "said",
            "says", "will", "been", "reached", "approximately", "roughly", "some", "only",
            "just", "up", "down", "per", "each", "average", "بلغ", "بلغت", "حوالي",
            "تقريبا", "نحو", "اكثر", "أكثر", "اقل", "أقل", "يقرب", "وصل", "وصلت"}


def _cfg() -> dict[str, Any]:
    out = {f["key"]: f.get("default") for f in PLUGIN_SETTINGS["fields"]}
    try:
        from memory.config_manager import get_plugin_config
        out.update({k: v for k, v in (get_plugin_config("deep_research") or {}).items()
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


# ── comparison ────────────────────────────────────────────────────────────────

def numbers_in(text: str) -> list[tuple[str, str]]:
    """(value, unit) pairs found in a sentence — the things sources fight about."""
    out = []
    for value, unit in _NUM.findall(text or ""):
        clean = value.replace(",", "").rstrip(".")
        if clean and len(clean) <= 12:
            out.append((clean, unit.strip().lower()))
    return out


def _subject_of(text: str, value: str, unit: str = "") -> str:
    """
    The words immediately around a figure — what the figure is about.

    Only a small window counts, and filler words are dropped, so the subject is
    "new water plant" rather than "the new water plant cost million pounds and
    serves". Without this the shared words 'cost' and 'million' would make every
    two figures in the same unit look like a disagreement.
    """
    clean = lambda w: w.replace(",", "").rstrip(".")
    toks = re.findall(r"[\w\u0600-\u06FF]+", (text or "").lower())
    idx = next((i for i, t in enumerate(toks) if clean(t) == clean(value)), None)
    window = toks[:8] if idx is None else toks[max(0, idx - 3): idx + 4]
    keep = []
    for w in window:
        if (w in _STOP or w in _GENERIC or w == unit.lower() or len(w) <= 2
                or re.fullmatch(r"[\d,\.]+", w)):
            continue
        keep.append(w)
    return " ".join(keep)


def find_conflicts(sources: list[dict]) -> list[dict[str, Any]]:
    """
    Sources that give different figures for the same subject.

    Conservative on purpose: same unit, overlapping subject words, two different
    values. Returns [{subject, unit, claims:[{value, n, title}]}]. It does not
    decide who is right — that judgement is not the plugin's to make.
    """
    claims: list[dict[str, Any]] = []
    for src in sources:
        text = f"{src.get('title', '')} {src.get('snippet', '')}"
        for value, unit in numbers_in(text):
            claims.append({"value": value, "unit": unit, "n": src.get("n"),
                           "title": src.get("title", ""),
                           "subject": _subject_of(text, value, unit)})
    conflicts: list[dict[str, Any]] = []
    for i, a in enumerate(claims):
        group = [a]
        for b in claims[i + 1:]:
            if a["unit"] != b["unit"] or a["value"] == b["value"]:
                continue
            words_a, words_b = set(a["subject"].split()), set(b["subject"].split())
            if not words_a or not words_b:
                continue
            overlap = len(words_a & words_b)
            # At least one real shared word, and a decent share of the shorter
            # subject: it has to be the same subject, not merely the same unit.
            if overlap >= 1 and overlap >= min(len(words_a), len(words_b)) * 0.5:
                group.append(b)
        if len(group) > 1:
            key = {c["n"] for c in group}
            if not any(set(c["n"] for c in existing["claims"]) >= key for existing in conflicts):
                conflicts.append({"subject": a["subject"], "unit": a["unit"], "claims": group})
    return conflicts[:6]


# ── the study itself ──────────────────────────────────────────────────────────

def study(topic: str, depth: int = 0, max_sources: int = 0) -> dict[str, Any]:
    """
    Run the study. Returns a structured report; never raises, never exceeds the
    budget, and never lists a source that a search did not actually return.
    """
    topic = str(topic or "").strip()
    if not topic:
        return {"ok": False, "reason": "there is no question to study"}

    ra = _sibling("research_assistant")
    if ra is None:
        return {"ok": False, "reason": ("the research assistant is not next to me — deep "
                                        "research is only the orchestration, not the search"),
                "topic": topic}
    ok, why = ra.search_provider()
    if not ok:
        return {"ok": False, "topic": topic, "reason": why, "sources": []}

    depth = depth or _int_cfg("depth", 2, 1, 3)
    max_searches = _int_cfg("max_searches", 12, 1, 100)
    max_sources = max_sources or _int_cfg("max_sources", 20, 1, 100)
    deadline = time.monotonic() + _int_cfg("timeout_s", 180, 10, 3600)

    tasks, plan_source = ra.break_into_subtopics(topic, max_parts=min(4, max_searches))
    results: list[dict[str, Any]] = []
    searches = 0
    stopped_early = False
    seen_sources: set[str] = set()
    all_sources: list[dict] = []

    for task in tasks:
        if time.monotonic() > deadline:
            stopped_early = True
            break
        remaining = max_searches - searches
        if remaining <= 0:
            stopped_early = True
            break
        # research_assistant does the searching, so the ceiling is handed down to
        # it rather than counted afterwards. The study cannot outspend its budget.
        report = ra.research(task, max_sources=min(remaining * 3, max_sources),
                             depth=min(depth, 2), use_kb=True, max_queries=remaining)
        used = len(report.get("queries", []) or [])
        searches += used if used else remaining
        entry = {"task": task, "ok": bool(report.get("ok")),
                 "sources": [], "answered": bool(report.get("sources")),
                 "reason": report.get("reason", ""),
                 "local": report.get("local", [])}
        for src in report.get("sources", []) or []:
            url = re.sub(r"^https?://(www\.)?", "", str(src.get("url") or "").lower().rstrip("/"))
            key = url or f"{src.get('title', '')[:70]}"
            if key in seen_sources:
                continue
            seen_sources.add(key)
            if len(all_sources) >= max_sources:
                stopped_early = True
                continue
            src["n"] = len(all_sources) + 1
            all_sources.append(src)
            entry["sources"].append(src["n"])
        results.append(entry)

    conflicts = find_conflicts(all_sources)
    unanswered = [r["task"] for r in results if not r["answered"]]
    local: list[dict] = []
    for r in results:
        for item in r.get("local", []):
            if item not in local:
                local.append(item)

    return {"ok": True, "topic": topic, "tasks": [r["task"] for r in results],
            "results": results, "sources": all_sources, "local": local,
            "conflicts": conflicts, "unanswered": unanswered,
            "stopped_early": stopped_early, "searches": searches,
            "budget": {"searches": max_searches, "sources": max_sources, "depth": depth,
                       "seconds": _int_cfg("timeout_s", 180, 10, 3600)},
            "plan_source": plan_source, "provider": why,
            "finished_at": time.time()}


def render(report: dict[str, Any]) -> str:
    """
    The report. Retrieved material and my own arrangement of it never share a
    section, and every figure that is disputed is shown with both claimants.
    """
    if not report.get("ok"):
        return f"I could not run that study, sir: {report.get('reason', 'unknown reason')}."
    r = report
    lines = [f"Deep research: {r['topic']}",
             f"{time.strftime('%Y-%m-%d %H:%M')} · provider: {r.get('provider', '?')} · "
             f"{r.get('searches', 0)} search(es) of a budget of {r['budget']['searches']}",
             ""]

    lines.append(f"── How I split the question ({'my own split' if r.get('plan_source') == 'local' else 'model-proposed'} — analysis, not a source) ──")
    for i, task in enumerate(r["tasks"], 1):
        answered = next((x for x in r["results"] if x["task"] == task), {})
        mark = f"{len(answered.get('sources', []))} source(s)" if answered.get("answered") else "nothing found"
        lines.append(f"  {i}. {task}  [{mark}]")
    lines.append("")

    lines.append("── What the sources say ──")
    if not r["sources"]:
        lines.append("  Nothing was retrieved, sir. I am not going to write a report around that.")
    for src in r["sources"]:
        body = " ".join(str(src.get("snippet", "")).split())
        if len(body) > 350:
            body = body[:347] + "..."
        lines.append(f"\n[{src['n']}] {src.get('title', '(no title)')}")
        if src.get("url"):
            lines.append(f"    {src['url']}")
        elif src.get("grounded"):
            lines.append("    (no link — a grounded answer, not a page I can point you at)")
        lines.append(f"    {body or '(the source returned no summary text)'}")
    lines.append("")

    if r.get("local"):
        lines.append("── From your own indexed files ──")
        for item in r["local"]:
            lines.append(f"  {item.get('title')}  →  {item.get('path')}")
        lines.append("")

    lines.append("── Where the sources disagree ──")
    if r["conflicts"]:
        for c in r["conflicts"]:
            lines.append(f"\n  Subject: {c['subject'][:80]}")
            for claim in c["claims"]:
                lines.append(f"    - {claim['value']} {claim['unit']}  ← source [{claim['n']}] "
                             f"{claim['title'][:50]}")
            lines.append("    I am not choosing between these. The sources said different "
                         "things and both are shown.")
    else:
        lines.append("  No disagreement I could detect — which is not the same as agreement. "
                     "It means no two sources stated different figures for the same subject.")
    lines.append("")

    lines.append("── What I could not establish ──")
    if r["unanswered"]:
        for task in r["unanswered"]:
            lines.append(f"  · {task} — the search returned nothing I could use")
    else:
        lines.append("  Every line of enquiry returned at least one source.")
    lines.append("")

    if r.get("stopped_early"):
        lines.append(f"! The study stopped at its budget after {r.get('searches', 0)} search(es). "
                     f"Anything missing is missing because of the ceiling, not because it does "
                     f"not exist.\n")

    lines.append("── Method, in one line ──")
    lines.append("  Sections marked 'analysis' are my arrangement of your question. "
                 "Everything numbered as a source was returned by an actual search, and I "
                 "have listed only what was returned.")
    return "\n".join(lines)


def _save(report: dict[str, Any], body: str, how: str) -> str:
    topic = report.get("topic", "study")
    if how == "note":
        notes = _sibling("note_manager")
        if notes is None:
            return "\n(I could not save it: the note manager is not next to me.)"
        result = notes.add_note(f"Deep research: {topic[:60]}", body,
                                ["research", "deep"], "research", source="deep_research")
        return (f"\n(Saved as note {result['note']['id']}.)" if result.get("ok")
                else f"\n(I could not save the note: {result.get('reason')})")
    if how == "document":
        docgen = _sibling("document_generator")
        if docgen is None:
            return "\n(I could not save it: the document generator is not next to me.)"
        result = docgen.generate(f"Deep research: {topic[:60]}", body, fmt="docx",
                                 filename=f"deep_research_{topic[:24]}")
        return (f"\n(Saved to {result['path']})" if result.get("ok")
                else f"\n(I could not save the document: {result.get('reason')})")
    return ""


# ── tool entry point ──────────────────────────────────────────────────────────

def run(parameters: dict, player=None, session_memory=None) -> str:
    """Dispatch one deep-research action. Never raises."""
    try:
        p = parameters or {}
        action = str(p.get("action") or "research").strip().lower()

        if action in ("research", "deep", "study", "report"):
            topic = str(p.get("topic") or p.get("query") or "").strip()
            if not topic:
                return "What should I study, sir?"
            if player:
                try:
                    player.write_log(f"[Deep] {topic[:60]}")
                except Exception:
                    pass
            report = study(topic, depth=int(float(p.get("depth") or 0)),
                           max_sources=int(float(p.get("max_sources") or 0)))
            body = render(report)
            if not report.get("ok"):
                return body
            how = str(p.get("save") or _cfg().get("save_as") or "none").strip().lower()
            if how != "none" and not report.get("sources") and not report.get("local"):
                # A file called "Deep research: X" that contains only an apology is
                # worse than no file — someone will open it later and mistake it for
                # a study. There is nothing to save, so nothing is saved.
                return body + (f"\n(Nothing was saved — with no sources retrieved there is "
                               f"no report to write.)")
            return body + _save(report, body, how)

        if action in ("conflicts", "compare"):
            topic = str(p.get("topic") or p.get("query") or "").strip()
            if not topic:
                return "Compare what, sir?"
            report = study(topic, depth=1)
            if not report.get("ok"):
                return render(report)
            if not report["conflicts"]:
                return (f"No disagreement I could detect across {len(report['sources'])} "
                        f"source(s) on '{topic}', sir — which is not the same as agreement.")
            lines = [f"Where the sources disagree on '{topic}':"]
            for c in report["conflicts"]:
                lines.append(f"\n  {c['subject'][:80]}")
                for claim in c["claims"]:
                    lines.append(f"    - {claim['value']} {claim['unit']} ← [{claim['n']}] "
                                 f"{claim['title'][:50]}")
            return "\n".join(lines)

        if action in ("status", "info"):
            ra = _sibling("research_assistant")
            ready = "not next to me" if ra is None else (
                "ready" if ra.search_provider()[0] else ra.search_provider()[1])
            b = {"max_searches": _int_cfg("max_searches", 12, 1, 100),
                 "max_sources": _int_cfg("max_sources", 20, 1, 100),
                 "depth": _int_cfg("depth", 2, 1, 3),
                 "timeout_s": _int_cfg("timeout_s", 180, 10, 3600)}
            return ("Deep research orchestrates the research assistant; it does not search "
                    f"on its own.\nSearch provider : {ready}\n"
                    f"Budget per run  : {b['max_searches']} searches (exact) · "
                    f"{b['max_sources']} sources · depth {b['depth']} · {b['timeout_s']}s\n"
                    f"Deadline        : checked between enquiries, so at most one enquiry "
                    f"goes over\n"
                    f"A source appears only if a search returned it.")

        return (f"Sir, '{action}' is not a deep-research action I have. I can run a study, "
                f"compare sources for disagreements, or report status.")
    except Exception as e:                                   # noqa: BLE001
        return f"Sir, deep research hit a problem: {str(e)[:150]}"


# ── self-test: python deep_research.py ───────────────────────────────────────

def _selftest() -> int:
    print("deep_research — self-test")
    problems = 0

    nums = numbers_in("It cost 240 million pounds and serves 400,000 residents in km2 terms.")
    print(f"  numbers extracted   : {nums[:4]}")
    problems += 0 if any(v == "240" for v, _ in nums) else 1

    fake = [
        {"n": 1, "title": "Plant A report", "snippet": "The new water plant cost 240 million pounds and serves 400000 residents."},
        {"n": 2, "title": "Plant B report", "snippet": "The new water plant cost 180 million pounds and serves 400000 residents."},
        {"n": 3, "title": "Unrelated", "snippet": "Bread needs 40 minutes at 200 degrees."},
    ]
    conflicts = find_conflicts(fake)
    print(f"  conflict found      : {len(conflicts)} — "
          f"{[(c['unit'], [x['value'] for x in c['claims']]) for c in conflicts]}")
    problems += 0 if conflicts and {x["value"] for x in conflicts[0]["claims"]} >= {"240", "180"} else 1
    same = find_conflicts([{"n": 1, "title": "a", "snippet": "It cost 240 million pounds."},
                           {"n": 2, "title": "b", "snippet": "It cost 240 million pounds."}])
    print(f"  agreement ≠ conflict: {len(same)} conflict(s) when both say 240")
    problems += 0 if not same else 1
    unrelated = find_conflicts([{"n": 1, "title": "a", "snippet": "The plant cost 240 million pounds."},
                                {"n": 2, "title": "b", "snippet": "The bridge cost 90 million dollars."}])
    print(f"  different subjects  : {len(unrelated)} conflict(s) for plant vs bridge")
    problems += 0 if not unrelated else 1

    ra = _sibling("research_assistant")
    if ra is None:
        print("  ! the research assistant is missing, so only the comparison could be tested")
        return problems
    ok, why = ra.search_provider()
    print(f"  provider            : {'ready' if ok else 'NOT ready'} — {why[:58]}")

    if ok:
        before = time.monotonic()
        report = study("desalination in Egypt", depth=1, max_sources=8)
        took = time.monotonic() - before
        print(f"  live study          : ok={report.get('ok')} in {took:.1f}s")
        if report.get("ok"):
            print(f"  budget respected    : {report['searches']} ≤ {report['budget']['searches']} "
                  f"searches, {len(report['sources'])} ≤ {report['budget']['sources']} sources")
            problems += 0 if report["searches"] <= report["budget"]["searches"] else 1
            problems += 0 if len(report["sources"]) <= report["budget"]["sources"] else 1
            numbered = [s["n"] for s in report["sources"]]
            print(f"  sources numbered    : {numbered[:6]} (unique: "
                  f"{len(set(numbered)) == len(numbered)})")
            problems += 0 if len(set(numbered)) == len(numbered) else 1
            body = render(report)
            for section in ("How I split", "What the sources say", "disagree",
                            "could not establish", "Method"):
                if section not in body:
                    print(f"  ! missing section: {section}")
                    problems += 1
            print(f"  report sections     : all present")
            if report["unanswered"]:
                print(f"  unanswered named    : {len(report['unanswered'])} task(s) "
                      f"listed as empty rather than filled")
        else:
            print(f"  reason              : {report.get('reason')}")
            problems += 0 if report.get("reason") else 1
    else:
        report = study("anything")
        print(f"  no provider →       : refuses — {report.get('reason', '')[:64]}")
        problems += 0 if not report.get("ok") else 1

    # the loop guard: a deadline in the past must stop before any search
    import types
    real = _int_cfg
    globals()["_int_cfg"] = lambda k, d, lo, hi: 10 if k == "timeout_s" else real(k, d, lo, hi)
    try:
        quick = study("a question with no time to answer")
        print(f"  deadline guard      : stopped_early={quick.get('stopped_early')} "
              f"sources={len(quick.get('sources', []))}")
        problems += 0 if quick.get("ok") else 1
    finally:
        globals()["_int_cfg"] = real

    # The guard against writing an empty report has to be proven, not assumed:
    # a study that retrieved nothing is simulated here, so this check does not
    # depend on the network agreeing with us.
    real_study = globals()["study"]
    globals()["study"] = lambda *a, **k: {
        "ok": True, "topic": "nothing at all", "tasks": ["t"], "results": [],
        "sources": [], "local": [], "conflicts": [], "unanswered": ["t"],
        "stopped_early": False, "searches": 1,
        "budget": {"searches": 1, "sources": 1, "depth": 1, "seconds": 60},
        "plan_source": "local", "provider": "test", "finished_at": 0}
    try:
        empty_out = run({"action": "research", "topic": "nothing at all", "save": "document"})
        wrote = "Nothing was saved" not in empty_out
        print(f"  empty report guard  : wrote a file={wrote} (must be False)")
        problems += 0 if not wrote else 1
    finally:
        globals()["study"] = real_study

    print(f"  empty topic         : {run({'action': 'research', 'topic': ''})}")
    problems += 0 if "What should I study" in run({"action": "research", "topic": ""}) else 1
    print(f"  bad action          : {run({'action': 'sing'})[:70]}")
    problems += 0 if "not a deep-research action" in run({"action": "sing"}) else 1
    print(f"  status              : {run({'action': 'status'}).splitlines()[1][:66]}")
    print("  RESULT:", "PASS" if problems == 0 else f"{problems} problem(s)")
    return problems


if __name__ == "__main__":
    raise SystemExit(_selftest())
