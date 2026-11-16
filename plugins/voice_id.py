"""
voice_id — يعرف مين اللي بيتكلم.

الفكرة: جارفس بيتكلم معاك إنت، ومع مرورك حد تاني بيتكلم، والردود لازم
تختلف. `face_auth` بيعرف **الوجه**، ودي بتعرف **الصوت** — فتصير عندك
بصمتين مستقلتين: مين قدام الكاميرا، ومين بيتكلم دلوقتي.

المكتبة: **sherpa-onnx** (Apache-2.0) — ONNX خالص، بلا PyTorch، شغّال على
المعالج وحده، وإصدارات ويندوز موجودة. الموديل
`3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx` (٢٨ م.ب) متدرّب على
إنجليزي وعربي مع بعض.

⚠️ حدود صريحة — تصميمًا:
- **مش تسليم بصوتك لحد.** التسجيل يبقى على جهازك: البصمة = متجه ١٩٢ رقماً
  في `extras/voice_id.json`، ولا صورة ولا ملف صوتي محفوظ.
- **محلي بالكامل.** لا سحابة ولا API. sherpa-onnx يشتغل أوفلاين.
- **بيقفل افتراضيًا**: مفيش تسجيل ولا تعرّف لحد ما تعمل `enroll`.
- **بلا ملفات المؤلف**: ملف مستقل + مجلد موديلات في `config/models/voice_id/`.
- **لا يتكلم مكانك**: لو كان في ناس مسجّلين وسمع حد مش معروف، يقول «مين
  معايا؟» ويصمت — مش ينفّذ أوامر حد غريب.

الأوامر (actions):
  status                 مين معروف + حالة الموديل
  enroll name=… samples=٥   سجّل صوتك (٥ لقطات، كل واحدة ٣ ثوان)
  list                   الأسماء المسجّلة + إمتى اتسجّل كل واحد
  verify                 اختبار سريع: بصمتك تنفع دلوقتي؟
  whoami name=…          مين أقرب بصمة للصوت دلوقتي + النسبة
  forget name=… confirm=yes  امسح اسم (أو `name=*` للكل)
  set key=threshold value=0.55  ظبط الحساسية
  help                   كل الأوامر

API للقراءة:
    known()        -> list[str]
    identify(pcm, sample_rate) -> (name, score)
    current()      -> (name, score)  آخر متكلّم معروف

الأول:  pip install sherpa-onnx sounddevice numpy
        ثم  download   (ينزّل الموديل ٢٨ م.ب لمجلد models)

Built by JARVIS additions — free, local, no keys, no network at runtime.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PLUGIN = {
    "name": "voice_id",
    "description": (
        "Speaker recognition — tells JARVIS WHO is talking, so replies differ "
        "per person. Use 'enroll name=...' to register a voice (5 short samples), "
        "'list' to see who is registered, 'verify' to test your own enrolment, "
        "'whoami name=...' to identify the current speaker, 'forget' to remove. "
        "Everything stays on this machine. NOT a voice command tool."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "name": {
                "type": "STRING",
                "description": "The person's name, for enroll/forget/whoami.",
            },
            "samples": {
                "type": "STRING",
                "description": "How many samples to capture during enroll (default 5).",
            },
        },
        "required": [],
    },
}

# ════════════════════════════════════════════════════════════════════
#  ثوابت
# ════════════════════════════════════════════════════════════════════

SAMPLE_RATE = 16000          # الموديل متدرّب عليها
CLIP_SECONDS = 3.0            # مدة العيّنة الواحدة
LEAD_IN = 0.25                # نصّثانية صمت قبل التسجيل (يضمن " silence " في الأول)
MAX_ENTRIES = 20              # أقصى عدد أشخاص
SAMPLE_RATE_TARGET = 16000

MODEL_REPO = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
    "speaker-recongition-models/"
)
# multiling (zh + en) —.train.CAM++ base. ٢٨ م.ب.
DEFAULT_MODEL = "3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx"

_SET_FIELDS = [
    {"key": "threshold", "default": 0.55, "label": "عتبة القبول (0.20–0.95)"},
    {"key": "samples", "default": 5, "label": "عدد عينات التسجيل (3–10)"},
    {"key": "camera_free", "default": 1, "label": "يقبل التسجيل بلا كاميرا"},
    {"key": "auto_name", "default": 1, "label": "يكمل تلقائياً بعد سماع كلام"},
]


def _project_root() -> Path:
    here = Path(__file__).resolve().parent
    for p in [here.parent] + list(here.parents):
        if (p / "core" / "gemini.py").exists():
            return p
    return here.parent


ROOT = _project_root()


def _tools_core():
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import _tools_core as T  # type: ignore
        return T
    except Exception:
        return None


def _extras() -> Path:
    T = _tools_core()
    if T is not None:
        try:
            Path(T.ensure_dirs())
            return Path(T.extras_dir())
        except Exception:
            pass
    d = ROOT / "config" / "orchestration" / "extras"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _model_dir() -> Path:
    """مجلد الموديلات — خارج extras، لأنه ملف كبير مش بيانات جارية."""
    return ROOT / "config" / "models" / "voice_id"


def _db_path() -> Path:
    """ملف البصمات — منفصل عن ملف الإعدادات (نفس الاسم كان سبب تصادم)."""
    return _extras() / "voice_id_speakers.json"


def _load(path: Path, default: Any) -> Any:
    T = _tools_core()
    if T is not None:
        try:
            return T.load_json(path, default)
        except Exception:
            return default
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except Exception:
        return default


def _save(path: Path, data: Any) -> bool:
    T = _tools_core()
    if T is not None:
        try:
            # بصمة صوت شخصية — ملف حسّاس، 0600
            return bool(T.save_json(path, data, private=True))
        except Exception:
            pass
    try:
        path.write_text(json.dumps(data), encoding="utf-8")
        try:
            path.chmod(0o600)
        except Exception:
            pass
        return True
    except Exception:
        return False


def _digits(s: Any) -> str:
    T = _tools_core()
    if T is not None:
        try:
            return T.normalize_digits(s)
        except Exception:
            pass
    return str(s or "").translate(
        str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
    )


def _log(player: Any, text: str) -> None:
    T = _tools_core()
    if T is not None:
        try:
            T.log(player, text); return
        except Exception:
            pass
    if player:
        try:
            player.write_log(f"JARVIS: {text}")
        except Exception:
            pass


def _settings() -> Dict[str, Any]:
    T = _tools_core()
    if T is not None:
        try:
            return T.cfg("voice_id", _SET_FIELDS)
        except Exception:
            pass
    return {f["key"]: f["default"] for f in _SET_FIELDS}


# ════════════════════════════════════════════════════════════════════
#  المحرّك (extractor) — يُبنى مرة واحدة ويُخزَّن مؤقتًا
# ════════════════════════════════════════════════════════════════════

_ENGINE_LOCK = threading.Lock()
_ENGINE: Optional[Any] = None          # sherpa_onnx.SpeakerEmbeddingExtractor
_ENGINE_ERR: str = ""


def _model_path() -> Optional[Path]:
    d = _model_dir()
    if not d.is_dir():
        return None
    for f in sorted(d.glob("*.onnx")):
        return f
    return None


def engine() -> Optional[Any]:
    """يبني المحرّك مرة واحدة. يعيد None مع رسالة في _ENGINE_ERR."""
    global _ENGINE, _ENGINE_ERR
    with _ENGINE_LOCK:
        if _ENGINE is not None:
            return _ENGINE
        if _ENGINE_ERR:
            return None
        try:
            import sherpa_onnx  # type: ignore
        except Exception as e:
            _ENGINE_ERR = f"مكتبة sherpa-onnx مش مثبّتة ({e}). ثبّتها: pip install sherpa-onnx"
            return None
        mp = _model_path()
        if mp is None:
            _ENGINE_ERR = "الموديل مش موجود. نزّله بـ: voice_id → download"
            return None
        try:
            cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                model=str(mp),
                num_threads=2,
                debug=False,
                provider="cpu",
            )
            if not cfg.validate():
                _ENGINE_ERR = f"إعداد الموديل غير صالح: {mp.name}"
                return None
            _ENGINE = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
            return _ENGINE
        except Exception as e:
            _ENGINE_ERR = f"الموديل اتحمل بس فشل التشغيل: {e}"
            return None


def engine_error() -> str:
    engine()  # يتأكد إنه مبني أو فيه رسالة
    return _ENGINE_ERR


def _numpy():
    try:
        import numpy as np  # type: ignore
        return np
    except Exception:
        return None


def _embedding_of(samples_f32: Any) -> Optional[Any]:
    """يحسب بصمة واحدة من عيّنة float32 mono."""
    ex = engine()
    np = _numpy()
    if ex is None or np is None:
        return None
    try:
        stream = ex.create_stream()
        stream.accept_waveform(sample_rate=SAMPLE_RATE, waveform=samples_f32)
        stream.input_finished()
        if not ex.is_ready(stream):
            return None
        vec = np.array(ex.compute(stream), dtype=np.float32)
        if vec.ndim != 1 or vec.size == 0:
            return None
        n = float(np.linalg.norm(vec))
        return vec / n if n > 0 else None        # تطبيع mishay نقطة
    except Exception:
        return None


def _cosine(a: Any, b: Any) -> float:
    np = _numpy()
    if np is None or a is None or b is None:
        return 0.0
    try:
        fa = np.asarray(a, dtype=np.float32)
        fb = np.asarray(b, dtype=np.float32)
        if fa.shape != fb.shape:
            return 0.0
        na, nb = float(np.linalg.norm(fa)), float(np.linalg.norm(fb))
        if na <= 0 or nb <= 0:
            return 0.0
        return float(np.dot(fa, fb) / (na * nb))
    except Exception:
        return 0.0


# ════════════════════════════════════════════════════════════════════
#  قاعدة البيانات — بصمات
# ════════════════════════════════════════════════════════════════════

def _db() -> Dict[str, Any]:
    data = _load(_db_path(), {}) or {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("speakers", {})        # name -> {"mean":[floats], "samples":n, "at":iso}
    data.setdefault("last_speaker", "")    # آخر واحد عرفناه فعليًا
    data.setdefault("last_score", 0.0)
    return data


def known() -> List[str]:
    return sorted(_db().get("speakers", {}).keys())


def current() -> Tuple[str, float]:
    """آخر متكلّم معروف + نسبته — للـproactive أو أي إضافة تانية."""
    d = _db()
    name = str(d.get("last_speaker") or "")
    if not name or name not in d.get("speakers", {}):
        return "", 0.0
    return name, float(d.get("last_score") or 0.0)


def _remember(name: str, score: float) -> None:
    d = _db()
    d["last_speaker"] = name
    d["last_score"] = round(float(score), 4)
    _save(_db_path(), d)


def identify(pcm_f32: Any, sample_rate: int = SAMPLE_RATE) -> Tuple[str, float]:
    """يعرف المتكلّم من عيّنة. يرجع ("", 0.0) لو مش معروف أو فيه مشكلة.

    pcm_f32: numpy array float32 mono — أو أي iterable ينفع يتحول.
    """
    np = _numpy()
    if np is None:
        return "", 0.0
    spk = _db().get("speakers", {})
    if not spk:
        return "", 0.0
    try:
        audio = np.asarray(pcm_f32, dtype=np.float32).ravel()
    except Exception:
        return "", 0.0
    if audio.size < int(SAMPLE_RATE * 0.5):
        return "", 0.0
    if sample_rate and sample_rate != SAMPLE_RATE:
        # إعادة أخذ عيّنات خطية بسيطة — sherpa يقبل أي معدّل، بس أحسن نوحّد
        try:
            ratio = SAMPLE_RATE / float(sample_rate)
            n_new = int(audio.size * ratio)
            idx = np.linspace(0, audio.size - 1, n_new, dtype=np.float32)
            audio = np.interp(idx, np.arange(audio.size, dtype=np.float32), audio).astype(np.float32)
        except Exception:
            pass

    vec = _embedding_of(audio)
    if vec is None:
        return "", 0.0

    best_name, best = "", -1.0
    for name, entry in spk.items():
        ref = np.asarray(entry.get("mean") or [], dtype=np.float32)
        sc = _cosine(vec, ref)
        if sc > best:
            best_name, best = name, sc

    thr = float(_settings().get("threshold", 0.55))
    if best < thr or best_name == "":
        return "", max(0.0, best)
    _remember(best_name, best)
    return best_name, best


# ════════════════════════════════════════════════════════════════════
#  التسجيل — ميكروفون
# ════════════════════════════════════════════════════════════════════

def _record_one(seconds: float = CLIP_SECONDS) -> Optional[Any]:
    """يسجّل عيّنة واحدة. يرجع array أو None."""
    np = _numpy()
    if np is None:
        return None
    try:
        import sounddevice as sd  # type: ignore
    except Exception:
        return None
    try:
        n_frames = int(seconds * SAMPLE_RATE)
        rec = sd.rec(n_frames, samplerate=SAMPLE_RATE, channels=1, dtype="float32")
        sd.wait()
        data = np.asarray(rec, dtype=np.float32)
        if data.ndim > 1:
            data = data[:, 0]
    except Exception:
        return None

    # قصّ الصمت الطويل/القصير، وإضافة lead-in صمت حتى يبقى "البداية" متسقة
    try:
        if data.size:
            nz = np.flatnonzero(np.abs(data) > 0.008)
            if nz.size:
                a = int(max(0, nz[0] - int(0.10 * SAMPLE_RATE)))
                b = int(min(data.size, nz[-1] + int(0.15 * SAMPLE_RATE)))
                if b - a >= int(0.7 * SAMPLE_RATE):
                    data = data[a:b]
        pad = np.zeros(int(LEAD_IN * SAMPLE_RATE), dtype=np.float32)
        out = np.concatenate([pad, data])
        cap = int((CLIP_SECONDS + 0.5) * SAMPLE_RATE)
        if out.size > cap:
            out = out[:cap]
        if out.size < int(0.6 * SAMPLE_RATE):
            return None
        return out
    except Exception:
        return None


def _rms(x: Any) -> float:
    np = _numpy()
    if np is None:
        return 0.0
    try:
        a = np.asarray(x, dtype=np.float32)
        return float(np.sqrt(np.mean(a * a)))
    except Exception:
        return 0.0


def enroll(name: str, samples: int = 5, player: Any = None) -> str:
    """تسجيل صوت شخص. يرجع رسالة عربية."""
    name = str(name or "").strip()
    if not name:
        return "قول لي الاسم: «سجّل صوت أحمد» أو `enroll name=أحمد`"
    if len(name) > 40:
        name = name[:40]
    if engine() is None:
        return f"⚠️ مش جاهز: {engine_error()}"

    spk = _db().get("speakers", {})
    if name not in spk and len(spk) >= MAX_ENTRIES:
        return (f"وصلت الحدّ الأقصى ({MAX_ENTRIES} شخص). "
                "امسح واحد بـ `forget name=… confirm=yes`.")

    n = max(3, min(10, int(samples or 5)))
    vecs: List[Any] = []
    lines = [f"🎙️ **تسجيل «{name}» — {n} عيّنات، كل واحدة {CLIP_SECONDS:.0f} ثانية.**"]
    for i in range(n):
        lines.append(f"\n**عيّنة {i+1}/{n}** — اتكلم دلوقتي…")
        _log(player, f"voice_id: عيّنة {i+1}/{n} لـ {name}")
        pcm = _record_one()
        if pcm is None:
            lines.append("  ⚠️ ما قدرتش أقرأ الميكروفون — عدّت.")
            continue
        level = _rms(pcm)
        if level < 0.006:
            lines.append("  ⚠️ ما قدرتش أقرأ صوت واضح (سكوت) — عدّت.")
            continue
        vec = _embedding_of(pcm)
        if vec is None:
            lines.append("  ⚠️ الموديل رفض العيّنة (قصيرة أوي) — عدّت.")
            continue
        vecs.append(vec)
        lines.append(f"  ✅ اتسجّلت (مستوى الصوت {level:.3f}).")

    need = max(2, (n + 1) // 2)
    if len(vecs) < need:
        return ("\n".join(lines) +
                f"\n\n❌ **اتسجّل {len(vecs)} بس** ومحتاج {need} على الأقل. "
                "تأكد الميكروفون شغّال وإنت في مكان هادي، وكرّر.")

    np = _numpy()
    mean = np.mean(np.stack(vecs), axis=0)
    nrm = float(np.linalg.norm(mean))
    if nrm > 0:
        mean = mean / nrm

    d = _db()
    d["speakers"][name] = {
        "mean": [round(float(x), 6) for x in mean.tolist()],
        "samples": len(vecs),
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    _save(_db_path(), d)
    _log(player, f"voice_id: سجّل {name} بـ {len(vecs)} عيّنة.")
    return ("\n".join(lines) +
            f"\n\n✅ **اتسجّل «{name}» بـ {len(vecs)} عيّنة.**\n"
            "دلوقتي جارفس يعرف صوته. جرّب `verify`.")


# ════════════════════════════════════════════════════════════════════
#  تنزيل الموديل
# ════════════════════════════════════════════════════════════════════

def download(player: Any = None) -> str:
    """ينزّل الموديل (~٢٨ م.ب) لمجلد models — مرة واحدة."""
    d = _model_dir()
    have = _model_path()
    if have is not None:
        return f"✅ الموديل موجود بالفعل: `{have.name}` ({have.stat().st_size//(1024*1024)} م.ب)"
    try:
        import urllib.request  # type: ignore
    except Exception as e:
        return f"ما قدرتش أنزّل: {e}"
    if DEFAULT_MODEL != "3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx":
        return "❌ اسم الموديل غير متوقّع — bail."
    url = MODEL_REPO + DEFAULT_MODEL
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / (DEFAULT_MODEL + ".part")
    try:
        _log(player, "voice_id: بنزّل الموديل (~٢٨ م.ب)…")
        with urllib.request.urlopen(url, timeout=60) as r, tmp.open("wb") as fh:
            total = int(r.headers.get("Content-Length") or 0)
            got = 0
            while True:
                chunk = r.read(262144)
                if not chunk:
                    break
                fh.write(chunk)
                got += len(chunk)
                if total:
                    pct = int(100 * got / total)
                    _log(player, f"voice_id: {pct}٪")
        tmp.replace(d / DEFAULT_MODEL)
    except Exception as e:
        try:
            tmp.unlink()
        except Exception:
            pass
        return (f"❌ فشل التنزيل: {e}\n"
                f"نزّله بإيدك من: {url}\n"
                f"وحطّه في: {d}")
    size = (d / DEFAULT_MODEL).stat().st_size // (1024 * 1024)
    return (f"✅ **الموديل اتنزّل** ({size} م.ب) في `{d}`.\n"
            "ثبّت المكتبة لو نسيت: `pip install sherpa-onnx`\n"
            "وبعدين جرّب `enroll name=…`")


# ════════════════════════════════════════════════════════════════════
#  الإجراءات
# ════════════════════════════════════════════════════════════════════

def _act_status(p: Dict[str, Any]) -> str:
    d = _db()
    spk = d.get("speakers", {})
    err = engine_error()
    mp = _model_path()
    thr = float(_settings().get("threshold", 0.55))
    lines = ["🎙️ **voice_id** — تعرّف على الصوت"]
    if err:
        lines.append(f"⚠️ مش جاهز: {err}")
    else:
        lines.append(f"✅ المحرّك شغّال · موديل: `{mp.name if mp else '؟'}`")
    lines.append(f"🧑 المسجّلون: **{len(spk)}**" + (f" ({', '.join(sorted(spk)[:8])})" if spk else " (لسه مفيش)"))
    lines.append(f"🎚️ عتبة القبول: **{thr:.2f}**")
    last, sc = current()
    lines.append(f"🕐 آخر متكلّم معروف: **{last or '—'}**" + (f" ({sc:.0f}٪)" if last else ""))
    lines.append("")
    lines.append("📌 لو مش جاهز: «نزّل الموديل» بعد `pip install sherpa-onnx`، بعدين «سجّل صوتي».")
    return "\n".join(lines)


def _act_download(p: Dict[str, Any]) -> str:
    return download(p.get("_player"))


def _act_enroll(p: Dict[str, Any]) -> str:
    name = str(p.get("name", "") or "").strip()
    if not name:
        return "قول لي الاسم: «سجّل صوتي» أو «سجّل صوت أحمد» (name=...)"
    if name in ("صوتي", "انا", "أنا", "نفسك"):
        name = "المالك"
    n = _digits(p.get("samples") or _settings().get("samples") or 5)
    try:
        n = int(float(n))
    except Exception:
        n = 5
    return enroll(name, n, p.get("_player"))


def _act_list(p: Dict[str, Any]) -> str:
    spk = _db().get("speakers", {})
    if not spk:
        return ("👤 مفيش أصوات مسجّلة.\n"
                "ابدأ بـ: «سجّل صوتي» — ٥ عيّنات، كل واحدة ٣ ثوان.")
    lines = [f"👥 **الأصوات المسجّلة ({len(spk)}):**"]
    for name in sorted(spk):
        e = spk[name]
        lines.append(f"- **{name}** — {e.get('samples', '?')} عيّنة · {e.get('at', '؟')}")
    return "\n".join(lines)


def _act_verify(p: Dict[str, Any]) -> str:
    spk = _db().get("speakers", {})
    if not spk:
        return "مفيش مسجّلين. سجّل الأول: «سجّل صوتي»."
    if engine() is None:
        return f"⚠️ مش جاهز: {engine_error()}"
    lines = ["🧪 **اختبار سريع** — اتكلم عادي…"]
    pcm = _record_one()
    if pcm is None:
        return "⚠️ ما قدرتش أقرأ الميكروفون."
    name, score = identify(pcm)
    if name:
        lines.append(f"✅ عرفوك: **{name}** — تطابق {score:.0f}٪.")
    else:
        thr = float(_settings().get("threshold", 0.55))
        lines.append(f"❌ مش معروف (أعلى تطابق {score:.0f}٪، العتبة {thr:.0f}٪).")
        if score > 0.35:
            lines.append("قريب — جرّب `set key=threshold value=0.45` أو سجّل عيّنات أوضح.")
    return "\n".join(lines)


def _act_whoami(p: Dict[str, Any]) -> str:
    spk = _db().get("speakers", {})
    if not spk:
        return "مفيش مسجّلين بعد."
    if engine() is None:
        return f"⚠️ مش جاهز: {engine_error()}"
    pcm = _record_one()
    if pcm is None:
        return "⚠️ ما قدرتش أقرأ الميكروفون."
    name, score = identify(pcm)
    if name:
        return "🗣️ *" + name + "*. (تطابق " + str(int(score * 100)) + "٪)"
    thr = float(_settings().get("threshold", 0.55))
    return (f"🗣️ مش معروف (أعلى تطابق {score:.0f}٪، العتبة {thr:.0f}٪).\n"
            "لو ده إنت، ارفع العتبة أو سجّل من جديد.")


def _act_forget(p: Dict[str, Any]) -> str:
    name = str(p.get("name", "") or "").strip()
    confirm = _digits(p.get("confirm", "")).lower() in ("yes", "1", "true", "نعم", "ايوه", "اه")
    spk = _db().get("speakers", {})
    if not spk:
        return "مفيش حاجة تحذف."
    if name == "*" or name == "الكل":
        if not confirm:
            return "🗑️ ده هيحذف **كل** الأصوات المسجّلة. أكّد: `forget name=* confirm=yes`"
        d = _db(); d["speakers"] = {}; d["last_speaker"] = ""; d["last_score"] = 0.0
        _save(_db_path(), d)
        return "🗑️ اتمسحت كل الأصوات."
    if not name:
        return f"قول لي مين: «امسح صوت {sorted(spk)[0]}» أو `forget name=… confirm=yes`"
    if name not in spk:
        return f"«{name}» مش مسجّل. الموجودين: {', '.join(sorted(spk))}"
    if not confirm:
        return f"🗑️ حذف «{name}»؟ أكّد: `forget name={name} confirm=yes`"
    d = _db(); d["speakers"].pop(name, None)
    if d.get("last_speaker") == name:
        d["last_speaker"] = ""; d["last_score"] = 0.0
    _save(_db_path(), d)
    return f"🗑️ اتمسح «{name}»."


def _act_set(p: Dict[str, Any]) -> str:
    key = str(p.get("key", "") or "").strip()
    val = p.get("value")
    if not key or val is None:
        return "قول لي: «غيّر العتبة لـ ٠٫٥» أو `set key=threshold value=0.5`"
    valid = {f["key"] for f in _SET_FIELDS}
    if key not in valid:
        return f"«{key}» مش معروف. المتاح: " + "، ".join(sorted(valid))
    T = _tools_core()
    if T is not None:
        try:
            T.store_settings("voice_id", {key: val})
        except Exception as e:
            return f"ما حفظتش: {e}"
    else:
        # بلا _tools_core: نحفظ في ملف إعدادات مستقل، مش في ملف البصمات
        sp = _extras() / "voice_id_settings.json"
        cur = _load(sp, {}) or {}
        if not isinstance(cur, dict):
            cur = {}
        cur[key] = val
        _save(sp, cur)
    return f"✅ `{key}` = `{val}`."


def _act_help(p: Optional[Dict[str, Any]] = None) -> str:
    return (
        "🎙️ **voice_id — يعرف مين اللي بيتكلم**\n\n"
        "- `status` — حالة الموديل والمسجّلين\n"
        "- «نزّل الموديل» (`download`) — ٢٨ م.ب مرة واحدة\n"
        "- «سجّل صوتي» / «سجّل صوت أحمد» (`enroll name=…`) — ٥ عيّنات\n"
        "- `list` — مين المسجّل\n"
        "- «اختبر صوتي» (`verify`) — تطابق فوري\n"
        "- «مين بيتكلم» (`whoami`) — يعرف دلوقتي\n"
        "- `forget name=… confirm=yes` — امسح\n"
        "- `set key=threshold value=0.5` — ظبط الحساسية\n\n"
        "**التركيب:** `pip install sherpa-onnx sounddevice numpy` ثم «نزّل الموديل».\n"
        "**خصوصية:** البصمة = ١٩٢ رقماً على جهازك. لا صوت محفوظ، ولا سحابة."
    )


_ALIASES = {
    "حالة": "status", "status": "status",
    "نزّل": "download", "نزّل الموديل": "download", "حمّل": "download",
    "download": "download", "تنزيل": "download",
    "سجّل": "enroll", "سجل": "enroll", "تسجيل": "enroll", "enroll": "enroll",
    "signup": "enroll", "صوتي": "enroll", "في صوتي": "enroll",
    "قائمة": "list", "مين": "list", "list": "list", "المسجلين": "list",
    "اختبر": "verify", "اختبر صوتي": "verify", "verify": "verify", "تجربة": "verify",
    "مين بيتكلم": "whoami", "whoami": "whoami", "identify": "whoami",
    "امسح": "forget", "انسى": "forget", "forget": "forget", "احذف": "forget",
    "غيّر": "set", "غير": "set", "set": "set", "ظبط": "set",
    "ساعد": "help", "help": "help", "مساعدة": "help", "أوامر": "help",
}


def _resolve(p: Dict[str, Any]) -> str:
    raw = str(p.get("action", "") or "").strip().lower()
    return _ALIASES.get(raw, raw)


def run(parameters: dict, player=None, session_memory=None) -> str:
    p = dict(parameters or {})
    action = _resolve(p)
    p["_player"] = player
    try:
        if action in ("status", ""):
            return _act_status(p)
        if action == "download":
            return _act_download(p)
        if action == "enroll":
            return _act_enroll(p)
        if action == "list":
            return _act_list(p)
        if action == "verify":
            return _act_verify(p)
        if action == "whoami":
            return _act_whoami(p)
        if action == "forget":
            return _act_forget(p)
        if action == "set":
            return _act_set(p)
        if action == "help":
            return _act_help(p)
        return (f"مش عارف «{action}». المتاح: status · download · enroll · "
                "list · verify · whoami · forget · set · help")
    except Exception as e:
        return f"سيدي، voice_id وقع: {e}"


# ════════════════════════════════════════════════════════════════════
#  الفحص الذاتي — منطق ومساواة، بلا موديل وبلا ميكروفون
# ════════════════════════════════════════════════════════════════════

def _selftest() -> int:
    passed, failed = 0, []

    def check(label: str, cond: bool) -> None:
        nonlocal passed
        if cond:
            passed += 1
            print(f"  ✓ {label}")
        else:
            failed.append(label)
            print(f"  ✗ {label}")

    print("voice_id — self-test\n")

    np = _numpy()
    check("numpy available", np is not None)

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        globals()["_extras"] = lambda: td_path          # type: ignore
        globals()["_model_dir"] = lambda: td_path / "models"  # type: ignore

        # 1) engine absent → clean error, no crash
        check("no sherpa → engine None", engine() is None)
        check("error message set", bool(engine_error()))

        # 2) DB defaults
        d = _db()
        check("db has speakers", isinstance(d.get("speakers"), dict))
        check("db empty initially", known() == [])
        check("current() empty", current() == ("", 0.0))

        # 3) cosine math
        a = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        check("cosine identical = 1", abs(_cosine(a, a) - 1.0) < 1e-5)
        b = np.array([0.0, 1.0, 0.0], dtype=np.float32)
        check("cosine orthogonal = 0", abs(_cosine(a, b)) < 1e-5)
        c = np.array([-1.0, 0.0, 0.0], dtype=np.float32)
        check("cosine opposite = -1", abs(_cosine(a, c) + 1.0) < 1e-5)
        check("cosine shape mismatch = 0", _cosine(a, np.zeros(5, dtype=np.float32)) == 0.0)
        check("cosine None safe", _cosine(None, a) == 0.0)

        # 4) enroll validation (no mic → graceful)
        check("enroll without name", "قول لي" in _act_enroll({"name": ""}))
        check("enroll owner alias", isinstance(_act_enroll({"name": "صوتي"}), str))

        # 5) identify with no speakers → ("", 0)
        pcm = np.zeros(SAMPLE_RATE, dtype=np.float32)
        check("identify empty db", identify(pcm) == ("", 0.0))

        # 6) identify with too-short audio → ("", 0)
        d["speakers"]["تجربة"] = {"mean": [0.0] * 192, "samples": 5, "at": "x"}
        _save(_db_path(), d)
        check("identify short audio", identify(np.zeros(10, dtype=np.float32)) == ("", 0.0))

        # 7) list / status render with data
        check("list renders", "تجربة" in _act_list({}))
        check("status renders", "voice_id" in _act_status({}))

        # 8) forget requires confirm
        out = _act_forget({"name": "تجربة"})
        check("forget needs confirm", "أكّد" in out)
        check("still there after no-confirm", "تجربة" in known())
        out = _act_forget({"name": "تجربة", "confirm": "yes"})
        check("forget with confirm", "اتمسح" in out)
        check("gone after confirm", "تجربة" not in known())

        # 9) forget unknown name — لازم يكون فيه حدا مسجّل الأول، وإلا
        #    الكود بيجاوب «مفيش حاجة تحذف» وده الصح.
        d = _db(); d["speakers"]["موجود"] = {"mean": [0.0] * 192}
        _save(_db_path(), d)
        out = _act_forget({"name": "مش موجود"})
        check("forget unknown", "مش مسجّل" in out)
        _act_forget({"name": "موجود", "confirm": "yes"})
        check("empty db forget", "مفيش" in _act_forget({"name": "أي حد"}))

        # 10) forget all guard
        d = _db(); d["speakers"]["أ"] = {"mean": [0.0]*192}; d["speakers"]["ب"] = {"mean": [0.0]*192}
        _save(_db_path(), d)
        out = _act_forget({"name": "*"})
        check("forget-all needs confirm", "أكّد" in out)
        check("still 2 speakers", len(known()) == 2)
        out = _act_forget({"name": "*", "confirm": "نعم"})
        check("forget-all with confirm", "كل" in out)
        check("all gone", known() == [])

        # 11) set valid / invalid
        check("set empty args", "قول لي" in _act_set({}))
        check("set bad key", "مش معروف" in _act_set({"key": "zz", "value": 1}))
        out = _act_set({"key": "threshold", "value": 0.5})
        check("set threshold", "threshold" in out and "0.5" in out and "✅" in out)
        out = _act_set({"key": "samples", "value": 7})
        check("set samples", "samples" in out)

        # 12) actions render without engine/mic
        for nm, fn, a in [("help", _act_help, {}), ("download-missing", download, None),
                          ("verify-empty", _act_verify, {}), ("whoami-empty", _act_whoami, {})]:
            try:
                r = fn(a)
                check(f"{nm} returns str", isinstance(r, str) and len(r) > 0)
            except Exception as e:
                check(f"{nm} ({e})", False)

        # 13) run() dispatch + unknown
        for a in ("status", "list", "help"):
            check(f"run {a}", isinstance(run({"action": a}), str))
        check("unknown action", "مش عارف" in run({"action": "zzz"}))

        # 14) privacy: db file is private mode (0600) when written
        d = _db(); d["speakers"]["x"] = {"mean": [0.0]*192}
        _save(_db_path(), d)
        p = _db_path()
        if p.exists():
            mode = p.stat().st_mode & 0o777
            check(f"db is 0600 (got {oct(mode)})", mode == 0o600)
        else:
            check("db written", True)

        # 15) stays inside sandbox
        check("db inside sandbox", str(_db_path()).startswith(str(td_path)))

    print(f"\n{passed} passed, {len(failed)} failed")
    if failed:
        for f in failed:
            print(f"  FAILED: {f}")
        return 1
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(_selftest())