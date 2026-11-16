"""sign_language.py — لغة الإشارة لجارفس: يقرأ إشاراتك ويتعلّمها منك ويعلمك إياها.

الفكرة: **أنت** تعلّمه الإشارة (كلمة شائعة مثل «ماء» أو «مساعدة») بأخذ بضع لقطات
من كاميرتك، ثم يقرأها بعد ذلك ويعيدها كلمة منطوقة. لا نماذج جاهزة تُنزَّل، ولا
بيانات تخرج من جهازك، ولا مفاتيح.

محرّكان:
  • mediapipe (الأدق): ٢١ نقطة ليدك ← بصمة ٥١ رقمًا ثابتة ضد الانتقال والدوران والحجم.
  • بديل بـ OpenCV فقط: كشف الجلد + أكبر كتلة ← بصمة ١٤ رقمًا (شكل تقريبي).
كل محرّك يُسجّل بصماته باسمه، ولا تُخلط بصمات محرّكين — والإضافة تقول ذلك صراحة.
"""

from __future__ import annotations

import importlib.util
import json
import math
import os
import random
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

PLUGIN = {
    "name": "sign_language",
    "description": (
        "Local sign-language reader and coach. It learns a sign from you (a few camera "
        "samples) and then recognises it and can speak the word. Fully local, no cloud, "
        "no downloaded model, nothing leaves the machine. "
        "Use for: 'read my sign', 'what sign is this', 'teach the sign for water', "
        "'learn the sign for help', 'practice my signs', 'quiz me on signs', "
        "'which signs do you know', 'delete the sign for water', 'sign language status'. "
        "First teach a word with action=learn word=... (the user holds the sign in front of "
        "the camera for a couple of seconds), then action=read recognises it. "
        "Practice is two steps: mode=ask gives a word, mode=check verifies the sign. "
        "Signs must be HELD STILL (static hand shapes); moving signs are not supported. "
        "Needs a webcam. Best accuracy with 'mediapipe' installed; otherwise it falls back "
        "to a coarser OpenCV skin-shape reader and says so."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "read | learn | practice | list | delete | set | status | help"},
            "word": {"type": "STRING", "description": "learn/practice/delete: the word this sign means, e.g. 'ماء' or 'help'"},
            "mode": {"type": "STRING", "description": "practice: ask (default — sets the word to make) | check (verifies the sign now)"},
            "samples": {"type": "STRING", "description": "learn: how many camera samples to take (4–12, default 8)"},
            "key": {"type": "STRING", "description": "set: threshold | samples_per_sign | camera_index | speak | engine"},
            "value": {"type": "STRING", "description": "set: the new value"},
            "speak": {"type": "STRING", "description": "read: yes → also say the recognised word out loud"},
            "confirm": {"type": "STRING", "description": "delete/replace/learn-over-existing: yes — required"},
        },
    },
}

PLUGIN_SETTINGS = {
    "namespace": "sign_language",
    "fields": [
        {"key": "threshold", "type": "text", "label": "Match threshold 0.50–0.99", "default": "0.90"},
        {"key": "margin", "type": "text", "label": "Required gap over the runner-up 0.00–0.30", "default": "0.05"},
        {"key": "samples_per_sign", "type": "text", "label": "Samples per sign 4–12", "default": "8"},
        {"key": "camera_index", "type": "text", "label": "Camera index (0, 1, …)", "default": "0"},
        {"key": "speak", "type": "choice", "label": "Speak the recognised word", "default": "yes", "choices": ["yes", "no"]},
        {"key": "engine", "type": "choice", "label": "Reader engine", "default": "auto", "choices": ["auto", "mediapipe", "fallback"]},
        {"key": "max_words", "type": "text", "label": "Maximum learned words", "default": "40"},
    ],
}

T: Optional[Any] = None
MODEL_FILE = "sign_language.json"
PENDING: dict = {"word": ""}


# ────────────────────────────────────────────────────────────── النواة المشتركة
def _boot() -> None:
    """يحمّل النواة المساعدة (`_tools_core.py`) مرة واحدة — بلا انهيار إن غابت."""
    global T
    name = "_jarvis_tools_core"
    if name in sys.modules:
        T = sys.modules[name]
        return
    path = Path(__file__).resolve().parent / "_tools_core.py"
    if not path.exists():
        return
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        return
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        T = module
    except Exception:
        sys.modules.pop(name, None)
        T = None


_boot()


def _clean(value: Any, limit: int = 40) -> str:
    """ينظّف نصًا من المستخدم: بلا أسطر جديدة، وبطول محدود."""
    text = " ".join(str(value or "").split())
    return text[:limit]


def _num(raw: Any, default: float, low: float, high: float) -> float:
    """يحوّل نصًا إلى رقم داخل حدود، ويعيد الافتراضي عند أي فشل."""
    try:
        value = float(str(raw).strip().replace("٬", "").replace(",", ""))
    except Exception:
        return default
    if not math.isfinite(value):
        return default
    return min(high, max(low, value))


def _cfg() -> dict:
    fields = PLUGIN_SETTINGS["fields"]
    if T is None:
        return {f["key"]: f.get("default") for f in fields}
    try:
        return T.cfg(PLUGIN_SETTINGS["namespace"], fields)
    except Exception:
        return {f["key"]: f.get("default") for f in fields}


def _store(values: dict) -> bool:
    if T is None:
        return False
    try:
        return bool(T.store_settings(PLUGIN_SETTINGS["namespace"], values))
    except Exception:
        return False


def _log(action: str, status: str = "ok", detail: str = "") -> None:
    if T is None:
        return
    try:
        T.log_event("sign_language", action, status, 0, detail[:120])
    except Exception:
        pass


def _extras() -> Optional[Path]:
    if T is None:
        return None
    try:
        return Path(T.extras_dir())
    except Exception:
        return None


def _model_path() -> Optional[Path]:
    base = _extras()
    return (base / MODEL_FILE) if base else None


def _load_model() -> dict:
    """يقرأ ملف الإشارات؛ أي تلف أو غياب = نموذج فارغ صالح (لا انهيار)."""
    empty = {"version": 1, "engine": "", "words": {}, "stats": {}}
    path = _model_path()
    if path is None or not path.exists():
        return empty
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return empty
    if not isinstance(data, dict):
        return empty
    words = data.get("words")
    if not isinstance(words, dict):
        words = {}
    clean = {}
    for name, entry in words.items():
        if not isinstance(entry, dict):
            continue
        samples = entry.get("samples")
        if not isinstance(samples, list) or not samples:
            continue
        good = [s for s in samples if isinstance(s, list) and len(s) >= 8
                and all(isinstance(v, (int, float)) and math.isfinite(float(v)) for v in s)]
        if good:
            clean[_clean(name, 30)] = {"samples": good[:12], "updated": str(entry.get("updated") or "")}
    stats = data.get("stats") if isinstance(data.get("stats"), dict) else {}
    return {
        "version": 1,
        "engine": str(data.get("engine") or ""),
        "words": clean,
        "stats": {k: v for k, v in stats.items() if isinstance(v, dict)},
    }


def _save_model(model: dict) -> bool:
    path = _model_path()
    if path is None:
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(model, ensure_ascii=False)
        if T is not None and hasattr(T, "save_json"):
            return bool(T.save_json(path, model, private=True))
        path.write_text(payload, encoding="utf-8")
        return True
    except Exception:
        return False


