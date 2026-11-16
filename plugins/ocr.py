"""
ocr.py — reading text out of pictures, locally.

WHERE THE IMAGE GOES
    Nowhere, by default. Tesseract runs inside your machine: no key, no network,
    no upload, no cost. That is the engine this plugin prefers and the only one
    it will use on its own.

    The one exception is the "cloud" engine, and it exists because it is the
    only thing that reads handwriting and messy photos well. It is listed as a
    choice and it is never chosen for you. If you pick it, your image goes to
    Gemini — that is what it means, and the plugin tells you so in its status.

    No image leaves this machine unless you personally set engine = cloud.

WHAT IT REFUSES TO DO
    It will not tell you it read something when it did not. Every result carries
    the engine's own confidence. When the average is low, or when only a few
    words were read with any certainty, the answer says plainly that the text
    could not be read reliably and shows what little it did see — rather than
    handing you a confident-looking string of nonsense.

ARABIC
    Tesseract reads Arabic when the language data is installed. The plugin asks
    the engine what it has, uses "ara+eng" when both are present, and tells you
    which one it actually used. Arabic needs the script trained data:
        Windows installer → tick Arabic
        Linux            → sudo apt install tesseract-ocr-ara

SETTINGS (Plugin Manager → OCR)
    engine      auto | tesseract | cloud  (auto = tesseract if installed)
    languages   ara+eng
    min_confidence  60 — below this the answer is "I could not read this"
    upscale     2 — small images are enlarged before reading; helps a lot
"""

from __future__ import annotations

import base64
import re
from pathlib import Path
from typing import Any

PLUGIN = {
    "name": "ocr",
    "description": (
        "Reads the text inside an image or a screenshot: 'read what is written in this "
        "picture', 'what does this screenshot say'. It works locally, reports how "
        "confident it is, and says so when the text is too poor to read reliably."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "read | screenshot | engines | status"},
            "path": {"type": "STRING", "description": "Image file to read"},
            "languages": {"type": "STRING", "description": "ara+eng (default)"},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "ocr",
    "title": "OCR / قراءة الصور",
    "fields": [
        {"key": "engine", "type": "choice", "label": "Engine (cloud = image leaves this machine)",
         "default": "auto", "choices": ["auto", "tesseract", "cloud"]},
        {"key": "languages", "type": "text", "label": "Tesseract languages", "default": "ara+eng"},
        {"key": "min_confidence", "type": "text", "label": "Minimum confidence (60)",
         "default": "60"},
        {"key": "upscale", "type": "text", "label": "Upscale small images (2)", "default": "2"},
    ],
}

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp", ".gif"}
MAX_IMAGE_MB = 25


def _cfg() -> dict[str, Any]:
    out = {f["key"]: f.get("default") for f in PLUGIN_SETTINGS["fields"]}
    try:
        from memory.config_manager import get_plugin_config
        out.update({k: v for k, v in (get_plugin_config("ocr") or {}).items()
                    if v not in (None, "")})
    except Exception:
        pass
    return out


def _int_cfg(key: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(str(_cfg().get(key) or default))))
    except Exception:
        return default


def _tesseract():
    """Import pytesseract lazily. Missing binary and missing module are different ills."""
    try:
        import pytesseract
        return pytesseract, ""
    except Exception as e:
        return None, f"pytesseract is not installed ({type(e).__name__})"


def _tesseract_langs(pytesseract) -> list[str]:
    try:
        return list(pytesseract.get_languages(config=""))
    except Exception:
        return []


def available() -> tuple[bool, str]:
    """(usable, why not) — nothing here ever claims to be ready when it is not."""
    engine = str(_cfg().get("engine") or "auto").strip().lower()
    pytesseract, why = _tesseract()
    if engine == "cloud":
        try:
            from memory.config_manager import get_api_key
            if str(get_api_key() or "").strip():
                return True, "cloud (Gemini) — the image WOULD be uploaded"
            return False, "cloud is chosen but no API key is configured"
        except Exception:
            return False, "cloud is chosen but the configuration could not be read"
    if pytesseract is None:
        if engine == "tesseract":
            return False, why
        return False, (f"no OCR engine is available ({why}). Install one: pip install "
                       f"pytesseract — and the tesseract program itself, which is separate")
    try:
        import PIL  # noqa: F401
    except Exception:
        return False, "Pillow is not installed, so I cannot open the image"
    langs = _tesseract_langs(pytesseract)
    if not langs:
        return False, ("the tesseract program itself is not installed (the Python package is "
                       "only a bridge to it)")
    wanted = _wanted_langs(langs)
    return True, f"tesseract (local, offline) using {wanted}"


