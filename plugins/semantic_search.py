"""
semantic_search.py — searching by meaning, not by matching letters.

WHAT THIS IS
    A small search engine the other plugins can borrow. It keeps its own index
    and answers questions about it. It does not read your files itself and it
    does not decide what is worth indexing — the Knowledge Base and the Note
    Manager hand it documents, and it makes them findable.

TWO WAYS TO SEARCH, AND WHY THERE ARE TWO
    Embeddings (meaning)  — when you switch them on, each document is turned
                            into a vector by Gemini, using the same API key the
                            assistant already has. Then "car maintenance" can
                            find a note that only ever says "oil change".

    BM25 (words)          — always available, no key, no network, no cost. It
                            is a proper relevance ranking, not a substring test:
                            rare words count more than common ones, and long
                            documents are not unfairly favoured.

    Embeddings are OFF by default, on purpose. Turning them on means the text
    of whatever you index is sent to Google. That is a real decision about your
    private documents and it is yours to make, not mine to make for you. Words
    are sent only if you enable them, and notes are kept out of the embeddings
    even then unless you ask for them by name (embed_notes).

A LIMIT WORTH KNOWING
    With embeddings off — the default — this is a word-based search wearing good
    ranking. A document that never writes "car" cannot be found by the query
    "car maintenance", no matter how clever the ranking is. That is not a bug
    and I will not dress it up as one: it is exactly the gap that embeddings
    exist to close, and the plugin says so when a search comes back empty.

HONESTY RULE
    Every result comes from the index. Nothing is invented to fill a gap. If
    the best score is below the relevance floor, the answer is "I found nothing
    relevant", not the least-bad match dressed up as an answer.

SETTINGS (Plugin Manager → Semantic search)
    embeddings      off | on — send indexed text to Gemini for vectors
    embed_notes     off | on — include personal notes in that (off by default)
    min_score       0.05 — below this, results are not returned at all
    max_chunks      how many documents the vector step will process (default 2000)
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
from pathlib import Path
from typing import Any

PLUGIN = {
    "name": "semantic_search",
    "description": (
        "Searches your indexed documents and notes by meaning rather than exact "
        "wording: 'search my notes for anything about the car'. Use it whenever the "
        "user asks to find something they saved, without knowing the exact words. It "
        "searches what has been indexed — it cannot search the whole computer."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "search | stats | reindex | clear"},
            "query": {"type": "STRING", "description": "What to look for, in the user's words"},
            "limit": {"type": "NUMBER", "description": "How many results, default 5"},
            "scope": {"type": "STRING", "description": "Optional: 'files', 'notes', or a folder path"},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "semantic_search",
    "title": "Semantic search / البحث الدلالي",
    "fields": [
        {"key": "embeddings", "type": "choice", "label": "Search by meaning (sends text to Gemini)",
         "default": "off", "choices": ["off", "on"]},
        {"key": "embed_notes", "type": "choice", "label": "Include notes in that (private)",
         "default": "off", "choices": ["off", "on"]},
        {"key": "min_score", "type": "text", "label": "Relevance floor (0.05)",
         "default": "0.05"},
        {"key": "max_chunks", "type": "text", "label": "Max documents to embed (2000)",
         "default": "2000"},
    ],
}

INDEX_PATH = Path(__file__).resolve().parent.parent / "config" / ".plugin_cache" / "semantic_index.json"
MAX_TEXT_PER_DOC = 1500          # characters kept per document for scoring + preview
EMBED_MODELS = ("gemini-embedding-001", "text-embedding-004", "embedding-001")
_EMBED_MODEL_OK: str | None = None      # the first model name that answered, cached

# ── Arabic-aware tokenising ───────────────────────────────────────────────────
# أ إ آ ا are the same letter to a reader, so they must be the same token here.
# Harakat and tatweel are decoration: "أحمَـد" and "احمد" are one word.
_ALEF = "\u0622\u0623\u0625\u0627"
_FOLD = {"\u0649": "\u064a", "\u0629": "\u0647", "\u0624": "\u0648", "\u0626": "\u064a"}
_HARAKAT = re.compile("[\u064b-\u0652\u0640\u0670]")
_TOKEN = re.compile(r"[\w\u0600-\u06FF]+", re.UNICODE)
_STOP = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is", "are", "was",
    "were", "be", "been", "it", "this", "that", "with", "as", "at", "by", "from", "but",
    "not", "have", "has", "had", "my", "your", "his", "her", "its", "our", "their", "i",
    "you", "he", "she", "we", "they", "me", "him", "them", "what", "which", "who", "when",
    "من", "في", "على", "عن", "الى", "إلى", "او", "أو", "و", "ثم", "هذا", "هذه", "ذلك",
    "التي", "الذي", "ما", "لا", "هو", "هي", "انا", "أنا", "انت", "أنت", "كان", "كانت",
    "بين", "كل", "بعض", "قد", "لم", "لن", "ان", "أن", "إن", "مع", "به", "بها", "له",
    "فيها", "فيه", "عند", "حتى", "اذا", "إذا", "كما", "لكن", "او", "هناك",
}


def _norm(token: str) -> str:
    """One token, folded to the form a reader would call 'the same word'."""
    token = _HARAKAT.sub("", token.lower())
    for ch in _ALEF:
        token = token.replace(ch, "\u0627")
    for src, dst in _FOLD.items():
        token = token.replace(src, dst)
    return token


def _tokens(text: str) -> list[str]:
    return [_norm(t) for t in _TOKEN.findall(text or "")
            if len(t) > 1 and _norm(t) not in _STOP]


def _cfg() -> dict[str, Any]:
    out = {f["key"]: f.get("default") for f in PLUGIN_SETTINGS["fields"]}
    try:
        from memory.config_manager import get_plugin_config
        out.update({k: v for k, v in (get_plugin_config("semantic_search") or {}).items()
                    if v not in (None, "")})
    except Exception:
        pass
    return out


def _embeddings_on() -> bool:
    return str(_cfg().get("embeddings", "off")).strip().lower() in ("on", "true", "1", "yes")


# ── the index on disk ─────────────────────────────────────────────────────────

_CACHE: dict[str, Any] | None = None


def _load() -> dict[str, Any]:
    """Read the index. A damaged file is never fatal — it becomes an empty index."""
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    try:
        data = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("docs"), list):
            raise ValueError("shape")
        _CACHE = data
    except FileNotFoundError:
        _CACHE = {"docs": [], "updated": 0.0}
    except Exception:
        _CACHE = {"docs": [], "updated": 0.0, "damaged": True}
    return _CACHE


def _save(data: dict[str, Any]) -> None:
    """Atomic write: a power cut mid-save leaves the old index intact."""
    global _CACHE
    data["updated"] = data.get("updated") or __import__("time").time()
    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(INDEX_PATH.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, INDEX_PATH)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise
    _CACHE = data


# ── embeddings: optional, explicit, cached ────────────────────────────────────

def _gemini_key() -> str:
    """The key the assistant already runs on. Never logged, never printed."""
    try:
        from memory.config_manager import get_api_key
        return str(get_api_key() or "").strip()
    except Exception:
        return ""


def _embed(texts: list[str]) -> list[list[float]] | None:
    """
    Turn text into vectors, or None when embeddings are off / unavailable.

    Every failure mode returns None and is explained by the caller — a search
    must still work when the network does not.
    """
    global _EMBED_MODEL_OK
    if not _embeddings_on() or not texts:
        return None
    key = _gemini_key()
    if not key:
        return None
    try:
        from google import genai
    except Exception:
        return None
    try:
        client = genai.Client(api_key=key)
        for model in ([_EMBED_MODEL_OK] if _EMBED_MODEL_OK else EMBED_MODELS):
            try:
                reply = client.models.embed_content(model=model, contents=texts)
                vectors = [list(v.values) for v in (reply.embeddings or [])]
                if len(vectors) == len(texts) and vectors:
                    _EMBED_MODEL_OK = model
                    return vectors
            except Exception:
                continue
    except Exception:
        return None
    return None


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine on flat vectors — the SDK's own matcher chokes on these."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


# ── BM25: the search that always works ────────────────────────────────────────

def _bm25(docs: list[dict], query: str) -> list[tuple[float, dict]]:
    """
    Rank documents against a query. Rare words weigh more; length is normalised.

    The title is scored alongside the body, and it counts double: a file called
    'plant.md' whose contents are in another language is still findable by the
    name its owner gave it. Searching for a filename is one of the most common
    things anyone does with a document store.
    """
    q = _tokens(query)
    if not q or not docs:
        return []
    tokenised = [_tokens(d.get("text", "")) + _tokens(d.get("title", "")) * 2 for d in docs]
    lengths = [len(t) or 1 for t in tokenised]
    avg = sum(lengths) / max(len(lengths), 1)
    df: dict[str, int] = {}
    for toks in tokenised:
        for t in set(toks):
            df[t] = df.get(t, 0) + 1
    n = len(docs)
    k1, b = 1.5, 0.75
    scored: list[tuple[float, dict]] = []
    for i, toks in enumerate(tokenised):
        counts: dict[str, int] = {}
        for t in toks:
            counts[t] = counts.get(t, 0) + 1
        score = 0.0
        for term in q:
            tf = counts.get(term, 0)
            if not tf:
                continue
            idf = math.log(1 + (n - df.get(term, 0) + 0.5) / (df.get(term, 0) + 0.5))
            score += idf * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * lengths[i] / avg))
        if score > 0:
            scored.append((score, docs[i]))
    scored.sort(key=lambda x: -x[0])
    return scored


def _normalise_scores(pairs: list[tuple[float, dict]]) -> list[tuple[float, dict]]:
    """Scale the best hit to 1.0 so a floor like 0.05 means something stable."""
    if not pairs:
        return []
    top = pairs[0][0] or 1.0
    return [(s / top, d) for s, d in pairs]


# ── public API, used by the other plugins ─────────────────────────────────────

def has_docs() -> bool:
    """Is there anything indexed at all?"""
    return bool(_load().get("docs"))


def available() -> tuple[bool, str]:
    """(usable, why not) — so a caller can say something useful to the user."""
    if not has_docs():
        return False, ("nothing has been indexed yet — ask me to index a folder "
                       "(Knowledge Base) or add a note first")
    mode = "meaning + words" if _embeddings_on() else "words (embeddings are off)"
    return True, mode


def index_docs(docs: list[dict], replace_source: str = "") -> int:
    """
    Add documents to the index, replacing anything previously indexed from the
    same source. Each doc: {id, source, title, text}. Returns how many landed.
    """
    data = _load()
    kept = [d for d in data["docs"] if d.get("source") != replace_source or not replace_source]
    added = 0
    for doc in docs:
        text = str(doc.get("text") or "").strip()
        if not text:
            continue
        kept.append({
            "id": str(doc.get("id") or f"{doc.get('source')}#{added}"),
            "source": str(doc.get("source") or ""),
            "title": str(doc.get("title") or "")[:200],
            "text": text[:MAX_TEXT_PER_DOC],
            "kind": str(doc.get("kind") or "file"),
        })
        added += 1
    data["docs"] = kept
    # Vectors are refreshed for the new documents only when the user asked for
    # meaning-based search; otherwise the BM25 path handles everything.
    if _embeddings_on():
        _embed_missing(data)
    _save(data)
    return added


def _embed_missing(data: dict[str, Any]) -> int:
    """Fill in vectors for documents that do not have one. Silently gives up on failure."""
    cfg = _cfg()
    try:
        cap = int(str(cfg.get("max_chunks") or "2000"))
    except Exception:
        cap = 2000
    want_notes = str(cfg.get("embed_notes", "off")).lower() in ("on", "true", "1", "yes")
    pending = [d for d in data["docs"]
               if not d.get("vec") and (want_notes or d.get("kind") != "note")]
    pending = pending[:max(cap, 0)]
    if not pending:
        return 0
    done = 0
    for i in range(0, len(pending), 16):        # small batches: kinder to quotas
        batch = pending[i:i + 16]
        vectors = _embed([f"{d.get('title', '')}\n{d.get('text', '')}" for d in batch])
        if not vectors:
            break
        for doc, vec in zip(batch, vectors):
            doc["vec"] = [round(v, 5) for v in vec]
            done += 1
    return done


def remove_source(source: str) -> int:
    """Drop everything indexed from one source (a file that was deleted, say)."""
    data = _load()
    before = len(data["docs"])
    data["docs"] = [d for d in data["docs"] if d.get("source") != source]
    removed = before - len(data["docs"])
    if removed:
        _save(data)
    return removed


def clear() -> int:
    """Empty the index entirely."""
    data = _load()
    n = len(data["docs"])
    _save({"docs": []})
    return n


def _pull_sources() -> list[dict]:
    """
    Rebuild help: read what the Knowledge Base and the Note Manager keep on
    disk. This is a file contract, not an import, so the three stay independent
    and either can be removed without breaking the others.
    """
    docs: list[dict] = []
    base = INDEX_PATH.parent.parent
    for path, kind in ((base / "knowledge" / "index.json", "file"),
                       (base / "notes" / "notes.json", "note")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if kind == "file":
            for f in raw.get("files", []):
                for i, chunk in enumerate(f.get("chunks", [])):
                    docs.append({"id": f"{f.get('path')}#{i}", "source": f.get("path", ""),
                                 "title": f.get("name", ""), "text": chunk, "kind": "file"})
        else:
            for note in raw.get("notes", []):
                docs.append({"id": f"note:{note.get('id')}", "source": f"note:{note.get('id')}",
                             "title": note.get("title", ""),
                             "text": f"{note.get('title', '')}\n{note.get('content', '')}",
                             "kind": "note"})
    return docs


def rebuild() -> int:
    """Re-read every source from disk and rebuild the index from scratch."""
    docs = _pull_sources()
    _save({"docs": []})
    if not docs:
        return 0
    data = {"docs": [{"id": d["id"], "source": d["source"], "title": d["title"],
                      "text": d["text"][:MAX_TEXT_PER_DOC], "kind": d["kind"]} for d in docs]}
    if _embeddings_on():
        _embed_missing(data)
    _save(data)
    return len(data["docs"])


def search(query: str, limit: int = 5, scope: str = "") -> list[dict]:
    """
    Find the documents that best answer `query`.

    Returns a list of {title, source, text, score, how} sorted best-first — and
    an empty list when nothing clears the relevance floor. An empty list means
    "no relevant result", and callers must say exactly that.
    """
    query = str(query or "").strip()
    if not query:
        return []
    docs = _load().get("docs", [])
    if scope:
        low = scope.strip().lower()
        if low in ("notes", "note"):
            docs = [d for d in docs if d.get("kind") == "note"]
        elif low in ("files", "file"):
            docs = [d for d in docs if d.get("kind") != "note"]
        else:
            docs = [d for d in docs if low in str(d.get("source", "")).lower()]
    if not docs:
        return []

    ranked = _normalise_scores(_bm25(docs, query))
    how = {id(d): "words" for _, d in ranked}

    if _embeddings_on():
        qvec = _embed([query])
        if qvec:
            sims = [(max(_cosine(qvec[0], d.get("vec") or []), 0.0), d) for d in docs]
            sims = [x for x in sims if x[0] > 0]
            sims.sort(key=lambda x: -x[0])
            if sims:
                top = sims[0][0] or 1.0
                for s, d in sims:
                    how[id(d)] = "meaning"
                # Blend: meaning first, words as the tie-breaker. Documents that
                # only one method found still appear.
                merged: dict[int, tuple[float, dict]] = {}
                for s, d in sims:
                    merged[id(d)] = (min(1.0, s / top), d)
                for s, d in ranked:
                    old = merged.get(id(d))
                    merged[id(d)] = ((s * 0.4) if not old else (old[0] * 0.8 + s * 0.2), d)
                ranked = sorted(merged.values(), key=lambda x: -x[0])

    try:
        floor = float(str(_cfg().get("min_score") or "0.05"))
    except Exception:
        floor = 0.05
    out = []
    for score, doc in ranked[:max(1, int(limit or 5))]:
        if score < floor:
            break
        out.append({"title": doc.get("title") or Path(str(doc.get("source", ""))).name,
                    "source": doc.get("source", ""), "text": doc.get("text", ""),
                    "score": round(score, 3), "how": how.get(id(doc), "words"),
                    "kind": doc.get("kind", "file")})
    return out


def stats() -> dict[str, Any]:
    """Counts for the status line — no content, so it is always safe to show."""
    data = _load()
    docs = data.get("docs", [])
    files = {d.get("source") for d in docs if d.get("kind") != "note"}
    return {
        "documents": len(docs),
        "files": len(files),
        "notes": sum(1 for d in docs if d.get("kind") == "note"),
        "embedded": sum(1 for d in docs if d.get("vec")),
        "embeddings": "on" if _embeddings_on() else "off",
        "updated": data.get("updated") or 0,
        "damaged": bool(data.get("damaged")),
    }


# ── tool entry point ──────────────────────────────────────────────────────────

def _render(results: list[dict], query: str, limit: int) -> str:
    if not results:
        hint = ("" if _embeddings_on() else
                " I search by words at the moment; if the words you used are not in the "
                "document, turning on 'search by meaning' in my settings would look for "
                "the idea instead.")
        return (f"I found nothing relevant for '{query}' in what I have indexed, sir — "
                f"not even a weak match. I will not guess at one.{hint}")
    lines = [f"{len(results)} result(s) for '{query}', sir:"]
    for i, r in enumerate(results, 1):
        body = " ".join(str(r["text"]).split())
        if len(body) > 260:
            body = body[:257] + "..."
        lines.append(f"\n{i}. {r['title']}  [{r['score']}, by {r['how']}]\n   {r['source']}\n   {body}")
    return "\n".join(lines)


def run(parameters: dict, player=None, session_memory=None) -> str:
    """Dispatch one search action. Never raises."""
    try:
        p = parameters or {}
        action = str(p.get("action") or "search").strip().lower()

        if action in ("search", "find", "ask", "query"):
            query = str(p.get("query") or "").strip()
            if not query:
                return "What should I look for, sir?"
            limit = int(float(p.get("limit") or 5))
            results = search(query, limit=limit, scope=str(p.get("scope") or ""))
            if player:
                try:
                    player.write_log(f"[Search] {query} → {len(results)} hit(s)")
                except Exception:
                    pass
            return _render(results, query, limit)

        if action in ("stats", "status", "info"):
            s = stats()
            if not s["documents"]:
                return ("Nothing is indexed yet, sir. Ask me to index a folder — I need an "
                        "authorised folder first, in the Knowledge Base settings.")
            return (f"Index: {s['documents']} document(s) — {s['files']} file(s) and "
                    f"{s['notes']} note(s). Search mode: {s['embeddings']}"
                    + (f". Vectors: {s['embedded']}." if s["embeddings"] == "on" else "."))

        if action in ("reindex", "rebuild"):
            n = rebuild()
            if not n:
                return ("I rebuilt the index and found nothing to put in it, sir — the "
                        "Knowledge Base and the notes file are both empty.")
            return f"Index rebuilt from disk, sir: {n} document(s)."

        if action == "clear":
            try:
                from core import confirm
                answer = confirm.request(key="semantic_clear", title="Empty the search index?",
                                         detail="Your files and notes are not touched.",
                                         run=lambda: f"Cleared {clear()} document(s).")
                if "[CONFIRMATION_PENDING]" in answer:
                    return answer
                # The callback erases the INDEX only — never a file, never a note —
                # and confirm.py's own refusal already says nothing was done.
                return f"{answer} Nothing was cleared."
            except Exception:
                return ("I could not put a confirmation on screen, sir, so I have not "
                        "emptied the index. Nothing was deleted.")

        return (f"Sir, '{action}' is not a search action I have. I can search, report stats, "
                f"rebuild, or clear the index.")
    except Exception as e:                                  # noqa: BLE001
        return f"Sir, the search hit a problem: {str(e)[:150]}"


# ── self-test: python semantic_search.py ─────────────────────────────────────

def _selftest() -> int:
    print("semantic_search — self-test")
    problems = 0

    print(f"  folding (Arabic)    : أحمَـد→{_norm('أحمَـد')} == احمد → "
          f"{_norm('أحمَـد') == _norm('احمد')}")
    problems += 0 if _norm("أحمَـد") == _norm("احمد") else 1
    print(f"  folding (Latin)     : AHMED→{_norm('AHMED')} == ahmed → "
          f"{_norm('AHMED') == _norm('ahmed')}")
    problems += 0 if _norm("AHMED") == _norm("ahmed") else 1
    print(f"  stop words dropped  : {_tokens('the car is in the garage')}")
    problems += 0 if "the" not in _tokens("the car") else 1

    backup = INDEX_PATH.read_text(encoding="utf-8") if INDEX_PATH.exists() else None
    docs = [
        {"id": "1", "source": "/x/cars.txt", "title": "cars.txt",
         "text": "The engine oil needs changing every ten thousand kilometres."},
        {"id": "2", "source": "/x/recipes.txt", "title": "recipes.txt",
         "text": "Bake the bread for forty minutes at two hundred degrees."},
        {"id": "3", "source": "note:n1", "title": "مشتريات", "kind": "note",
         "text": "أشتري زيت للسيارة من البنزينة"},
    ]
    clear()
    added = index_docs(docs)
    print(f"  indexed             : {added} document(s)")
    problems += 0 if added == 3 else 1

    # With embeddings OFF, a paraphrase MUST fail — and saying so is the honest
    # test. Keyword search finds words; meaning search needs the embeddings.
    for q, expect, label in (("engine oil change", "cars.txt", "exact words"),
                             ("car maintenance", None, "paraphrase (words off: no hit)"),
                             ("زيت العربية", "مشتريات", "Arabic"),
                             ("quantum tunnelling in bananas", None, "irrelevant")):
        hits = search(q, limit=3)
        top = hits[0]["title"] if hits else None
        mark = "✓" if top == expect else ("✓" if expect is None and not hits else "✗")
        problems += 0 if mark == "✓" else 1
        print(f"  {label:<19} : '{q}' → {top or 'nothing relevant'}  {mark}")

    print(f"  never fabricates    : irrelevant query returns {len(search('xylophone submarine'))} hit(s)"
          f" · paraphrase with words only: {len(search('car maintenance'))} hit(s)")
    problems += 0 if not search("xylophone submarine") and not search("car maintenance") else 1

    # اسم الملف داخل الترتيب: ملف بالعربية يجب أن يُوجد باسمه الإنجليزي
    index_docs([{"id": "9", "source": "/x/plant.md", "title": "plant.md",
                 "text": "افتُتحت محطة المياه في المنصورة يوم 14 مارس."}])
    by_name = search("plant", limit=2)
    print(f"  name beats language : 'plant' → {len(by_name)} hit(s) → "
          f"{by_name[0]['title'] if by_name else '—'}")
    problems += 0 if by_name else 1

    scope = search("زيت", scope="notes")
    print(f"  scope filter        : 'زيت' within notes → {len(scope)} hit(s), "
          f"all notes: {all(h['kind'] == 'note' for h in scope)}  "
          f"(files excluded: {not any(h['kind'] != 'note' for h in scope)})")
    problems += 0 if all(h["kind"] == "note" for h in scope) else 1

    removed = remove_source("/x/recipes.txt")
    # four documents exist at this point: the three above plus the Arabic one
    # added for the filename test.
    print(f"  remove source       : {removed} removed → {stats()['documents']} left")
    problems += 0 if removed == 1 and stats()["documents"] == 3 else 1

    print(f"  embeddings          : {stats()['embeddings']} (off by default, as it must be)")
    problems += 0 if stats()["embeddings"] == "off" else 1
    print(f"  status line         : {run({'action': 'status'})[:88]}")
    print(f"  bad action          : {run({'action': 'zzz'})[:88]}")
    problems += 0 if "not a search action" in run({"action": "zzz"}) else 1
    print(f"  empty query         : {run({'action': 'search', 'query': ''})}")
    problems += 0 if "What should I look for" in run({"action": "search"}) else 1

    # restore whatever was there before the test
    try:
        if backup is None:
            INDEX_PATH.unlink(missing_ok=True)
        else:
            INDEX_PATH.write_text(backup, encoding="utf-8")
        global _CACHE
        _CACHE = None
        print("  index restored      : ok")
    except Exception as e:
        print(f"  restore failed      : {e}")
        problems += 1
    print("  RESULT:", "PASS" if problems == 0 else f"{problems} problem(s)")
    return problems


if __name__ == "__main__":
    raise SystemExit(_selftest())
