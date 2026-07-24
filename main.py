"""検図 KENZU — 電気通信施工管理 第二次検定 図記号手描き判定。

Gemini は画像内の特徴観察のみを担当し、合否はアプリケーションコードで決定する。
"""
from __future__ import annotations

import base64
import binascii
import datetime
import io
import json
import logging
import os
import random
import threading
import time
import uuid
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, TypedDict

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from google import genai
from google.genai import types
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

ROOT = Path(__file__).resolve().parent
logger = logging.getLogger("kenzu")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

MAX_IMAGE_B64_CHARS = int(os.environ.get("MAX_IMAGE_B64_CHARS", "1500000"))
MAX_IMAGE_BYTES = int(os.environ.get("MAX_IMAGE_BYTES", "1000000"))
# 送信前に許容する画像の最長辺(px)。これを超えたらサーバー側で縮小してから判定する。
MAX_IMAGE_DIM = int(os.environ.get("MAX_IMAGE_DIM", "1024"))
MAX_IMAGE_PIXELS = int(os.environ.get("MAX_IMAGE_PIXELS", "4000000"))
MIN_INK_PIXELS = int(os.environ.get("MIN_INK_PIXELS", "20"))
INK_THRESHOLD = int(os.environ.get("INK_THRESHOLD", "245"))
MAX_OBSERVATION_CHARS = 500
RATE_LIMIT = int(os.environ.get("RATE_LIMIT", "20"))
RATE_WINDOW = int(os.environ.get("RATE_WINDOW", "60"))
DEFAULT_GEMINI_MODEL = "gemini-3.1-flash-lite"
FEEDBACK_BUCKET = os.environ.get("FEEDBACK_BUCKET", "")
ALL_JUDGMENTS_BUCKET = os.environ.get("ALL_JUDGMENTS_BUCKET", "")