def _wanted_langs(installed: list[str]) -> str:
    """Use what is actually installed — and never silently drop Arabic."""
    asked = [p.strip() for p in str(_cfg().get("languages") or "ara+eng").split("+") if p.strip()]
    have = [p for p in asked if p in installed]
    if have:
        return "+".join(have)
    return "eng" if "eng" in installed else (installed[0] if installed else "eng")


def _open_image(path: Path):
    """Open, sanity-check, and enlarge if it is small. Enlarging matters more than it sounds."""
    from PIL import Image
    if path.stat().st_size > MAX_IMAGE_MB * 1024 * 1024:
        return None, f"that image is larger than {MAX_IMAGE_MB} MB"
    img = Image.open(path)
    if img.width < 40 or img.height < 40:
        return None, "that image is too small to contain readable text"
    scale = _int_cfg("upscale", 2, 1, 4)
    if scale > 1 and max(img.width, img.height) < 1200:
        img = img.resize((img.width * scale, img.height * scale), Image.LANCZOS)
    if img.mode not in ("L", "RGB"):
        img = img.convert("RGB")
    return img, ""


def _cloud_read(path: Path) -> dict[str, Any]:
    """The opt-in path: the image is uploaded to Gemini. Only if engine = cloud."""
    try:
        from memory.config_manager import get_api_key
        key = str(get_api_key() or "").strip()
    except Exception:
        key = ""
    if not key:
        return {"ok": False, "text": "", "reason": "no API key is configured"}
    try:
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        suffix = path.suffix.lower().lstrip(".") or "png"
        mime = "image/jpeg" if suffix in ("jpg", "jpeg") else f"image/{suffix}"
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=key)
        reply = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=[types.Part.from_bytes(data=base64.b64decode(data), mime_type=mime),
                      "Transcribe every word in this image exactly as written, keeping the "
                      "reading order. If a part is unreadable, write [unclear] there. "
                      "Do not translate and do not explain."])
        out = (getattr(reply, "text", "") or "").strip()
        return {"ok": bool(out), "text": out, "engine": "cloud (Gemini)",
                "confidence": None, "reason": "" if out else "the model returned nothing"}
    except Exception as e:
        return {"ok": False, "text": "", "reason": f"the cloud engine failed: {str(e)[:90]}"}


