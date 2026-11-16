"""
_face_core.py — قلب التحقق من الوجه (مكتبة مشتركة، لا تُحمَّل كإضافة).

هذا الملف يحمل كل ما لا يخصّ الواجهة النصّية: النماذج، الكشف، البصمة، العتبة،
البوّابات، فحص الحياة، والمشهد (الإيفيكت). وإضافة `face_auth.py` تستدعيه.

البنية التقنية (نفس ما عايرناه على صورك الحقيقية):
  • الكاشف YuNet  — 227 ك.ب · خمس نقاط ملامح · عتبة ثقة 0.9 · إدخال 320×320
  • المُعرّف SFace — 37.8 م.ب · متجه 128 بُعدًا
  • المقارنة: جيب تمام الزاوية المحلي (np.dot/norms) — لأن `rec.match` ينهار
    على المتجهات المسطّحة، وقد رأينا ذلك فعلًا.
  • العتبة تُعاير من بيانات صاحب الجهاز، لا من الرقم العام 0.363:
        threshold = max_impostor + 0.55 × margin        (والأرضية 0.363)
    وهذه الصيغة هي التي تُعيد 0.526 على أرقامنا المسجّلة (0.740 و0.265).

قواعد ثابتة اكتشفناها بالتجربة، وهي مثبّتة هنا لا في التعليقات:
  1. بوّابات التدريب **لا تُطبَّق** وقت التحقق — العتبة وحدها تقرّر.
  2. ترتيب البوّابات: الحجم **قبل** تعدّد الوجوه.
  3. الوجوه الأصغر من 0.5٪ من الإطار ضجيج كاشف، تُتجاهل ولا تُرفض لأجلها.
  4. قاعدة السيادة: يُرفض الإطار إذا كان وجه ثانٍ ≥ 0.80 × الأكبر.

لا سطر واحد هنا يرمي استثناءً إلى الخارج: كل شيء يعيد نتيجة ورسالة.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import secrets
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Optional

# ── المسارات ──────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent          # مجلد plugins
BASE_DIR = ROOT.parent                          # جذر مشروع Mark-LV
CONFIG_DIR = BASE_DIR / "config"
MODELS_DIR = CONFIG_DIR / "models"
FACE_DIR = CONFIG_DIR / "orchestration" / "face_auth"

PROFILE_FILE = FACE_DIR / "profile.json"        # العتبة والأرقام
EMBED_FILE = FACE_DIR / "embedding.npy"         # متجه المالك (float32 [128])
PASSWORD_FILE = FACE_DIR / "password.json"      # بصمة كلمة المرور الاحتياطية

# النماذج الرسمية من OpenCV Zoo (Apache 2.0) — بحجم أدنى للتحقق من سلامة التنزيل
MODEL_SPECS: dict[str, dict[str, Any]] = {
    "yunet.onnx": {
        "url": ("https://github.com/opencv/opencv_zoo/raw/main/models/"
                "face_detection_yunet/face_detection_yunet_2023mar.onnx"),
        "min_bytes": 150_000, "max_bytes": 600_000, "about": "كاشف الوجه YuNet (~227 ك.ب)",
    },
    "sface.onnx": {
        "url": ("https://github.com/opencv/opencv_zoo/raw/main/models/"
                "face_recognition_sface/face_recognition_sface_2021dec.onnx"),
        "min_bytes": 20_000_000, "max_bytes": 60_000_000, "about": "مُعرّف الوجه SFace (~37.8 م.ب)",
    },
}

GENERAL_THRESHOLD = 0.363       # عتبة SFace الرسمية — أرضية لا تُنزل تحتها
SAFETY_FRAC = 0.55             # ما يُعيد 0.526 على أرقامنا (0.740 / 0.265)
DIM = 128                       # أبعاد المتجه

# نصوص البوّابات — تُقال كما هي، بصيغة أمر قصير
GATE_SAY = {
    "no_face": "اقترب من الكاميرا",
    "too_small": "اقترب أكثر — الوجه صغير في الإطار",
    "many_faces": "وجه واحد فقط أمام الكاميرا",
    "low_conf": "ثبّت رأسك لحظة",
    "off_axis": "انظر إلى الكاميرا مباشرة",
    "dark": "الإضاءة غير مناسبة — أضِف ضوءًا",
    "bright": "الضوء قوي على وجهك — غيّر اتجاهك",
    "blur": "ثبّت — اللقطة غير واضحة",
    "ok": "لقطة جيدة",
}


# ── الاستيرادات الكسولة: لا كلفة إن لم تُستخدم ────────────────────────────────
def _np():
    try:
        import numpy  # noqa: PLC0415
        return numpy
    except Exception:
        return None


def _cv2():
    try:
        import cv2  # noqa: PLC0415
        return cv2
    except Exception:
        return None


def _cv2_status() -> tuple[bool, str]:
    if _np() is None:
        return False, "حزمة numpy غير مثبّتة:  pip install numpy"
    if _cv2() is None:
        return False, "حزمة opencv غير مثبّتة:  pip install opencv-python"
    return True, ""


def _core():
    """النواة المشتركة للحزمة إن وُجدت (للسجل والتأكيد). لا يعتمد عليها الوجه نفسه."""
    name = "_jarvis_orch_core"
    if name in sys.modules:
        return sys.modules[name]
    path = ROOT / "_orch_core.py"
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        return None
    return module


def log(action: str, status: str, error: str = "", detail: Any = None) -> None:
    """سجل منظَّم إن وُجدت النواة — وبلا أسرار أبدًا (البصمات وكلمات المرور لا تُمرَّر هنا)."""
    core = _core()
    if core is None:
        return
    try:
        core.log_event("face_auth", action, status, 0, error, detail)
    except Exception:
        pass


# ── النماذج ───────────────────────────────────────────────────────────────────
def model_path(name: str) -> Path:
    return MODELS_DIR / name


def models_ready() -> tuple[bool, str]:
    """هل النموذجان موجودان وبالحجم الصحيح؟ فحص ملفات صريح، بلا تحميل نماذج."""
    problems = []
    for name, spec in MODEL_SPECS.items():
        path = model_path(name)
        if not path.exists():
            problems.append(f"ناقص: {name} ({spec['about']})")
            continue
        size = path.stat().st_size
        if not (spec["min_bytes"] <= size <= spec["max_bytes"]):
            problems.append(f"تالف أو ناقص: {name} ({size} بايت)")
    if problems:
        return False, " — ".join(problems)
    return True, "النموذجان موجودان"


def download_models(progress=None) -> tuple[bool, str]:
    """ينزّل النموذجين من OpenCV الرسمية إلى config/models/.

    التنزيل إلى ملف `.part` ثم النقل الذرّي، فلا يبقى ملف نصفه موجود ليُقرأ لاحقًا
    كأنه كامل — وهذا خطأ يقع فيه كثيرًا من يكتب التنزيل بسرعة.
    """
    ok_cv, why_cv = _cv2_status()
    if not ok_cv:
        return False, why_cv
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    done: list[str] = []
    for name, spec in MODEL_SPECS.items():
        target = model_path(name)
        if target.exists() and spec["min_bytes"] <= target.stat().st_size <= spec["max_bytes"]:
            done.append(f"{name} (موجود)")
            continue
        part = target.with_suffix(target.suffix + ".part")
        started = time.time()
        try:
            request = urllib.request.Request(spec["url"], headers={"User-Agent": "JARVIS-local/1.0"})
            with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 — عنوان رسمي ثابت
                total = int(response.headers.get("Content-Length") or 0)
                read = 0
                with open(part, "wb") as handle:
                    while True:
                        chunk = response.read(262_144)
                        if not chunk:
                            break
                        handle.write(chunk)
                        read += len(chunk)
                        if progress and total:
                            progress(name, read, total)
            size = part.stat().st_size
            if not (spec["min_bytes"] <= size <= spec["max_bytes"]):
                part.unlink(missing_ok=True)
                return False, f"حجم {name} غير متوقع ({size} بايت) — لم أعتمده وحذفته."
            os.replace(part, target)
            done.append(f"{name} ({size // 1024} ك.ب في {time.time() - started:.1f}s)")
        except Exception as exc:  # noqa: BLE001
            try:
                part.unlink(missing_ok=True)
            except Exception:
                pass
            return False, f"تعذّر تنزيل {name}: {type(exc).__name__} — {str(exc)[:120]}"
    log("setup", "ok", detail={"files": done})
    return True, " · ".join(done)


# ── الكاشف والمُعرّف (كسولان: لا يُحمَّلان إلا عند أول استخدام) ─────────────────
_DETECTOR: dict[str, Any] = {"obj": None, "size": (320, 320), "score": 0.9}
_RECOGNIZER: dict[str, Any] = {"obj": None}


def reset_engine() -> None:
    """يُفرغ المحمّلين — يُستخدم في الفحوص وبعد تغيير النماذج."""
    _DETECTOR["obj"] = None
    _RECOGNIZER["obj"] = None


def detector(det_size: tuple[int, int] = (320, 320), score: float = 0.9):
    """يعيد كاشف YuNet، أو (None, رسالة). يستخدم إدخال 320×320 كما عايرنا."""
    ok_cv, why = _cv2_status()
    if not ok_cv:
        return None, why
    ready, why_models = models_ready()
    if not ready:
        return None, f"{why_models} — قل: face_auth action=setup"
    cv2 = _cv2()
    size = (int(det_size[0]), int(det_size[1]))
    if _DETECTOR["obj"] is not None and _DETECTOR["size"] == size and _DETECTOR["score"] == float(score):
        return _DETECTOR["obj"], ""
    try:
        net = cv2.FaceDetectorYN.create(str(model_path("yunet.onnx")), "", size,
                                        score_threshold=float(score), nms_threshold=0.3, top_k=50)
    except Exception as exc:  # noqa: BLE001
        return None, f"تعذّر فتح كاشف الوجه: {type(exc).__name__} — {str(exc)[:120]}"
    _DETECTOR.update({"obj": net, "size": size, "score": float(score)})
    return net, ""


def recognizer():
    ok_cv, why = _cv2_status()
    if not ok_cv:
        return None, why
    ready, why_models = models_ready()
    if not ready:
        return None, f"{why_models} — قل: face_auth action=setup"
    if _RECOGNIZER["obj"] is not None:
        return _RECOGNIZER["obj"], ""
    cv2 = _cv2()
    try:
        rec = cv2.FaceRecognizerSF.create(str(model_path("sface.onnx")), "")
    except Exception as exc:  # noqa: BLE001
        return None, f"تعذّر فتح مُعرّف الوجه: {type(exc).__name__} — {str(exc)[:120]}"
    _RECOGNIZER["obj"] = rec
    return rec, ""


def detect(frame, det_size: tuple[int, int] = (320, 320), score: float = 0.9) -> tuple[list[dict], str]:
    """يكشف الوجوه. يعيد قائمة، كل وجه: box · score · landmarks · raw (الصف الخام لـalignCrop)."""
    net, why = detector(det_size, score)
    if net is None:
        return [], why
    np = _np()
    try:
        height, width = frame.shape[:2]
        net.setInputSize((int(width), int(height)))
        _, faces = net.detect(frame)
    except Exception as exc:  # noqa: BLE001
        return [], f"فشل الكشف: {type(exc).__name__} — {str(exc)[:120]}"
    out: list[dict] = []
    if faces is None:
        return out, ""
    for row in np.asarray(faces, dtype=np.float32):
        values = [float(v) for v in row]
        if len(values) < 15:
            continue
        box = values[0:4]
        landmarks = [[values[4], values[5]], [values[6], values[7]], [values[8], values[9]],
                     [values[10], values[11]], [values[12], values[13]]]
        out.append({"box": box, "score": values[14], "landmarks": landmarks, "raw": values})
    return out, ""


def embed(frame, face: dict) -> tuple[Optional[Any], str]:
    """متجه SFace لوجه مكشوف (128 رقمًا). المسطّح يُسوّى هنا حتى لا ينفجر `match`."""
    rec, why = recognizer()
    if rec is None:
        return None, why
    np = _np()
    try:
        raw = np.asarray(face.get("raw") or [], dtype=np.float32)
        if raw.size < 14:
            return None, "بيانات الوجه غير كاملة."
        aligned = rec.alignCrop(frame, raw)
        feature = rec.feature(aligned)
        vector = np.asarray(feature, dtype=np.float32).ravel()
        if vector.size != DIM:
            return None, f"أبعاد المتجه غير متوقعة ({vector.size})."
        return vector, ""
    except Exception as exc:  # noqa: BLE001
        return None, f"تعذّر استخراج البصمة: {type(exc).__name__} — {str(exc)[:120]}"


# ── الرياضيات: المقارنة والعتبة ───────────────────────────────────────────────
def cosine(a, b) -> float:
    """جيب تمام الزاوية — بديل `rec.match` لأن الأخير ينهار على المتجهات المسطّحة."""
    np = _np()
    if np is None:
        return 0.0
    try:
        va = np.asarray(a, dtype=np.float32).ravel()
        vb = np.asarray(b, dtype=np.float32).ravel()
        na, nb = float(np.linalg.norm(va)), float(np.linalg.norm(vb))
        if na < 1e-9 or nb < 1e-9 or va.size != vb.size:
            return 0.0
        return float(np.dot(va, vb) / (na * nb))
    except Exception:
        return 0.0


def normalize(vector):
    np = _np()
    if np is None:
        return None
    v = np.asarray(vector, dtype=np.float32).ravel()
    n = float(np.linalg.norm(v))
    return v if n < 1e-9 else (v / n).astype(np.float32)


def calibrate(self_scores: list[float], impostor_scores: list[float], *,
              safety_frac: float = SAFETY_FRAC, floor: float = GENERAL_THRESHOLD) -> dict:
    """يعاير العتبة من بياناتك: threshold = max_impostor + 0.55 × margin.

    الأرقام المسجّلة عندنا: min_self = 0.740 · max_impostor = 0.265 · margin = 0.475
    → 0.265 + 0.55 × 0.475 = 0.526 — وهي عتبتك المعيَّرة.
    وإن كان الهامش سالبًا (وجه قريب من غريب) نقول ذلك بصراحة ولا نتظاهر بنجاح.
    """
    selfs = sorted(float(s) for s in (self_scores or []))
    imps = sorted(float(s) for s in (impostor_scores or []))
    min_self = selfs[0] if selfs else 0.0
    max_imp = imps[-1] if imps else floor
    margin = min_self - max_imp if selfs else 0.0
    raw = max_imp + float(safety_frac) * margin if selfs else floor
    threshold = round(max(float(floor), raw), 3)
    separated = bool(selfs) and margin > 0.10 and min_self > floor
    return {
        "threshold": threshold, "min_self": round(min_self, 3), "max_impostor": round(max_imp, 3),
        "margin": round(margin, 3), "samples": len(selfs), "impostors": len(imps),
        "floor": floor, "safety_frac": safety_frac, "separated": separated,
        "quality": quality_percent(min_self if selfs else 0.0),
    }


def quality_percent(mean_self: float) -> int:
    """جودة التسجيل: نسبة التشابه المتوسط إلى 0.85 سقفًا — 0.79 تعطي 93٪."""
    try:
        return int(round(100 * min(1.0, max(0.0, float(mean_self)) / 0.85)))
    except Exception:
        return 0


# ── البوّابات: ستّ بوّابات بالترتيب الصحيح ────────────────────────────────────
DEFAULTS = {
    "min_face_px": 140,        # أصغر ضلع مقبول للوجه وقت التدريب
    "min_area_frac": 0.005,    # أقل من 0.5٪ من الإطار = ضجيج، يُتجاهل
    "dominance": 0.80,         # وجه ثانٍ ≥ 0.80 × الأكبر يرفض الإطار
    "min_conf": 0.90,
    "facing_tol": 0.35,        # انحراف الأنف عن منتصف العينين ÷ مسافة العينين
    "clarity_min": 18.0,       # تباين لابلاس على قصّة الوجه
    "bright_min": 45.0, "bright_max": 225.0,
    "max_yaw_ratio": 0.45,
}


def _clarity(gray) -> float:
    cv2 = _cv2()
    np = _np()
    if cv2 is None or np is None:
        return 0.0
    try:
        return float(cv2.Laplacian(gray, cv2.CV_64F).var())
    except Exception:
        return 0.0


def evaluate_shot(frame, faces: list[dict], params: Optional[dict] = None) -> dict:
    """بوّابات التدريب الستّ. الترتيب مقصود: الحجم قبل تعدّد الوجوه.

    تُستخدم **وقت التدريب فقط** — التحقق لا يمرّ من هنا (العتبة وحدها تقرّر).
    """
    settings = dict(DEFAULTS)
    settings.update(params or {})
    np = _np()
    cv2 = _cv2()
    metrics: dict[str, Any] = {"faces": len(faces)}
    if np is None or cv2 is None:
        return {"ok": False, "key": "no_cv", "say": "بيئة الصور غير جاهزة", "metrics": metrics}
    height, width = frame.shape[:2]
    frame_area = float(height * width) or 1.0
    if not faces:
        return {"ok": False, "key": "no_face", "say": GATE_SAY["no_face"], "metrics": metrics}
    # 1) الحجم أولًا — قبل أي كلام عن تعدّد الوجوه
    biggest = max(faces, key=lambda f: float(f["box"][2]) * float(f["box"][3]))
    box_w, box_h = float(biggest["box"][2]), float(biggest["box"][3])
    side = min(box_w, box_h)
    metrics.update({"box": [round(box_w), round(box_h)], "side_px": round(side),
                    "score": round(float(biggest["score"]), 3)})
    if side < float(settings["min_face_px"]):
        return {"ok": False, "key": "too_small", "say": GATE_SAY["too_small"], "metrics": metrics}
    # 2) تعدّد الوجوه — بالاستثناءات: الضجيج يُتجاهل، والسيادة 0.80
    biggest_area = box_w * box_h
    rivals = []
    for face in faces:
        if face is biggest:
            continue
        area = float(face["box"][2]) * float(face["box"][3])
        if area / frame_area < float(settings["min_area_frac"]):
            continue                                   # ضجيج كاشف: يُتجاهل ولا يرفض
        if area >= float(settings["dominance"]) * biggest_area:
            rivals.append(area)
    if rivals:
        metrics["rivals"] = len(rivals)
        return {"ok": False, "key": "many_faces", "say": GATE_SAY["many_faces"], "metrics": metrics}
    # 3) ثقة الكشف
    confidence = float(biggest["score"])
    if confidence < float(settings["min_conf"]):
        return {"ok": False, "key": "low_conf", "say": GATE_SAY["low_conf"], "metrics": metrics}
    # 4) المواجهة: انحراف الأنف عن منتصف العينين، وقصر المسافة الأفقية
    landmarks = biggest["landmarks"]
    right_eye, left_eye, nose = landmarks[0], landmarks[1], landmarks[2]
    eye_mid_x = (right_eye[0] + left_eye[0]) / 2.0
    eye_mid_y = (right_eye[1] + left_eye[1]) / 2.0
    eye_dist = max(1.0, abs(left_eye[0] - right_eye[0]))
    horizontal = abs(nose[0] - eye_mid_x) / eye_dist
    vertical = abs(nose[1] - eye_mid_y) / max(1.0, box_h)
    metrics.update({"nose_off_x": round(horizontal, 3), "nose_off_y": round(vertical, 3),
                    "eye_dist": round(eye_dist)})
    if horizontal > float(settings["facing_tol"]) or vertical > float(settings["max_yaw_ratio"]):
        return {"ok": False, "key": "off_axis", "say": GATE_SAY["off_axis"], "metrics": metrics}
    # 5) الإضاءة والإضاءة الزائدة على الوجه نفسه
    x0 = max(0, int(biggest["box"][0]))
    y0 = max(0, int(biggest["box"][1]))
    x1 = min(width, int(x0 + box_w))
    y1 = min(height, int(y0 + box_h))
    crop = frame[y0:y1, x0:x1]
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop is not None and crop.size else None
    brightness = float(np.mean(gray)) if gray is not None else 0.0
    metrics["brightness"] = round(brightness, 1)
    if brightness < float(settings["bright_min"]):
        return {"ok": False, "key": "dark", "say": GATE_SAY["dark"], "metrics": metrics}
    if brightness > float(settings["bright_max"]):
        return {"ok": False, "key": "bright", "say": GATE_SAY["bright"], "metrics": metrics}
    # 6) الوضوح (لبناء المرجع نطلب الجودة العالية)
    clarity = _clarity(gray) if gray is not None else 0.0
    metrics["clarity"] = round(clarity, 1)
    if clarity < float(settings["clarity_min"]):
        return {"ok": False, "key": "blur", "say": GATE_SAY["blur"], "metrics": metrics}
    return {"ok": True, "key": "ok", "say": GATE_SAY["ok"], "metrics": metrics}


# ── فحص الحياة ────────────────────────────────────────────────────────────────
class BlinkTracker:
    """رمشة عبر تغيّر إضاءة منطقة العين — لا ندّعي قياس جفن بنقاط YuNet الخمس.

    نراقب متوسط إضاءة رقعتين صغيرتين حول العينين: الرمشة تُحدث هبوطًا سريعًا
    ثم عودة (هبوط ≥ dip_ratio من المستوى الراهن، بمدى 1-6 إطارات).
    """

    def __init__(self, dip_ratio: float = 0.12, window: int = 25):
        self.dip_ratio = float(dip_ratio)
        self.window = int(window)
        self.series: list[float] = []
        self.seen = False

    def _eye_level(self, frame, face: dict) -> Optional[float]:
        np, cv2 = _np(), _cv2()
        if np is None or cv2 is None:
            return None
        landmarks = face.get("landmarks") or []
        if len(landmarks) < 2:
            return None
        box = face.get("box") or [0, 0, 10, 10]
        half = max(4, int(min(float(box[2]), float(box[3])) * 0.10))
        height, width = frame.shape[:2]
        values = []
        for point in (landmarks[0], landmarks[1]):
            x, y = int(point[0]), int(point[1])
            x0, y0 = max(0, x - half), max(0, y - half)
            x1, y1 = min(width, x + half), min(height, y + half)
            patch = frame[y0:y1, x0:x1]
            if patch is None or patch.size == 0:
                continue
            gray = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)
            values.append(float(np.mean(gray)))
        if not values:
            return None
        return sum(values) / len(values)

    def update(self, frame, face: dict) -> bool:
        level = self._eye_level(frame, face)
        if level is None:
            return False
        self.series.append(level)
        if len(self.series) > self.window:
            self.series = self.series[-self.window:]
        if not self.seen and len(self.series) >= 4:
            baseline = sorted(self.series[:-1])[len(self.series[:-1]) // 2]   # الوسيط
            if baseline > 1.0 and (baseline - level) / baseline >= self.dip_ratio:
                self.seen = True
        return self.seen


class MotionTracker:
    """حركة رأس خفيفة: إزاحة الأنف عن منتصف العينين مقيسةً بمسافة العينين."""

    def __init__(self, min_shift: float = 0.06, window: int = 40):
        self.min_shift = float(min_shift)
        self.window = int(window)
        self.samples: list[tuple[float, float]] = []
        self.seen = False

    def update(self, frame, face: dict) -> bool:
        del frame
        landmarks = face.get("landmarks") or []
        if len(landmarks) < 3:
            return False
        right_eye, left_eye, nose = landmarks[0], landmarks[1], landmarks[2]
        eye_dist = max(1.0, abs(left_eye[0] - right_eye[0]))
        dx = (nose[0] - (right_eye[0] + left_eye[0]) / 2.0) / eye_dist
        dy = (nose[1] - (right_eye[1] + left_eye[1]) / 2.0) / eye_dist
        self.samples.append((dx, dy))
        if len(self.samples) > self.window:
            self.samples = self.samples[-self.window:]
        if not self.seen and len(self.samples) >= 5:
            xs = [s[0] for s in self.samples]
            ys = [s[1] for s in self.samples]
            if (max(xs) - min(xs)) >= self.min_shift or (max(ys) - min(ys)) >= self.min_shift:
                self.seen = True
        return self.seen


# ── الملف الشخصي: البصمة والعتبة وكلمة المرور الاحتياطية ──────────────────────
def profile_paths() -> tuple[Path, Path]:
    return PROFILE_FILE, EMBED_FILE


def profile_load() -> tuple[Optional[dict], Optional[Any], str]:
    """يعيد (profile, embedding, سبب). لا يرمي أبدًا."""
    np = _np()
    if not PROFILE_FILE.exists() or not EMBED_FILE.exists():
        return None, None, "لا توجد بصمة بعد — قل: face_auth action=train لتسجيل وجهك."
    try:
        profile = json.loads(PROFILE_FILE.read_text(encoding="utf-8"))
        if not isinstance(profile, dict) or "threshold" not in profile:
            return None, None, "ملف البصمة تالف."
    except Exception as exc:  # noqa: BLE001
        return None, None, f"تعذّرت قراءة ملف البصمة: {type(exc).__name__}"
    if np is None:
        return profile, None, "حزمة numpy غير مثبّتة — لا أستطيع قراءة المتجه."
    try:
        vector = np.load(str(EMBED_FILE))
    except Exception as exc:  # noqa: BLE001
        return profile, None, f"تعذّر قراءة المتجه: {type(exc).__name__}"
    if np.asarray(vector).ravel().size != DIM:
        return profile, None, "المتجه المحفوظ أبعاده غير سليمة."
    return profile, vector, ""


def profile_save(profile: dict, embedding) -> tuple[bool, str]:
    np = _np()
    if np is None:
        return False, "numpy غير مثبّتة."
    FACE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        vector = normalize(np.asarray(embedding, dtype=np.float32).ravel())
        if vector is None or vector.size != DIM:
            return False, "المتجه غير صالح للحفظ."
        np.save(str(EMBED_FILE), vector)
        PROFILE_FILE.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
        for path in (EMBED_FILE, PROFILE_FILE):
            try:
                os.chmod(path, 0o600)
            except Exception:
                pass
        return True, "حُفظت البصمة."
    except Exception as exc:  # noqa: BLE001
        return False, f"تعذّر حفظ البصمة: {type(exc).__name__} — {str(exc)[:120]}"


def profile_delete() -> tuple[bool, str]:
    removed = []
    for path in (PROFILE_FILE, EMBED_FILE):
        try:
            if path.exists():
                path.unlink()
                removed.append(path.name)
        except Exception:
            pass
    log("profile_delete", "ok", detail={"files": removed})
    return True, ("حُذفت البصمة: " + "، ".join(removed)) if removed else "لا بصمة موجودة لأحذفها."


def decide(similarity: float, profile: dict) -> dict:
    """قرار التحقق: العتبة وحدها — بلا بوّابات التدريب."""
    try:
        threshold = float(profile.get("threshold") or GENERAL_THRESHOLD)
    except Exception:
        threshold = GENERAL_THRESHOLD
    ok = float(similarity) >= threshold
    return {"ok": ok, "similarity": round(float(similarity), 3), "threshold": round(threshold, 3),
            "difference": round(float(similarity) - threshold, 3)}


# ── كلمة المرور الاحتياطية: الباب الذي لا يُغلق عليك أبدًا ─────────────────────
def _hash_password(password: str) -> tuple[bool, str]:
    salt = secrets.token_bytes(16)
    try:
        digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=16384, r=8, p=1, dklen=32)
        return True, json.dumps({"algo": "scrypt", "salt": salt.hex(), "hash": digest.hex(),
                                 "n": 16384, "r": 8, "p": 1}, ensure_ascii=False)
    except Exception:
        try:
            digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000, dklen=32)
            return True, json.dumps({"algo": "pbkdf2_sha256", "salt": salt.hex(), "hash": digest.hex(),
                                     "rounds": 200_000}, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)


def password_set(password: str) -> tuple[bool, str]:
    """يحفظ بصمة كلمة المرور (لا الكلمة نفسها) في ملف بصلاحية 600."""
    password = str(password or "")
    if len(password) < 6:
        return False, "كلمة المرور قصيرة — ست أحرف على الأقل."
    ok, blob = _hash_password(password)
    if not ok:
        return False, f"تعذّر حساب بصمة كلمة المرور: {blob}"
    FACE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        PASSWORD_FILE.write_text(blob, encoding="utf-8")
        try:
            os.chmod(PASSWORD_FILE, 0o600)
        except Exception:
            pass
        log("password_set", "ok")
        return True, "حفظت كلمة المرور الاحتياطية (بصمتها فقط، لا الكلمة)."
    except Exception as exc:  # noqa: BLE001
        return False, f"تعذّر الحفظ: {type(exc).__name__}"


def password_check(password: str) -> bool:
    """مقارنة ثابتة الزمن — بلا طباعة، وبلا تسجيل، وبلا كشف."""
    try:
        if not PASSWORD_FILE.exists():
            return False
        blob = json.loads(PASSWORD_FILE.read_text(encoding="utf-8"))
        salt = bytes.fromhex(str(blob.get("salt", "")))
        expected = bytes.fromhex(str(blob.get("hash", "")))
        if not salt or not expected:
            return False
        if blob.get("algo") == "pbkdf2_sha256":
            digest = hashlib.pbkdf2_hmac("sha256", str(password).encode("utf-8"), salt,
                                         int(blob.get("rounds") or 200_000), dklen=len(expected))
        else:
            digest = hashlib.scrypt(str(password).encode("utf-8"), salt=salt,
                                    n=int(blob.get("n") or 16384), r=int(blob.get("r") or 8),
                                    p=int(blob.get("p") or 1), dklen=len(expected))
        return secrets.compare_digest(digest, expected)
    except Exception:
        return False


def password_exists() -> bool:
    return PASSWORD_FILE.exists()


# ── المشهد: شبكة EDITH ────────────────────────────────────────────────────────
_MESH_N = 15          # 15×15 = 225 نقطة — العدد الذي عايرناه
_AMBER = (0, 191, 255)     # BGR — ذهبي/عنبري أثناء المسح
_GREEN = (90, 220, 90)
_RED = (70, 70, 235)
_DIM = (90, 110, 130)
_AMBER_DIM = (55, 105, 140)  # BGR — عنبري خافت للجزء الذي لم يصل إليه المسح بعد


def _mesh_points(box: list[float], landmarks: list[list[float]], amplitude: float = 0.18):
    """شبكة 225 نقطة تُسحب نحو الملامح بوزن غاوسي — انثناء الشبكة على الوجه."""
    np = _np()
    if np is None:
        return []
    x, y, w, h = (float(v) for v in box[:4])
    gx = np.linspace(x, x + w, _MESH_N)
    gy = np.linspace(y, y + h, _MESH_N)
    grid = np.stack(np.meshgrid(gx, gy), axis=-1).reshape(-1, 2).astype(np.float32)   # 225×2
    sigma = max(6.0, 0.30 * min(w, h))
    warped = grid.copy()
    for point in landmarks[:5]:
        fx, fy = float(point[0]), float(point[1])
        dx = grid[:, 0] - fx
        dy = grid[:, 1] - fy
        distance2 = dx * dx + dy * dy
        weight = np.exp(-distance2 / (2.0 * sigma * sigma))
        pull = np.clip(np.sqrt(distance2) * amplitude, 0.0, sigma * 0.6)
        scale = np.divide(pull, np.sqrt(distance2), out=np.zeros_like(pull), where=distance2 > 1e-6)
        warped[:, 0] -= (dx * scale) * weight
        warped[:, 1] -= (dy * scale) * weight
    return warped


def render_effect(frame, face: Optional[dict], *, progress: float = 1.0, status: str = "scan",
                  match: float = 0.0, threshold: float = GENERAL_THRESHOLD, phase: float = 0.0,
                  liveness: str = ""):
    """يرسم مشهد EDITH: شبكة 225 نقطة · مسح من الجبهة إلى الذقن · قراءات · تيار بيانات.

    status: scan (أثناء الكشف) · ok (قبول) · fail (رفض)
    النصوص على الشاشة إنجليزية كما في المشهد الأصلي (خطوط OpenCV لا ترسم العربية)،
    والكلام نفسه عربي عبر الواجهة النصّية.
    """
    cv2, np = _cv2(), _np()
    if cv2 is None or np is None:
        return frame
    canvas = frame.copy()
    height, width = canvas.shape[:2]
    accent = {"ok": _GREEN, "fail": _RED}.get(status, _AMBER)
    # تيار البيانات على اليسار
    for index in range(16):
        length = int(40 + 60 * abs(math.sin(phase * 1.7 + index * 0.55)) + (70 * match if status != "scan" else 0))
        y = 60 + index * 26
        cv2.line(canvas, (28, y), (28 + length, y), accent if index % 3 else _DIM, 2)
    if face is None:
        cv2.putText(canvas, "SCANNING...", (width // 2 - 90, height // 2), cv2.FONT_HERSHEY_DUPLEX,
                    0.9, accent, 2, cv2.LINE_AA)
        return canvas
    box = [float(v) for v in face["box"][:4]]
    landmarks = face.get("landmarks") or []
    points = _mesh_points(box, landmarks)
    top, bottom = box[1], box[1] + box[3]
    frontier = top + (bottom - top) * max(0.0, min(1.0, progress))
    for index, (px, py) in enumerate(points):
        if py <= frontier:
            color = accent
            radius = 1
        else:
            color = _AMBER_DIM
            radius = 1
        cv2.circle(canvas, (int(px), int(py)), radius, color, -1, cv2.LINE_AA)
        # خطوط الشبكة كاملة: ما وصل إليه المسح بعنبري، وما لم يصل بعد بعنبري خافت
        if index % _MESH_N != _MESH_N - 1 and index + 1 < len(points):
            nx2, ny2 = points[index + 1]
            line_color = accent if min(py, ny2) <= frontier else _AMBER_DIM
            cv2.line(canvas, (int(px), int(py)), (int(nx2), int(ny2)), line_color, 1, cv2.LINE_AA)
        if index + _MESH_N < len(points):
            bx, by = points[index + _MESH_N]
            line_color = accent if min(py, by) <= frontier else _AMBER_DIM
            cv2.line(canvas, (int(px), int(py)), (int(bx), int(by)), line_color, 1, cv2.LINE_AA)
    # خط المسح نفسه
    cv2.line(canvas, (int(box[0]), int(frontier)), (int(box[0] + box[2]), int(frontier)),
             accent, 2, cv2.LINE_AA)
    for corner in ((box[0], box[1]), (box[0] + box[2], box[1]), (box[0], box[1] + box[3]),
                   (box[0] + box[2], box[1] + box[3])):
        cv2.circle(canvas, (int(corner[0]), int(corner[1])), 3, accent, -1, cv2.LINE_AA)
    # لوحة القراءات بجوار الوجه — عمود التسميات 96px فلا يتلاصق النصّان أبدًا
    _LABEL_W, _PANEL_W = 96, 235
    panel_x = max(16, min(width - _PANEL_W - 12, int(box[0] + box[2] + 18)))
    panel_y = max(30, int(box[1]))
    lines = [
        ("DEPTH", f"{1.0 + 0.4 * (1.0 - min(1.0, box[2] / 300.0)):.2f} m"),
        ("MESH", f"{len(points)} PTS"),
        ("SCAN", f"{int(max(0.0, min(1.0, progress)) * 100)}%"),
        ("MATCH", f"{match:.2f} / {threshold:.2f}"),
        ("LIVENESS", (liveness or "waiting").upper()),
    ]
    cv2.rectangle(canvas, (panel_x - 8, panel_y - 16), (panel_x + _PANEL_W, panel_y + 26 * len(lines)),
                  (12, 18, 24), -1)
    for row, (label, value) in enumerate(lines):
        y = panel_y + row * 26
        cv2.putText(canvas, label, (panel_x, y), cv2.FONT_HERSHEY_PLAIN, 1.1, _DIM, 1, cv2.LINE_AA)
        cv2.putText(canvas, str(value)[:15], (panel_x + _LABEL_W, y), cv2.FONT_HERSHEY_PLAIN, 1.2,
                    accent, 1, cv2.LINE_AA)
    # الخاتمة
    if status == "ok":
        cv2.putText(canvas, "WELCOME BACK, SIR", (int(box[0]), int(box[1]) - 16),
                    cv2.FONT_HERSHEY_DUPLEX, 0.7, _GREEN, 2, cv2.LINE_AA)
        check = (int(box[0] + box[2] - 26), int(box[1] - 26))
        cv2.line(canvas, (check[0] - 12, check[1]), (check[0] - 4, check[1] + 8), _GREEN, 2, cv2.LINE_AA)
        cv2.line(canvas, (check[0] - 4, check[1] + 8), (check[0] + 12, check[1] - 10), _GREEN, 2, cv2.LINE_AA)
    elif status == "fail":
        cv2.putText(canvas, "ACCESS DENIED", (int(box[0]), int(box[1]) - 16),
                    cv2.FONT_HERSHEY_DUPLEX, 0.7, _RED, 2, cv2.LINE_AA)
    else:
        cv2.putText(canvas, "EDITH // FACE AUTH", (28, 36), cv2.FONT_HERSHEY_PLAIN, 1.4, accent, 1, cv2.LINE_AA)
    return canvas


# ── الكاميرا ──────────────────────────────────────────────────────────────────
def open_camera(index: int = 0, backend: str = "auto"):
    """يفتح الكاميرا. يعيد (cap, رسالة) ولا يرمي أبدًا."""
    cv2 = _cv2()
    if cv2 is None:
        return None, "حزمة opencv غير مثبّتة:  pip install opencv-python"
    try:
        index = int(index)
    except Exception:
        index = 0
    attempts = []
    if backend == "dshow" or (backend == "auto" and os.name == "nt"):
        attempts.append(("CAP_DSHOW", getattr(cv2, "CAP_DSHOW", None)))
    attempts.append(("افتراضي", None))
    for label, api in attempts:
        try:
            cap = cv2.VideoCapture(index, api) if api is not None else cv2.VideoCapture(index)
        except Exception:
            continue
        if cap is not None and cap.isOpened():
            try:
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
            except Exception:
                pass
            return cap, ""
        try:
            if cap is not None:
                cap.release()
        except Exception:
            pass
    return None, (f"تعذّر فتح الكاميرا رقم {index} ({'، '.join(a[0] for a in attempts)}). "
                  f"راجع أن الكاميرا غير مستخدمة ببرنامج آخر، وحدّد رقمها من الإعدادات.")


def read_frame(cap) -> tuple[Optional[Any], str]:
    if cap is None:
        return None, "الكاميرا غير مفتوحة."
    try:
        ok, frame = cap.read()
        if not ok or frame is None:
            return None, "لم تصل صورة من الكاميرا."
        return frame, ""
    except Exception as exc:  # noqa: BLE001
        return None, f"فشل قراءة الإطار: {type(exc).__name__}"


if __name__ == "__main__":
    print("_face_core — مكتبة مشتركة للتحقق من الوجه، لا تعمل وحدها.")
    ok_cv, why = _cv2_status()
    print("cv2/numpy:", "جاهزة" if ok_cv else why)
    ready, note = models_ready()
    print("النماذج:", "جاهزة" if ready else note)
    print("مثال العتبة من أرقامنا:", calibrate([0.740], [0.265])["threshold"])
