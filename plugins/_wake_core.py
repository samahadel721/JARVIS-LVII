"""
_wake_core.py — نواة كلمات التنبيه العربية، بلا إنترنت بعد التجهيز.

هذا ملف مشترك (يبدأ بشرطة سفلية) فلا يُحمَّل كإضافة. يوفّره للاستعمال:

• **تطبيع عربي حقيقي** قبل المقارنة: إزالة التشكيل والتطويل، وتوحيد الألف والهمزة
  والياء والتاء المربوطة — فـ«يَافَلَاحْ» و«يافالح» و«يا فالح» شيء واحد.
• **مطابقة ضبابية** على كلمات النصّ، لا على مطابقة حرفية هشّة: «يا فلاح» و«يا فالخ»
  تُقبلان، و«سلام عليكم» تُرفض. + كشف العبارة داخل جملة أطول («طيب يا فالح اعمل كذا»).
• **محرّكان**: Vosk (عربي، مجاني، يعمل بلا إنترنت — الافتراضي) وPorcupine
  (أخفّ وأسرع، لكنه يحتاج مفتاحًا مجانيًا وتنشيطًا دوريًا بالإنترنت — اختياري تمامًا).
• **تنزيل نموذج آمن**: ملف `.part` ثم إعادة تسمية ذرّية، وتحقّق من الحجم، و**حماية من
  اختراق المسارات** داخل ملفات zip المصدّرة (أي مسار هارب يُرفض ولا يُكتب).
• **حلقة إنصات** كاملة (إطارات صوت ← نصّ ← قرار ← تبريد) قابلة للفحص بلا ميكروفون،
  فالاختبار الذاتي لا يعتمد على عتاد.

لا يرمي هذا الملف استثناءً للخارج أبدًا: كل دالة تعيد (نجح، رسالة) أو قاموسًا واضحًا.
بلا أسرار: لا مفاتيح ولا كلمات مرور في أي سطر هنا، والمفاتيح تمرّ عبر الحجب في السجل.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

SAMPLE_RATE = 16000  # ما يطلبه المحرّكان معًا؛ يمنع أي تحويل في المسار

#: مجلد النماذج في المشروع نفسه (config/models) — نفس مكان نماذج كلمة التنبيه الحالية.
MODELS_DIR = Path("config") / "models"
#: مجلد إعداداتنا نحن (خارج أي ملف من ملفات المشروع).
WAKE_DIR = Path("config") / "orchestration" / "wake_arabic"

VOSK_MODEL = {
    "name": "vosk-model-ar-mgb2-0.4",
    "url": "https://alphacephei.com/vosk/models/vosk-model-ar-mgb2-0.4.zip",
    "size_mb": 318,
    "license": "Apache 2.0",
    "min_bytes": 200 * 1024 * 1024,   # حماية من تنزيل مقطوع يُظنّ صالحًا
    "note": "نموذج عربي (MGB2) — مجاني ومفتوح، يعمل بلا إنترنت بعد تنزيله مرة واحدة.",
}

PORCUPINE_NOTE = (
    "Porcupine: مفتاح مجاني من منصّة Picovoice + ملفان .ppn لكلمتَي التنبيه، "
    "ويحتاج تنشيطًا دوريًا بالإنترنت. أخفّ على المعالج من Vosk."
)

# ── تطبيع عربي ────────────────────────────────────────────────────────────────
_DIACRITICS = re.compile(r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED\u0640]")
_UNIFY = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ى": "ي", "ئ": "ي",
                        "ؤ": "و", "ة": "ه", "گ": "ك", "پ": "ب", "ڤ": "ف",
                        # النطق المصري: «يا لذيذ» تُقال «يا لزيز» — وVosk يكتبها كما يسمعها
                        "ذ": "ز", "ظ": "ز", "ث": "س"})
_KEEP = re.compile(r"[^\w\s]", re.UNICODE)


def ar_normalize(text: str) -> str:
    """يطبّع نصًّا عربيًا للمقارنة: بلا تشكيل ولا تطويل، وبألف/ياء/هاء موحّدة.

    وتُطبَّق قاعدة النطق المصري: ذ/ظ ← ز وث ← س، فـ«يا لذيذ» و«يا لزيز» و«يالزيز» سواء.
    القاعدة تُطبَّق على الطرفين معًا (النصّ والعبارة) فلا تفتح بابًا للتخمين.
    """
    if not text:
        return ""
    value = str(text).strip().lower()
    value = _DIACRITICS.sub("", value)
    value = value.translate(_UNIFY)
    value = _KEEP.sub(" ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def ar_tokens(text: str) -> list[str]:
    """كلمات النصّ بعد التطبيع."""
    return [t for t in ar_normalize(text).split(" ") if t]


def edit_distance(a: str, b: str) -> int:
    """مسافة ليفنشتاين — صغيرة بالحجم الذي نتعامل معه (كلمات قصيرة)."""
    a, b = str(a), str(b)
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost))
        previous = current
    return previous[-1]


def token_score(a: str, b: str) -> float:
    """تشابه كلمتين: 1.0 متطابقتان، ويتناقص مع كل حرف مختلف."""
    a, b = str(a), str(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    score = max(0.0, 1.0 - edit_distance(a, b) / float(max(len(a), len(b))))
    # قاعدة صريحة ومحدودة: الكلمة نفسها بحروفها وترتيب مختلف («فلاح» ↔ «فالح») — خطأ
    # ترتيب يقع فيه المحرّك كثيرًا، ونقبله بدرجة 0.85 لا أكثر. الحدّ الأدنى ٣ حروف
    # كي لا تصير الكلمات القصيرة كلمةً واحدة.
    if len(a) >= 3 and len(b) >= 3 and sorted(a) == sorted(b):
        score = max(score, 0.85)
    return score


def bigram_dice(a: str, b: str) -> float:
    """تشابه Dice على الحروف الثنائية — يرحم أخطاء المحرّك التي يهدمها ليفنشتاين.

    مثال حقيقي من التجربة: الصوت «يا فالح» سمعه Vosk «يافا لحل» — ليفنشتاين يرفضه،
    وهذه الدالة تراه تشابهًا 0.83 فتقبله.
    """
    a, b = str(a), str(b)
    if len(a) < 2 or len(b) < 2:
        return 1.0 if a == b else 0.0
    grams_a = [a[i:i + 2] for i in range(len(a) - 1)]
    grams_b = [b[i:i + 2] for i in range(len(b) - 1)]
    pool = list(grams_b)
    shared = 0
    for gram in grams_a:
        if gram in pool:
            pool.remove(gram)
            shared += 1
    return 2.0 * shared / float(len(grams_a) + len(grams_b))


def match_phrase(transcript: str, phrase: str, min_score: float = 0.80) -> dict:
    """هل ظهرت العبارة في النصّ؟ يعيد {hit, score, how, phrase, window} بلا رمي أبدًا.

    ثلاث طرائق بترتيب القوة: مطابقة تامّة · عبارة كاملة داخل جملة أطول · نافذة كلمات
    بمطابقة ضبابية (أي أن «يا فلاح» و«يا فالخ» تُقبلان لـ«يا فالح»).
    """
    phrase_tokens = ar_tokens(phrase)
    text_tokens = ar_tokens(transcript)
    result = {"hit": False, "score": 0.0, "how": "", "phrase": phrase, "window": ""}
    if not phrase_tokens or not text_tokens:
        return result
    try:
        limit = max(0.5, min(1.0, float(min_score)))
    except Exception:
        limit = 0.80
    if text_tokens == phrase_tokens:
        result.update({"hit": True, "score": 1.0, "how": "exact", "window": " ".join(text_tokens)})
        return result
    span = len(phrase_tokens)
    # نوافذ متتالية بنفس طول العبارة (±1 كلمة للتعويض عن زيادة/نقص من المحرّك)
    for width in {span, span + 1}:
        if width <= 0 or width > len(text_tokens):
            continue
        for start in range(0, len(text_tokens) - width + 1):
            window = text_tokens[start:start + width]
            score = _window_score(window, phrase_tokens)
            if score >= limit and score > result["score"]:
                result.update({"hit": True, "score": round(score, 3),
                               "how": "window" if width == span else "window+1",
                               "window": " ".join(window)})
    # مسار الحروف الثنائية: يقبل ما يقطّعه المحرّك خطأً («يافا لحل» ← «يا فالح»)
    joined_phrase = "".join(phrase_tokens)
    for width in {2, 3}:
        if width <= 0 or width > len(text_tokens):
            continue
        for start in range(0, len(text_tokens) - width + 1):
            joined_window = "".join(text_tokens[start:start + width])
            dice = bigram_dice(joined_window, joined_phrase)
            if dice >= limit and dice > result["score"]:
                result.update({"hit": True, "score": round(dice, 3), "how": "bigrams",
                               "window": " ".join(text_tokens[start:start + width])})
    # حالة شائعة جدًا من Vosk: العبارة كلمة واحدة ملتصقة («يافالح»)
    if not result["hit"] and len(phrase_tokens) >= 2:
        joined = "".join(phrase_tokens)
        for token in text_tokens:
            score = token_score(token, joined)
            if score >= limit and score > result["score"]:
                result.update({"hit": True, "score": round(score, 3), "how": "joined", "window": token})
    return result


def _window_score(window: list[str], phrase_tokens: list[str]) -> float:
    """متوسط تشابه الكلمات في نافذة، مع محاذاة مرنة بالطول."""
    if not window or not phrase_tokens:
        return 0.0
    if window == phrase_tokens:
        return 1.0
    # محاذاة بالنهاية: قارن كل كلمة بموقعها، وما تبقّى يُقارن بأقرب كلمة
    scores = []
    for index, token in enumerate(window):
        target = phrase_tokens[index] if index < len(phrase_tokens) else phrase_tokens[-1]
        best = token_score(token, target)
        if index >= len(phrase_tokens):
            best = max(best, *(token_score(token, p) for p in phrase_tokens))
        scores.append(best)
    if len(phrase_tokens) > len(window):
        scores.extend(0.0 for _ in range(len(phrase_tokens) - len(window)))
    return sum(scores) / float(max(len(window), len(phrase_tokens)))


def match_any(transcript: str, phrases: Iterable[str], min_score: float = 0.80) -> dict:
    """أفضل مطابقة بين النصّ وقائمة العبارات. يعيد قاموسًا بلا رمي أبدًا."""
    best: dict = {"hit": False, "score": 0.0, "how": "", "phrase": "", "window": ""}
    for phrase in phrases or []:
        found = match_phrase(transcript, phrase, min_score)
        if found["hit"] and found["score"] > best["score"]:
            best = found
    return best


def parse_phrases(raw: Any) -> list[str]:
    """يقبل «يا فالح, يا لذيذ ؛ hey jarvis» بأي فاصل، ويعيد قائمة نظيفة بلا تكرار."""
    if isinstance(raw, (list, tuple)):
        parts = [str(x) for x in raw]
    else:
        parts = re.split(r"[,\n;؛،|]+", str(raw or ""))
    seen, out = set(), []
    for part in parts:
        clean = str(part).strip()
        if not clean:
            continue
        key = ar_normalize(clean)
        if key and key not in seen:
            seen.add(key)
            out.append(clean)
    return out


def redact(text: Any, secrets: Iterable[str]) -> str:
    """يخفي أي سرّ من نصّ قبل عرضه أو تسجيله — طول السرّ ≥ 4 وإلا تُجوهل."""
    value = str(text if text is not None else "")
    for secret in secrets or []:
        token = str(secret or "").strip()
        if len(token) >= 4 and token in value:
            value = value.replace(token, "***")
    return value


class Cooldown:
    """تبريد لكل عبارة: يمنع أن تُشعل كلمةٌ واحدة عشر أزرار."""

    def __init__(self, seconds: float = 4.0):
        try:
            self.seconds = max(0.0, float(seconds))
        except Exception:
            self.seconds = 4.0
        self._last: dict[str, float] = {}

    def allow(self, phrase: str, now: Optional[float] = None) -> bool:
        """هل يُسمح بإطلاق هذه العبارة الآن؟"""
        stamp = time.time() if now is None else float(now)
        key = ar_normalize(phrase) or str(phrase)
        last = self._last.get(key, -1e9)
        if stamp - last >= self.seconds:
            self._last[key] = stamp
            return True
        return False

    def reset(self) -> None:
        self._last.clear()


# ── المحرّكات ─────────────────────────────────────────────────────────────────
_NP: Any = None


def _np():
    """numpy عند الحاجة فقط — فلا يُشترط لبقية المسارات."""
    global _NP
    if _NP is None:
        try:
            import numpy as _numpy  # noqa: PLC0415
            _NP = _numpy
        except Exception:
            _NP = False
    return _NP or None


def _module_present(name: str) -> bool:
    """هل الحزمة مثبّتة؟ بلا استيراد فعلي — فلا نُحمّل شيئًا ثقيلًا لمجرد السؤال."""
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


def vosk_available() -> tuple[bool, str]:
    """هل حزمة vosk مثبّتة؟ يعيد (نجح، رسالة)."""
    if _module_present("vosk"):
        return True, "حزمة vosk موجودة."
    return False, "حزمة vosk غير مثبّتة:  pip install vosk   (تعمل بلا إنترنت بعد تنزيل النموذج)"


def porcupine_available() -> tuple[bool, str]:
    """هل حزمة pvporcupine مثبّتة؟ (اختيارية تمامًا)"""
    if _module_present("pvporcupine"):
        return True, "حزمة pvporcupine موجودة."
    return False, "حزمة pvporcupine غير مثبّتة (اختيارية):  pip install pvporcupine"


def openwake_available() -> tuple[bool, str]:
    """هل حزمة openWakeWord مثبّتة؟ (اختيارية — تُستخدم مع نموذج مدرَّب خاص بك)"""
    if _module_present("openwakeword"):
        return True, "حزمة openwakeword موجودة."
    return False, ("حزمة openwakeword غير مثبّتة:  pip install openwakeword"
                   "   (مطلوبة فقط إن كان لديك ملف نموذج .onnx خاص بك — غير لازمة لـ Vosk)")


def core_module() -> Optional[Any]:
    """نواة الحزمة إن كانت محمّلة (للتسجيل فقط — وإن غابت لا يحدث شيء)."""
    return sys.modules.get("_jarvis_orch_core")


def log(action: str, status: str, error: str = "", detail: Any = None) -> None:
    """سجل منظَّم إن وُجدت النواة — وبلا أسرار أبدًا (المفاتيح تمرّ عبر حجب النواة)."""
    core = core_module()
    if core is None:
        return
    try:
        core.log_event("wake_arabic", action, status, 0, error, detail)
    except Exception:
        pass


def model_dir(name: Optional[str] = None) -> Path:
    """مجلد النموذج العربي المتوقّع."""
    return MODELS_DIR / (name or VOSK_MODEL["name"])


def model_present() -> tuple[bool, str]:
    """هل النموذج العربي مكتمل؟ نتحقّق من وجود ملفات جوهرية لا من وجود مجلد فقط."""
    target = model_dir()
    if not target.exists():
        return False, f"النموذج غير موجود: {target}  →  action=setup"
    required = ("am/final.mdl", "conf/mfcc.conf")
    for member in required:
        if not (target / member).exists():
            return False, f"النموذج ناقص (لا يوجد {member}) — أعد setup"
    # الرسم قد يكون HCLG أو HCLr أو HCLr.fst حسب النموذج — نكتفي بواحد صالح
    graphs = [target / "graph" / name for name in ("HCLG.fst", "HCLr.fst", "HCLr.fst.bin", "HCLG.fst.bin")]
    if not any(path.exists() for path in graphs):
        return False, "النموذج ناقص (لا يوجد رسم بياني graph/*.fst) — أعد setup"
    return True, str(target)


def download_file(url: str, dest: Path, min_bytes: int = 0,
                  progress: Optional[Callable[[str], None]] = None,
                  timeout: float = 120.0, chunk: int = 1024 * 512) -> tuple[bool, str]:
    """ينزّل إلى `dest.part` ثم يعيد التسمية — فلا يبقى ملف نصفه صالح إن انقطع الاتصال."""
    dest = Path(dest)
    part = dest.with_suffix(dest.suffix + ".part")
    say = progress or (lambda _m: None)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if part.exists():
            part.unlink()
    except Exception as exc:  # noqa: BLE001
        return False, f"تعذّر تهيئة مجلد التنزيل ({type(exc).__name__})."
    try:
        scheme = url.split(":", 1)[0].lower()
        done = 0
        with open(part, "wb") as handle:
            if scheme in ("http", "https"):
                request = urllib.request.Request(url, headers={"User-Agent": "JARVIS-local/1.0"})
                with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 — عنوان ثابت معلن
                    total = int(response.headers.get("Content-Length") or 0)
                    while True:
                        block = response.read(chunk)
                        if not block:
                            break
                        handle.write(block)
                        done += len(block)
                        if total:
                            say(f"… {done * 100 // total}٪ من {total // (1024 * 1024)} م.ب")
            else:
                source_path = url[7:] if url.startswith("file://") else url[5:]
                with open(source_path, "rb") as source:
                    while True:
                        block = source.read(chunk)
                        if not block:
                            break
                        handle.write(block)
                        done += len(block)
        size = part.stat().st_size
        if min_bytes and size < min_bytes:
            part.unlink(missing_ok=True)
            return False, (f"الملف أصغر من المتوقّع ({size // 1024} ك.ب) — غالبًا التنزيل انقطع. "
                           f"أعد المحاولة، وسيبدأ من جديد.")
        os.replace(part, dest)
        return True, f"نُزّل {dest.name} ({size // (1024 * 1024)} م.ب)."
    except Exception as exc:  # noqa: BLE001
        try:
            part.unlink(missing_ok=True)
        except Exception:
            pass
        return False, f"تعذّر التنزيل ({type(exc).__name__}: {str(exc)[:120]}). تحقّق من الإنترنت ثم أعد."


def safe_extract(zip_path: Path, dest_dir: Path, progress: Optional[Callable[[str], None]] = None) -> tuple[bool, str]:
    """يفكّ zip بأمان: يرفض أي مسار هارب (.. أو مطلق أو قرص)، ثم يرفع المجلد الجذري الوحيد.

    قاعدة صريحة: أي عضو في الملف يُرفض ← لا يُفكّ شيء ولا يُكتب شيء على القرص.
    """
    zip_path, dest_dir = Path(zip_path), Path(dest_dir)
    say = progress or (lambda _m: None)
    try:
        if not zipfile.is_zipfile(zip_path):
            return False, "الملف المنزَّل ليس ملف zip صالحًا."
        with zipfile.ZipFile(zip_path) as archive:
            names = archive.namelist()
            if not names:
                return False, "الملف فارغ."
            for name in names:
                candidate = name.replace("\\", "/")
                if candidate.startswith("/") or re.match(r"^[A-Za-z]:", candidate):
                    return False, f"رفضت الفكّ: مسار مطلق داخل الملف ({candidate[:60]})."
                parts = [p for p in candidate.split("/") if p not in ("", ".")]
                if any(p == ".." for p in parts):
                    return False, f"رفضت الفكّ: مسار هارب داخل الملف ({candidate[:60]})."
            staging = Path(tempfile.mkdtemp(prefix="wake_unzip_"))
            try:
                for index, member in enumerate(archive.infolist(), start=1):
                    archive.extract(member, staging)
                    if index % 400 == 0:
                        say(f"… استُخرج {index} من {len(names)} ملفًا")
                roots = [child for child in staging.iterdir()]
                if len(roots) == 1 and roots[0].is_dir():
                    source = roots[0]
                else:
                    source = staging
                dest_dir.mkdir(parents=True, exist_ok=True)
                for child in source.iterdir():
                    target = dest_dir / child.name
                    if target.exists():
                        if target.is_dir():
                            shutil.rmtree(target, ignore_errors=True)
                        else:
                            target.unlink(missing_ok=True)
                    shutil.move(str(child), str(target))
                return True, f"استُخرج {len(names)} ملفًا إلى {dest_dir}"
            finally:
                shutil.rmtree(staging, ignore_errors=True)
    except Exception as exc:  # noqa: BLE001
        return False, f"تعذّر فكّ الملف ({type(exc).__name__}: {str(exc)[:120]})."


def setup_vosk_model(progress: Optional[Callable[[str], None]] = None) -> tuple[bool, str]:
    """ينزّل النموذج العربي ويفكّه في config/models — مرة واحدة، ثم يعمل بلا إنترنت للأبد."""
    say = progress or (lambda _m: None)
    ok, note = vosk_available()
    if not ok:
        return False, note
    present, where = model_present()
    if present:
        return True, f"النموذج موجود أصلًا: {where}"
    target = model_dir()
    archive = MODELS_DIR / f"{VOSK_MODEL['name']}.zip"
    say(f"… تنزيل {VOSK_MODEL['name']} (~{VOSK_MODEL['size_mb']} م.ب) — مرة واحدة فقط.")
    ok, note = download_file(VOSK_MODEL["url"], archive, min_bytes=int(VOSK_MODEL["min_bytes"] / 10),
                             progress=say)
    if not ok:
        return False, note
    say("… فكّ الملف (قد يأخذ دقيقة).")
    ok, note = safe_extract(archive, target, progress=say)
    if not ok:
        return False, note
    present, where = model_present()
    if present:
        try:
            archive.unlink(missing_ok=True)   # لم يعد له لزوم بعد الفكّ
        except Exception:
            pass
        return True, f"النموذج جاهز: {where} — والحذف لا يضرّ (بلا إنترنت بعد الآن)."
    return False, "فُكّ الملف لكن النموذج ناقص — أعد setup."


class VoskEngine:
    """محرّك Vosk: يحوّل إطارات صوت int16 إلى نصّ عربي جزئي/نهائي."""

    kind = "vosk"

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else model_dir()
        self._model = None
        self._rec = None
        self.error = ""

    def load(self) -> tuple[bool, str]:
        """يحمّل النموذج مرة واحدة (ثقيل: يبقى في الذاكرة بعدها)."""
        if self._rec is not None:
            return True, "محمّل."
        try:
            from vosk import Model, KaldiRecognizer, SetLogLevel  # noqa: PLC0415
            SetLogLevel(-1)
            self._model = Model(str(self.path))
            self._rec = KaldiRecognizer(self._model, SAMPLE_RATE)
            return True, "محمّل."
        except Exception as exc:  # noqa: BLE001
            self.error = f"{type(exc).__name__}: {str(exc)[:140]}"
            return False, f"تعذّر تحميل نموذج Vosk ({self.error})."

    def feed(self, frame: bytes) -> list[str]:
        """يغذّي إطارًا ويستخرج أي نصّ جديد (جزئي أو نهائي)."""
        if self._rec is None:
            return []
        texts: list[str] = []
        try:
            if self._rec.AcceptWaveform(frame):
                text = json.loads(self._rec.Result()).get("text", "")
                if text:
                    texts.append(text)
            else:
                partial = json.loads(self._rec.PartialResult()).get("partial", "")
                if partial:
                    texts.append(partial)
        except Exception:
            return texts
        return texts

    def reset(self) -> None:
        """يفرّغ الجملة الجارية (بعد كل كشف كي لا تتكرّر العبارة)."""
        try:
            if self._rec is not None:
                self._rec.Reset()
        except Exception:
            pass

    def flush(self) -> list[str]:
        """يُغلق الجملة: صمت قصير ثم النتيجة النهائية — درس من التجربة الحقيقية.

        بلا هذا، عبارة قصيرة تنتهي عند حدّ النافذة قد لا يُعلنها Vosk أبدًا.
        """
        if self._rec is None:
            return []
        texts: list[str] = []
        try:
            for _ in range(3):
                self._rec.AcceptWaveform(b"\x00" * 8000)
            final = json.loads(self._rec.FinalResult()).get("text", "")
            if final:
                texts.append(final)
            self._rec.Reset()
        except Exception:
            pass
        return texts


class OpenWakeEngine:
    """محرّك openWakeWord: نماذج صغيرة جدًا (~١ م.ب للعبارة) مدرَّبة على عبارتك.

    ميزته على Vosk: أسرع بأكثر من ١٥ ضعفًا وذاكرة أقل بكثير — لكنه **يحتاج نموذجًا
    مدرَّبًا على عبارتك أنت** (ملف .onnx جاهز). اختياري تمامًا — Vosk العربي هو الافتراضي
    ولا يحتاج أي نموذج منك. يدعم واجهتَي المكتبة
    القديمة (wakeword_model_paths) والحديثة (wakeword_models) ليُعمل مع أي إصدار.
    """

    kind = "openwake"

    def __init__(self, model_paths: Iterable[str], phrases: Iterable[str] = (),
                 threshold: float = 0.55):
        self.paths = [str(p).strip() for p in (model_paths or []) if str(p).strip()]
        labels = [str(p).strip() for p in (phrases or [])]
        self.keywords: list[str] = []
        for index, path in enumerate(self.paths):
            if index < len(labels) and labels[index]:
                self.keywords.append(labels[index])
            else:
                self.keywords.append(Path(path).stem.replace("_", " "))
        try:
            self.threshold = max(0.05, min(0.99, float(threshold)))
        except Exception:
            self.threshold = 0.55
        self.api = ""
        self.error = ""
        self.best: dict[str, float] = {}
        self.last_score = 0.0
        self.frames = 0
        self._model = None

    def load(self) -> tuple[bool, str]:
        """يحمّل النماذج. يعيد (نجح، رسالة) بلا رمي أبدًا."""
        if self._model is not None:
            return True, "محمّل."
        ok, note = openwake_available()
        if not ok:
            return False, note
        if not self.paths:
            return False, "لا نماذج مضبوطة — ضع مسار ملف .onnx المدرَّب في إعدادات الإضافة."
        for path in self.paths:
            if not Path(path).exists():
                return False, f"ملف النموذج غير موجود: {path}"
        if _np() is None:
            return False, "numpy مطلوبة لمحرّك openWakeWord (وهي موجودة أصلًا في متطلبات المشروع)."
        try:
            from openwakeword.model import Model  # noqa: PLC0415
            last = ""
            for kwargs, label in (({"wakeword_models": self.paths, "inference_framework": "onnx"},
                                   "wakeword_models"),
                                  ({"wakeword_model_paths": self.paths}, "wakeword_model_paths")):
                try:
                    self._model = Model(**kwargs)
                    self.api = label
                    break
                except TypeError as exc:
                    last = f"{type(exc).__name__}: {str(exc)[:70]}"
                    continue
            if self._model is None:
                return False, f"واجهة openWakeWord في هذه النسخة غير متوافقة ({last})."
            for name in self._model.models.keys():
                self.best.setdefault(str(name), 0.0)
            return True, f"محمّل — {len(self._model.models)} نموذجًا (واجهة {self.api})."
        except Exception as exc:  # noqa: BLE001
            self.error = f"{type(exc).__name__}: {str(exc)[:140]}"
            return False, f"تعذّر تحميل نموذج openWakeWord ({self.error})."

    def feed(self, frame: bytes) -> int:
        """يعيد رقم النموذج الذي تجاوز العتبة، أو -1، أو -2 عند خطأ.

        إطار الميكروفون قد يكون أطول من ١٢٨٠ عيّنة المطلوبة، فنقطّعه داخليًا.
        """
        if self._model is None:
            return -2
        np = _np()
        if np is None:
            return -2
        try:
            samples = np.frombuffer(frame, dtype=np.int16)
            if samples.size < 1280:
                return -1
            best_index, best_score = -1, 0.0
            names = list(self._model.models.keys())
            for start in range(0, samples.size - 1279, 1280):
                frame_scores = self._model.predict(samples[start:start + 1280])
                for index, name in enumerate(names):
                    value = float(frame_scores.get(name, 0.0))
                    self.best[name] = max(self.best.get(name, 0.0), value)
                    if value >= self.threshold and value > best_score:
                        best_index, best_score = index, value
            self.frames += 1
            self.last_score = round(best_score, 3)
            return best_index
        except Exception:
            return -2

    def reset(self) -> None:
        try:
            if self._model is not None:
                self._model.reset()
        except Exception:
            pass

    def flush(self) -> list[str]:
        """لا جملة نصّية هنا (كشف مباشر)."""
        return []


class PorcupineEngine:
    """محرّك Porcupine: كشف مباشر للكلمة المفتاحية (أخفّ، ويحتاج مفتاحًا و.ppn)."""

    kind = "porcupine"

    def __init__(self, access_key: str, keyword_paths: Iterable[str]):
        self.access_key = str(access_key or "")
        self.keyword_paths = [str(p) for p in (keyword_paths or []) if str(p).strip()]
        self._engine = None
        self.error = ""
        self.keywords = [Path(p).stem for p in self.keyword_paths]

    def load(self) -> tuple[bool, str]:
        """ينشئ المحرّك. لا يُسجَّل المفتاح في أي سجل — ولا حتى هنا."""
        if self._engine is not None:
            return True, "محمّل."
        if not self.access_key:
            return False, "لا مفتاح Porcupine — ضعه في إعدادات الإضافة (ويُحفظ محليًا فقط)."
        if not self.keyword_paths:
            return False, "لا ملفات .ppn — صدّر كلمتَي التنبيه من منصّة Picovoice وأعطني مساريهما."
        try:
            import pvporcupine  # noqa: PLC0415
            self._engine = pvporcupine.create(access_key=self.access_key,
                                              keyword_paths=self.keyword_paths)
            return True, "محمّل."
        except Exception as exc:  # noqa: BLE001
            self.error = f"{type(exc).__name__}: {str(exc)[:140]}"
            return False, "تعذّر تشغيل Porcupine (تحقّق من المفتاح والملفات والإنترنت للتنشيط)."

    @property
    def frame_length(self) -> int:
        """حجم الإطار المطلوب (عيّنات)."""
        try:
            return int(self._engine.frame_length)
        except Exception:
            return 512

    def feed(self, frame: bytes) -> int:
        """يعيد رقم الكلمة المكتشَفة أو -1 (و-2 عند خطأ)."""
        if self._engine is None:
            return -2
        try:
            pcm = list(memoryview(frame).cast("h")) if frame else []
            needed = self.frame_length
            if len(pcm) < needed:
                return -1
            return int(self._engine.process(pcm[:needed]))
        except Exception:
            return -2

    def reset(self) -> None:
        pass

    def flush(self) -> list[str]:
        """لا جملة تُغلق في Porcupine (كشف مباشر بلا نصّ)."""
        return []


def make_engine(preference: str = "auto", access_key: str = "",
                keyword_paths: Iterable[str] = (), load: bool = True,
                openwake_models: Iterable[str] = (), openwake_phrases: Iterable[str] = (),
                openwake_threshold: float = 0.55) -> tuple[Optional[Any], str]:
    """يختار محرّكًا حسب توفر البيئة. يعيد (محرّك أو None، سبب).

    الترتيب: نموذجك المدرَّب (openWakeWord) إن وُجد ← Porcupine إن وُجد مفتاح ← Vosk العربي.
    `load=False` = فحص جهوزية فقط بلا تحميل النموذج الثقيل في الذاكرة (لعرض الحالة).
    """
    want = str(preference or "auto").strip().lower()
    models = [str(m).strip() for m in (openwake_models or []) if str(m).strip()]
    if want in ("openwake", "auto") and models:
        ok, note = openwake_available()
        if ok:
            engine = OpenWakeEngine(models, openwake_phrases, openwake_threshold)
            label = (f"openWakeWord — {len(models)} نموذجًا مدرَّبًا على عبارتك، "
                     f"عتبة {engine.threshold} (أخفّ بكثير من Vosk: √١ م.ب مقابل ٣١٨ م.ب)")
            if not load:
                return engine, label
            loaded, why = engine.load()
            if loaded:
                return engine, label
            if want == "openwake":
                return None, why
        elif want == "openwake":
            return None, note
    if want in ("porcupine", "auto") and str(access_key or "").strip() and list(keyword_paths or []):
        ok, note = porcupine_available()
        if ok:
            engine = PorcupineEngine(access_key, keyword_paths)
            label = "Porcupine — كلمات من ملفات .ppn، أخفّ على المعالج."
            if not load:
                return engine, label
            loaded, why = engine.load()
            if loaded:
                return engine, label
            return None, why
        if want == "porcupine":
            return None, note
    if want == "porcupine":
        return None, "اخترتَ Porcupine لكن المفتاح أو ملفات .ppn ناقصة."
    ok, note = vosk_available()
    if not ok:
        return None, note
    present, where = model_present()
    if not present:
        return None, f"{where}  (أو انتقل إلى Porcupine بمفتاح مجاني — {PORCUPINE_NOTE})"
    label = "Vosk — عربي، بلا إنترنت وبلا مفاتيح" + (f" ({where})" if where else "")
    engine = VoskEngine()
    if not load:
        return engine, label
    loaded, why = engine.load()
    if not loaded:
        return None, why
    return engine, label


# ── الميكروفون ────────────────────────────────────────────────────────────────
def mic_devices() -> tuple[list[str], str]:
    """قائمة أجهزة الإدخال، أو سبب واضح لعدم توفّرها."""
    try:
        import sounddevice as sd  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        return [], (f"مكتبة الصوت غير متاحة ({type(exc).__name__}). على ويندوز: pip install sounddevice "
                    f"(موجودة أصلًا في متطلبات المشروع).")
    try:
        names = []
        for index, device in enumerate(sd.query_devices()):
            if int(device.get("max_input_channels") or 0) > 0:
                names.append(f"{index}: {device.get('name')}")
        return names, (f"{len(names)} مدخلًا." if names else "لا يوجد ميكروفون مُعرَّف في ويندوز.")
    except Exception as exc:  # noqa: BLE001
        return [], f"تعذّر سؤال أجهزة الصوت ({type(exc).__name__})."


def open_mic(device: Optional[Any] = None, blocksize: int = 4000) -> tuple[Optional[Any], str]:
    """يفتح الميكروفون على 16 ك.هتز أحادي int16. يعيد (stream أو None، سبب)."""
    try:
        import sounddevice as sd  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        return None, f"مكتبة الصوت غير متاحة ({type(exc).__name__}) — لن أستمع بلا ميكروفون."
    index = None
    if device is not None and str(device).strip() != "":
        try:
            index = int(str(device).strip())
        except Exception:
            index = None
    try:
        stream = sd.RawInputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                                   blocksize=max(256, int(blocksize)), device=index)
        stream.start()
        return stream, "الميكروفون مفتوح."
    except Exception as exc:  # noqa: BLE001
        return None, (f"تعذّر فتح الميكروفون ({type(exc).__name__}: {str(exc)[:100]}). "
                      f"جرّب رقم جهاز آخر في الإعدادات، أو أغلق أي برنامج يستخدمه.")


def frame_iter(stream: Any, timeout_s: float = 0.0, stop_event: Optional[threading.Event] = None,
               error_sink: Optional[list] = None) -> Iterable[bytes]:
    """يولّد إطارات int16 من مجرى مفتوح، وينتهي عند انتهاء المدة أو طلب الإيقاف."""
    started = time.time()
    while True:
        if stop_event is not None and stop_event.is_set():
            return
        if timeout_s and time.time() - started >= timeout_s:
            return
        try:
            block, overflowed = stream.read(4000)
        except Exception as exc:  # noqa: BLE001
            if error_sink is not None:
                error_sink.append(f"{type(exc).__name__}: {str(exc)[:100]}")
            return
        del overflowed
        yield bytes(block)


# ── حلقة الإنصات ──────────────────────────────────────────────────────────────
def listen_loop(frames: Iterable[bytes], engine: Any, phrases: Iterable[str], *,
                min_score: float = 0.80, cooldown_s: float = 4.0, timeout_s: float = 0.0,
                stop_event: Optional[threading.Event] = None,
                on_wake: Optional[Callable[[dict], None]] = None,
                on_text: Optional[Callable[[str], None]] = None,
                started_at: Optional[float] = None) -> dict:
    """القلب: إطارات ← محرّك ← مطابقة ← تبريد ← نداء. لا يرمي أبدًا.

    يعيد ملخّصًا: عدد الإشعالات، آخر ما سُمع، المدة، وسبب التوقف.
    """
    phrases = [p for p in (phrases or []) if str(p).strip()]
    gate = Cooldown(cooldown_s)
    started = time.time() if started_at is None else float(started_at)
    summary: dict = {"wakes": [], "heard": [], "seconds": 0.0, "stopped": "", "errors": []}
    recent: list[str] = []   # آخر ما قيل، لأن المحرّك يقطّع العبارة أحيانًا: «يا» ثم «الذين»

    def _match(texts: list[str]) -> dict:
        found: dict = {"hit": False}
        for text in texts:
            if not text:
                continue
            recent.append(text)
            del recent[:-5]
            if on_text is not None:
                try:
                    on_text(text)
                except Exception:
                    pass
            if not found.get("hit"):
                single = match_any(text, phrases, min_score)
                if single.get("hit"):
                    found = single
        if not found.get("hit") and len(recent) > 1:
            found = match_any(" ".join(recent), phrases, min_score)
        return found

    for frame in frames:
        if stop_event is not None and stop_event.is_set():
            summary["stopped"] = "طلب إيقاف"
            break
        if timeout_s and time.time() - started >= timeout_s:
            summary["stopped"] = "انتهت المدة"
            break
        hit: dict = {"hit": False}
        try:
            if getattr(engine, "kind", "") in ("porcupine", "openwake"):
                kind = str(getattr(engine, "kind", "engine"))
                index = engine.feed(frame)
                if index == -2:
                    summary["errors"].append(f"{kind} أعاد خطأً في المعالجة.")
                    continue
                if index >= 0:
                    labels = list(getattr(engine, "keywords", None) or phrases)
                    phrase = labels[index] if index < len(labels) else (phrases[0] if phrases else str(index))
                    score = round(float(getattr(engine, "last_score", 1.0) or 1.0), 3)
                    hit = {"hit": True, "score": score, "how": kind, "phrase": phrase, "window": phrase}
            else:
                texts = engine.feed(frame) or []
                for text in texts:
                    if text:
                        summary["heard"].append(text)
                if len(summary["heard"]) > 40:
                    summary["heard"] = summary["heard"][-40:]
                hit = _match(texts)
        except Exception as exc:  # noqa: BLE001
            summary["errors"].append(f"{type(exc).__name__}: {str(exc)[:100]}")
            continue
        if hit.get("hit") and gate.allow(str(hit.get("phrase") or "?")):
            record = {"phrase": hit.get("phrase"), "score": hit.get("score"), "how": hit.get("how"),
                      "window": hit.get("window"), "at": round(time.time() - started, 2)}
            summary["wakes"].append(record)
            try:
                if getattr(engine, "kind", "") == "vosk":
                    engine.reset()
            except Exception:
                pass
            if on_wake is not None:
                try:
                    on_wake(record)
                except Exception as exc:  # noqa: BLE001
                    summary["errors"].append(f"on_wake: {type(exc).__name__}: {str(exc)[:80]}")
        elif hit.get("hit"):
            try:
                if getattr(engine, "kind", "") == "vosk":
                    engine.reset()
            except Exception:
                pass
    # فرصة أخيرة: نُغلق الجملة عند المحرّك (تجربة حقيقية: عبارة قصيرة قد لا تُعلَن بغير هذا)
    try:
        if getattr(engine, "kind", "") == "vosk" and not (stop_event is not None and stop_event.is_set()):
            tail = engine.flush() or []
            for text in tail:
                if text:
                    summary["heard"].append(text)
            tail_hit = _match(tail)
            # نفس مفتاح التبريد: فلا تُشعل العبارة مرتين (مرة في الحلقة ومرة عند الإغلاق)
            if tail_hit.get("hit") and gate.allow(str(tail_hit.get("phrase") or "?")):
                record = {"phrase": tail_hit.get("phrase"), "score": tail_hit.get("score"),
                          "how": tail_hit.get("how") + "+flush", "window": tail_hit.get("window"),
                          "at": round(time.time() - started, 2)}
                summary["wakes"].append(record)
                if on_wake is not None:
                    try:
                        on_wake(record)
                    except Exception as exc:  # noqa: BLE001
                        summary["errors"].append(f"on_wake: {type(exc).__name__}: {str(exc)[:80]}")
    except Exception as exc:  # noqa: BLE001
        summary["errors"].append(f"flush: {type(exc).__name__}: {str(exc)[:80]}")
    if not summary["stopped"]:
        summary["stopped"] = "انتهت الإطارات"
    summary["seconds"] = round(time.time() - started, 2)
    return summary


def engine_note(engine: Any) -> str:
    """جملة صادقة عن المحرّك الحالي لعرضها للمستخدم."""
    if engine is None:
        return "لا محرّك."
    if getattr(engine, "kind", "") == "openwake":
        loaded, why = engine.load()
        return (f"openWakeWord — {len(engine.paths)} نموذجًا · عتبة {engine.threshold} · "
                + ("محمّل" if loaded else why))
    if getattr(engine, "kind", "") == "porcupine":
        return f"Porcupine — {len(getattr(engine, 'keyword_paths', []))} كلمة مفتاحية من ملفات .ppn."
    present, where = model_present()
    return f"Vosk — {'النموذج جاهز: ' + where if present else 'النموذج ناقص (action=setup)'}"


# ── فحص سريع عند التشغيل المباشر ──────────────────────────────────────────────
def _quick_report() -> None:
    print("_wake_core — مكتبة مشتركة لكلمات التنبيه العربية، لا تعمل وحدها.")
    ok_v, note_v = vosk_available()
    ok_p, note_p = porcupine_available()
    ok_o, note_o = openwake_available()
    print(f"  vosk: {'موجودة' if ok_v else note_v}")
    print(f"  pvporcupine: {'موجودة' if ok_p else note_p}")
    print(f"  openwakeword: {'موجودة' if ok_o else note_o}")
    present, where = model_present()
    print(f"  النموذج العربي: {'جاهز — ' + where if present else where}")
    real = match_phrase("يافا لحل", "يا فالح")
    print(f"  ما سمعه Vosk فعلًا في التجربة: «يافا لحل» ← "
          f"{'مقبولة ✓' if real['hit'] else 'مرفوضة ✗'} ({real['score']:.3f} عبر {real['how']})")
    found = match_phrase("يا فلاح", "يا فالح")
    print(f"  تطبيع «يَافَلَاحْ» ← {ar_normalize('يَافَلَاحْ')!r} · «يا فلاح» ضد «يا فالح» ← "
          f"{'مقبولة' if found['hit'] else 'مرفوضة'} ({found['score']:.3f})")
    print(f"  التكلفة: 0 م.ب إضافية عند غياب النموذج · {VOSK_MODEL['size_mb']} م.ب مرة واحدة عند التجهيز.")


if __name__ == "__main__":
    _quick_report()