def _load_symbols() -> dict[str, dict[str, Any]]:
    try:
        payload = json.loads((ROOT / "symbols.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("symbols.json could not be loaded") from exc

    symbols = payload.get("symbols")
    if not isinstance(symbols, list) or not symbols:
        raise RuntimeError("symbols.json must contain a non-empty symbols array")

    result: dict[str, dict[str, Any]] = {}
    for index, symbol in enumerate(symbols):
        if not isinstance(symbol, dict):
            raise RuntimeError(f"symbols[{index}] must be an object")
        symbol_id = symbol.get("id")
        required_features = symbol.get("required_features")
        forbidden_features = symbol.get("forbidden_features", [])
        confusable_symbols = symbol.get("confusable_symbols", [])
        if not isinstance(symbol_id, str) or not symbol_id:
            raise RuntimeError(f"symbols[{index}].id is invalid")
        if symbol_id in result:
            raise RuntimeError(f"duplicate symbol id: {symbol_id}")
        if not isinstance(required_features, list) or not required_features or not all(isinstance(v, str) and v for v in required_features):
            raise RuntimeError(f"symbol {symbol_id} must have non-empty required_features")
        if not isinstance(forbidden_features, list) or not all(isinstance(v, str) and v for v in forbidden_features):
            raise RuntimeError(f"symbol {symbol_id} has invalid forbidden_features")
        if not isinstance(confusable_symbols, list) or not all(isinstance(v, str) and v for v in confusable_symbols):
            raise RuntimeError(f"symbol {symbol_id} has invalid confusable_symbols")
        result[symbol_id] = symbol

    if not any(bool(symbol.get("verified")) for symbol in result.values()):
        raise RuntimeError("at least one verified symbol is required")
    return result


SYMBOLS = _load_symbols()
_clients: dict[str, genai.Client] = {}
_storage_client: Any = None
_client_lock = threading.Lock()
_storage_lock = threading.Lock()
_rate_limited_keys: dict[str, tuple[float, int]] = {}  # APIキー → (レート制限時刻, 連続失敗回数)
_rate_limit_lock = threading.Lock()

# 段階的な指数バックオフ（秒単位）
# インデックス = 連続失敗回数 - 1（0-indexed）
_BACKOFF_SECONDS = [
    75,      # 失敗1回目：75秒（1分 + 15秒バッファ）
    600,     # 失敗2回目：10分
    3600,    # 失敗3回目：1時間
    10800,   # 失敗4回目：3時間
    18000,   # 失敗5回目：5時間
    36000,   # 失敗6回目：10時間
    86400,   # 失敗7回目以上：24時間
]


class RateLimitStatus(TypedDict):
    """APIキーのレート制限状態情報。"""

    remaining: float
    consecutive_count: int
    backoff_seconds: int


def _split_env_list(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _gemini_models(key_label: str | None = None) -> list[str]:
    """Return model fallback order by account type.

    Free tier (primary/extra_*): 3.1 Flash Lite → 3.5 Flash
    Paid tier (paid): 3.1 Flash Lite (cost-effective) → 3.5 Flash
    """
    if key_label == "paid":
        # Paid accounts: prioritize cost-effective 3.1 Flash Lite
        paid_models = _split_env_list(os.environ.get("GEMINI_MODELS_PAID"))
        if paid_models:
            return paid_models
        return ["gemini-3.1-flash-lite", "gemini-3.5-flash"]
    else:
        # Free tier: 3.1 Flash Lite → 3.5 Flash
        free_models = _split_env_list(os.environ.get("GEMINI_MODELS_FREE"))
        if free_models:
            return free_models
        return ["gemini-3.1-flash-lite", "gemini-3.5-flash"]


def _gemini_api_keys() -> list[tuple[str, str]]:
    """Return API keys in cost-priority order: free/default first, paid last."""
    keys: list[tuple[str, str]] = []
    primary = os.environ.get("GEMINI_API_KEY")
    if primary:
        keys.append(("primary", primary))
    for index, api_key in enumerate(_split_env_list(os.environ.get("GEMINI_API_KEYS")), start=1):
        if api_key not in [value for _, value in keys]:
            keys.append((f"extra_{index}", api_key))
    paid = os.environ.get("GEMINI_PAID_API_KEY")
    if paid and paid not in [value for _, value in keys]:
        keys.append(("paid", paid))
    return keys


def _get_genai_client(api_key: str | None = None) -> genai.Client:
    if api_key is None:
        keys = _gemini_api_keys()
        if not keys:
            raise HTTPException(503, "judgment service is not configured")
        api_key = keys[0][1]
    with _client_lock:
        client = _clients.get(api_key)
        if client is None:
            client = genai.Client(api_key=api_key)
            _clients[api_key] = client
    return client


def _get_rate_limit_status(api_key: str) -> RateLimitStatus | None:
    """APIキーのレート制限状態を確認（ロック保持）。

    戻り値：
      None: レート制限なし
      RateLimitStatus: 制限中の詳細情報
    """
    now = time.time()
    with _rate_limit_lock:
        if api_key not in _rate_limited_keys:
            return None

        limited_at, consecutive_count = _rate_limited_keys[api_key]

        # 連続失敗回数に応じたバックオフ時間を取得（0-indexed）
        backoff_index = min(consecutive_count - 1, len(_BACKOFF_SECONDS) - 1)
        backoff_seconds = _BACKOFF_SECONDS[backoff_index]
        elapsed = now - limited_at

        if elapsed >= backoff_seconds:
            del _rate_limited_keys[api_key]
            return None

        return {
            "remaining": backoff_seconds - elapsed,
            "consecutive_count": consecutive_count,
            "backoff_seconds": backoff_seconds,
        }


def _mark_rate_limited(api_key: str) -> None:
    """429エラーを記録し、バックオフカウントをインクリメント（ロック保持）。"""
    now = time.time()
    with _rate_limit_lock:
        if api_key in _rate_limited_keys:
            limited_at, consecutive_count = _rate_limited_keys[api_key]
            consecutive_count = min(consecutive_count + 1, len(_BACKOFF_SECONDS))
        else:
            consecutive_count = 1
        _rate_limited_keys[api_key] = (now, consecutive_count)


def _generate_vision_result(image: bytes, prompt: str, symbol_id: str) -> VisionResult:
    api_keys = _gemini_api_keys()
    if not api_keys:
        raise HTTPException(503, "judgment service is not configured")

    last_error: Exception | None = None
    for key_label, api_key in api_keys:
        # レート制限中のキーはスキップ
        rate_limit_info = _get_rate_limit_status(api_key)
        if rate_limit_info:
            if rate_limit_info["consecutive_count"] >= 3:
                logger.warning(
                    "API key in backoff due to repeated rate limits; skipping",
                    extra={
                        "key_label": key_label,
                        "consecutive_failures": rate_limit_info["consecutive_count"],
                        "backoff_minutes": rate_limit_info["backoff_seconds"] / 60,
                        "remaining_seconds": max(0, int(rate_limit_info["remaining"])),
                    },
                )
            else:
                logger.debug(
                    "API key temporarily rate limited; skipping",
                    extra={
                        "key_label": key_label,
                        "consecutive_failures": rate_limit_info["consecutive_count"],
                        "remaining_seconds": max(0, int(rate_limit_info["remaining"])),
                    },
                )
            continue

        models = _gemini_models(key_label)
        if not models:
            raise HTTPException(503, "judgment model is not configured")
        for model in models:
            try:
                response = _get_genai_client(api_key).models.generate_content(
                    model=model,
                    contents=[types.Part.from_bytes(data=image, mime_type="image/png"), prompt],
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=VisionResult,
                        temperature=0.0,
                    ),
                )
                return VisionResult.model_validate_json(response.text or "")
            except ValidationError as exc:
                last_error = exc
                logger.warning(
                    "invalid Gemini response; trying next Gemini candidate",
                    extra={"symbol_id": symbol_id, "model": model, "key_label": key_label},
                )
            except Exception as exc:
                last_error = exc
                # レート制限エラー（429）を検出したら、このキーを記録して次のキーを試す
                if getattr(exc, "status_code", None) == 429:
                    _mark_rate_limited(api_key)
                    logger.warning(
                        "rate limit reached; marking API key as rate limited",
                        extra={"symbol_id": symbol_id, "key_label": key_label},
                    )
                    break  # このキーのモデル試行をスキップして次のキーへ
                logger.warning(
                    "Gemini candidate failed; trying next Gemini candidate",
                    extra={"symbol_id": symbol_id, "model": model, "key_label": key_label},
                )

    error_msg = str(last_error) if last_error else "unknown error"
    logger.error(
        "all Gemini candidates failed (rate limiting or API unavailable)",
        extra={"symbol_id": symbol_id, "error": error_msg},
    )
    raise HTTPException(503, "judgment service unavailable") from last_error


def _decode_png(image_b64: str) -> bytes:
    if len(image_b64) > MAX_IMAGE_B64_CHARS:
        raise HTTPException(413, "image too large")

    raw = image_b64.split(",", 1)[-1]
    try:
        image = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(400, "invalid base64 image") from exc

    if not image:
        raise HTTPException(400, "empty image")
    if len(image) > MAX_IMAGE_BYTES:
        raise HTTPException(413, "image too large")
    if not image.startswith(b"\x89PNG\r\n\x1a\n"):
        raise HTTPException(400, "PNG image required")
    return _validate_and_prepare_png(image)


def _validate_and_prepare_png(image: bytes) -> bytes:
    """PNG を検証し、空画像・巨大画像を Gemini に送らない形に正規化する。

    クライアント側の 512px 正規化は UX 用の補助にすぎないため、課金と安全性の
    境界はサーバー側で確定する。画像として壊れている、ピクセル数が多すぎる、
    またはほぼ白紙のリクエストは外部 API 呼び出し前に拒否する。
    """
    try:
        with Image.open(io.BytesIO(image)) as img:
            if img.format != "PNG":
                raise HTTPException(400, "PNG image required")
            if img.width <= 0 or img.height <= 0 or img.width * img.height > MAX_IMAGE_PIXELS:
                raise HTTPException(413, "image too large")
            img.load()
            flat = _flatten_image(img)
            if _count_ink_pixels(flat) < MIN_INK_PIXELS:
                raise HTTPException(400, "empty drawing")
            longest = max(flat.size)
            if longest > MAX_IMAGE_DIM:
                scale = MAX_IMAGE_DIM / longest
                new_size = (max(1, round(flat.width * scale)), max(1, round(flat.height * scale)))
                flat = flat.resize(new_size, Image.LANCZOS)
            out = io.BytesIO()
            flat.save(out, format="PNG", optimize=True)
            return out.getvalue()
    except HTTPException:
        raise
    except Exception as exc:
        logger.info("invalid PNG image rejected")
        raise HTTPException(400, "invalid PNG image") from exc


def _flatten_image(img: Image.Image) -> Image.Image:
    if img.mode in ("RGBA", "LA", "P"):
        rgba = img.convert("RGBA")
        bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        return Image.alpha_composite(bg, rgba).convert("RGB")
    return img.convert("RGB")


def _count_ink_pixels(img: Image.Image) -> int:
    gray = img.convert("L")
    return sum(1 for value in gray.getdata() if value < INK_THRESHOLD)


def _save_to_gcs(
    bucket_name: str,
    symbol_id: str,
    image: bytes,
    judgment_data: dict[str, Any],
    prefix: str = "judgments",
) -> None:
    """GCS にエンドポイントの判定データと画像を保存する。失敗は判定処理に波及させない。"""
    global _storage_client
    if not bucket_name:
        return
    try:
        if _storage_client is None:
            with _storage_lock:
                if _storage_client is None:
                    from google.cloud import storage

                    _storage_client = storage.Client()
        bucket = _storage_client.bucket(bucket_name)
        day = datetime.date.today().isoformat()
        name = f"{prefix}/{symbol_id}/{day}/{uuid.uuid4().hex}"
        bucket.blob(f"{name}.png").upload_from_string(image, content_type="image/png")
        bucket.blob(f"{name}.json").upload_from_string(
            json.dumps(judgment_data, ensure_ascii=False), content_type="application/json"
        )
        logger.debug(f"saved to {bucket_name}/{name}", extra={"symbol_id": symbol_id})
    except Exception:
        logger.exception(
            "failed to save judgment data to GCS",
            extra={"symbol_id": symbol_id, "bucket": bucket_name, "prefix": prefix},
        )


def _save_feedback(symbol_id: str, image: bytes, judgment: dict[str, Any]) -> None:
    """明示的な異議報告だけを匿名保存する。失敗は判定処理に波及させない。"""
    judgment["disputed"] = True
    _save_to_gcs(FEEDBACK_BUCKET, symbol_id, image, judgment, prefix="disputed")


app = FastAPI(title="KENZU", docs_url=None, redoc_url=None)


def _page(name: str, request: Request) -> HTMLResponse:
    html = (ROOT / name).read_text(encoding="utf-8")
    return HTMLResponse(html.replace("__BASE_URL__", str(request.base_url)))


@app.exception_handler(404)
async def not_found(request: Request, exc: Exception) -> HTMLResponse:
    del request, exc
    html = """<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>404 — 配線用図記号ドリル</title><link rel="stylesheet" href="/theme.css"></head>
<body style="display:flex;flex-direction:column;align-items:center;justify-content:center;padding:40px 20px;text-align:center">
<div class="box" style="padding:28px 32px;max-width:340px"><p class="label-caps" style="color:var(--on-surface-variant);margin-bottom:10px">Sheet Not Found</p>
<p style="font-size:44px;font-weight:700;color:var(--primary);font-family:var(--mono)">404</p>
<p style="font-size:13px;color:var(--on-surface-variant);margin:12px 0 20px;line-height:1.7">この図面番号のページは存在しません。</p>
<a class="btn btn-primary" href="/" style="display:inline-block;text-decoration:none">表紙へ戻る</a></div></body></html>"""
    return HTMLResponse(html, status_code=404)


_hits: dict[str, deque[float]] = defaultdict(deque)
_hits_lock = threading.Lock()


def _client_ip(request: Request) -> str:
    # Cloud Run 直公開では request.client.host を使う。X-Forwarded-For はクライアント偽装を避けるため参照しない。
    return request.client.host if request.client else "unknown"


def _check_rate(request: Request) -> None:
    now = time.monotonic()
    ip = _client_ip(request)
    with _hits_lock:
        queue = _hits[ip]
        while queue and now - queue[0] > RATE_WINDOW:
            queue.popleft()
        if len(queue) >= RATE_LIMIT:
            raise HTTPException(429, "しばらく待ってから再度お試しください")
        queue.append(now)
        # 長時間使われていないキーを定期的に掃除する。
        if len(_hits) > 10_000:
            stale = [key for key, values in _hits.items() if not values or now - values[-1] > RATE_WINDOW]
            for key in stale[:5_000]:
                _hits.pop(key, None)


class JudgeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol_id: str = Field(min_length=1, max_length=100)
    image_b64: str = Field(min_length=1, max_length=MAX_IMAGE_B64_CHARS)


class VisionResult(BaseModel):
    model_config = ConfigDict(strict=True)
    required: list[bool] = Field(default_factory=list)
    forbidden: list[bool] = Field(default_factory=list)
    confusions: list[bool] = Field(default_factory=list)
    observation: str = Field(default="", max_length=MAX_OBSERVATION_CHARS)

    @staticmethod
    def _at(values: list[bool], index: int) -> bool:
        return values[index] if 0 <= index < len(values) else False


class ReportCheck(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    feature: str = Field(min_length=1, max_length=300)
    ok: bool


class ReportJudgment(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    passed: bool
    checks: list[ReportCheck] = Field(max_length=50)
    mistakes: list[str] = Field(default_factory=list, max_length=50)
    observation: str = Field(default="", max_length=MAX_OBSERVATION_CHARS)

    @field_validator("mistakes")
    @classmethod
    def validate_mistakes(cls, values: list[str]) -> list[str]:
        if any(not value or len(value) > 300 for value in values):
            raise ValueError("invalid mistake")
        return values


class ReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol_id: str = Field(min_length=1, max_length=100)
    image_b64: str = Field(min_length=1, max_length=MAX_IMAGE_B64_CHARS)
    judgment: ReportJudgment


@app.get("/")
def landing(request: Request) -> HTMLResponse:
    return _page("landing.html", request)


@app.get("/og.png")
def og() -> FileResponse:
    return FileResponse(ROOT / "og.png", media_type="image/png")


@app.get("/favicon.ico")
def favicon() -> FileResponse:
    return FileResponse(ROOT / "favicon.ico", media_type="image/x-icon")


@app.get("/favicon-32.png")
def favicon32() -> FileResponse:
    return FileResponse(ROOT / "favicon-32.png", media_type="image/png")


@app.get("/apple-touch-icon.png")
def apple_icon() -> FileResponse:
    return FileResponse(ROOT / "apple-touch-icon.png", media_type="image/png")


@app.get("/theme.css")
def theme() -> FileResponse:
    return FileResponse(ROOT / "theme.css", media_type="text/css")


@app.get("/drill")
def drill(request: Request) -> HTMLResponse:
    return _page("drill.html", request)


@app.get("/standards")
def standards(request: Request) -> HTMLResponse:
    return _page("standards.html", request)


@app.get("/api/catalog")
def catalog() -> list[dict[str, Any]]:
    """解説ページ用: 収録記号の全情報(お手本SVG・解説・判定ポイント)"""
    return [
        {
            "id": s["id"],
            "name": s["name"],
            "category": s["category"],
            "description": s.get("description", ""),
            "ref_svg": s.get("ref_svg", ""),
            "required_features": s.get("required_features", []),
            "common_mistakes": s.get("common_mistakes", []),
        }
        for s in SYMBOLS.values() if s["verified"]
    ]


@app.get("/api/symbols")
def list_symbols() -> list[dict[str, Any]]:
    return [
        {"id": s["id"], "name": s["name"], "category": s["category"], "verified": s["verified"]}
        for s in SYMBOLS.values()
    ]


@app.get("/api/question")
def question() -> dict[str, Any]:
    pool = [symbol for symbol in SYMBOLS.values() if symbol["verified"]]
    symbol = random.choice(pool)
    return {"id": symbol["id"], "name": symbol["name"], "category": symbol["category"]}


@app.post("/api/judge")
def judge(req: JudgeRequest, request: Request) -> dict[str, Any]:
    _check_rate(request)
    symbol = SYMBOLS.get(req.symbol_id)
    if not symbol:
        raise HTTPException(404, "unknown symbol")
    image = _decode_png(req.image_b64)

    required_features: list[str] = symbol["required_features"]
    forbidden_features: list[str] = symbol.get("forbidden_features", [])
    confusable_symbols: list[str] = symbol.get("confusable_symbols", [])

    prompt = f"""あなたは施工図の図記号を厳密に識別する採点補助です。
画像は受験者が手描きした電気設備の図記号で、課題は「{symbol['name']}」です。

判定方針:
- 線の多少の歪み、傾き、太さ、位置ずれは許容する。
- ただし、本数、接続関係、貫通、内外、塗りつぶし、文字、方向など、記号を識別する位相的特徴は厳密に判定する。
- ここでの「左・右・上・下」は画像の見た目どおりの方向を指す。左右反転・上下反転・180度回転で指定と逆になっている場合は、該当する必須特徴を false、対応する禁止特徴を true にする。
- 見えない特徴を推測で true にしない。
- 対象記号らしく見えても、禁止特徴または類似記号の決定的特徴があれば明示する。
- 各項目を独立に評価し、指定JSON以外を返さない。
- required/forbidden/confusions は、下記の各番号(0,1,2...)に対応する true/false を、その順序どおりに並べた配列で返す。

必須特徴(required): 画像に存在すれば true
{json.dumps({str(i): f for i, f in enumerate(required_features)}, ensure_ascii=False, indent=2)}

禁止特徴(forbidden): 画像に存在すれば true。1つでも true なら不合格
{json.dumps({str(i): f for i, f in enumerate(forbidden_features)}, ensure_ascii=False, indent=2)}

類似記号(confusions): 画像がその記号の決定的特徴を持ち、課題よりその記号に見える場合 true
{json.dumps({str(i): name for i, name in enumerate(confusable_symbols)}, ensure_ascii=False, indent=2)}

observationには、最も重要な根拠を日本語で簡潔に記述してください。"""

    result = _generate_vision_result(image, prompt, req.symbol_id)

    checks: list[dict[str, Any]] = []
    for index, feature in enumerate(required_features):
        checks.append({"feature": f"必須: {feature}", "ok": result._at(result.required, index)})
    for index, feature in enumerate(forbidden_features):
        checks.append({"feature": f"除外: {feature}がない", "ok": not result._at(result.forbidden, index)})
    for index, name in enumerate(confusable_symbols):
        checks.append({"feature": f"識別: {name}の決定的特徴ではない", "ok": not result._at(result.confusions, index)})

    failed_required = [
        feature for index, feature in enumerate(required_features)
        if not result._at(result.required, index)
    ]
    hit_forbidden = [
        feature for index, feature in enumerate(forbidden_features)
        if result._at(result.forbidden, index)
    ]
    hit_confusions = [
        name for index, name in enumerate(confusable_symbols)
        if result._at(result.confusions, index)
    ]
    mistakes = [f"必須特徴が不足: {value}" for value in failed_required]
    mistakes += [f"対象外の特徴を検出: {value}" for value in hit_forbidden]
    mistakes += [f"{value}と判別できない可能性があります" for value in hit_confusions]

    n_ok = sum(check["ok"] for check in checks)
    passed = n_ok == len(checks)
    score = f"{n_ok}/{len(checks)}"

    # GCS に全判定を保存
    judgment_data = {
        "symbol_id": req.symbol_id,
        "passed": passed,
        "score": score,
        "checks": checks,
        "mistakes": mistakes,
        "observation": result.observation,
        "disputed": False,
        "saved_type": "judgment",
    }
    _save_to_gcs(ALL_JUDGMENTS_BUCKET, req.symbol_id, image, judgment_data, prefix="judgments")

    return {
        "symbol_id": req.symbol_id,
        "passed": passed,
        "score": score,
        "checks": checks,
        "mistakes": mistakes,
        "observation": result.observation,
        "ref_svg": symbol.get("ref_svg", ""),
    }


@app.post("/api/report")
def report(req: ReportRequest, request: Request) -> dict[str, bool]:
    _check_rate(request)
    symbol = SYMBOLS.get(req.symbol_id)
    if not symbol:
        raise HTTPException(404, "unknown symbol")
    image = _decode_png(req.image_b64)

    expected_features = [f"必須: {value}" for value in symbol["required_features"]]
    expected_features += [f"除外: {value}がない" for value in symbol.get("forbidden_features", [])]
    expected_features += [f"識別: {value}の決定的特徴ではない" for value in symbol.get("confusable_symbols", [])]
    received_features = [check.feature for check in req.judgment.checks]
    if received_features != expected_features:
        raise HTTPException(400, "judgment does not match symbol")

    judgment = req.judgment.model_dump()
    judgment.update(
        {
            "disputed": True,
            "symbol_id": req.symbol_id,
            "date": datetime.date.today().isoformat(),
        }
    )
    _save_feedback(req.symbol_id, image, judgment)
    return {"ok": True}


@app.get("/healthz")
def healthz() -> dict[str, bool]:
    return {"ok": True}


@app.get("/readyz")
def readyz() -> dict[str, Any]:
    if not SYMBOLS:
        raise HTTPException(503, "no symbols loaded")
    if not _gemini_api_keys():
        raise HTTPException(503, "Gemini is not configured")
    return {"ok": True, "symbols": len(SYMBOLS), "feedback_enabled": bool(FEEDBACK_BUCKET)}
