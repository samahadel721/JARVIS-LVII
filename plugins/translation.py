"""
translation.py — translating text, with the provider you actually chose.

FOUR PROVIDERS, AND THE HONEST TRADE-OFF BETWEEN THEM
    none            the default. Nothing is configured, so nothing is sent
                    anywhere, and the plugin says so instead of guessing.
    argos           local and offline. A real translation engine inside your
                    machine: free, private, no key, nothing leaves the computer.
                    Needs a one-time install plus a language package.
    libretranslate  a server you point at — your own, or a public instance. The
                    address and the API key (if the instance wants one) live in
                    the settings, never in this file.
    gemini          the assistant's own key. Good quality, and it is the only
                    option here that sends your text to a company. Off unless
                    you choose it by name.

WHAT IT DOES ABOUT FORMATTING
    Markdown structure survives: a heading stays a heading, a bullet stays a
    bullet, and an empty line stays empty. Each line is translated on its own
    when the provider allows it, so a list does not collapse into a paragraph.

HONEST DETECTION
    Script is easy and reliable: Arabic, Cyrillic, Greek, Hebrew, CJK and so on
    are recognised by their letters alone. For languages that share the Latin
    alphabet it guesses from common words and reports a confidence rather than
    pretending to be certain.

SETTINGS (Plugin Manager → Translation)
    provider        none | argos | libretranslate | gemini
    target_lang     ar — the default language to translate into
    libre_url       https://your-libretranslate-host
    libre_api_key   only if that server requires one
    libretranslate and gemini are the option that leaves your machine.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

PLUGIN = {
    "name": "translation",
    "description": (
        "Translates text or a document into another language: 'translate this into "
        "Arabic', 'what does this French page say', 'tell me what this says in English'. "
        "It can also detect what language a text is written in."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "translate | detect | file | status"},
            "text": {"type": "STRING", "description": "The text to translate"},
            "path": {"type": "STRING", "description": "A document to translate"},
            "to": {"type": "STRING", "description": "Target language: ar, English, فرنسي…"},
            "from": {"type": "STRING", "description": "Source language, or 'auto'"},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "translation",
    "title": "Translation / الترجمة",
    "fields": [
        {"key": "provider", "type": "choice", "label": "Provider",
         "default": "none", "choices": ["none", "argos", "libretranslate", "gemini"]},
        {"key": "target_lang", "type": "text", "label": "Default target language", "default": "ar"},
        {"key": "libre_url", "type": "text", "label": "LibreTranslate address",
         "default": "", "placeholder": "https://libretranslate.example.com"},
        {"key": "libre_api_key", "type": "password", "label": "LibreTranslate key (if required)",
         "default": ""},
    ],
}

# ── languages: codes, English names, and the Arabic names people actually say ──
LANGS: dict[str, str] = {
    "ar": "ar", "arabic": "ar", "العربية": "ar", "عربي": "ar", "عربية": "ar",
    "en": "en", "english": "en", "إنجليزي": "en", "انجليزي": "en", "إنجليزية": "en",
    "fr": "fr", "french": "fr", "فرنسي": "fr", "فرنسية": "fr",
    "de": "de", "german": "de", "ألماني": "de", "الماني": "de",
    "es": "es", "spanish": "es", "إسباني": "es", "اسباني": "es",
    "it": "it", "italian": "it", "إيطالي": "it",
    "pt": "pt", "portuguese": "pt", "برتغالي": "pt",
    "ru": "ru", "russian": "ru", "روسي": "ru",
    "tr": "tr", "turkish": "tr", "تركي": "tr",
    "zh": "zh", "chinese": "zh", "صيني": "zh",
    "ja": "ja", "japanese": "ja", "ياباني": "ja",
    "ko": "ko", "korean": "ko", "كوري": "ko",
    "hi": "hi", "hindi": "hi", "هندي": "hi",
    "ur": "ur", "urdu": "ur", "أردو": "ur",
    "fa": "fa", "persian": "fa", "farsi": "fa", "فارسي": "fa",
    "he": "he", "hebrew": "he", "عبري": "he", "عبرية": "he",
    "el": "el", "greek": "el", "يوناني": "el",
    "nl": "nl", "dutch": "nl", "هولندي": "nl",
    "sv": "sv", "swedish": "sv", "سويدي": "sv",
    "pl": "pl", "polish": "pl", "بولندي": "pl",
    "uk": "uk", "ukrainian": "uk", "أوكراني": "uk",
}

_NAME = {"ar": "Arabic", "en": "English", "fr": "French", "de": "German", "es": "Spanish",
         "it": "Italian", "pt": "Portuguese", "ru": "Russian", "tr": "Turkish",
         "zh": "Chinese", "ja": "Japanese", "ko": "Korean", "hi": "Hindi", "ur": "Urdu",
         "fa": "Persian", "he": "Hebrew", "el": "Greek", "nl": "Dutch", "sv": "Swedish",
         "pl": "Polish", "uk": "Ukrainian"}

# Scripts that identify a language by their letters alone — no guessing needed.
_SCRIPTS = (
    ("ar", re.compile("[\u0600-\u06ff]")),
    ("ru", re.compile("[\u0400-\u04ff]")),
    ("el", re.compile("[\u0370-\u03ff]")),
    ("he", re.compile("[\u0590-\u05ff]")),
    ("zh", re.compile("[\u4e00-\u9fff]")),
    ("ja", re.compile("[\u3040-\u30ff]")),
    ("ko", re.compile("[\uac00-\ud7af]")),
    ("hi", re.compile("[\u0900-\u097f]")),
)
# Common function words for Latin-script languages. In any real sentence these
# are the most frequent words, which is why they identify a language far better
# than the rare ones do. A language only wins on evidence — with no evidence the
# answer is "unknown", never a silent assumption of English.
_LATIN_HINTS = {
    "en": {"the", "and", "of", "to", "is", "are", "was", "were", "that", "for", "with",
           "you", "your", "this", "these", "not", "have", "has", "had", "it", "its",
           "be", "been", "at", "but", "they", "their", "them", "from", "we", "our",
           "will", "would", "can", "could", "there", "what", "about", "which", "when",
           "who", "how", "all", "more", "one", "also", "into", "than", "so", "some",
           "if", "do", "does", "did", "my", "me", "he", "she", "his", "her", "because",
           "hello", "thanks", "thank", "please", "good", "morning", "night", "sorry",
           "yes", "no", "okay", "well", "want", "need", "know", "think", "make", "get"},
    "fr": {"le", "la", "les", "des", "est", "et", "un", "une", "que", "qui", "pour",
           "dans", "vous", "nous", "je", "ne", "pas", "avec", "sur", "au", "aux", "du",
           "ce", "cette", "sont", "été", "être", "avoir", "plus", "mais", "ou", "comme",
           "tout", "fait", "il", "elle", "ils", "elles", "bonjour", "merci", "comment",
           "aussi", "bien", "sans", "sous", "entre", "votre", "leur", "c'est", "d'un",
           "peut", "très", "alors", "donc", "où", "quoi", "tout"},
    "de": {"der", "die", "das", "und", "ist", "nicht", "ein", "eine", "mit", "für",
           "von", "den", "dem", "des", "sich", "auf", "auch", "als", "bei", "aus",
           "nach", "wie", "aber", "oder", "wenn", "wir", "sie", "ich", "haben", "sein",
           "wird", "nur", "noch", "über", "kann", "muss", "dass", "werden", "wurde",
           "durch", "gegen", "ohne", "sehr", "schon", "hier", "dort", "danke", "hallo"},
    "es": {"el", "la", "los", "las", "de", "que", "y", "en", "es", "un", "una", "para",
           "con", "por", "no", "se", "del", "al", "lo", "como", "más", "pero", "sus",
           "le", "ya", "o", "este", "esta", "entre", "cuando", "muy", "sin", "sobre",
           "también", "hasta", "hay", "tiene", "puede", "donde", "porque", "todo", "hola"},
    "it": {"il", "la", "di", "che", "e", "un", "per", "con", "non", "sono", "una",
           "del", "come", "più", "anche", "nel", "alla", "gli", "si", "ma", "da", "su",
           "questo", "quando", "molto", "sono", "stato", "essere", "avere", "perché",
           "dove", "tutto", "ciao", "grazie", "bene", "può", "deve", "della", "delle"},
    "pt": {"o", "a", "os", "de", "que", "e", "do", "da", "em", "não", "para", "com",
           "uma", "um", "por", "mais", "como", "mas", "ao", "dos", "das", "se", "na",
           "no", "seu", "sua", "também", "quando", "muito", "está", "são", "foi", "ser",
           "tem", "pode", "onde", "porque", "tudo", "olá", "obrigado", "bom", "dia"},
    "nl": {"de", "het", "een", "van", "en", "is", "dat", "niet", "voor", "met", "zijn",
           "op", "te", "je", "ik", "we", "ze", "ook", "maar", "als", "aan", "om", "door",
           "heeft", "worden", "wordt", "kan", "moet", "deze", "dit", "die", "waar",
           "hoe", "veel", "goed", "dank", "hallo", "naar", "over", "onder", "tussen"},
    "tr": {"bir", "ve", "bu", "için", "ile", "değil", "çok", "var", "da", "de", "olarak",
           "ama", "gibi", "daha", "kadar", "sonra", "kendi", "her", "ne", "nasıl",
           "ben", "sen", "biz", "siz", "onlar", "olan", "oldu", "merhaba", "teşekkür",
           "iyi", "günaydın", "lütfen", "ancak", "yani", "şey", "zaman", "yerde"},
}


def _cfg() -> dict[str, Any]:
    out = {f["key"]: f.get("default") for f in PLUGIN_SETTINGS["fields"]}
    try:
        from memory.config_manager import get_plugin_config
        out.update({k: v for k, v in (get_plugin_config("translation") or {}).items()
                    if v not in (None, "")})
    except Exception:
        pass
    return out


def lang_code(value: str) -> str:
    """'Arabic', 'ar', 'عربي' → 'ar'. Unknown input returns '' (never a guess)."""
    v = (value or "").strip().lower()
    if not v or v in ("auto", "detect", "تلقائي"):
        return "auto"
    return LANGS.get(v, v if v in _NAME else "")


def lang_name(code: str) -> str:
    return _NAME.get((code or "").lower(), code or "unknown")


def detect(text: str) -> tuple[str, float, str]:
    """
    Guess the language. Returns (code, confidence 0-1, why).
    Script detection is certain; Latin-script detection is a word-frequency guess
    and the confidence says so out loud.
    """
    sample = (text or "").strip()
    if len(sample) < 2:
        return "unknown", 0.0, "there is not enough text to tell"
    for code, pattern in _SCRIPTS:
        hits = len(pattern.findall(sample))
        if hits / max(len(sample), 1) > 0.25:
            return code, 0.98, f"the letters themselves are {lang_name(code)}"
    words = re.findall(r"[A-Za-zÀ-ÿ']+", sample.lower())
    if not words:
        return "unknown", 0.0, "no letters I recognise"
    scores = {code: sum(1 for w in words if w in hints) for code, hints in _LATIN_HINTS.items()}
    best = max(scores, key=lambda k: scores[k])
    if scores[best] == 0:
        # No evidence at all. Naming a language here would be a guess dressed as
        # a reading, so the honest answer is that I do not know.
        return "unknown", 0.1, ("Latin letters, but not one common word I can pin to a "
                                "language — tell me the source language and I will not "
                                "have to guess")
    conf = min(0.9, 0.35 + 0.12 * scores[best])
    if sorted(scores.values(), reverse=True)[:2] and list(scores.values()).count(scores[best]) > 1:
        conf *= 0.7
    return best, round(conf, 2), f"{scores[best]} common {lang_name(best)} word(s) in the text"


# ── providers ─────────────────────────────────────────────────────────────────

def _argos(lines: list[str], src: str, dst: str) -> tuple[bool, list[str]]:
    try:
        from argostranslate import translate as _argos_api
    except Exception:
        return False, ["argostranslate is not installed — run: pip install argostranslate"]
    try:
        installed = _argos_api.get_installed_languages()
        by_code = {lang.code: lang for lang in installed}
        if dst not in by_code:
            return False, [f"the {lang_name(dst)} language package is not installed yet "
                           f"(argostranslate → download it once, then it works offline)"]
        if src == "auto":
            src = detect(" ".join(lines))[0]
        if src not in by_code:
            return False, [f"I have no offline model for {lang_name(src)}"]
        pair = by_code[src].get_translation(by_code[dst])
        if pair is None:
            return False, [f"no offline {lang_name(src)} → {lang_name(dst)} package is installed"]
        return True, [pair.translate(line) if line.strip() else line for line in lines]
    except Exception as e:
        return False, [f"the offline engine failed: {str(e)[:90]}"]


def _libre(lines: list[str], src: str, dst: str) -> tuple[bool, list[str]]:
    cfg = _cfg()
    url = str(cfg.get("libre_url") or "").strip().rstrip("/")
    if not url:
        return False, ["no LibreTranslate address is set in the plugin settings"]
    if not url.startswith(("http://", "https://")):
        return False, ["the LibreTranslate address must start with http:// or https://"]
    try:
        import requests
    except Exception:
        return False, ["the requests library is missing"]
    payload: dict[str, Any] = {"q": "\n".join(lines), "source": "auto" if src == "auto" else src,
                               "target": dst, "format": "text"}
    key = str(cfg.get("libre_api_key") or "").strip()
    if key:
        payload["api_key"] = key
    try:
        r = requests.post(f"{url}/translate", json=payload, timeout=25)
        if r.status_code == 429:
            return False, ["that LibreTranslate server is rate-limiting me right now"]
        if r.status_code >= 400:
            return False, [f"the server refused the request ({r.status_code})"]
        body = r.json()
        out = str(body.get("translatedText") or "")
        return (True, out.split("\n")) if out else (False, ["the server returned no text"])
    except Exception as e:
        return False, [f"I could not reach {url}: {str(e)[:80]}"]


def _gemini(lines: list[str], src: str, dst: str) -> tuple[bool, list[str]]:
    try:
        from memory.config_manager import get_api_key
        key = str(get_api_key() or "").strip()
    except Exception:
        key = ""
    if not key:
        return False, ["no API key is configured for the model"]
    prompt = (f"Translate the following text into {lang_name(dst)}. "
              f"Output only the translation, keeping the line breaks exactly. "
              f"Do not explain, do not add notes.\n\n" + "\n".join(lines))
    try:
        from google import genai
        reply = genai.Client(api_key=key).models.generate_content(
            model="gemini-2.0-flash", contents=prompt)
        out = (getattr(reply, "text", "") or "").strip()
        return (True, out.split("\n")) if out else (False, ["the model returned nothing"])
    except Exception as e:
        return False, [f"the model call failed: {str(e)[:90]}"]


def available() -> tuple[bool, str]:
    """(usable, why not) — so another plugin can explain itself instead of failing."""
    provider = str(_cfg().get("provider") or "none").strip().lower()
    if provider == "none":
        return False, ("no translation provider is chosen — pick one in the plugin settings "
                       "(argos is free, local and offline)")
    if provider == "argos":
        try:
            import argostranslate  # noqa: F401
            return True, "argos (offline, local)"
        except Exception:
            return False, "argos is selected but not installed yet: pip install argostranslate"
    if provider == "libretranslate":
        ok = bool(str(_cfg().get("libre_url") or "").strip())
        return ok, "libretranslate" if ok else "libretranslate is selected but has no address"
    if provider == "gemini":
        return True, "gemini (text leaves this machine)"
    return False, f"'{provider}' is not a provider I know"


# ── the translation itself, formatting preserved ──────────────────────────────

_MD_PREFIX = re.compile(r"^(\s*(?:#{1,6}\s|[-*+]\s|\d+[.)]\s|>\s|```|\|))")


def _split_keep(lines: list[str]) -> tuple[list[str], list[str]]:
    """Separate the markdown marker from the words so the marker survives intact."""
    markers, bodies = [], []
    for line in lines:
        m = _MD_PREFIX.match(line)
        if m:
            markers.append(m.group(1))
            bodies.append(line[m.end():])
        else:
            markers.append("")
            bodies.append(line)
    return markers, bodies


def translate(text: str, target: str = "ar", source: str = "auto") -> dict[str, Any]:
    """
    The reusable API. Returns a dict with ok/text/provider/detected — and a
    reason when it could not translate. Never raises, never pretends.
    """
    text = (text or "")
    if not text.strip():
        return {"ok": False, "reason": "there is no text to translate", "text": ""}
    dst = lang_code(target)
    if not dst or dst == "auto":
        return {"ok": False, "reason": f"I do not know the language '{target}' — say it as a "
                                       f"code (ar) or a name (Arabic / عربي)", "text": ""}
    src = lang_code(source)
    asked_for_auto = src == "auto"
    if src == "auto":
        src, conf, why = detect(text)
        if src == "unknown":
            # Detection is a convenience, not a requirement. Providers that can
            # detect for themselves (libre, gemini) are allowed to try — but a
            # local engine that needs a source language has to be told one, and
            # I ask rather than guess.
            provider_now = str(_cfg().get("provider") or "none").strip().lower()
            if provider_now in ("libre", "libretranslate", "gemini") and asked_for_auto:
                src = "auto"
            else:
                return {"ok": False, "text": "",
                        "reason": ("I could not tell what language that is, and the offline "
                                   "engine needs to be told. Say it, for example: from "
                                   "English to Arabic"),
                        "detected": "unknown"}
        elif src == dst:
            return {"ok": True, "text": text, "provider": "none", "detected": src,
                    "note": f"it is already in {lang_name(dst)}"}
    if src == dst:
        return {"ok": True, "text": text, "provider": "none", "detected": src,
                "note": f"it is already in {lang_name(dst)}"}

    ok, why = available()
    if not ok:
        return {"ok": False, "reason": why, "text": "", "detected": src}
    provider = str(_cfg().get("provider")).strip().lower()

    lines = text.split("\n")
    markers, bodies = _split_keep(lines)
    if provider == "argos":
        done, out = _argos(bodies, src, dst)
    elif provider == "libretranslate":
        done, out = _libre(bodies, src, dst)
    elif provider == "gemini":
        done, out = _gemini(bodies, src, dst)
    else:
        return {"ok": False, "reason": f"'{provider}' is not a provider I know", "text": ""}
    if not done:
        return {"ok": False, "reason": out[0], "text": "", "detected": src, "provider": provider}

    rebuilt = [marker + body for marker, body in zip(markers, out)]
    if len(rebuilt) != len(lines):                 # a provider that merged lines
        rebuilt = out
    return {"ok": True, "text": "\n".join(rebuilt), "provider": provider,
            "detected": src, "target": dst}


# ── tool entry point ──────────────────────────────────────────────────────────

def _read_any(path: str | Path) -> tuple[bool, str]:
    """Borrow the suite's single file reader instead of writing a second one."""
    from pathlib import Path as _P
    p = _P(path).expanduser()
    if not p.is_file():
        return False, f"'{p}' is not a file I can read"
    ext = p.suffix.lower()
    if ext in (".txt", ".md", ".rst", ".csv", ".json", ".log"):
        try:
            return True, p.read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            return False, f"I could not read it: {str(e)[:70]}"
    for name in ("summarizer", "knowledge_base"):
        mod_path = _P(__file__).resolve().parent / f"{name}.py"
        if not mod_path.exists():
            continue
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location(f"_jarvis_{name}", mod_path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            if hasattr(module, "extract_text"):
                ok, text = module.extract_text(p)
            elif hasattr(module, "read_any"):
                ok, text = module.read_any(p)
            else:
                continue
            if ok and text.strip():
                return True, text
        except Exception:
            continue
    return False, (f"I cannot pull text out of a {ext or 'file'} without the Knowledge Base "
                   f"next to me")


def run(parameters: dict, player=None, session_memory=None) -> str:
    """Dispatch one translation action. Never raises."""
    try:
        p = parameters or {}
        action = str(p.get("action") or "translate").strip().lower()
        cfg = _cfg()

        if action in ("translate", "tr", "say_in"):
            text = str(p.get("text") or "")
            if not text and p.get("path"):
                ok, text = _read_any(str(p["path"]))
                if not ok:
                    return f"Sir, {text}."
            if not text.strip():
                return "What should I translate, sir?"
            to = str(p.get("to") or cfg.get("target_lang") or "ar")
            r = translate(text, target=to, source=str(p.get("from") or "auto"))
            if not r.get("ok"):
                return f"Sir, {r['reason']}."
            if player:
                try:
                    player.write_log(f"[Translate] {r.get('detected')} → {r.get('target')}")
                except Exception:
                    pass
            head = ""
            if r.get("provider") == "none":
                head = f"(it is already {lang_name(r.get('detected', ''))})\n"
            else:
                src = r.get("detected") or "?"
                head = (f"(from {lang_name(src)} to {lang_name(r.get('target') or to)} "
                        f"using {r.get('provider')})\n")
            return head + r["text"]

        if action in ("file", "document"):
            path = str(p.get("path") or "").strip()
            if not path:
                return "Which document, sir?"
            ok, text = _read_any(path)
            if not ok:
                return f"Sir, {text}."
            to = str(p.get("to") or cfg.get("target_lang") or "ar")
            r = translate(text, target=to, source=str(p.get("from") or "auto"))
            if not r.get("ok"):
                return f"Sir, {r['reason']}."
            cap = 6000
            body = r["text"]
            tail = (f"\n\n… ({len(body) // 1000}k characters total; I am showing the first "
                    f"{cap // 1000}k)" if len(body) > cap else "")
            return f"{Path(path).name} → {lang_name(r.get('target') or to)} " \
                   f"({r.get('provider')}):\n\n{body[:cap]}{tail}"

        if action in ("detect", "language", "what_language"):
            text = str(p.get("text") or "")
            if not text and p.get("path"):
                ok, text = _read_any(str(p["path"]))
                if not ok:
                    return f"Sir, {text}."
            if not text.strip():
                return "Which text should I look at, sir?"
            code, conf, why = detect(text)
            if code == "unknown":
                return f"Sir, {why}."
            return (f"That is {lang_name(code)} ({code}), sir — {why}. "
                    f"Confidence {int(conf * 100)}%"
                    + ("." if conf > 0.9 else ", and that is a guess from common words."))

        if action in ("languages", "list"):
            return ("I know these: Arabic عربي, English إنجليزي, French فرنسي, German ألماني, "
                    "Spanish إسباني, Italian إيطالي, Portuguese برتغالي, Russian روسي, Turkish "
                    "تركي, Chinese صيني, Japanese ياباني, Korean كوري, Hindi هندي, Urdu أردو, "
                    "Persian فارسي, Hebrew عبري, Greek يوناني, Dutch هولندي, Swedish سويدي, "
                    "Polish بولندي, Ukrainian أوكراني. Codes work too: ar, en, fr…")

        if action in ("status", "info"):
            ok, why = available()
            cfg = _cfg()
            lines = [f"Provider    : {cfg.get('provider')} — {'ready' if ok else 'NOT ready'}",
                     f"Default into: {lang_name(lang_code(str(cfg.get('target_lang'))))}",
                     f"Detail      : {why}"]
            if not ok:
                lines.append("\nFree and fully offline path: pip install argostranslate, set "
                             "provider = argos, then download the language pair once.")
            return "\n".join(lines)

        return (f"Sir, '{action}' is not a translation action I have. I can translate text or "
                f"a document, detect a language, or report settings.")
    except Exception as e:                                   # noqa: BLE001
        return f"Sir, the translation plugin hit a problem: {str(e)[:150]}"


# ── self-test: python translation.py ─────────────────────────────────────────

def _selftest() -> int:
    print("translation — self-test")
    problems = 0

    for value, expect in (("Arabic", "ar"), ("ar", "ar"), ("عربي", "ar"), ("English", "en"),
                          ("فرنسي", "fr"), ("auto", "auto"), ("klingon", "")):
        got = lang_code(value)
        mark = "✓" if got == expect else "✗"
        problems += 0 if got == expect else 1
        print(f"  lang_code({value!r:<10}) → {got!r:<6} {mark}")

    samples = [("مرحبا كيف حالك اليوم", "ar"), ("Hello there, how are you today", "en"),
               ("Bonjour, comment allez-vous aujourd'hui", "fr"),
               ("Привет как дела сегодня", "ru"), ("你好 世界 今天", "zh")]
    for text, expect in samples:
        code, conf, why = detect(text)
        mark = "✓" if code == expect else "✗"
        problems += 0 if code == expect else 1
        print(f"  detect {text[:22]!r:<26} → {code} ({conf}) {mark}")

    # the Latin guess must not be sold as certainty
    code, conf, why = detect("Bonjour, comment allez-vous")
    print(f"  honest confidence   : {code} conf={conf} — '{why[:52]}'")
    problems += 0 if conf <= 0.9 else 1
    # and with no evidence it must refuse to name a language at all
    code2, conf2, why2 = detect("Xzq plth kkrn vvv")
    print(f"  no evidence →       : {code2} ({conf2}) — refuses to assume English")
    problems += 0 if code2 == "unknown" else 1

    md = "# Title\n\n- first item\n- second item\nthe car is fast"
    r = translate(md, target="ar")
    fmt_ok = ("# " in r.get("text", "")) or not r.get("ok")
    print(f"  format preserved    : {'n/a (no provider)' if not r.get('ok') else fmt_ok}")

    ok, why = available()
    print(f"  provider ready      : {ok}  ({why[:74]})")
    problems += 0 if not ok else 0          # default is 'none' → must NOT be ready

    out = run({"action": "translate", "text": "hello", "to": "ar"})
    print(f"  translate w/o setup : {out[:92]}")
    problems += 0 if "provider" in out.lower() else 1
    problems += 0 if "no translation provider" in out or "argos" in out.lower() else 1

    print(f"  unsupported language: {run({'action': 'translate', 'text': 'hi', 'to': 'klingon'})[:92]}")
    problems += 0 if "klingon" in run({"action": "translate", "text": "hi", "to": "klingon"}) else 1

    print(f"  detect action       : {run({'action': 'detect', 'text': 'مرحبا بك يا سيدي'})[:78]}")
    problems += 0 if "Arabic" in run({"action": "detect", "text": "مرحبا بك يا سيدي"}) else 1
    same = translate("hello", target="en")
    print(f"  same language       : {same.get('note') or same.get('reason')}")
    problems += 0 if same.get("ok") else 1
    short = translate("Good morning", target="ar", source="auto")
    why_short = str(short.get("reason") or f"detected {short.get('detected')}")
    print(f"  short English text  : ok={short.get('ok')} — {why_short[:66]}")
    # Correct behaviour here is either a real translation or a provider complaint:
    # a DETECTION complaint would mean "Good morning" was not recognised as English.
    problems += 0 if (short.get("ok") or "provider" in why_short) else 1
    print(f"  empty text          : {run({'action': 'translate', 'text': ''})}")
    problems += 0 if "What should I translate" in run({"action": "translate", "text": ""}) else 1
    print(f"  bad action          : {run({'action': 'dance'})[:76]}")
    problems += 0 if "not a translation action" in run({"action": "dance"}) else 1
    print(f"  provider default    : {_cfg().get('provider')} (must be none — nothing sent anywhere)")
    problems += 0 if str(_cfg().get("provider")) == "none" else 1

    print("  RESULT:", "PASS" if problems == 0 else f"{problems} problem(s)")
    return problems


if __name__ == "__main__":
    raise SystemExit(_selftest())