def extract(path: str | Path, languages: str = "") -> dict[str, Any]:
    """
    Read an image. The reusable API: {ok, text, confidence, engine, reason}.

    Honesty is built in, not bolted on: a low average confidence is returned as
    ok=False with the little text it did see, so the caller cannot accidentally
    present guesswork as a transcription.
    """
    p = Path(path).expanduser()
    try:
        if not p.is_file():
            return {"ok": False, "text": "", "reason": f"'{p}' is not a file"}
        if p.suffix.lower() not in IMAGE_EXTS:
            return {"ok": False, "text": "",
                    "reason": f"'{p.suffix}' is not an image format I read"}
    except Exception as e:
        return {"ok": False, "text": "", "reason": f"path problem: {str(e)[:70]}"}

    engine = str(_cfg().get("engine") or "auto").strip().lower()
    if engine == "cloud":
        return _cloud_read(p)

    pytesseract, why = _tesseract()
    if pytesseract is None:
        return {"ok": False, "text": "", "reason": why}
    img, problem = _open_image(p)
    if img is None:
        return {"ok": False, "text": "", "reason": problem}

    installed = _tesseract_langs(pytesseract)
    used = languages or _wanted_langs(installed)
    try:
        data = pytesseract.image_to_data(img, lang=used, output_type=pytesseract.Output.DICT)
    except Exception as e:
        msg = str(e)
        if "tesseract is not installed" in msg or "not found" in msg.lower():
            return {"ok": False, "text": "", "reason":
                    "the tesseract program is not installed (the Python package is only a "
                    "bridge to it)"}
        if "language" in msg.lower() or "traineddata" in msg.lower():
            return {"ok": False, "text": "", "reason":
                    f"the language data '{used}' is missing — install it in tesseract "
                    f"(Arabic: tesseract-ocr-ara)"}
        return {"ok": False, "text": "", "reason": f"the engine failed: {msg[:90]}"}

    words, confs = [], []
    for word, conf in zip(data.get("text", []), data.get("conf", [])):
        word = str(word).strip()
        try:
            c = float(conf)
        except Exception:
            c = -1.0
        if word and c >= 0:
            words.append(word)
            confs.append(c)
    text = " ".join(words).strip()
    avg = round(sum(confs) / len(confs), 1) if confs else 0.0
    floor = _int_cfg("min_confidence", 60, 0, 100)
    low = sum(1 for c in confs if c < floor)

    if not text:
        return {"ok": False, "text": "", "engine": f"tesseract ({used})", "confidence": 0.0,
                "reason": "I found no readable text in that image at all"}
    if avg < floor:
        return {"ok": False, "text": text, "engine": f"tesseract ({used})",
                "confidence": avg, "low_words": low, "reason":
                f"the text is too poor to read reliably (confidence {avg}%, below the "
                f"{floor}% floor) — here is the raw guess, but do not trust it"}
    return {"ok": True, "text": text, "engine": f"tesseract ({used})", "confidence": avg,
            "low_words": low, "words": len(words), "reason": ""}


def grab_screenshot() -> tuple[bool, Any]:
    """A picture of the screen right now, into memory. No file is written."""
    try:
        from PIL import ImageGrab
        img = ImageGrab.grab()
        return True, img
    except Exception as e:
        return False, (f"I cannot take a screenshot here ({type(e).__name__}) — on Linux I "
                       f"need a display and scrot/imagemagick, on Windows it just works")


# ── tool entry point ──────────────────────────────────────────────────────────

def _report(result: dict[str, Any]) -> str:
    if result.get("ok"):
        head = f"(read by {result.get('engine')}, confidence {result.get('confidence')}%)"
        if result.get("low_words"):
            head += f" — {result['low_words']} word(s) were uncertain"
        return f"{head}\n\n{result['text']}"
    text = result.get("text", "")
    reason = result.get("reason") or "I could not read that image"
    if text:
        return f"Sir, {reason}.\n\nWhat I could make out:\n{text}"
    return f"Sir, {reason}."


def run(parameters: dict, player=None, session_memory=None) -> str:
    """Dispatch one OCR action. Never raises."""
    try:
        p = parameters or {}
        action = str(p.get("action") or "read").strip().lower()

        if action in ("read", "image", "extract", "text"):
            path = str(p.get("path") or "").strip()
            if not path:
                return "Which image, sir?"
            result = extract(path, languages=str(p.get("languages") or ""))
            if player:
                try:
                    player.write_log(f"[OCR] {Path(path).name} → "
                                     f"{'ok' if result.get('ok') else 'unreadable'}")
                except Exception:
                    pass
            return _report(result)

        if action in ("screenshot", "screen", "clipboard"):
            ok, img = grab_screenshot()
            if not ok:
                return f"Sir, {img}."
            pytesseract, why = _tesseract()
            if pytesseract is None:
                return f"Sir, I can take the screenshot but {why}."
            tmp = Path(__file__).resolve().parent.parent / "config" / ".plugin_cache" / "screen_ocr.png"
            try:
                tmp.parent.mkdir(parents=True, exist_ok=True)
                img.save(tmp)
                result = extract(tmp)
                if player:
                    try:
                        player.write_log("[OCR] screen → "
                                         f"{'ok' if result.get('ok') else 'unreadable'}")
                    except Exception:
                        pass
                return _report(result)
            except Exception as e:
                return f"Sir, I could not process the screenshot: {str(e)[:110]}"
            finally:
                try:
                    tmp.unlink(missing_ok=True)      # nothing of your screen is left on disk
                except Exception:
                    pass

        if action in ("engines", "status", "info"):
            ok, why = available()
            cfg = _cfg()
            lines = [f"Engine      : {cfg.get('engine')} — {'ready' if ok else 'NOT ready'}",
                     f"Detail      : {why}",
                     f"Languages   : {cfg.get('languages')} (uses only what is installed)",
                     f"Confidence  : below {cfg.get('min_confidence')}% I refuse to call it a reading",
                     f"Privacy     : {'images are UPLOADED to Gemini' if cfg.get('engine') == 'cloud' else 'nothing leaves this machine'}"]
            if not ok:
                lines.append("\nTo install a local engine: pip install pytesseract pillow — and "
                             "the tesseract program itself from its own installer, ticking "
                             "Arabic if you want it.")
            return "\n".join(lines)

        if action in ("languages", "langs"):
            pytesseract, why = _tesseract()
            if pytesseract is None:
                return f"Sir, {why}."
            installed = _tesseract_langs(pytesseract)
            if not installed:
                return "Sir, the tesseract program is not installed, so it has no languages."
            return (f"Tesseract here reads: {', '.join(installed)}. "
                    f"I would use {_wanted_langs(installed)}.")

        return (f"Sir, '{action}' is not an OCR action I have. I can read an image, read the "
                f"screen, list languages, or report status.")
    except Exception as e:                                   # noqa: BLE001
        return f"Sir, the OCR plugin hit a problem: {str(e)[:150]}"


