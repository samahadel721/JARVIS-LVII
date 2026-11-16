"""
summarizer.py — shortening a text without inventing anything.

HOW IT WORKS, AND WHY THIS WAY
    The summary is built from sentences that are already in your text. Nothing
    is rephrased, nothing is added, nothing is "filled in" from general
    knowledge. That is the whole point: a summariser that invents a plausible
    sentence is worse than no summariser, because you cannot tell which part
    lied.

    Sentences are ranked by how much of the document's meaning they carry —
    rare words weigh more than common ones — with bonuses for the opening of a
    paragraph and for sentences that carry names, numbers, dates or currency.
    Those are exactly the details people lose when they skim.

    There is also a model mode (use_model = on). It writes a fluent summary
    using the assistant's own Gemini key, and every answer it produces is
    labelled "model-written" so you always know which kind you are reading.

    Works on: plain text, a file, a folder of files, or the text another plugin
    hands it (that is how the research plugins use it).

SETTINGS (Plugin Manager → Summarizer)
    mode        short | medium | detailed — the default length
    bullets     off | on — bullet points instead of a paragraph
    use_model   off | on — fluent, model-written summary instead of extractive
    max_sentences  40 — hard cap on how much any summary may contain
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from typing import Any

PLUGIN = {
    "name": "summarizer",
    "description": (
        "Shortens a text, a file, or a folder of files into the sentences that carry "
        "the meaning: 'summarise this document', 'give me the short version of these "
        "notes', 'make bullet points from that report'. It keeps names, numbers and "
        "dates and it never adds anything that was not in the source."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "summarize | facts | file | status"},
            "text": {"type": "STRING", "description": "The text to shorten"},
            "path": {"type": "STRING", "description": "A file or folder to shorten"},
            "mode": {"type": "STRING", "description": "short | medium | detailed"},
            "bullets": {"type": "STRING", "description": "yes | no"},
            "focus": {"type": "STRING", "description": "Optional topic to bias the summary toward"},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "summarizer",
    "title": "Summarizer / الملخّص",
    "fields": [
        {"key": "mode", "type": "choice", "label": "Default length",
         "default": "medium", "choices": ["short", "medium", "detailed"]},
        {"key": "bullets", "type": "choice", "label": "Bullet points",
         "default": "off", "choices": ["off", "on"]},
        {"key": "use_model", "type": "choice",
         "label": "Model-written summary (sends text to Gemini)",
         "default": "off", "choices": ["off", "on"]},
        {"key": "max_sentences", "type": "text", "label": "Hard cap (40)", "default": "40"},
    ],
}

_RATIO = {"short": 0.12, "medium": 0.28, "detailed": 0.50}
_MIN_SENTENCES = {"short": 1, "medium": 3, "detailed": 5}

# Sentence ends in English and Arabic. Arabic uses ؟ for questions and ۔ is rare.
_SPLIT = re.compile(r"(?<=[.!?\u061f\u3002])\s+|\n{2,}")
_NUMBERISH = re.compile(r"\d|[$€£\u00a3]|\bEGP\b|\bUSD\b|\bAED\b|\bSAR\b|\u0660-\u0669")
# A capitalised word that is not the first word of the sentence suggests a name.
_NAME = re.compile(r"(?<!^)(?<!\. )\b[A-Z][a-z]{2,}\b")
_ARABIC_NAME = re.compile(r"\b(?:أ|إ|ا|ب|ت|م|ع|د|س|ف|ح|ك|ن|ص|ق|ر|ه|و|ي)?[A-Za-z]?[\u0621-\u064a]{3,}\b")


def _cfg() -> dict[str, Any]:
    out = {f["key"]: f.get("default") for f in PLUGIN_SETTINGS["fields"]}
    try:
        from memory.config_manager import get_plugin_config
        out.update({k: v for k, v in (get_plugin_config("summarizer") or {}).items()
                    if v not in (None, "")})
    except Exception:
        pass
    return out


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in ("on", "yes", "true", "1", "y")


def _cap_sentences() -> int:
    try:
        return max(1, min(200, int(str(_cfg().get("max_sentences") or "40"))))
    except Exception:
        return 40


_STOP = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is", "are", "was",
    "were", "be", "been", "it", "this", "that", "with", "as", "at", "by", "from", "but",
    "not", "have", "has", "had", "my", "your", "will", "would", "can", "could", "there",
    "من", "في", "على", "عن", "الى", "أو", "ثم", "هذا", "هذه", "ذلك", "التي", "الذي",
    "ما", "لا", "هو", "هي", "انا", "انت", "كان", "كانت", "بين", "كل", "بعض", "قد",
    "لم", "لن", "ان", "مع", "به", "بها", "له", "عند", "حتى", "اذا", "كما", "لكن",
}


def _norm(word: str) -> str:
    w = word.lower().replace("\u0640", "")
    for ch in "\u0622\u0623\u0625":
        w = w.replace(ch, "\u0627")
    return w.replace("\u0649", "\u064a").replace("\u0629", "\u0647")


def _words(text: str) -> list[str]:
    out = []
    for raw in re.findall(r"[\w\u0600-\u06FF]+", text or ""):
        w = _norm(raw)
        if len(w) > 1 and w not in _STOP:
            out.append(w)
    return out


def sentences(text: str) -> list[str]:
    """Split into sentences, keeping Arabic and English punctuation. Never empty parts."""
    parts = [p.strip() for p in _SPLIT.split(text or "")]
    return [re.sub(r"\s+", " ", p) for p in parts if len(p.strip()) > 3]


def key_facts(text: str) -> list[str]:
    """
    The sentences carrying a number, a date or a currency amount — deduplicated.
    These are the lines a careful reader would circle.
    """
    facts: list[str] = []
    seen: set[str] = set()
    for s in sentences(text):
        if not _NUMBERISH.search(s):
            continue
        key = re.sub(r"\W+", "", s.lower())[:80]
        if key in seen:
            continue
        seen.add(key)
        facts.append(s)
    return facts


def _score(sentence: str, freq: dict[str, int], position: int, total: int) -> float:
    words = _words(sentence)
    if not words:
        return 0.0
    # rare words count for more: log-damped frequency
    base = sum(1.0 / (1.0 + __import__("math").log(1 + freq.get(w, 1))) for w in words)
    score = base / (len(words) ** 0.55)          # do not let long sentences win by size
    if position == 0:
        score *= 1.35                            # openings usually set the subject
    elif position < 3:
        score *= 1.12
    if position > total * 0.9:
        score *= 0.92                            # closings repeat more often than they add
    if _NUMBERISH.search(sentence):
        score *= 1.25                            # numbers, dates, money
    if _NAME.search(sentence) or len(_ARABIC_NAME.findall(sentence)) >= 2:
        score *= 1.10                            # proper nouns
    if 8 <= len(words) <= 40:
        score *= 1.05
    return score


def _rank(text: str, focus: str = "") -> list[tuple[float, int, str]]:
    sents = sentences(text)
    if not sents:
        return []
    freq: dict[str, int] = {}
    for w in _words(text):
        freq[w] = freq.get(w, 0) + 1
    focus_words = set(_words(focus)) if focus else set()
    ranked = []
    for i, s in enumerate(sents):
        score = _score(s, freq, i, len(sents))
        if focus_words:
            overlap = len(focus_words & set(_words(s))) / max(len(focus_words), 1)
            score *= (1.0 + overlap)             # bias toward the requested topic
        ranked.append((score, i, s))
    ranked.sort(key=lambda x: -x[0])
    return ranked


def summarize_structured(text: str, mode: str = "medium", bullets: bool = False,
                         focus: str = "") -> dict[str, Any]:
    """
    The reusable form: returns the chosen sentences plus the counts behind them,
    so another plugin can quote the summary and disclose how it was made.
    """
    text = (text or "").strip()
    if not text:
        return {"ok": False, "reason": "there is no text to summarise", "summary": "",
                "sentences": [], "total_sentences": 0, "words": 0, "mode": mode}
    sents = sentences(text)
    if len(sents) <= 1:
        return {"ok": True, "summary": " ".join(sents), "sentences": sents,
                "total_sentences": len(sents), "words": len(text.split()), "mode": mode,
                "note": "the source is a single sentence — there is nothing to shorten"}

    mode = (mode or "medium").strip().lower()
    if mode not in _RATIO:
        mode = "medium"
    ranked = _rank(text, focus)
    want = max(_MIN_SENTENCES[mode], int(round(len(sents) * _RATIO[mode])))
    want = min(want, _cap_sentences(), len(sents))
    chosen = sorted(ranked[:want], key=lambda x: x[1])       # back into reading order

    lines = [s for _, _, s in chosen]
    if bullets:
        body = "\n".join(f"- {s.rstrip('.')}" for s in lines)
    else:
        body = " ".join(lines)
    return {"ok": True, "summary": body, "sentences": lines, "bullets": bool(bullets),
            "total_sentences": len(sents), "words": len(text.split()),
            "kept_ratio": round(len(lines) / len(sents), 2), "mode": mode,
            "facts": key_facts(text)[:12]}


def _model_summary(text: str, mode: str) -> tuple[bool, str]:
    """
    Optional fluent summary through the assistant's own Gemini key.
    Returns (ok, text_or_reason) — never raises, never claims success on failure.
    """
    try:
        from memory.config_manager import get_api_key
        key = str(get_api_key() or "").strip()
    except Exception:
        key = ""
    if not key:
        return False, "no API key is configured, so the model mode is unavailable"
    target = {"short": "about 4 sentences", "medium": "about 8 sentences",
              "detailed": "two or three paragraphs"}.get(mode, "about 8 sentences")
    prompt = (f"Summarise the text below in {target}, in the same language as the text. "
              f"Use only information present in the text — do not add anything, and say "
              f"so if something is missing.\n\n{text[:12000]}")
    try:
        from google import genai
        reply = genai.Client(api_key=key).models.generate_content(
            model="gemini-2.0-flash", contents=prompt)
        out = (getattr(reply, "text", "") or "").strip()
        return (True, out) if out else (False, "the model returned nothing")
    except Exception as e:
        return False, f"the model call failed ({str(e)[:80]})"


def _sibling(name: str):
    """Load a sibling plugin by path — the pattern the rest of the suite uses."""
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


def read_any(path: str | Path) -> tuple[bool, str]:
    """
    Get text out of a file. The Knowledge Base does the real extraction (PDF,
    DOCX, plain text) — this borrows it so there is only one extractor in the
    suite, and falls back to a plain read for text files.
    """
    p = Path(path).expanduser()
    if not p.exists() or p.is_dir():
        return False, f"'{p}' is not a file I can read"
    kb = _sibling("knowledge_base")
    if kb is not None and hasattr(kb, "extract_text"):
        try:
            ok, text = kb.extract_text(p)
            if ok and text.strip():
                return True, text
        except Exception:
            pass
    if p.suffix.lower() in (".txt", ".md", ".rst", ".log", ".csv", ".json", ".py", ".yml",
                            ".yaml", ".toml", ".ini", ".cfg"):
        try:
            return True, p.read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            return False, f"I could not read it: {str(e)[:80]}"
    return False, (f"I cannot pull text out of a {p.suffix or 'unknown'} file without the "
                   f"Knowledge Base — drop knowledge_base.py in next to me and I can.")


def summarize(text: str, mode: str = "medium", bullets: bool = False,
              focus: str = "") -> str:
    """
    The one-line API other plugins call. Returns the summary itself, or a plain
    sentence explaining why there is none. Never invents, never raises.
    """
    try:
        result = summarize_structured(text, mode=mode, bullets=bullets, focus=focus)
        return result["summary"] if result.get("ok") else f"(no summary: {result.get('reason')})"
    except Exception as e:                                   # noqa: BLE001
        return f"(no summary: {str(e)[:80]})"


# ── tool entry point ──────────────────────────────────────────────────────────

def _do_summary(text: str, mode: str, bullets: bool, focus: str, player=None) -> str:
    text = (text or "").strip()
    if not text:
        return "What should I summarise, sir?"
    if _truthy(_cfg().get("use_model")):
        ok, out = _model_summary(text, mode)
        if ok:
            return f"[model-written summary — may paraphrase; source had {len(text.split())} words]\n{out}"
        # fall through to the honest local path, and say why
        note = f"(the model mode did not run: {out}. This is the extractive summary instead.)\n\n"
    else:
        note = ""
    r = summarize_structured(text, mode=mode, bullets=bullets, focus=focus)
    if not r.get("ok"):
        return f"Sir, {r.get('reason')}."
    if player:
        try:
            player.write_log(f"[Summary] {r['words']} words → {r['mode']}")
        except Exception:
            pass
    tail = (f"\n\n({r['total_sentences']} sentences → {len(r['sentences'])} kept as the "
            f"{r['mode']} version. Extractive: these are the source's own sentences.)")
    return note + r["summary"] + tail


def run(parameters: dict, player=None, session_memory=None) -> str:
    """Dispatch one summariser action. Never raises."""
    try:
        p = parameters or {}
        action = str(p.get("action") or "summarize").strip().lower()
        cfg = _cfg()
        mode = str(p.get("mode") or cfg.get("mode") or "medium").strip().lower()
        bullets = _truthy(p.get("bullets")) or _truthy(cfg.get("bullets"))
        focus = str(p.get("focus") or "")

        if action in ("summarize", "summary", "shorten", "tldr"):
            text = str(p.get("text") or "")
            if not text and p.get("path"):
                ok, text = read_any(str(p["path"]))
                if not ok:
                    return f"Sir, {text}."
            return _do_summary(text, mode, bullets, focus, player)

        if action in ("file", "summarize_file", "document"):
            path = str(p.get("path") or "").strip()
            if not path:
                return "Which file, sir?"
            ok, text = read_any(path)
            if not ok:
                return f"Sir, {text}."
            return _do_summary(text, mode, bullets, focus, player)

        if action in ("folder", "directory"):
            path = str(p.get("path") or "").strip()
            if not path:
                return "Which folder, sir?"
            folder = Path(path).expanduser()
            if not folder.is_dir():
                return f"Sir, '{path}' is not a folder I can read."
            exts = {".txt", ".md", ".rst", ".py", ".csv", ".json", ".log", ".docx", ".pdf"}
            parts, skipped = [], 0
            for f in sorted(folder.iterdir())[:50]:
                if not f.is_file() or f.suffix.lower() not in exts:
                    continue
                ok, text = read_any(f)
                if ok and text.strip():
                    parts.append(f"--- {f.name} ---\n{text}")
                else:
                    skipped += 1
            if not parts:
                return f"I could not read any text file in {folder.name}, sir."
            joined = "\n\n".join(parts)
            out = _do_summary(joined, mode, bullets, focus, player)
            return (f"{len(parts)} file(s) from '{folder.name}'"
                    + (f", {skipped} skipped as unreadable" if skipped else "") + ".\n\n" + out)

        if action in ("facts", "numbers", "key_points"):
            text = str(p.get("text") or "")
            if not text and p.get("path"):
                ok, text = read_any(str(p["path"]))
                if not ok:
                    return f"Sir, {text}."
            facts = key_facts(text)
            if not facts:
                return ("There is not a single number, date or amount in that text, sir — "
                        "nothing to pull out.")
            return "The lines carrying figures:\n" + "\n".join(f"- {f}" for f in facts[:12])

        if action in ("status", "info"):
            cfg = _cfg()
            return (f"Mode: {cfg.get('mode')} · bullets: {cfg.get('bullets')} · "
                    f"model-written: {cfg.get('use_model')} · cap: {_cap_sentences()} sentences. "
                    f"Extractive by default — the source's own sentences, nothing added.")

        return (f"Sir, '{action}' is not a summariser action I have. I can summarise text or "
                f"a file, list the numbers, or report settings.")
    except Exception as e:                                   # noqa: BLE001
        return f"Sir, the summariser hit a problem: {str(e)[:150]}"


# ── self-test: python summarizer.py ──────────────────────────────────────────

def _selftest() -> int:
    print("summarizer — self-test")
    problems = 0
    src = (
        "The new water treatment plant opens in Mansoura on 14 March 2026. "
        "It cost 240 million Egyptian pounds and will serve 400,000 residents. "
        "The old plant was built in 1974 and had failed twice this year. "
        "Engineer Hala Mansour led the project for three years. "
        "Separately, the ministry said school hours will not change, and the "
        "committee will meet again in April to review the remaining papers. "
        "Nothing else was decided at the meeting and the file stays open."
    )
    r = summarize_structured(src, mode="short")
    print(f"  short               : {r['total_sentences']} sentences → {len(r['sentences'])}")
    problems += 0 if r["ok"] and 1 <= len(r["sentences"]) <= 4 else 1
    joined = " ".join(r["sentences"])
    print(f"  keeps the date      : {'14 March 2026' in joined}")
    problems += 0 if "14 March 2026" in joined or "240 million" in joined else 1

    r2 = summarize_structured(src, mode="detailed", bullets=True)
    print(f"  detailed + bullets  : {len(r2['sentences'])} sentences, bullets={r2['bullets']}")
    problems += 0 if r2["bullets"] and r2["summary"].startswith("- ") else 1
    problems += 0 if len(r2["sentences"]) >= len(r["sentences"]) else 1

    invented = []
    for s in r2["sentences"]:
        if s not in sentences(src) and s.rstrip(".") not in [x.rstrip(".") for x in sentences(src)]:
            invented.append(s)
    print(f"  nothing invented    : {len(invented)} new sentence(s)")
    problems += 0 if not invented else 1

    facts = key_facts(src)
    print(f"  key facts           : {len(facts)} line(s) with figures")
    problems += 0 if any("240 million" in f for f in facts) else 1

    ar = ("افتتحت محطة معالجة المياه الجديدة في المنصورة يوم ١٤ مارس ٢٠٢٦. "
          "وقال المهندس إن التكلفة بلغت ٢٤٠ مليون جنيه. "
          "وقد اجتمع الفريق ثلاث مرات خلال الشهر الماضي لمناقشة الجدول الزمني.")
    ra = summarize_structured(ar, mode="short")
    print(f"  Arabic              : {len(ra['sentences'])}/{ra['total_sentences']} sentences kept")
    problems += 0 if ra["ok"] and ra["sentences"] else 1

    print(f"  empty input         : {run({'action': 'summarize', 'text': ''})}")
    problems += 0 if "What should I summarise" in run({"action": "summarize", "text": ""}) else 1
    print(f"  bad action          : {run({'action': 'sing'})[:80]}")
    problems += 0 if "not a summariser action" in run({"action": "sing"}) else 1

    tmp = Path(__file__).resolve().parent / "_summarizer_probe.txt"
    tmp.write_text(src, encoding="utf-8")
    out = run({"action": "file", "path": str(tmp)})
    print(f"  file mode           : works={('sentences' in out)}")
    problems += 0 if "sentences" in out else 1
    tmp.unlink(missing_ok=True)

    print(f"  use_model default   : {_cfg().get('use_model')} (must be off — local by default)")
    problems += 0 if not _truthy(_cfg().get("use_model")) else 1
    print("  RESULT:", "PASS" if problems == 0 else f"{problems} problem(s)")
    return problems


if __name__ == "__main__":
    raise SystemExit(_selftest())