# ─────────────────────────────────────────────────────── بصمة اليد: ٢١ نقطة
def _canonical(points: list[tuple[float, float]]) -> Optional[list[float]]:
    """يحوّل ٢١ نقطة يد إلى بصمة ٥١ قيمة ثابتة ضد الانتقال والدوران والحجم.

    المرساة: الرسغ (نقطة ٠) وقاعدة الوسطى (نقطة ٩). نقيسها بوحدات حجم اليد،
    ثم نديرها بحيث يشير محور الكف إلى أعلى — فالإشارة تُقرأ في أي موضع من الكادر.
    """
    if not isinstance(points, list) or len(points) != 21:
        return None
    try:
        wx, wy = float(points[0][0]), float(points[0][1])
        mx, my = float(points[9][0]), float(points[9][1])
    except Exception:
        return None
    dx, dy = mx - wx, my - wy
    size = math.hypot(dx, dy)
    if not math.isfinite(size) or size < 1e-6:
        return None
    theta = -math.pi / 2.0 - math.atan2(dy, dx)
    c, s = math.cos(theta), math.sin(theta)

    def _rot(px: float, py: float) -> tuple[float, float]:
        rx, ry = px - wx, py - wy
        return ((c * rx - s * ry) / size, (s * rx + c * ry) / size)

    flat: list[float] = []
    rotated: list[tuple[float, float]] = []
    for px, py in points:
        try:
            qx, qy = _rot(float(px), float(py))
        except Exception:
            return None
        if not (math.isfinite(qx) and math.isfinite(qy)):
            return None
        rotated.append((qx, qy))
        flat.extend((qx, qy))

    tips = [4, 8, 12, 16, 20]
    for tip in tips:                                        # بُعد كل طرف عن الرسغ
        flat.append(round(math.hypot(rotated[tip][0], rotated[tip][1]), 4))
    for a, b in zip(tips, tips[1:]):                        # المسافات بين الأطراف
        flat.append(round(math.hypot(rotated[a][0] - rotated[b][0],
                                     rotated[a][1] - rotated[b][1]), 4))
    return [round(v, 4) for v in flat]


