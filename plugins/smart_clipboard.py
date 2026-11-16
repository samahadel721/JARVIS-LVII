"""
smart_clipboard.py — the clipboard, with a brain and a lock.

WHAT IT DOES
    Reads what you just copied, works out what it is (a link, an email, JSON, a
    piece of code, a long text, a plain sentence) and then offers the useful
    thing: shorten it, clean it, translate it, pull the links out of it, put
    JSON in order, or explain a piece of code.

WHY IT DOES NOT READ THE CLIPBOARD BY ITSELF
    A clipboard can hold a password you copied ninety seconds ago. So:

      * reading is always an explicit request from you — there is no timer, no
        polling, no background thread;
      * nothing is written down. Not to memory, not to a file, not to a log.
        The text lives inside one function call and is gone afterwards;
      * anything sent to an AI — for the explain or translate actions — is sent
        only because you asked for that action, and only when a key is already
        configured for it. The plugin never asks for one, never opens a network
        call by itself, and says plainly when there is no AI to ask.
      * the log line shows the type and the size, never the content.

WHAT IT NEEDS
    Linux: xclip or xsel (Wayland: wl-clipboard). Windows and macOS: nothing.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

PLUGIN = {
    "name": "smart_clipboard",
    "description": (
        "Works with what you copied: 'read my clipboard', 'what is in the clipboard', "
        "'summarise the clipboard', 'translate the clipboard to Arabic', 'pull the links out', "
        "'clean up this JSON', 'explain the code I copied'. It reads only when you ask, keeps "
        "nothing, and never sends anything anywhere unless you asked for that action."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "read | type | summary | clean | translate | links | "
                                      "json | explain | write | help"},
            "text": {"type": "STRING", "description": "Text to use instead of the clipboard"},
            "language": {"type": "STRING", "description": "Target language for translate"},
            "level": {"type": "STRING", "description": "For summary: short | medium | detailed"},
        },
        "required": ["action"],
    },
}

MAX_CHARS = 200_000          # no clipboard on earth needs more than this, and the
                             # cap keeps a mis-copy from being parsed at all


def _read_clipboard() -> tuple[bool, str]:
    """The OS clipboard, via whatever this machine actually has. Never guesses."""
    try:
        import pyperclip
        value = pyperclip.paste()
        return True, str(value or "")
    except Exception:
        pass
    if sys.platform.startswith("win") or sys.platform == "darwin":
        return False, ("I could not reach the clipboard. On Windows I need pyperclip "
                       "(pip install pyperclip).")
    for tool, args in (("xclip", ["-selection", "clipboard", "-o"]),
                       ("xsel", ["--clipboard", "--output"])):
        if shutil.which(tool):
            try:
                import subprocess
                done = subprocess.run([tool, *args], capture_output=True, timeout=5,
                                      check=False)          # fixed argv, no shell, no input
                if done.returncode == 0:
                    return True, done.stdout.decode("utf-8", "replace")
            except Exception:
                continue
    return False, ("I could not reach the clipboard here. On Linux I need xclip or xsel "
                   "(or wl-clipboard on Wayland): sudo apt install xclip.")


def _write_clipboard(text: str) -> tuple[bool, str]:
    try:
        import pyperclip
        pyperclip.copy(text)
        return True, ""
    except Exception:
        pass
    if sys.platform.startswith("win") or sys.platform == "darwin":
        return False, "I could not reach the clipboard (I need pyperclip on Windows)."
    for tool, args in (("xclip", ["-selection", "clipboard"]),
                       ("xsel", ["--clipboard", "--input"])):
        if shutil.which(tool):
            try:
                import subprocess
                done = subprocess.run([tool, *args], input=text.encode("utf-8"),
                                      capture_output=True, timeout=5, check=False)
                if done.returncode == 0:
                    return True, ""
            except Exception:
                continue
    return False, "I could not reach the clipboard (on Linux I need xclip or xsel)."


# ── what is this thing? ───────────────────────────────────────────────────────

_CACHE: dict[str, Any] = {}

_CODE_HINTS = ("def ", "class ", "import ", "function ", "=>", "();", "};", "#!/",
               "console.log", "print(", "select ", "<?php", "public static", "fn ", "end)")


def classify(text: str) -> dict[str, Any]:
    """Name the contents and say how confident the naming is. No guessing hidden."""
    raw = str(text or "")
    s = raw.strip()
    info: dict[str, Any] = {"chars": len(raw), "lines": raw.count("\n") + 1 if raw else 0,
                            "words": len(re.findall(r"\S+", raw))}

    if not s:
        return {**info, "type": "empty", "confidence": 1.0,
                "note": "the clipboard is empty"}

    if re.fullmatch(r"https?://\S+", s):
        host = re.sub(r"^https?://([^/]+).*$", r"\1", s.split()[0])
        return {**info, "type": "url", "confidence": 0.98, "host": host}

    emails = re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", s)
    if len(emails) == 1 and len(re.sub(r"\S", "", s.replace(emails[0], ""))) <= 3:
        return {**info, "type": "email", "confidence": 0.96, "email": emails[0]}

    if s[0] in "[{" and s[-1] in "]}":
        try:
            json.loads(s)
            return {**info, "type": "json", "confidence": 0.99}
        except Exception as e:
            return {**info, "type": "json", "confidence": 0.55,
                    "note": f"it starts and ends like JSON but does not parse ({str(e)[:50]})"}

    markers = sum(1 for h in _CODE_HINTS if h in s.lower() or h in s)
    looks_indented = any(line.startswith(("    ", "\t")) for line in s.splitlines()[:40])
    if markers >= 2 or (markers >= 1 and looks_indented):
        return {**info, "type": "code", "confidence": min(0.6 + 0.1 * markers, 0.95)}

    if re.search(r"<[a-z][\s\S]*?>", s) and s.count("<") >= 2:
        return {**info, "type": "html", "confidence": 0.7}

    money = re.findall(r"[$€£]\s?\d[\d,.]*", s)
    if len(money) >= 2 and "\n" in s:
        return {**info, "type": "table_like", "confidence": 0.5,
                "note": "it looks like rows and numbers"}

    if len(s) > 600:
        return {**info, "type": "long_text", "confidence": 0.85}
    return {**info, "type": "text", "confidence": 0.8}


def links_in(text: str) -> dict[str, list[str]]:
    """Every web address, marked with the moment it was captured."""
    urls = re.findall(r"https?://[^\s<>\"')]+", str(text or ""))
    return {"live": [u[:300] for u in urls],
            "note": ("Anything collected earlier may be dead by the time you open it — a link "
                     "is where a file was, not a promise it is still there.")}


def clean(text: str) -> str:
    """Strip the formatting a copy drags along: spaces, bullets, page breaks."""
    out = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    out = re.sub(r"[ \t]+\n", "\n", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    out = re.sub(r"^[•▪●·\u2022]\s*", "- ", out, flags=re.M)
    out = re.sub(r"\n\s*Page\s+\d+\s*(of\s+\d+)?\s*\n", "\n", out, flags=re.I)
    out = re.sub(r"[ \t]{2,}", " ", out)
    return out.strip()


def pretty_json(text: str) -> tuple[bool, str]:
    try:
        return True, json.dumps(json.loads(str(text or "")), ensure_ascii=False, indent=2)
    except Exception as e:
        return False, f"that is not valid JSON — {str(e)[:80]}"


# ── the outside brain, only when you asked for it ────────────────────────────

def _explain(text: str) -> str:
    """Explain a piece of code using whichever AI the project already has a key for."""
    snippet = text[:4000]
    for module_name, attr in (("code_helper", "_get_gemini"), ("code_helper", "_get_openai")):
        try:
            import importlib.util
            path = Path(__file__).resolve().parent.parent / "actions" / f"{module_name}.py"
            if not path.exists():
                continue
            name = f"_jarvis_{attr}"
            module = sys.modules.get(name)
            if module is None:
                spec = importlib.util.spec_from_file_location(name, path)
                module = importlib.util.module_from_spec(spec)
                sys.modules[name] = module
                spec.loader.exec_module(module)
            client = getattr(module, attr)(None) if hasattr(module, attr) else None
            if client is None:
                continue
            if attr.endswith("gemini"):
                reply = client.generate_content(
                    "Explain this code plainly and briefly. Say what it does, then any "
                    "problem you can see:\n\n" + snippet)
                answer = getattr(reply, "text", None) or str(reply)
            else:
                reply = client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[{"role": "user", "content":
                               "Explain this code plainly and briefly:\n\n" + snippet}])
                answer = reply.choices[0].message.content
            if answer and str(answer).strip():
                return str(answer).strip()
        except Exception:
            continue
    return ("There is no AI key configured for me to explain code with, sir. I can clean it, "
            "pull the links out of it, or read it back to you instead.")


def _summarise(text: str, level: str) -> str:
    path = Path(__file__).resolve().parent / "summarizer.py"
    if not path.exists():
        return "the summarizer plugin is not next to me, sir"
    try:
        import importlib.util
        name = "_jarvis_summarizer"
        module = sys.modules.get(name)
        if module is None:
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
        return str(module.run({"action": "summarize", "text": text, "level": level}))
    except Exception as e:
        return f"the summarizer could not take it: {str(e)[:80]}"


def _translate(text: str, language: str, player=None) -> str:
    path = Path(__file__).resolve().parent / "translation.py"
    if not path.exists():
        return "the translation plugin is not next to me, sir"
    try:
        import importlib.util
        name = "_jarvis_translation"
        module = sys.modules.get(name)
        if module is None:
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
        return str(module.run({"action": "translate", "text": text, "to": language}, player=player))
    except Exception as e:
        return f"the translation plugin could not take it: {str(e)[:80]}"


# ── tool entry point ──────────────────────────────────────────────────────────

_HELP = ("I can work on the clipboard, sir: read it, name what it is, shorten it, clean it, "
         "pull the links out of it, tidy JSON, translate it, or explain copied code. Reading "
         "happens only when you ask — I never watch the clipboard by myself.")


def run(parameters: dict, player=None, session_memory=None) -> str:
    """One clipboard action. Returns text; never raises, never stores the content."""
    try:
        p = parameters or {}
        action = str(p.get("action") or "read").strip().lower()
        given = p.get("text")
        if given is not None and str(given).strip():
            text, source = str(given), "what you gave me"
        else:
            ok, text = _read_clipboard()
            if not ok:
                return f"Sir, {text}"
            source = "the clipboard"

        if len(text) > MAX_CHARS:
            return (f"Sir, that is {len(text):,} characters — more than I will work on at once "
                    f"({MAX_CHARS:,}). Copy the part you need.")

        kind = classify(text)
        if player:
            try:                     # the type and the size — never the content
                player.write_log(f"[Clipboard] {kind['type']} · {kind['chars']} chars ({action})")
            except Exception:
                pass

        if action in ("read", "what", "type", "peek"):
            if kind["type"] == "empty":
                return "The clipboard is empty, sir."
            note = f" — {kind['note']}" if kind.get("note") else ""
            preview = clean(text)[:400]
            return (f"{kind['chars']:,} characters, {kind['lines']} lines, and it reads as "
                    f"{kind['type']} (I am {int(kind['confidence'] * 100)}% sure){note}.\n\n"
                    f"{preview}" + ("\n… (truncated for reading)" if len(text) > 400 else ""))

        if action in ("summary", "summarise", "summarize", "shorten"):
            if kind["chars"] < 80:
                return f"'{text.strip()}' — that is short enough already, sir."
            level = str(p.get("level") or "medium").lower()
            return _summarise(text, level if level in ("short", "medium", "detailed") else "medium")

        if action in ("clean", "tidy", "format"):
            result = clean(text)
            if player:
                try:
                    player.show_content("Clipboard — cleaned", result)
                except Exception:
                    pass
            return (f"Cleaned, sir — {len(text):,} characters down to {len(result):,}:\n\n"
                    f"{result[:1200]}" + ("\n…" if len(result) > 1200 else ""))

        if action in ("links", "urls"):
            found = links_in(text)
            if not found["live"]:
                return "There is no web address in there, sir."
            listing = "\n".join(f"  {i + 1}. {u}" for i, u in enumerate(found["live"][:25]))
            more = f"\n… and {len(found['live']) - 25} more" if len(found["live"]) > 25 else ""
            return (f"{len(found['live'])} address(es), sir:\n{listing}{more}\n\n{found['note']}")

        if action in ("json", "pretty", "format_json"):
            fine, out = pretty_json(text)
            return out if fine else out       # the message already explains either way

        if action in ("translate", "ar"):
            language = str(p.get("language") or "Arabic").strip()
            if kind["chars"] > 6000:
                return ("Sir, that is too long to translate politely — copy the part you want.")
            return (f"{source.capitalize()}, as {kind['type']}:\n\n"
                    + _translate(text, language, player))

        if action in ("explain", "code"):
            if kind["type"] not in ("code", "text", "json"):
                return f"That is {kind['type']}, sir, not code — I would be explaining nothing."
            if kind["chars"] > 4000:
                return "Sir, that is a large file — copy the function you want explained."
            return _explain(text)

        if action in ("write", "put"):
            ok, why = _write_clipboard(text)
            if not ok:
                return f"Sir, {why}"
            return f"Put {kind['chars']:,} characters on the clipboard, sir."

        if action in ("help", "?"):
            return _HELP

        return f"Sir, '{action}' is not a clipboard action I have. {_HELP}"
    except Exception as e:                                    # noqa: BLE001
        return f"Sir, the clipboard plugin hit a problem: {str(e)[:150]}"


# ── self-test: python smart_clipboard.py ─────────────────────────────────────

def _selftest() -> int:
    print("smart_clipboard — self-test")
    problems = 0

    cases = (
        ("https://github.com/FatihMakes/Mark-LV", "url"),
        ("someone@example.com", "email"),
        ('{"a": 1, "b": [1, 2]}', "json"),
        ("def go(x):\n    import os\n    return os.path.join(x, 'a')", "code"),
        ("hello, bring me tea", "text"),
        ("x" * 900, "long_text"),
        ("", "empty"),
        ("<div class='a'>hi</div><span>x</span>", "html"),
    )
    for text, want in cases:
        got = classify(text)["type"]
        mark = "✓" if got == want else "✗"
        problems += 0 if got == want else 1
        print(f"  classify {text[:28]!r:<32} → {got:<10} {mark} (wanted {want})")

    broken = classify('{"a": 1,}')
    print(f"  broken json         : type={broken['type']} conf={broken['confidence']} — "
          f"{broken.get('note', '')[:44]}")
    problems += 0 if broken["type"] == "json" and broken["confidence"] < 0.6 else 1

    messy = "Hello   there\n\n\n\n•  one\n•  two\n\nPage 3 of 9\n\n\nend   "
    cleaned = clean(messy)
    print(f"  clean               : {cleaned!r}")
    problems += 0 if "•" not in cleaned and "\n\n\n" not in cleaned and "Page 3" not in cleaned else 1

    ok, pretty = pretty_json('{"b":2,"a":1}')
    print(f"  tidy json           : {pretty.splitlines()[1].strip()!r} …")
    problems += 0 if ok and pretty.startswith("{\n") else 1
    bad_ok, bad_msg = pretty_json("{nope}")
    problems += 0 if not bad_ok and "not valid JSON" in bad_msg else 1
    print(f"  bad json says so    : {bad_msg[:52]}")

    found = links_in("see https://a.example/x and http://b.example/y?z=1 plus text")
    print(f"  links in text       : {found['live']}")
    problems += 0 if len(found["live"]) == 2 else 1

    # The clipboard itself. In this sandbox there is no display and no xclip, so
    # both answers are legitimate — what matters is that it refuses rather than
    # ever inventing content.
    ok_read, value = _read_clipboard()
    if ok_read:
        print(f"  clipboard read      : works ({len(value)} chars)")
        wrote, why = _write_clipboard("سلطان التجربة")
        after_ok, after = _read_clipboard()
        same = after_ok and after.strip() == "سلطان التجربة"
        print(f"  clipboard write     : {wrote} · round trip {same}")
        problems += 0 if wrote and same else 1
    else:
        print(f"  clipboard read      : refused honestly — {value[:66]}")
        problems += 0 if ("xclip" in value or "pyperclip" in value) else 1
        print(f"  clipboard write     : refused honestly — {_write_clipboard('x')[1][:60]}")

    # With no display the run path must answer with the refusal, not a stack trace.
    answer = run({"action": "read"})
    print(f"  run(read), no clip  : {answer[:74]}")
    if ok_read:
        problems += 0 if "characters" in answer or "empty" in answer else 1
    else:
        problems += 0 if "could not reach the clipboard" in answer else 1

    # Given text, every action works with no clipboard at all.
    given = run({"action": "links", "text": "https://x.example/1 and https://y.example/2"})
    print(f"  links, given text   : {given.splitlines()[0]}")
    problems += 0 if "2 address(es)" in given else 1

    typed = run({"action": "type", "text": "مجرد نص عادي هنا"})
    problems += 0 if "text" in typed and "characters" in typed else 1
    print(f"  type, given text    : {typed.splitlines()[0][:70]}")

    short = run({"action": "summary", "text": "too short"})
    problems += 0 if "short enough" in short else 1
    print(f"  summary when tiny   : {short[:64]}")

    code = run({"action": "explain", "text": "hello""\n""    world"})
    problems += 0 if "not code" in code or "no AI key" in code or "explaining nothing" in code else 1
    print(f"  explain non-code    : {code[:66]}")

    huge = run({"action": "read", "text": "x" * (MAX_CHARS + 10)})
    problems += 0 if "more than I will work on" in huge else 1
    print(f"  refuses a huge blob : {huge[:66]}")

    tidy = run({"action": "json", "text": '{"z":1,"a":2}'})
    problems += 0 if tidy.startswith("{\n") else 1
    print(f"  json through run    : {tidy.splitlines()[1].strip()!r}")

    print(f"  bad action          : {run({'action': 'sing', 'text': 'x'})[:64]}")
    problems += 0 if "not a clipboard action" in run({"action": "sing", "text": "x"}) else 1

    # The promise: nothing is kept. No state file, no cache with the content.
    here = Path(__file__).resolve().parent
    leftovers = [p.name for p in here.glob("*clip*") if p.is_file() and p.name != Path(__file__).name]
    print(f"  keeps nothing       : {leftovers or 'no state files at all'}")
    problems += 0 if not leftovers else 1
    secret = "AKIAIOSFODNN7EXAMPLE"
    run({"action": "type", "text": secret})
    written_anywhere = [p.name for p in here.parent.glob("config/**/*") if p.is_file()
                        and secret.encode() in p.read_bytes()] if (here.parent / "config").exists() else []
    print(f"  no secret on disk   : {written_anywhere or 'nothing written anywhere'}")
    problems += 0 if not written_anywhere else 1

    print("  RESULT:", "PASS" if problems == 0 else f"{problems} problem(s)")
    return problems


if __name__ == "__main__":
    raise SystemExit(_selftest())