# ── self-test: python ocr.py ─────────────────────────────────────────────────

def _selftest() -> int:
    print("ocr — self-test")
    problems = 0

    from PIL import Image, ImageDraw
    tmp_dir = Path(__file__).resolve().parent
    probe = tmp_dir / "_ocr_probe.png"

    # a picture with real words in it, drawn here so the test needs nothing
    img = Image.new("RGB", (900, 220), "white")
    draw = ImageDraw.Draw(img)
    draw.text((30, 40), "INVOICE 2026 TOTAL 450 EGP", fill="black")
    draw.text((30, 110), "Mansoura - 14 March", fill="black")
    img.save(probe)
    print(f"  test image          : {probe.name} ({img.width}x{img.height})")

    ok, why = available()
    print(f"  engine available    : {ok}  ({why[:78]})")

    result = extract(probe)
    print(f"  extract()           : ok={result.get('ok')} conf={result.get('confidence')} "
          f"engine={result.get('engine', '—')}")
    print(f"  reason              : {result.get('reason') or '(read it)'}")
    if result.get("text"):
        print(f"  text seen           : {result['text'][:60]!r}")

    # The interface must be honest in BOTH worlds: with an engine and without one.
    if ok:
        problems += 0 if result.get("ok") else 1
        problems += 0 if "INVOICE" in result.get("text", "").upper() else 1
        print("  ✓ read the drawn text")
    else:
        problems += 0 if not result.get("ok") else 1
        problems += 0 if result.get("reason") else 1
        print("  ✓ refused honestly instead of inventing text (no engine on this machine)")

    miss = extract(tmp_dir / "_does_not_exist.png")
    print(f"  missing file        : {miss['reason'][:66]}")
    problems += 0 if not miss["ok"] else 1

    notimg = extract(Path(__file__) if False else tmp_dir / "ocr.py")
    print(f"  not an image        : {notimg['reason'][:66]}")
    problems += 0 if not notimg["ok"] else 1

    print(f"  privacy default     : engine={_cfg().get('engine')} "
          f"(auto = local only; 'cloud' is never chosen for you)")
    problems += 0 if str(_cfg().get("engine")) != "cloud" else 1

    print(f"  status              : {run({'action': 'status'}).splitlines()[0]}")
    print(f"  bad action          : {run({'action': 'sing'})[:72]}")
    problems += 0 if "not an OCR action" in run({"action": "sing"}) else 1
    print(f"  no path             : {run({'action': 'read'})}")
    problems += 0 if "Which image" in run({"action": "read"}) else 1

    probe.unlink(missing_ok=True)
    print("  RESULT:", "PASS" if problems == 0 else f"{problems} problem(s)")
    return problems


if __name__ == "__main__":
    raise SystemExit(_selftest())