def _cos(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na < 1e-9 or nb < 1e-9:
        return 0.0
    return dot / (na * nb)


def _pearson(a: list[float], b: list[float]) -> float:
    ma = sum(a) / len(a)
    mb = sum(b) / len(b)
    return _cos([x - ma for x in a], [y - mb for y in b])


def _similarity(a: list[float], b: list[float]) -> float:
    """تشابه ٠..١ = نصف ارتباط خام + نصف ارتباط بعد عزل المتوسط (مقاوم للإضاءة والوضع)."""
    if not a or not b or len(a) != len(b):
        return 0.0
    return max(0.0, _cos(a, b)) * 0.5 + max(0.0, _pearson(a, b)) * 0.5


# ─────────────────────────────────────────────────────── المحرّكان والكاميرا
_MP: dict = {"module": None, "hands": None, "why": ""}


def _mediapipe_hands() -> tuple[Any, str]:
    """يُجهّز كاشف الأيدي مرة واحدة؛ يعيد (الكاشف أو None، السبب)."""
    if _MP["hands"] is not None:
        return _MP["hands"], ""
    if _MP["module"] is None and not _MP["why"]:
        try:
            import mediapipe as mp      # noqa: PLC0415 — استيراد ثقيل عند الحاجة فقط
            _MP["module"] = mp
        except Exception as exc:
            _MP["why"] = type(exc).__name__
            return None, _MP["why"]
    mp = _MP["module"]
    try:
        _MP["hands"] = mp.solutions.hands.Hands(static_image_mode=True, max_num_hands=1,
                                                min_detection_confidence=0.55)
    except Exception as exc:
        _MP["why"] = type(exc).__name__
        return None, _MP["why"]
    return _MP["hands"], ""


def _mp_descriptor(frame: Any) -> tuple[Optional[list[float]], str]:
    """بصمة من mediapipe: ٢١ نقطة ← بصمة ٥١ رقمًا (تُطبَّع اليد اليسرى لتطابق اليمنى)."""
    hands, why = _mediapipe_hands()
    if hands is None:
        return None, f"mediapipe غير متاح ({why})"
    try:
        import cv2                  # noqa: PLC0415
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        result = hands.process(rgb)
    except Exception as exc:
        return None, f"تعذّر تحليل الإطار ({type(exc).__name__})"
    if not getattr(result, "multi_hand_landmarks", None):
        return None, "لا أرى يدًا في الصورة"
    landmarks = result.multi_hand_landmarks[0].landmark
    points = [(float(p.x), float(p.y)) for p in landmarks]
    label = "Right"
    try:
        if result.multi_handedness:
            label = str(result.multi_handedness[0].classification[0].label)
    except Exception:
        label = "Right"
    if label.lower().startswith("left"):
        points = [(1.0 - x, y) for (x, y) in points]
    feats = _canonical(points)
    if feats is None:
        return None, "اليد غير واضحة — قرّبها من الكاميرا"
    return feats, ""


def _fallback_descriptor(frame: Any) -> tuple[Optional[list[float]], str]:
    """بصمة بـ OpenCV فقط: كشف جلد ← أكبر كتلة ← عزم Hu وشكل تقريبي (١٤ رقمًا).

    دقّتها أقل بكثير من mediapipe وتصلح لأشكال اليد المتباعدة (كف مفتوح/قبضة/إصبع).
    """
    try:
        import cv2                  # noqa: PLC0415
        import numpy as np          # noqa: PLC0415
    except Exception as exc:
        return None, f"OpenCV أو numpy غير مثبّتة ({type(exc).__name__})"
    try:
        height, width = frame.shape[:2]
        if not height or not width:
            return None, "إطار فارغ"
        scale = 320.0 / float(width)
        small = cv2.resize(frame, (320, max(1, int(height * scale))))
        ycrcb = cv2.cvtColor(small, cv2.COLOR_BGR2YCrCb)
        mask = cv2.inRange(ycrcb, np.array([0, 133, 77], np.uint8), np.array([255, 173, 127], np.uint8))
        kernel = np.ones((5, 5), np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel, iterations=2)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            return None, "لا أرى يدًا في الصورة"
        contour = max(contours, key=cv2.contourArea)
        area = float(cv2.contourArea(contour))
        total = float(small.shape[0] * small.shape[1])
        if area < total * 0.012:
            return None, "اليد صغيرة جدًا — قرّبها من الكاميرا"
        if area > total * 0.78:
            return None, "اليد تملأ الكادر — أبعدها قليلًا"
        hull = cv2.convexHull(contour)
        hull_area = float(cv2.contourArea(hull))
        solidity = area / hull_area if hull_area > 1e-6 else 0.0
        bx, by, bw, bh = cv2.boundingRect(contour)
        extent = area / float(max(1, bw * bh))
        aspect = bw / float(max(1, bh))
        perimeter = float(cv2.arcLength(contour, True))
        circularity = (perimeter * perimeter) / (4.0 * math.pi * area) if area > 1e-6 else 0.0
        moments = cv2.moments(contour)
        hu = cv2.HuMoments(moments).flatten()
        hu_feats = [math.copysign(math.log10(abs(float(v)) + 1e-12), float(v)) for v in hu]
        fingers = 0
        try:
            hull_idx = cv2.convexHull(contour, returnPoints=False)
            defects = cv2.convexityDefects(contour, hull_idx)
            if defects is not None:
                for row in range(defects.shape[0]):
                    depth = float(defects[row, 0][3]) / 256.0
                    if depth > 12.0:
                        fingers += 1
        except Exception:
            fingers = 0
        fingers = max(0, min(5, fingers))
        cx = moments["m10"] / (moments["m00"] + 1e-6)
        cy = moments["m01"] / (moments["m00"] + 1e-6)
        dx = (cx - (bx + bw / 2.0)) / (bw + 1e-6)
        dy = (cy - (by + bh / 2.0)) / (bh + 1e-6)
        feats = [solidity, extent, math.log1p(max(aspect, 0.0)), circularity]
        feats.extend(hu_feats)
        feats.extend([fingers / 5.0, dx, dy])
        if not all(math.isfinite(float(v)) for v in feats):
            return None, "قراءة غير صالحة للإطار"
        return [round(float(v), 4) for v in feats], ""
    except Exception as exc:
        return None, f"تعذّر تحليل الإطار ({type(exc).__name__})"


def _cv_ready() -> bool:
    """هل OpenCV و numpy متاحتان؟ (يُفحص مرة واحدة لكل نداء — خفيف)"""
    try:
        __import__("cv2")
        __import__("numpy")
        return True
    except Exception:
        return False


def _pick_engine(prefer: str = "auto") -> tuple[str, str]:
    """يختار المحرّك المتاح: mediapipe أولًا، وإلا بديل OpenCV."""
    prefer = str(prefer or "auto").strip().lower()
    if prefer == "mediapipe":
        hands, why = _mediapipe_hands()
        if hands is not None:
            return "mediapipe", ""
        return "", (f"mediapipe غير متاح ({why}) — ثبّتها بـ:  pip install mediapipe"
                    "  (تعمل مع بايثون 3.11/3.12؛ وعلى 3.13 قد لا تتوفر)")
    if prefer == "fallback":
        if _cv_ready():
            return "fallback", ""
        return "", "OpenCV أو numpy غير مثبّتة — وهما في requirements المشروع عادةً"
    hands, _why = _mediapipe_hands()
    if hands is not None:
        return "mediapipe", ""
    if _cv_ready():
        return "fallback", "mediapipe غير مثبّتة — أقرأ الآن بالمحرّك التقريبي (OpenCV)"
    return "", ("لا محرّك متاح: ثبّت mediapipe  pip install mediapipe"
                "  (أو تأكد من opencv-python و numpy — وهما يأتيان مع المشروع)")


def _open_camera(index: int) -> tuple[Any, str]:
    """يفتح الكاميرا بأول واجهة تعمل (DirectShow على ويندوز أولًا)."""
    try:
        import cv2                  # noqa: PLC0415
    except Exception:
        return None, "OpenCV غير مثبّتة — pip install opencv-python"
    apis = []
    if os.name == "nt":
        apis = [getattr(cv2, "CAP_DSHOW", 700), getattr(cv2, "CAP_ANY", 0)]
    else:
        apis = [getattr(cv2, "CAP_ANY", 0)]
    for api in apis:
        capture = None
        try:
            capture = cv2.VideoCapture(index, api)
            if capture is not None and capture.isOpened():
                capture.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
                capture.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
                return capture, ""
        except Exception:
            pass
        try:
            if capture is not None:
                capture.release()
        except Exception:
            pass
    return None, (f"لا أجد كاميرا على الفهرس {index}. أغلق أي برنامج يستخدمها (تيمز/زووم)، "
                  "وتأكد أن ويندوز يسمح للتطبيقات بالكاميرا، أو جرّب camera_index آخر (1، 2).")


def _grab_descriptor(capture: Any, engine: str) -> tuple[Optional[list[float]], str]:
    """يقرأ إطارًا واحدًا ويعيد بصمته إن وُجدت يد."""
    try:
        ok, frame = capture.read()
    except Exception as exc:
        return None, f"تعذّر قراءة الإطار ({type(exc).__name__})"
    if not ok or frame is None:
        return None, "الكاميرا لا تعطي صورة"
    if engine == "mediapipe":
        return _mp_descriptor(frame)
    return _fallback_descriptor(frame)


def _capture_learn(engine: str, want: int, timeout_s: float = 14.0) -> tuple[list[list[float]], str]:
    """يجمع لقطات لإشارة واحدة، يتخطى اللقطات التي لا يد فيها."""
    index = int(_num(_cfg().get("camera_index"), 0, 0, 8))
    capture, why = _open_camera(index)
    if capture is None:
        return [], why
    samples: list[list[float]] = []
    last_note = "لا أرى يدًا — ضع كفّك أمام الكاميرا"
    try:
        deadline = time.time() + max(3.0, timeout_s)
        while len(samples) < want and time.time() < deadline:
            feats, note = _grab_descriptor(capture, engine)
            if feats:
                samples.append(feats)
                time.sleep(0.16)
            else:
                last_note = note or last_note
                time.sleep(0.10)
    finally:
        try:
            capture.release()
        except Exception:
            pass
    return samples, last_note if samples else last_note


def _capture_read(engine: str, frames: int = 5, timeout_s: float = 6.0) -> tuple[Optional[list[float]], str]:
    """يقرأ حتى `frames` لقطة ويختار أوضحها (لا يعيد إلا إذا رُئيت يد)."""
    index = int(_num(_cfg().get("camera_index"), 0, 0, 8))
    capture, why = _open_camera(index)
    if capture is None:
        return None, why
    best: Optional[list[float]] = None
    last_note = "لا أرى يدًا — ضع كفّك أمام الكاميرا"
    try:
        deadline = time.time() + max(2.0, timeout_s)
        taken = 0
        while taken < frames and time.time() < deadline:
            feats, note = _grab_descriptor(capture, engine)
            taken += 1
            if feats:
                best = feats
                time.sleep(0.08)
            else:
                last_note = note or last_note
                time.sleep(0.08)
    finally:
        try:
            capture.release()
        except Exception:
            pass
    if best is None:
        return None, last_note
    return best, ""


# ───────────────────────────────────────────────────────────── التصنيف
def _score_word(features: list[float], entry: dict) -> float:
    """درجة الكلمة = أفضل تشابه مع أيّ من لقطاتها المسجّلة."""
    best = 0.0
    for sample in entry.get("samples", []):
        if isinstance(sample, list) and len(sample) == len(features):
            best = max(best, _similarity(features, sample))
    return best


def _rank(features: list[float], model: dict) -> list[tuple[str, float]]:
    """يرتّب كل الكلمات المسجّلة تنازليًا بالتشابه."""
    ranked = [(name, _score_word(features, entry)) for name, entry in model.get("words", {}).items()]
    ranked.sort(key=lambda item: item[1], reverse=True)
    return ranked


def _engine_matches(model: dict, engine: str) -> bool:
    """هل بصمات النموذج من نفس المحرّك؟ (بصمات المحرّكين لا تُقارن ببعضها)"""
    stored = str(model.get("engine") or "")
    return (not stored) or stored == engine


# ───────────────────────────────────────────────────────────── الأفعال
def _act_status(_params: dict) -> str:
    """حالة الإضافة: المحرّكات، الإعدادات، عدد الكلمات، والملف."""
    settings = _cfg()
    engine, note = _pick_engine(str(settings.get("engine") or "auto"))
    model = _load_model()
    lines = ["سيدي، حالة لغة الإشارة:"]
    if engine:
        lines.append(f"  المحرّك: {engine}" + (" (mediapipe — الأدق)" if engine == "mediapipe" else " (بديل OpenCV — تقريبي)"))
    else:
        lines.append("  المحرّك: ❌ لا شيء متاح")
    if note:
        lines.append(f"  ملاحظة: {note}")
    lines.append(f"  بايثون: {sys.version.split()[0]}")
    try:
        import cv2                  # noqa: PLC0415
        lines.append(f"  OpenCV: {getattr(cv2, '__version__', '?')}")
    except Exception:
        lines.append("  OpenCV: غير مثبّتة")
    try:
        import mediapipe            # noqa: PLC0415
        lines.append(f"  mediapipe: {getattr(mediapipe, '__version__', '?')}")
    except Exception:
        lines.append("  mediapipe: غير مثبّتة — pip install mediapipe  (أدق كثيرًا)")
    words = model.get("words", {})
    lines.append(f"  الكلمات المتعلَّمة: {len(words)}")
    if words:
        lines.append("  " + " · ".join(sorted(words.keys())[:12]) + (" …" if len(words) > 12 else ""))
    stored_engine = model.get("engine") or "—"
    lines.append(f"  محرّك البصمات المحفوظة: {stored_engine}")
    lines.append(f"  العتبة: {_threshold():.2f} · الفارق المطلوب: {_margin():.2f} · "
                 f"لقطات/إشارة: {_samples_per_sign()} · كاميرا: {settings.get('camera_index')}")
    if stored_engine not in ("", "—") and engine and stored_engine != engine:
        lines.append("  ⚠️ البصمات محفوظة بمحرّك مختلف عن المتاح الآن — أعد التسجيل أو ثبّت mediapipe.")
    if not words:
        lines.append("  ابدأ بـ: «تعلّم إشارة الماء» — واعمل الإشارة أمام الكاميرا ثانيتين.")
    return "\n".join(lines)


def _threshold() -> float:
    return _num(_cfg().get("threshold"), 0.80, 0.50, 0.99)


def _samples_per_sign() -> int:
    return int(_num(_cfg().get("samples_per_sign"), 8, 4, 12))


def _margin() -> float:
    return _num(_cfg().get("margin"), 0.05, 0.0, 0.30)


def _decide(ranked: list[tuple[str, float]]) -> tuple[str, float, float, bool]:
    """القرار النهائي: أفضل كلمة، درجتها، درجة الثانية، وهل تُقبل؟

    شرطان معًا: (١) الدرجة ≥ العتبة، و(٢) الفارق عن ثاني أقرب إشارة ≥ الفارق المطلوب.
    الشرط الثاني هو الذي يمنع قبول إشارة غريبة لأنها «تشبه» إحداهن قليلًا.
    """
    if not ranked:
        return "", 0.0, 0.0, False
    best_word, best_score = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else 0.0
    accepted = best_score >= _threshold() and (best_score - second) >= _margin()
    return best_word, best_score, second, accepted


def _act_learn(params: dict) -> str:
    """يسجّل إشارة جديدة (أو يحدّثها) من لقطات كاميرا حقيقية."""
    word = _clean(params.get("word"))
    if not word:
        return ("سيدي، قل لي الكلمة التي تعنيها الإشارة. مثال: action=learn word=ماء — "
                "واعمل الإشارة أمام الكاميرا ثانيتين.")
    settings = _cfg()
    engine, note = _pick_engine(str(settings.get("engine") or "auto"))
    if not engine:
        return f"سيدي، {note}"
    model = _load_model()
    if model.get("words") and not _engine_matches(model, engine):
        return (f"سيدي، الإشارات المحفوظة سُجّلت بمحرّك «{model.get('engine')}» والمتاح الآن «{engine}». "
                "بصمات المحرّكين لا تُقارن ببعضها — إمّا ثبّت mediapipe (pip install mediapipe) "
                "أو امسح الكلمات القديمة: delete word=* confirm=yes.")
    max_words = int(_num(settings.get("max_words"), 40, 1, 200))
    if word not in model["words"] and len(model["words"]) >= max_words:
        return f"سيدي، وصلنا للحد ({max_words} كلمة). امسح كلمة أولًا: delete word=… confirm=yes"
    if word in model["words"] and str(params.get("confirm") or "").strip().lower() not in ("yes", "نعم", "أيوه", "ايوه"):
        return (f"سيدي، «{word}» مسجّلة بالفعل. أعد التسجيل؟ قل: action=learn word={word} confirm=yes")
    want = int(_num(params.get("samples"), _samples_per_sign(), 4, 12))
    started = time.time()
    samples, why = _capture_learn(engine, want)
    if len(samples) < 4:
        return (f"سيدي، لم ألتقط ما يكفي (أخذت {len(samples)} من {want}). السبب: {why}\n"
                "قرّب كفّك، حسّن الإضاءة (لا ظل على اليد)، وثبّت الإشارة ثانيتين ثم أعد المحاولة.")
    similar_note = ""
    if model.get("words"):
        candidate = samples[0] if samples else []
        closest = ""
        closest_score = 0.0
        for other_name, other_entry in model["words"].items():
            if other_name == word:
                continue
            score = _score_word(candidate, other_entry)
            if score > closest_score:
                closest, closest_score = other_name, score
        if closest and closest_score >= 0.85:
            similar_note = (f"\n  ⚠️ لاحظ: هذه الإشارة قريبة من «{closest}» ({closest_score:.2f}) — "
                            "لو أردت دقة أعلى اختر وضعًا مختلفًا لليد.")
    entry = {"samples": samples[:12], "updated": time.strftime("%Y-%m-%d %H:%M")}
    model["words"][word] = entry
    if not model.get("engine"):
        model["engine"] = engine
    model.setdefault("stats", {}).setdefault(word, {"asked": 0, "correct": 0})
    saved = _save_model(model)
    took = round(time.time() - started, 1)
    quality = _self_similarity(samples)
    lines = [f"سيدي، تعلّمت إشارة «{word}» من {len(samples)} لقطة في {took} ثانية."]
    lines.append(f"  المحرّك: {engine} · تماسك اللقطات: {quality:.2f} "
                 + ("(ثابتة ✔)" if quality >= 0.80 else "(متقلّبة ⚠️ — أعد التسجيل بإضاءة أثبت ومسافة واحدة)"))
    if similar_note:
        lines.append(similar_note)
    lines.append("  " + ("حُفظت في ملف الإشارات ✔" if saved else "⚠️ تعذّر الحفظ على القرص — الإشارة في الذاكرة فقط"))
    _log("learn", "ok" if saved else "warn", f"{word} · {len(samples)} · {engine}")
    return "\n".join(lines)


def _self_similarity(samples: list[list[float]]) -> float:
    """أقل تشابه بين أي لقطتين: مقياس ثبات يدك أثناء التسجيل."""
    worst = 1.0
    for i in range(len(samples)):
        for j in range(i + 1, len(samples)):
            if len(samples[i]) == len(samples[j]):
                worst = min(worst, _similarity(samples[i], samples[j]))
    return worst if len(samples) > 1 else 1.0


def _speak(text: str) -> str:
    """ينطق الكلمة عبر voice_reader إن وُجدت — ويعيد ملاحظة صريحة إن لم تُنطق."""
    if T is None or not hasattr(T, "call_plugin"):
        return "لم أنطقها (النواة المشتركة غير محمّلة)."
    try:
        ok, note = T.call_plugin("voice_reader", {"action": "say", "text": text})
    except Exception as exc:
        return f"لم أنطقها ({type(exc).__name__})."
    return "ونطقتها." if ok else f"لم أنطقها: {str(note)[:80]}"


def _act_read(params: dict) -> str:
    """يقرأ الإشارة الحالية ويعيد الكلمة — أو يقول بصراحة إنها غير معروفة."""
    settings = _cfg()
    engine, note = _pick_engine(str(settings.get("engine") or "auto"))
    if not engine:
        return f"سيدي، {note}"
    model = _load_model()
    if not model.get("words"):
        return ("سيدي، لم أتعلّم أي إشارة بعد. علّمني واحدة أولًا: "
                "action=learn word=ماء — ثم اعمل الإشارة أمام الكاميرا ثانيتين.")
    if not _engine_matches(model, engine):
        return (f"سيدي، البصمات سُجّلت بمحرّك «{model.get('engine')}» والمتاح الآن «{engine}» — "
                "ثبّت mediapipe (pip install mediapipe) أو أعد التسجيل.")
    feats, why = _capture_read(engine)
    if feats is None:
        return f"سيدي، لم أقرأ إشارة: {why}"
    ranked = _rank(feats, model)
    threshold, margin = _threshold(), _margin()
    best_word, best_score, second, accepted = _decide(ranked)
    if not accepted:
        if best_score < threshold:
            why = f"{best_score:.2f} أقل من العتبة {threshold:.2f}"
        else:
            why = (f"الفرق بين «{best_word}» ({best_score:.2f}) و«{ranked[1][0]}» ({second:.2f}) "
                   f"أقل من الفارق المطلوب {margin:.2f} — الإشارة ملتبسة")
        runner = f" (أقرب شيء: «{best_word}»)" if best_word else ""
        return (f"سيدي، هذه الإشارة غير معروفة لي: {why}{runner}.\n"
                "ثبّت الإشارة ثانيتين في ضوء جيد، أو علّمني إياها: action=learn word=الكلمة\n"
                "وإن كانت إشارتك الصحيحة تُرفض كثيرًا: set key=threshold value=0.86  أو  set key=margin value=0.03")
    want_speak = str(params.get("speak", settings.get("speak", "yes"))).strip().lower() in ("yes", "نعم", "true", "1", "أيوه", "ايوه")
    lines = [f"سيدي، الإشارة تعني: «{best_word}»  ({best_score:.2f} ≥ {threshold:.2f}"
             + (f" · الفارق عن التالية {best_score - second:.2f} ≥ {margin:.2f}" if second else "") + ")"]
    others = [f"{name} {score:.2f}" for name, score in ranked[1:4] if score > 0.4]
    if others:
        lines.append("  الأقرب بعدها: " + " · ".join(others))
    if want_speak:
        lines.append("  " + _speak(best_word))
    _log("read", "ok", best_word)
    return "\n".join(lines)


def _act_practice(params: dict) -> str:
    """وضع المدرّب: mode=ask يطلب إشارة، وmode=check يتحقق منها ويسجّل النتيجة."""
    mode = str(params.get("mode") or "ask").strip().lower()
    model = _load_model()
    words = sorted(model.get("words", {}).keys())
    if not words:
        return ("سيدي، لا توجد إشارات متعلَّمة بعد. ابدأ بـ: action=learn word=ماء — "
                "ثم «درّبني على الإشارات».")
    if mode in ("check", "تحقق", "اختبر", "صح"):
        pending = str(PENDING.get("word") or "").strip()
        asked_for = _clean(params.get("word")) or pending
        if not asked_for:
            return "سيدي، لم أطلب منك إشارة بعد. قل أولًا: action=practice mode=ask"
        settings = _cfg()
        engine, note = _pick_engine(str(settings.get("engine") or "auto"))
        if not engine:
            return f"سيدي، {note}"
        if not _engine_matches(model, engine):
            return f"سيدي، البصمات بمحرّك «{model.get('engine')}» والمتاح «{engine}» — لا أستطيع التحقق."
        feats, why = _capture_read(engine)
        if feats is None:
            return f"سيدي، لم أقرأ إشارة: {why}"
        ranked = _rank(feats, model)
        got, score, _second, accepted = _decide(ranked)
        stats = model.setdefault("stats", {}).setdefault(asked_for, {"asked": 0, "correct": 0})
        stats["asked"] = int(stats.get("asked", 0)) + 1
        correct = bool(got) and got == asked_for and accepted
        if correct:
            stats["correct"] = int(stats.get("correct", 0)) + 1
        _save_model(model)
        PENDING["word"] = ""
        asked_n = max(1, int(stats.get("asked", 1)))
        accuracy = 100.0 * float(stats.get("correct", 0)) / asked_n
        if correct:
            return (f"سيدي، صحيح ✔ — طلبت «{asked_for}» وقرأت «{got}» ({score:.2f}). "
                    f"دقّتك في هذه الكلمة: {accuracy:.0f}% من {asked_n} محاولة.")
        if got and accepted:
            return (f"سيدي، ليست هي — طلبت «{asked_for}» وقرأت «{got}» ({score:.2f}). "
                    f"دقّتك في «{asked_for}»: {accuracy:.0f}% من {asked_n}.")
        return (f"سيدي، لم أتعرّف على إشارة واضحة. المطلوب كان «{asked_for}» — "
                "ثبّت يدك ثانيتين في ضوء جيد ثم أعد المحاولة.")

    chosen = _clean(params.get("word"))
    if chosen and chosen not in model["words"]:
        return f"سيدي، «{chosen}» غير متعلَّمة. الكلمات المتاحة: " + " · ".join(words[:12])
    if not chosen:
        weakest = []
        for name in words:
            stats = model.get("stats", {}).get(name, {})
            asked = int(stats.get("asked", 0) or 0)
            correct = int(stats.get("correct", 0) or 0)
            acc = (correct / asked) if asked else 1.0
            weakest.append((acc, name))
        weakest.sort()
        chosen = weakest[0][1] if weakest and weakest[0][0] < 0.6 else random.choice(words)
    PENDING["word"] = chosen
    _log("practice", "ok", f"ask {chosen}")
    lines = [f"سيدي، اعمل إشارة: «{chosen}»",
             "  ثبّت يدك ثانيتين أمام الكاميرا، ثم قل: «تحقق» (action=practice mode=check)."]
    stats = model.get("stats", {}).get(chosen, {})
    asked = int(stats.get("asked", 0) or 0)
    if asked:
        correct = int(stats.get("correct", 0) or 0)
        lines.append(f"  سجلّك في هذه الكلمة: {correct}/{asked} ({100.0 * correct / asked:.0f}%).")
    return "\n".join(lines)


def _act_list(_params: dict) -> str:
    """يسرد الإشارات المتعلَّمة ودقّتك في كل واحدة."""
    model = _load_model()
    words = model.get("words", {})
    if not words:
        return ("سيدي، لم أتعلّم أي إشارة بعد.\n"
                "أمثلة للبدء: ماء · طعام · مساعدة · نعم · لا · توقف · شكرًا · مرحبا · طبيب · حمام.\n"
                "قل: «تعلّم إشارة ماء» واعمل الإشارة أمام الكاميرا ثانيتين.")
    lines = [f"سيدي، أعرف {len(words)} إشارة (محرّك البصمات: {model.get('engine') or '—'}):"]
    for name in sorted(words):
        stats = model.get("stats", {}).get(name, {})
        asked = int(stats.get("asked", 0) or 0)
        correct = int(stats.get("correct", 0) or 0)
        acc = f"{100.0 * correct / asked:.0f}%" if asked else "—"
        lines.append(f"  • {name}: {len(words[name].get('samples', []))} لقطة · دقّتك {acc}")
    lines.append("  للتحقق: «اقرأ إشارتي» · للتدريب: «درّبني على الإشارات»")
    return "\n".join(lines)


def _act_delete(params: dict) -> str:
    """يحذف إشارة (أو الكل) — لا يحذف شيئًا بلا confirm=yes."""
    word = _clean(params.get("word"))
    if not word:
        return "سيدي، أي كلمة أحذف؟ delete word=ماء — أو word=* لكل الكلمات."
    if str(params.get("confirm") or "").strip().lower() not in ("yes", "نعم", "أيوه", "ايوه"):
        return f"سيدي، تأكيد مطلوب: delete word={word} confirm=yes"
    model = _load_model()
    if word in ("*", "الكل", "all"):
        count = len(model.get("words", {}))
        if not count:
            return "سيدي، لا توجد إشارات لأحذفها."
        model["words"] = {}
        model["stats"] = {}
        model["engine"] = ""
        _save_model(model)
        PENDING["word"] = ""
        _log("delete", "ok", f"all:{count}")
        return f"سيدي، حذفت كل الإشارات ({count}). ابدأ تسجيلًا جديدًا متى شئت."
    if word not in model.get("words", {}):
        return f"سيدي، «{word}» غير مسجّلة أصلًا. اعرض الموجود: action=list"
    del model["words"][word]
    model.get("stats", {}).pop(word, None)
    if not model["words"]:
        model["engine"] = ""
    _save_model(model)
    if PENDING.get("word") == word:
        PENDING["word"] = ""
    _log("delete", "ok", word)
    return f"سيدي، حذفت إشارة «{word}». الباقي: {len(model['words'])}."


def _act_set(params: dict) -> str:
    """يغيّر إعدادًا واحدًا مع تحقق صريح من المدى."""
    key = str(params.get("key") or "").strip().lower()
    value = params.get("value")
    if not key:
        return ("سيدي، أي إعداد؟ المفاتيح: threshold (0.50–0.99) · margin (0.00–0.30) · "
                "samples_per_sign (4–12) · camera_index (0–8) · speak (yes/no) · engine (auto/mediapipe/fallback)")
    allowed = {"threshold", "margin", "samples_per_sign", "camera_index", "speak", "engine"}
    if key not in allowed:
        return f"سيدي، «{key}» ليس إعدادًا معروفًا. المتاح: {' · '.join(sorted(allowed))}"
    if value in (None, ""):
        return f"سيدي، ما القيمة الجديدة لـ {key}؟"
    if key == "threshold":
        num = _num(value, -1, 0.50, 0.99)
        if num < 0:
            return "سيدي، العتبة رقم بين 0.50 و0.99 — مثال: set key=threshold value=0.85"
        text = f"{num:.2f}"
    elif key == "margin":
        num = _num(value, -1, 0.0, 0.30)
        if num < 0:
            return "سيدي، الفارق رقم بين 0.00 و0.30 — مثال: set key=margin value=0.05"
        text = f"{num:.2f}"
    elif key == "samples_per_sign":
        num = int(_num(value, -1, 4, 12))
        if num < 0:
            return "سيدي، اللقطات رقم صحيح بين 4 و12."
        text = str(num)
    elif key == "camera_index":
        num = int(_num(value, -1, 0, 8))
        if num < 0:
            return "سيدي، فهرس الكاميرا من 0 إلى 8."
        text = str(num)
    elif key == "speak":
        text = "yes" if str(value).strip().lower() in ("yes", "نعم", "true", "1", "أيوه", "ايوه") else "no"
    else:
        text = str(value).strip().lower()
        if text not in ("auto", "mediapipe", "fallback"):
            return "سيدي، المحرّك: auto أو mediapipe أو fallback."
    saved = _store({key: text})
    _log("set", "ok" if saved else "warn", f"{key}={text}")
    return (f"سيدي، {key} = {text}." + ("" if saved else " (تعذّر الحفظ — ساري لهذه الجلسة فقط)"))


def _act_help() -> str:
    """دليل مختصر يقرأه المستخدم أو الموديل."""
    return "\n".join([
        "سيدي، لغة الإشارة — كل شيء محلي، الكاميرا فقط، ولا صورة تُحفظ أو تُرسل.",
        "",
        "١) علّمني إشارة:  learn word=ماء   (ثبّت الإشارة أمام الكاميرا ثانيتين)",
        "   • لو الكلمة موجودة: أضف confirm=yes لتحديثها",
        "   • أمثلة كلمات مفيدة: ماء · طعام · مساعدة · نعم · لا · توقف · شكرًا · مرحبا · طبيب · حمام",
        "٢) اقرأ إشارتي:  read   (وزيادة speak=yes لتنطق الكلمة)",
        "٣) درّبني:  practice mode=ask   ← يطلب كلمة، ثم  practice mode=check  ← يتحقق ويسجّل دقّتك",
        "٤) أعرف إيه؟  list   ·   احذف:  delete word=ماء confirm=yes   ·   الكل: delete word=* confirm=yes",
        "٥) إعدادات:  set key=threshold value=0.90  ·  set key=margin value=0.05  ·  samples_per_sign  ·  camera_index  ·  speak  ·  engine",
        "٦) الحالة:  status",
        "",
        "مهم: الإشارات الثابتة فقط (اليد ساكنة) — الإشارات المتحرّكة غير مدعومة.",
        "الدقة: mediapipe ممتازة (pip install mediapipe، وبايثون 3.11/3.12)؛ البديل بـOpenCV تقريبي.",
        "لا نماذج تُنزَّل، لا مفاتيح، لا إنترنت: البصمات أرقام في ملفك المحلي فقط.",
        "التوفيق: لو إشارتك الصحيحة تُرفض، أنزل threshold إلى 0.86؛ ولو إشارة غريبة تُقبل، ارفع margin إلى 0.08.",
    ])


def run(parameters: Optional[dict] = None) -> str:
    """نقطة الدخول التي يناديها جارفس. لا ترمي أبدًا — تعيد نصًا عربيًا في كل الحالات."""
    params = parameters if isinstance(parameters, dict) else {}
    action = _clean(params.get("action"), 24).lower() or "status"
    aliases = {
        "read": "read", "اقرا": "read", "اقرأ": "read", "قراءة": "read", "شوف": "read",
        "learn": "learn", "تعلم": "learn", "تعلّم": "learn", "سجل": "learn", "سجّل": "learn",
        "practice": "practice", "تدريب": "practice", "درب": "practice", "درّب": "practice",
        "list": "list", "قائمة": "list", "الكلمات": "list",
        "delete": "delete", "احذف": "delete", "مسح": "delete",
        "set": "set", "اعداد": "set", "إعداد": "set",
        "status": "status", "حالة": "status",
        "help": "help", "مساعدة": "help",
    }
    action = aliases.get(action, action)
    try:
        if action == "read":
            return _act_read(params)
        if action == "learn":
            return _act_learn(params)
        if action == "practice":
            return _act_practice(params)
        if action == "list":
            return _act_list(params)
        if action == "delete":
            return _act_delete(params)
        if action == "set":
            return _act_set(params)
        if action == "status":
            return _act_status(params)
        if action == "help":
            return _act_help()
        return ("سيدي، الأفعال: read · learn · practice · list · delete · set · status · help\n"
                "مثال: action=learn word=ماء  ثم  action=read")
    except Exception as exc:            # لا انهيار للجارفس أبدًا
        _log(action, "error", type(exc).__name__)
        return f"سيدي، حدث خطأ غير متوقع في لغة الإشارة ({type(exc).__name__}). جرّب action=status."


# ─────────────────────────────────────────────────────────────── الفحص الذاتي
def _selftest() -> int:
    """الفحص الذاتي — معزول تمامًا: لا كاميرا، ولا كتابة في مخزن المشروع."""
    restorer = None
    originals = None
    if T is not None:
        try:
            originals = (T.extras_dir, T.cfg, T.store_settings, T.log_event)
            tmp = Path(tempfile.mkdtemp(prefix="sign_selftest_"))
            memory: dict = {}

            def _fake_extras() -> Path:
                return tmp

            def _fake_cfg(namespace: str, fields: list[dict]) -> dict:
                out = {f["key"]: f.get("default") for f in fields}
                out.update({k: v for k, v in memory.get(namespace, {}).items() if v not in (None, "")})
                return out

            def _fake_store(namespace: str, values: dict) -> bool:
                memory.setdefault(namespace, {}).update(values or {})
                return True

            T.extras_dir = _fake_extras          # type: ignore[assignment]
            T.cfg = _fake_cfg                    # type: ignore[assignment]
            T.store_settings = _fake_store       # type: ignore[assignment]
            T.log_event = lambda *a, **k: None   # type: ignore[assignment]
            restorer = tmp
        except Exception:
            originals = None
            restorer = None
    try:
        return _selftest_body()
    finally:
        if T is not None and originals is not None:
            T.extras_dir, T.cfg, T.store_settings, T.log_event = originals  # type: ignore[assignment]
        if restorer is not None:
            shutil.rmtree(restorer, ignore_errors=True)


def _selftest_body() -> int:
    print("sign_language — self-test")
    problems = 0

    def check(label: str, ok: bool, extra: str = "") -> None:
        nonlocal problems
        print(("  ✓ " if ok else "  ✗ ") + label + (f"  [{extra}]" if extra and not ok else ""))
        if not ok:
            problems += 1

    # ── يد اصطناعية: ٢١ نقطة، مع تحكّم في مدّ كل صبع ──
    def synth(ext: list[float], spread: float = 1.0) -> list[tuple[float, float]]:
        wrist = (0.50, 0.94)
        pts = [wrist]
        angles = [-34.0, -17.0, 0.0, 15.0, 31.0]
        lengths = [(0.20, 0.075, 0.055), (0.225, 0.085, 0.060),
                   (0.235, 0.090, 0.062), (0.215, 0.080, 0.056), (0.180, 0.065, 0.048)]
        for finger in range(5):
            base = math.radians(angles[finger] * spread)
            cur = [wrist[0] + math.sin(base) * 0.13, wrist[1] - math.cos(base) * 0.13]
            pts.append((cur[0], cur[1]))                      # MCP
            curl = (1.0 - float(ext[finger])) * math.radians(78.0)
            for seg, limb in enumerate(lengths[finger]):
                bend = base + curl * (1.0 + 0.25 * seg)
                cur = [cur[0] + math.sin(bend) * limb, cur[1] - math.cos(bend) * limb]
                pts.append((round(cur[0], 4), round(cur[1], 4)))
        return pts[:21]

    fist = synth([0.0, 0.0, 0.0, 0.0, 0.0])
    open_hand = synth([1.0, 1.0, 1.0, 1.0, 1.0])
    point = synth([0.0, 1.0, 0.0, 0.0, 0.0])

    # ── ٠) القيم الافتراضية كما تُسلَّم ──
    check("العتبة الافتراضية ٠٫٩٠", abs(_threshold() - 0.90) < 1e-9, f"{_threshold()}")
    check("الفارق الافتراضي ٠٫٠٥", abs(_margin() - 0.05) < 1e-9, f"{_margin()}")
    check("اللقطات الافتراضية ٨", _samples_per_sign() == 8, str(_samples_per_sign()))
    check("القرار يرفض الدرجة القريبة بلا فارق كافٍ",
          _decide([("أ", 0.95), ("ب", 0.93)])[3] is False)
    check("القرار يقبل درجة عالية بفارق كافٍ",
          _decide([("أ", 0.97), ("ب", 0.80)])[3] is True)
    check("القرار يرفض ما تحت العتبة",
          _decide([("أ", 0.70), ("ب", 0.10)])[3] is False)

    # ── ١) البصمة: ثابتة ضد الانتقال والدوران والحجم ──
    base = _canonical(open_hand)
    moved = [(x + 0.11, y - 0.07) for x, y in open_hand]
    scaled = [(0.5 + (x - 0.5) * 1.7, 0.5 + (y - 0.5) * 1.7) for x, y in open_hand]
    ang = math.radians(23.0)
    rotated = [(0.5 + (x - 0.5) * math.cos(ang) - (y - 0.5) * math.sin(ang),
                0.5 + (x - 0.5) * math.sin(ang) + (y - 0.5) * math.cos(ang)) for x, y in open_hand]
    check("البصمة تُحسب (٥١ قيمة)", bool(base) and len(base) == 51, str(len(base or [])))
    check("نفس اليد مع إزاحة: تشابه > 0.99", _similarity(base, _canonical(moved) or []) > 0.99,
          f"{_similarity(base, _canonical(moved) or []):.3f}")
    check("نفس اليد مع تكبير ١٫٧×: تشابه > 0.99", _similarity(base, _canonical(scaled) or []) > 0.99,
          f"{_similarity(base, _canonical(scaled) or []):.3f}")
    check("نفس اليد مع دوران ٢٣°: تشابه > 0.95", _similarity(base, _canonical(rotated) or []) > 0.95,
          f"{_similarity(base, _canonical(rotated) or []):.3f}")

    # ── ٢) التمييز: أشكال مختلفة = تشابه منخفض ──
    sim_fo = _similarity(base, _canonical(fist) or [])
    sim_op = _similarity(base, _canonical(point) or [])
    check("الكف المفتوح ≠ القبضة", sim_fo < 0.75, f"{sim_fo:.3f}")
    check("الكف المفتوح ≠ السبابة", sim_op < 0.95, f"{sim_op:.3f}")
    check("مقارنة أطوال مختلفة لا تنهار", _similarity([1.0, 2.0], [1.0, 2.0, 3.0]) == 0.0)
    check("مقارنة قائمة فارغة لا تنهار", _similarity([], []) == 0.0)
    check("نقاط ناقصة تُرفض", _canonical(fist[:10]) is None)

    # ── ٣) التعلّم والقراءة بمحرّك مزيّف (بلا كاميرا) ──
    G = globals()
    patched = ("_pick_engine", "_capture_learn", "_capture_read", "_speak")
    saved_fns = {name: G[name] for name in patched if name in G}
    state = {"shape": fist}

    def fake_engine(prefer: str = "auto") -> tuple[str, str]:
        return "test", ""

    def fake_learn(engine: str, want: int, timeout_s: float = 14.0) -> tuple[list[list[float]], str]:
        shape = state["shape"]
        variants = []
        for i in range(min(want, 6)):
            jitter = [(x + 0.004 * (i % 3), y + 0.003 * (i % 2)) for x, y in shape]
            variants.append(_canonical(jitter) or [])
        return [v for v in variants if v], ""

    def fake_read(engine: str, frames: int = 5, timeout_s: float = 6.0) -> tuple[Optional[list[float]], str]:
        return _canonical(state["shape"]), ""

    try:
        G["_pick_engine"] = fake_engine
        G["_capture_learn"] = fake_learn
        G["_capture_read"] = fake_read
        G["_speak"] = lambda text: "ونطقتها."

        out = run({"action": "learn", "word": ""})
        check("learn بلا كلمة يشرح ولا ينهار", "قل لي الكلمة" in out, out[:80])

        state["shape"] = fist
        out = run({"action": "learn", "word": "ماء"})
        check("تعلّم «ماء» ينجح", "تعلّمت إشارة «ماء»" in out, out[:120])
        check("حُفظ ملف الإشارات في المجلد المؤقت", (_model_path() is not None and _model_path().exists()))

        state["shape"] = open_hand
        out = run({"action": "learn", "word": "توقف"})
        check("تعلّم «توقف» ينجح", "تعلّمت إشارة «توقف»" in out, out[:120])

        out = run({"action": "learn", "word": "ماء"})
        check("تسجيل كلمة موجودة يطلب confirm", "confirm=yes" in out, out[:120])
        state["shape"] = fist
        out = run({"action": "learn", "word": "ماء", "confirm": "yes"})
        check("التحديث بـconfirm ينجح", "تعلّمت إشارة «ماء»" in out, out[:80])

        state["shape"] = fist
        out = run({"action": "read"})
        check("قراءة القبضة ← «ماء»", ("تعني: «ماء»" in out) and ("غير معروفة" not in out), out[:140])
        check("القراءة تخبر أن النطق تم", "ونطقتها" in out, out[:160])
        state["shape"] = open_hand
        out = run({"action": "read"})
        check("قراءة الكف المفتوح ← «توقف»", ("تعني: «توقف»" in out) and ("غير معروفة" not in out), out[:140])

        state["shape"] = point
        model = _load_model()
        ranked = _rank(_canonical(point) or [], model)
        threshold = _threshold()
        check("شكل ثالث يبقى تحت العتبة (يرفض الغريب)",
              (not ranked) or ranked[0][1] < threshold, f"{ranked[0][1]:.3f} < {threshold:.2f}")

        out = run({"action": "list"})
        check("list يعرض الكلمتين", ("ماء" in out) and ("توقف" in out), out[:120])

        out = run({"action": "practice", "mode": "ask"})
        check("practice ask يطلب كلمة", "اعمل إشارة" in out, out[:100])
        pending = PENDING.get("word")
        check("practice ask يحفظ الكلمة المطلوبة", bool(pending), str(pending))
        state["shape"] = fist if pending == "ماء" else open_hand
        out = run({"action": "practice", "mode": "check"})
        check("practice check يصحّح", "صحيح" in out, out[:140])
        stats = _load_model().get("stats", {}).get(pending or "", {})
        check("الإحصاء يُسجّل محاولة صحيحة",
              int(stats.get("asked", 0)) >= 1 and int(stats.get("correct", 0)) >= 1, str(stats))

        PENDING["word"] = "ماء"
        state["shape"] = open_hand
        out = run({"action": "practice", "mode": "check"})
        check("practice check يرفض الخطأ", "ليست هي" in out or "لم أتعرّف" in out, out[:140])

        out = run({"action": "delete", "word": "ماء"})
        check("الحذف بلا confirm يُرفض", "confirm=yes" in out, out[:100])
        out = run({"action": "delete", "word": "ماء", "confirm": "yes"})
        check("الحذف بـconfirm ينجح", "حذفت" in out, out[:100])
        check("الكلمة اختفت من الملف", "ماء" not in _load_model().get("words", {}))

        out = run({"action": "set", "key": "threshold", "value": "0.9"})
        check("set threshold ينجح", "0.90" in out, out[:100])
        out = run({"action": "set", "key": "threshold", "value": "5"})
        check("set threshold خارج المدى يُقصّ للأقصى", "0.99" in out, out[:100])
        out = run({"action": "set", "key": "nonsense", "value": "1"})
        check("set بمفتاح مجهول يشرح", "ليس إعدادًا معروفًا" in out, out[:100])
        out = run({"action": "set", "key": "camera_index", "value": "abc"})
        check("set بقيمة غير رقمية يشرح", "من 0 إلى 8" in out or "بين" in out, out[:100])

        out = run({"action": "delete", "word": "*", "confirm": "yes"})
        check("حذف الكل ينجح", "حذفت كل الإشارات" in out, out[:100])
        check("الملف أصبح فارغًا", not _load_model().get("words"))
    finally:
        for _name, _fn in saved_fns.items():
            G[_name] = _fn

    # ── ٤) بلا محرّك ولا كاميرا: رسائل صريحة بلا انهيار ──
    def no_engine(prefer: str = "auto") -> tuple[str, str]:
        return "", "لا محرّك متاح: ثبّت mediapipe"

    saved_engine = G["_pick_engine"]
    try:
        G["_pick_engine"] = no_engine
        out = run({"action": "read"})
        check("بلا محرّك: read يشرح", "لا محرّك متاح" in out or "لم أتعلّم" in out, out[:100])
        out = run({"action": "learn", "word": "ماء", "confirm": "yes"})
        check("بلا محرّك: learn يشرح ولا ينهار", "لا محرّك متاح" in out, out[:100])
        out = run({"action": "practice", "mode": "check"})
        check("بلا محرّك: practice check يشرح", "لا محرّك" in out or "لا توجد" in out or "لم أطلب" in out, out[:100])
    finally:
        G["_pick_engine"] = saved_engine

    # ── ٤ب) المحرّك الاحتياطي (OpenCV) على صور اصطناعية — يختبر المسار الحقيقي ──
    if _cv_ready():
        try:
            import cv2                       # noqa: PLC0415
            import numpy as np               # noqa: PLC0415

            def _scene(kind: str) -> Any:
                img = np.full((480, 640, 3), 70, np.uint8)
                cx, cy = 320, 240
                cv2.ellipse(img, (cx, cy + 60), (85, 95), 0, 0, 360, (150, 180, 225), -1)
                if kind == "open":
                    for dx in (-70, -35, 0, 35, 70):
                        cv2.line(img, (cx + dx, cy + 20), (cx + dx * 3 // 2, cy - 120), (150, 180, 225), 26)
                elif kind == "fist":
                    cv2.ellipse(img, (cx, cy + 45), (78, 60), 0, 0, 360, (150, 180, 225), -1)
                return img

            open_hand_img = _scene("open")
            fist_img = _scene("fist")
            f_open, note_open = _fallback_descriptor(open_hand_img)
            f_fist, _note_fist = _fallback_descriptor(fist_img)
            check("OpenCV: يرى يدًا في صورة واضحة", bool(f_open), str(note_open)[:60])
            check("OpenCV: بصمة ١٤ قيمة", bool(f_open) and len(f_open) == 14, str(len(f_open or [])))
            check("OpenCV: يفرّق بين الكف والقبضة",
                  bool(f_open and f_fist) and _similarity(f_open, f_fist) < 0.80,
                  f"{_similarity(f_open or [], f_fist or []):.3f}")
            darker = np.clip(open_hand_img.astype(np.float32) * 0.75, 0, 255).astype(np.uint8)
            f_dark, _ = _fallback_descriptor(darker)
            check("OpenCV: الإضاءة الأقل ٢٥٪ لا تغيّر الشكل",
                  bool(f_dark) and _similarity(f_open or [], f_dark or []) > 0.95,
                  f"{_similarity(f_open or [], f_dark or []):.3f}")
            f_none, note_none = _fallback_descriptor(np.full((480, 640, 3), 70, np.uint8))
            check("OpenCV: صورة بلا يد تُرفض برسالة", f_none is None and "لا أرى" in note_none, note_none[:50])
            f_empty, note_empty = _fallback_descriptor(np.zeros((0, 0, 3), np.uint8))
            check("OpenCV: إطار فارغ لا ينهار", f_empty is None and bool(note_empty), note_empty[:50])
        except Exception as exc:
            check("OpenCV: مسار الصور يعمل", False, type(exc).__name__)
    else:
        print("  – تخطّي: OpenCV/numpy غير متاحتين هنا")

    # ── ٥) أوامر عامة ──
    check("help يعمل", "علّمني إشارة" in run({"action": "help"}))
    out = run({"action": "معرفش"})
    check("فعل مجهول يعرض القائمة", "الأفعال" in out, out[:80])
    check("run() بلا وسائط لا ينهار", isinstance(run(None), str))
    check("run() بمعاملات غريبة لا ينهار", isinstance(run({"action": 123, "word": {"x": 1}}), str))

    # ── ٦) كل الأرقام في مكانها (لا NaN يتسلل إلى الملف) ──
    check("البصمة بلا NaN/Inf", all(math.isfinite(v) for v in (base or [])), "ok")
    check("العتبة بعد التعديل ٠٫٩٩ (آخر set)", abs(_threshold() - 0.99) < 1e-9, f"{_threshold()}")
    check("margin يُضبط ويُحفظ", "0.08" in run({"action": "set", "key": "margin", "value": "0.08"}))
    check("margin خارج المدى يُقصّ", "0.30" in run({"action": "set", "key": "margin", "value": "9"}))

    print("RESULT:", "PASS" if problems == 0 else f"{problems} problem(s)")
    return problems


if __name__ == "__main__":
    raise SystemExit(1 if _selftest() else 0)
