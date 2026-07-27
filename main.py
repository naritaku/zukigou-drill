"""検図 KENZU — 電気通信施工管理 第二次検定 図記号手描き判定。

Gemini は画像内の特徴観察のみを担当し、合否はアプリケーションコードで決定する。
"""
from __future__ import annotations

import base64
import binascii
import datetime
import hashlib
import html
import io
import json
import logging
import os
import random
import re
import threading
import time
import uuid
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, TypedDict
from urllib.parse import urlsplit

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from google import genai
from google.cloud import firestore
from google.genai import types
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

ROOT = Path(__file__).resolve().parent
logger = logging.getLogger("kenzu")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        logger.warning("invalid integer environment variable; using default", extra={"name": name})
        return default


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
DAILY_JUDGE_LIMIT = _env_int("DAILY_JUDGE_LIMIT", 1000)
DAILY_PAID_LIMIT = _env_int("DAILY_PAID_LIMIT", 100)
DAILY_IP_LIMIT = _env_int("DAILY_IP_LIMIT", 50)
QUOTA_SHARDS = max(1, _env_int("QUOTA_SHARDS", 10))
MAX_INSTANCES = max(1, _env_int("MAX_INSTANCES", 2))
JUDGMENT_RECORD_TTL = _env_int("JUDGMENT_RECORD_TTL", 3600)
DAILY_IP_SALT = os.environ.get("DAILY_IP_SALT", "zukigou-drill")
# Cloud Run / ロードバランサ配下では X-Forwarded-For の末尾が実クライアント。
# プロキシを介さず直接公開する場合は "0" にして接続元 IP を使う。
TRUST_FORWARDED_FOR = os.environ.get("TRUST_FORWARDED_FOR", "1") not in ("0", "false", "False")
# X-Forwarded-For の末尾から数えて何番目をクライアントとみなすか（1 = 末尾）。
# 「信頼するプロキシ段数」ではない点に注意（1 は 0 段読み飛ばす、の意味）。
CLIENT_IP_INDEX_FROM_END = max(1, _env_int("CLIENT_IP_INDEX_FROM_END", 1))
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").strip()
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
        name = symbol.get("name")
        category = symbol.get("category")
        verified = symbol.get("verified")
        if not isinstance(symbol_id, str) or not symbol_id:
            raise RuntimeError(f"symbols[{index}].id is invalid")
        if symbol_id in result:
            raise RuntimeError(f"duplicate symbol id: {symbol_id}")
        if not isinstance(required_features, list) or not required_features or not all(isinstance(v, str) and v for v in required_features):
            raise RuntimeError(f"symbol {symbol_id} must have non-empty required_features")
        if not isinstance(forbidden_features, list) or not all(isinstance(v, str) and v for v in forbidden_features):
            raise RuntimeError(f"symbol {symbol_id} has invalid forbidden_features")
        # /api/symbols と /api/catalog が添字アクセスする項目。ここで検証しておかないと
        # 欠損に気付くのが起動後のリクエスト時（500）になる。
        if not isinstance(name, str) or not name.strip():
            raise RuntimeError(f"symbol {symbol_id} has invalid name")
        if not isinstance(category, str) or not category.strip():
            raise RuntimeError(f"symbol {symbol_id} has invalid category")
        if not isinstance(verified, bool):
            raise RuntimeError(f"symbol {symbol_id} has invalid verified")
        result[symbol_id] = symbol

    if not any(bool(symbol.get("verified")) for symbol in result.values()):
        raise RuntimeError("at least one verified symbol is required")
    return result


SYMBOLS = _load_symbols()
_clients: dict[str, genai.Client] = {}
_storage_client: Any = None
_firestore_client: Any = None
_firestore_unavailable = False  # 初期化に一度失敗したら以後は試行せず即メモリfallback
_client_lock = threading.Lock()
_storage_lock = threading.Lock()
_firestore_lock = threading.Lock()
_rate_limited_keys: dict[str, tuple[float, int]] = {}  # APIキー → (レート制限時刻, 連続失敗回数)
_rate_limit_lock = threading.Lock()
# メモリに記録の無いキーの Firestore 参照を短時間キャッシュし、判定/ヘルスチェック毎の
# read を「キー数 ÷ TTL」に抑える。値は (取得時刻, 生データ or None)。生データは
# (limited_at, consecutive_count, backoff_seconds) で、残り時間は都度再計算する。
_fs_status_cache: dict[str, tuple[float, tuple[float, int, int] | None]] = {}
_FS_CACHE_TTL = int(os.environ.get("RATE_LIMIT_FS_CACHE_TTL", "30"))

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

# 最後の 429 からこの秒数を超えて経過していたら、エスカレーション段階をリセットする。
# （長期間回復していたキーが再び 429 になったとき、過去の段階を引き継がず 1 から数え直す）
_BACKOFF_RESET_SECONDS = _BACKOFF_SECONDS[-1]  # 24時間


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


def _rate_limit_doc_id(api_key: str) -> str:
    """生の API キーを Firestore のドキュメント ID やログに出さないためのハッシュ。"""
    return hashlib.sha256(api_key.encode()).hexdigest()[:16]


def _get_firestore_client() -> firestore.Client | None:
    """Firestore クライアントを取得（シングルトン）。

    初期化に一度失敗したら `_firestore_unavailable` を立て、以後は即 None を返す。
    これをしないと ADC 不在環境で呼び出し毎に firestore.Client() が
    ~3 秒スタックし、レート制限チェック全体が詰まる。
    """
    global _firestore_client, _firestore_unavailable
    if _firestore_client is not None:
        return _firestore_client
    if _firestore_unavailable:
        return None
    with _firestore_lock:
        if _firestore_client is None and not _firestore_unavailable:
            try:
                _firestore_client = firestore.Client()
            except Exception as e:
                logger.warning(
                    f"Firestore unavailable; falling back to in-memory rate limiting only: {e}"
                )
                _firestore_unavailable = True
                return None
    return _firestore_client


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
    """APIキーのレート制限状態を確認。Firestore と メモリから読み込み。

    戻り値：
      None: レート制限なし
      RateLimitStatus: 制限中の詳細情報
    """
    now = time.time()

    def _active(limited_at: float, consecutive_count: int, backoff_seconds: int) -> RateLimitStatus | None:
        # 満了していれば None。エントリ/ドキュメントは消さず残し、引き継ぎに使う。
        elapsed = now - limited_at
        if elapsed >= backoff_seconds:
            return None
        return {
            "remaining": backoff_seconds - elapsed,
            "consecutive_count": consecutive_count,
            "backoff_seconds": backoff_seconds,
        }

    with _rate_limit_lock:
        if api_key in _rate_limited_keys:
            limited_at, consecutive_count = _rate_limited_keys[api_key]
            backoff_index = min(consecutive_count - 1, len(_BACKOFF_SECONDS) - 1)
            return _active(limited_at, consecutive_count, _BACKOFF_SECONDS[backoff_index])

        cached = _fs_status_cache.get(api_key)
        if cached is not None and now - cached[0] < _FS_CACHE_TTL:
            raw = cached[1]
            return None if raw is None else _active(*raw)

    # 外部 I/O 中に他のリクエストを直列化しない。
    raw: tuple[float, int, int] | None = None
    db = _get_firestore_client()
    if db:
        try:
            doc = db.collection("rate_limits").document(_rate_limit_doc_id(api_key)).get()
            if doc.exists:
                data = doc.to_dict()
                raw = (
                    data.get("timestamp", now),
                    data.get("consecutive_count", 0),
                    data.get("backoff_seconds", 0),
                )
        except Exception as e:
            logger.debug(f"Failed to read rate limit from Firestore: {e}")
            return None

    with _rate_limit_lock:
        # I/O 中に 429 が記録された場合は、古い Firestore 読み取りで上書きしない。
        if api_key in _rate_limited_keys:
            limited_at, count = _rate_limited_keys[api_key]
            return _active(limited_at, count, _BACKOFF_SECONDS[min(count - 1, len(_BACKOFF_SECONDS) - 1)])
        newer = _fs_status_cache.get(api_key)
        if newer is not None and newer[0] > now:
            current = newer[1]
            return None if current is None else _active(*current)
        _fs_status_cache[api_key] = (now, raw)
    return None if raw is None else _active(*raw)


def _mark_rate_limited(api_key: str) -> None:
    """429エラーを記録し、バックオフカウントをインクリメント。Firestore に永続化。

    直前の段階はメモリ→Firestore の順に引き継ぐ。ただし最後の 429 から
    `_BACKOFF_RESET_SECONDS` を超えて経過している記録は陳腐化とみなし、1 から数え直す。
    """
    now = time.time()
    doc_id = _rate_limit_doc_id(api_key)
    last_at: float | None = None
    previous_count = 0
    with _rate_limit_lock:
        if api_key in _rate_limited_keys:
            last_at, previous_count = _rate_limited_keys[api_key]
        else:
            cached = _fs_status_cache.get(api_key)
            if cached and cached[1]:
                last_at, previous_count, _ = cached[1]

    db = _get_firestore_client()
    if last_at is None and db:
        try:
            doc = db.collection("rate_limits").document(doc_id).get()
            if doc.exists:
                data = doc.to_dict()
                last_at = data.get("timestamp")
                previous_count = data.get("consecutive_count", 0)
        except Exception as e:
            logger.debug(f"Failed to read rate limit from Firestore: {e}")

    with _rate_limit_lock:
        # Firestore I/O 中に別スレッドが記録していれば、そちらを基準にする。
        current = _rate_limited_keys.get(api_key)
        if current and (last_at is None or current[0] > last_at):
            last_at, previous_count = current
        if last_at is not None and now - last_at > _BACKOFF_RESET_SECONDS:
            previous_count = 0
        consecutive_count = min(previous_count + 1, len(_BACKOFF_SECONDS))
        _rate_limited_keys[api_key] = (now, consecutive_count)
        _fs_status_cache.pop(api_key, None)

    backoff_seconds = _BACKOFF_SECONDS[min(consecutive_count - 1, len(_BACKOFF_SECONDS) - 1)]
    if db:
        try:
            db.collection("rate_limits").document(doc_id).set({
                "timestamp": now,
                "consecutive_count": consecutive_count,
                "backoff_seconds": backoff_seconds,
            })
        except Exception as e:
            logger.error(f"Failed to save rate limit status to Firestore: {e}")
    logger.info(
        f"Rate limited: key {doc_id}",
        extra={"consecutive_count": consecutive_count, "backoff_minutes": backoff_seconds / 60},
    )


def _mark_key_succeeded(api_key: str) -> None:
    """十分な安定期間の後だけ、成功時にバックオフを1段減衰させる。"""
    now = time.time()
    doc_id = _rate_limit_doc_id(api_key)
    with _rate_limit_lock:
        memory = _rate_limited_keys.get(api_key)
        cached = _fs_status_cache.get(api_key)
    known_count: int | None = None
    last_at: float | None = None
    if memory:
        last_at = memory[0]
        known_count = memory[1]
    elif cached is not None and cached[1]:
        last_at, known_count, _ = cached[1]
    if known_count is None or last_at is None or known_count <= 0:
        return
    backoff = _BACKOFF_SECONDS[min(known_count - 1, len(_BACKOFF_SECONDS) - 1)]
    if now - last_at < backoff * 2:
        return
    new_count = max(0, known_count - 1)
    with _rate_limit_lock:
        current = _rate_limited_keys.get(api_key)
        if current and current != memory:
            return
        if new_count:
            _rate_limited_keys[api_key] = (last_at, new_count)
        else:
            _rate_limited_keys.pop(api_key, None)
        _fs_status_cache.pop(api_key, None)
    db = _get_firestore_client()
    if not db:
        return
    try:
        db.collection("rate_limits").document(doc_id).set({
            "consecutive_count": new_count,
            "backoff_seconds": 0 if new_count == 0 else _BACKOFF_SECONDS[new_count - 1],
        }, merge=True)
    except Exception as e:
        logger.warning(f"Failed to decay rate limit status in Firestore: {e}")


def _is_rate_limit_error(exc: Exception) -> bool:
    """Gemini SDK の 429(RESOURCE_EXHAUSTED) を判定する。

    google-genai の APIError/ClientError は `code`(int) と `status`(str) を持ち、
    `status_code` は持たない。SDK 実装の変更や別経路の例外にも耐えるよう、
    複数の属性を許容する。
    """
    for attr in ("code", "status_code"):
        if getattr(exc, attr, None) == 429:
            return True
    if getattr(exc, "status", None) == "RESOURCE_EXHAUSTED":
        return True
    return False


class QuotaToken(TypedDict):
    backend: str
    key: str
    ref: Any
    released: bool


_quota_memory: dict[str, int] = defaultdict(int)
_quota_memory_lock = threading.Lock()
# メモリ退避は「今どちらで数えているか」ではなく「いつ・何回退避したか」で監視する。
# 1 リクエスト成功しただけで firestore に戻る単純なフラグでは、部分障害が隠れてしまう。
_quota_state_lock = threading.Lock()
_quota_memory_fallbacks = 0
_quota_memory_fallback_at = 0.0
_QUOTA_DEGRADED_LOG_INTERVAL = 60.0
JST = datetime.timezone(datetime.timedelta(hours=9))


def _quota_day(now: datetime.datetime | None = None) -> str:
    return (now or datetime.datetime.now(datetime.UTC)).astimezone(JST).date().isoformat()


def _note_quota_fallback() -> bool:
    """メモリ退避を記録し、ログを出すべきかを返す。障害中は全リクエストがここを
    通るため、ログは `_QUOTA_DEGRADED_LOG_INTERVAL` に 1 回へ間引く。"""
    global _quota_memory_fallbacks, _quota_memory_fallback_at
    now = time.time()
    with _quota_state_lock:
        should_log = now - _quota_memory_fallback_at > _QUOTA_DEGRADED_LOG_INTERVAL
        _quota_memory_fallbacks += 1
        _quota_memory_fallback_at = now
    return should_log


def _quota_status() -> dict[str, Any]:
    with _quota_state_lock:
        fallbacks, last = _quota_memory_fallbacks, _quota_memory_fallback_at
    degraded = bool(last) and time.time() - last < _QUOTA_DEGRADED_LOG_INTERVAL
    return {"quota_backend": "memory" if degraded else "firestore", "quota_fallbacks": fallbacks}


class QuotaTarget(TypedDict):
    field: str
    limit: int
    doc_id: str
    memory_key: str
    per_shard: int


def _quota_target(day: str, field: str, limit: int, subject: str) -> QuotaTarget:
    subject_hash = hashlib.sha256(f"{subject}|{DAILY_IP_SALT}".encode()).hexdigest()[:16]
    if subject == "global":
        # 全体カウンタは 1 ドキュメントに書き込みが集中するのでシャードへ分散する。
        shard = random.randrange(QUOTA_SHARDS)
        per_shard = (limit + QUOTA_SHARDS - 1) // QUOTA_SHARDS
    else:
        # 主体別カウンタは subject_hash 自体が書き込みを分散する。ここでさらにシャードを
        # 掛けても 1 主体は常に同じシャードへ落ちるだけで、実効上限が limit/QUOTA_SHARDS
        # まで縮む（DAILY_IP_LIMIT=50, QUOTA_SHARDS=10 なら 1 IP あたり 5 回/日）。
        shard = 0
        per_shard = limit
    return {
        "field": field,
        "limit": limit,
        "doc_id": f"{field}-{subject_hash}-{shard}",
        "memory_key": f"{day}:{field}:{subject_hash}",
        "per_shard": per_shard,
    }


def _consume_memory_quotas(targets: list[QuotaTarget]) -> list[QuotaToken] | None:
    # 退避したことを予約の成否より先に記録する。枠が枯渇して 429 を返す経路も退避中で
    # あることに変わりはなく、後から記録すると障害中の拒否だけが監視から漏れる。
    if _note_quota_fallback():
        logger.error(
            "Firestore quota unavailable; using memory quota",
            extra={"quota": ",".join(target["field"] for target in targets)},
        )
    tokens: list[QuotaToken] = []
    with _quota_memory_lock:
        for target in targets:
            key = target["memory_key"]
            local_limit = max(1, target["limit"] // MAX_INSTANCES)
            if _quota_memory[key] >= local_limit:
                # 一部だけ取れた状態を残さない。
                for token in tokens:
                    _quota_memory[token["key"]] = max(0, _quota_memory[token["key"]] - 1)
                return None
            _quota_memory[key] += 1
            tokens.append({"backend": "memory", "key": key, "ref": None, "released": False})
    return tokens


def _consume_daily_quotas(specs: list[tuple[str, int, str]]) -> list[QuotaToken] | None:
    """複数の日次枠を 1 トランザクションでまとめて予約する。

    全部取れなければ 1 つも取らない。個別に予約すると、後段が枯渇したときに前段を
    解放して回る必要があり、その解放が落ちると枠が減ったまま戻らない。
    """
    if any(limit <= 0 for _, limit, _ in specs):
        # 上限 0 は「無制限」ではなく「その枠を閉じる」の意味。
        return None
    day = _quota_day()
    targets = [_quota_target(day, field, limit, subject) for field, limit, subject in specs]
    db = _get_firestore_client()
    if not db:
        return _consume_memory_quotas(targets)
    counters = db.collection("quota").document(day).collection("counters")
    refs = [counters.document(target["doc_id"]) for target in targets]
    try:
        transaction = db.transaction()

        @firestore.transactional
        def reserve(tx: Any) -> list[QuotaToken] | None:
            # Firestore のトランザクションは全ての読み取りを書き込みより先に行う必要がある。
            snapshots = [ref.get(transaction=tx) for ref in refs]
            for snapshot, target in zip(snapshots, targets):
                current = snapshot.to_dict().get("count", 0) if snapshot.exists else 0
                if not isinstance(current, int) or current >= target["per_shard"]:
                    return None
            expires = datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=2)
            for ref in refs:
                tx.set(ref, {"count": firestore.Increment(1), "expires_at": expires}, merge=True)
            return [
                {"backend": "firestore", "key": target["doc_id"], "ref": ref, "released": False}
                for target, ref in zip(targets, refs)
            ]

        return reserve(transaction)
    except Exception as e:
        logger.error(
            "Firestore quota failed; retrying with memory quota",
            extra={"quota": ",".join(target["field"] for target in targets), "error": str(e)},
        )
        return _consume_memory_quotas(targets)


def _consume_daily_quota(field: str, limit: int, subject: str = "global") -> QuotaToken | None:
    tokens = _consume_daily_quotas([(field, limit, subject)])
    return tokens[0] if tokens else None


def _release_daily_quota(token: QuotaToken | None) -> None:
    if not token:
        return
    if token.get("released", False):
        return
    token["released"] = True
    try:
        if token["backend"] == "memory":
            with _quota_memory_lock:
                _quota_memory[token["key"]] = max(0, _quota_memory[token["key"]] - 1)
        else:
            token["ref"].set({"count": firestore.Increment(-1)}, merge=True)
    except Exception:
        logger.exception("failed to release daily quota", extra={"quota_key": token["key"]})


def _generate_vision_result(
    image: bytes,
    prompt: str,
    symbol_id: str,
    required_count: int,
    forbidden_count: int,
) -> VisionResult:
    """期待する特徴数を必ず受け取る。長さが合わない応答は次候補へ回し、
    呼び出し側（_flag_at）が添字を安全に使えることを保証する。"""
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

        paid_token = None
        if key_label == "paid":
            paid_token = _consume_daily_quota("paid_calls", DAILY_PAID_LIMIT)
        if key_label == "paid" and not paid_token:
            logger.warning("Daily paid Gemini quota exhausted", extra={"symbol_id": symbol_id})
            continue

        models = _gemini_models(key_label)
        if not models:
            _release_daily_quota(paid_token)
            raise HTTPException(503, "judgment model is not configured")
        length_mismatches = 0
        key_succeeded = False
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
                result = VisionResult.model_validate_json(response.text or "")
                if (len(result.required) != required_count
                        or len(result.forbidden) != forbidden_count):
                    raise ValueError("Gemini feature array length mismatch")
                _mark_key_succeeded(api_key)
                key_succeeded = True
                return result
            except (ValidationError, ValueError) as exc:
                last_error = exc
                if isinstance(exc, ValueError) and not isinstance(exc, ValidationError):
                    length_mismatches += 1
                logger.warning(
                    "invalid Gemini response; trying next Gemini candidate",
                    extra={"symbol_id": symbol_id, "model": model, "key_label": key_label,
                           "reason": "length_mismatch" if length_mismatches else "validation_error"},
                )
                if length_mismatches >= 2:
                    break
            except Exception as exc:
                last_error = exc
                # レート制限エラー（429）を検出したら、このキーを記録して次のキーを試す
                if _is_rate_limit_error(exc):
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
        if paid_token and not key_succeeded:
            _release_daily_quota(paid_token)

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
    """INK_THRESHOLD 未満の輝度を持つピクセル数を数える。

    getdata() の Python ループは 4MP で ~280ms かかり、白紙に近い巨大画像を
    投げるだけで CPU を消費させられる。histogram() は C 実装で同じ結果を
    ~16ms で返すため、外部 API 呼び出し前の門番として十分軽い。
    """
    histogram = img.convert("L").histogram()
    return sum(histogram[:INK_THRESHOLD])


def _get_storage_client() -> Any:
    global _storage_client
    if _storage_client is None:
        with _storage_lock:
            if _storage_client is None:
                from google.cloud import storage

                _storage_client = storage.Client()
    return _storage_client


def _save_to_gcs(
    bucket_name: str,
    symbol_id: str,
    image: bytes,
    judgment_data: dict[str, Any],
    prefix: str = "judgments",
) -> None:
    """GCS にエンドポイントの判定データと画像を保存する。失敗は判定処理に波及させない。"""
    if not bucket_name:
        return
    try:
        bucket = _get_storage_client().bucket(bucket_name)
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


def _record_judgment(judgment_id: str, symbol_id: str, judgment: dict[str, Any]) -> bool:
    """異議報告用の短命レコードを保存する。失敗時も判定自体は継続する。"""
    db = _get_firestore_client()
    if not db:
        logger.error("judgment record unavailable", extra={"reason": "firestore_unavailable"})
        return False
    try:
        now = datetime.datetime.now(datetime.UTC)
        db.collection("judgments").document(judgment_id).set({
            "symbol_id": symbol_id,
            "judgment": judgment,
            "created_at": now,
            "expires_at": now + datetime.timedelta(seconds=JUDGMENT_RECORD_TTL),
            "disputed": False,
        })
        return True
    except Exception:
        logger.exception("failed to record judgment", extra={"symbol_id": symbol_id})
        return False


def _save_pending_feedback(judgment_id: str, image: bytes) -> None:
    """保留画像を保存する。異議報告より先に必ず完了している必要があるため、
    バックグラウンドタスクにはしない（Cloud Run は既定でレスポンス後に CPU を絞るので、
    バックグラウンドの完了時刻を報告 API 側から当てにできない）。失敗は判定に波及させない。"""
    if not FEEDBACK_BUCKET:
        return
    try:
        _get_storage_client().bucket(FEEDBACK_BUCKET).blob(
            f"pending/{judgment_id}.png"
        ).upload_from_string(image, content_type="image/png")
    except Exception:
        logger.exception("failed to save pending feedback image")


def _promote_feedback(judgment_id: str, record: dict[str, Any]) -> None:
    """報告された判定を disputed/ へ確定させる。失敗は報告APIへ波及させない。

    判定メタデータを先に、画像コピーを後に、それぞれ独立した try で行う。まとめて
    1 つの try に入れると、保留画像が無いときに copy_blob の例外でメタデータごと
    失われる（報告は既に disputed 済みでユーザーは再送できない）。
    """
    if not FEEDBACK_BUCKET:
        return
    try:
        bucket = _get_storage_client().bucket(FEEDBACK_BUCKET)
    except Exception:
        logger.exception("failed to open feedback bucket", extra={"judgment_id": judgment_id})
        return
    try:
        bucket.blob(f"disputed/{judgment_id}.json").upload_from_string(
            json.dumps(record, ensure_ascii=False, default=str), content_type="application/json"
        )
    except Exception:
        logger.exception("failed to save disputed judgment record", extra={"judgment_id": judgment_id})
    try:
        source = bucket.blob(f"pending/{judgment_id}.png")
        bucket.copy_blob(source, bucket, f"disputed/{judgment_id}.png")
    except Exception:
        logger.exception("failed to copy disputed image", extra={"judgment_id": judgment_id})


def _claim_judgment(judgment_id: str) -> tuple[str, dict[str, Any] | None]:
    """異議報告を一度だけ受理する。戻り値は ok / unknown / expired / replayed /
    unavailable。サーバー側の障害（unavailable）は、利用者の入力起因である
    unknown / expired と混ぜない。ユーザーに「期限切れ」と誤って見せてしまう。"""
    db = _get_firestore_client()
    if not db:
        return "unavailable", None
    ref = db.collection("judgments").document(judgment_id)
    try:
        transaction = db.transaction()

        @firestore.transactional
        def claim(tx: Any) -> tuple[str, dict[str, Any] | None]:
            snapshot = ref.get(transaction=tx)
            if not snapshot.exists:
                return "unknown", None
            data = snapshot.to_dict()
            if data.get("expires_at") <= datetime.datetime.now(datetime.UTC):
                return "expired", None
            if data.get("disputed"):
                return "replayed", None
            tx.update(ref, {"disputed": True, "disputed_at": firestore.SERVER_TIMESTAMP})
            return "ok", data

        return claim(transaction)
    except Exception:
        logger.exception("failed to claim judgment", extra={"judgment_id": judgment_id})
        return "unavailable", None


# docs_url/redoc_url だけを None にしても /openapi.json は既定で公開されるため、
# openapi_url も明示的に無効化する。
app = FastAPI(title="KENZU", docs_url=None, redoc_url=None, openapi_url=None)
_page_cache: dict[str, str] = {}
_page_cache_lock = threading.Lock()


def _page_source(name: str) -> str:
    """初回アクセス時に読み、以後はメモリから返す。import 時にまとめて読むと、
    HTML が 1 つでも欠けた瞬間にアプリ全体が起動しなくなる。"""
    cached = _page_cache.get(name)
    if cached is not None:
        return cached
    text = (ROOT / name).read_text(encoding="utf-8")
    with _page_cache_lock:
        _page_cache[name] = text
    return text


def _page(name: str, request: Request) -> HTMLResponse:
    base_url = PUBLIC_BASE_URL or str(request.base_url)
    if not base_url.endswith("/"):
        base_url += "/"
    parsed = urlsplit(base_url)
    local_http = parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "testserver")
    valid_host = bool(parsed.netloc and re.fullmatch(r"[A-Za-z0-9.\-:]+", parsed.netloc))
    if not valid_host or (parsed.scheme != "https" and not local_http):
        logger.warning("invalid public base URL omitted")
        base_url = ""
    rendered = _page_source(name).replace("__BASE_URL__", html.escape(base_url, quote=True))
    # PUBLIC_BASE_URL 未設定時はボディが Host 依存になるので、共有キャッシュに
    # 他ホスト向けの OG URL を配らせない。
    return HTMLResponse(
        rendered,
        headers={"Cache-Control": "private, max-age=60, must-revalidate", "Vary": "Host"},
    )


@app.exception_handler(404)
async def not_found(request: Request, exc: Exception) -> Response:
    # /api/* は JSON クライアント向けなので、HTML の 404 ページを返さない。
    if request.url.path.startswith("/api/"):
        detail = getattr(exc, "detail", "not found")
        return JSONResponse({"detail": detail}, status_code=404)
    del exc
    page = """<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>404 — 配線用図記号ドリル</title><link rel="stylesheet" href="/theme.css"></head>
<body style="display:flex;flex-direction:column;align-items:center;justify-content:center;padding:40px 20px;text-align:center">
<div class="box" style="padding:28px 32px;max-width:340px"><p class="label-caps" style="color:var(--on-surface-variant);margin-bottom:10px">Sheet Not Found</p>
<p style="font-size:44px;font-weight:700;color:var(--primary);font-family:var(--mono)">404</p>
<p style="font-size:13px;color:var(--on-surface-variant);margin:12px 0 20px;line-height:1.7">この図面番号のページは存在しません。</p>
<a class="btn btn-primary" href="/" style="display:inline-block;text-decoration:none">表紙へ戻る</a></div></body></html>"""
    return HTMLResponse(page, status_code=404)


_hits: dict[str, deque[float]] = defaultdict(deque)
_hits_lock = threading.Lock()


def _client_ip(request: Request) -> str:
    """レート制限のキーになるクライアント識別子を返す。

    Cloud Run では TCP の接続元がフロントエンドプロキシになるため、
    request.client.host をそのまま使うと全ユーザーが 1 つのレート制限バケットを
    共有してしまう。Cloud Run は実際の接続元を X-Forwarded-For の**末尾**に
    追加するため、末尾の値を採用する。クライアントが偽装できるのは左側だけなので、
    末尾を見る限り偽装されない。

    プロキシ経由でない環境（ローカル実行など）では X-Forwarded-For が無いので
    request.client.host にフォールバックする。
    """
    if TRUST_FORWARDED_FOR:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            candidates = [value.strip() for value in forwarded.split(",") if value.strip()]
            if candidates:
                # CLIENT_IP_INDEX_FROM_END は「末尾から数えて何番目か」で、段数ではない。
                # `a, b, c` なら 1 で c、2 で b。前段にプロキシを 1 つ足したら +1 する。
                # 大きくしすぎるとクライアントが自分で入れた値を掴むので注意。
                if len(candidates) < CLIENT_IP_INDEX_FROM_END:
                    logger.warning(
                        "x-forwarded-for shorter than CLIENT_IP_INDEX_FROM_END",
                        extra={"index": CLIENT_IP_INDEX_FROM_END, "received": len(candidates)},
                    )
                    return candidates[-1]
                return candidates[-CLIENT_IP_INDEX_FROM_END]
    return request.client.host if request.client else "unknown"


def _check_rate(request: Request) -> None:
    now = time.monotonic()
    ip = _client_ip(request)
    with _hits_lock:
        queue = _hits[ip]
        while queue and now - queue[0] > RATE_WINDOW:
            queue.popleft()
        if len(queue) >= RATE_LIMIT:
            raise HTTPException(429, "rate_limited")
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
    observation: str = Field(default="")

    @field_validator("observation")
    @classmethod
    def truncate_observation(cls, value: str) -> str:
        # 長さ超過で検証エラーにすると、その候補が失敗扱いになり全キー・全モデルを
        # 消費してしまう。観察文は表示用なので、切り詰めて受け入れる。
        if len(value) > MAX_OBSERVATION_CHARS:
            return value[:MAX_OBSERVATION_CHARS]
        return value


def _flag_at(values: list[bool], index: int) -> bool:
    # 添字が範囲内であることは _generate_vision_result の長さ検証が保証する。
    # 欠けた要素を False で埋めると、Gemini が答えていない項目を「不合格」として
    # 利用者に見せてしまうため、ここでは黙って補わない。
    return values[index]


class ReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    judgment_id: str = Field(pattern=r"^[0-9a-f]{32}$")


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
        for s in SYMBOLS.values() if s["verified"]
    ]


@app.get("/api/question")
def question() -> dict[str, Any]:
    pool = [symbol for symbol in SYMBOLS.values() if symbol["verified"]]
    symbol = random.choice(pool)
    # description は判定後の解説表示に使う。第二次検定は記号から名称と説明を
    # 答えさせるため、描いたあとに用途まで確認できるようにしている。
    return {
        "id": symbol["id"],
        "name": symbol["name"],
        "category": symbol["category"],
        "description": symbol.get("description", ""),
    }


def build_vision_prompt(symbol: dict[str, Any], *, blind: bool = False) -> str:
    """Gemini に渡す観察プロンプトを組み立てる。

    `blind=True` は記号名を伏せた対照条件。「課題は〇〇です」と正解を先に教えると
    確証バイアスが乗る可能性があるため、評価ハーネス(scripts/run_judgment_eval.py)が
    通常条件と A/B して影響を測れるようにしてある。本番の /api/judge は常に
    `blind=False`（本番と同じ文面で評価するために、この関数を共有する）。
    """
    required_features: list[str] = symbol["required_features"]
    forbidden_features: list[str] = symbol.get("forbidden_features", [])

    subject = (
        "画像は受験者が手描きした電気設備の図記号です。"
        if blind
        else f"画像は受験者が手描きした電気設備の図記号で、課題は「{symbol['name']}」です。"
    )

    return f"""あなたは施工図の図記号を厳密に識別する採点補助です。
{subject}

判定方針:
- 線の多少の歪み、傾き、太さ、位置ずれは許容する。
- ただし、本数、接続関係、貫通、内外、塗りつぶし、文字、方向など、記号を識別する位相的特徴は厳密に判定する。
- ここでの「左・右・上・下」は画像の見た目どおりの方向を指す。左右反転・上下反転・180度回転で指定と逆になっている場合は、該当する必須特徴を false、対応する禁止特徴を true にする。
- 見えない特徴を推測で true にしない。
- 対象記号らしく見えても、禁止特徴があれば明示する。
- 各項目を独立に評価し、指定JSON以外を返さない。
- required/forbidden は、下記の各番号(0,1,2...)に対応する true/false を、その順序どおりに並べた配列で返す。

必須特徴(required): 画像に存在すれば true
{json.dumps({str(i): f for i, f in enumerate(required_features)}, ensure_ascii=False, indent=2)}

禁止特徴(forbidden): 画像に存在すれば true。1つでも true なら不合格
{json.dumps({str(i): f for i, f in enumerate(forbidden_features)}, ensure_ascii=False, indent=2)}

observationには、最も重要な根拠を日本語で簡潔に記述してください。"""


def score_observation(symbol: dict[str, Any], result: VisionResult) -> dict[str, Any]:
    """観察結果から合否・チェック一覧・不足特徴を決定的に算出する。

    Gemini は特徴の観察だけを担い、合否はここで決める。評価ハーネスも本番と
    同じ採点を通すために、この関数を共有する。
    """
    required_features: list[str] = symbol["required_features"]
    forbidden_features: list[str] = symbol.get("forbidden_features", [])

    checks: list[dict[str, Any]] = []
    for index, feature in enumerate(required_features):
        checks.append({"feature": f"必須: {feature}", "ok": _flag_at(result.required, index)})
    for index, feature in enumerate(forbidden_features):
        checks.append({"feature": f"除外: {feature}がない", "ok": not _flag_at(result.forbidden, index)})

    failed_required = [
        feature for index, feature in enumerate(required_features)
        if not _flag_at(result.required, index)
    ]
    hit_forbidden = [
        feature for index, feature in enumerate(forbidden_features)
        if _flag_at(result.forbidden, index)
    ]
    mistakes = [f"必須特徴が不足: {value}" for value in failed_required]
    mistakes += [f"対象外の特徴を検出: {value}" for value in hit_forbidden]

    n_ok = sum(check["ok"] for check in checks)
    return {
        "passed": n_ok == len(checks),
        "score": f"{n_ok}/{len(checks)}",
        "checks": checks,
        "mistakes": mistakes,
    }


@app.post("/api/judge")
def judge(req: JudgeRequest, request: Request, background: BackgroundTasks) -> dict[str, Any]:
    _check_rate(request)
    symbol = SYMBOLS.get(req.symbol_id)
    # 未検証の記号は出題も一覧もされない。判定だけ通ると、検証前の判定基準で
    # 採点した結果を利用者に返してしまう。
    if not symbol or not symbol["verified"]:
        raise HTTPException(404, "unknown symbol")
    image = _decode_png(req.image_b64)
    quota_tokens = _consume_daily_quotas([
        ("ip_calls", DAILY_IP_LIMIT, _client_ip(request)),
        ("judge_calls", DAILY_JUDGE_LIMIT, "global"),
    ])
    if not quota_tokens:
        raise HTTPException(429, "daily_quota_exceeded")

    try:
        result = _generate_vision_result(
            image,
            build_vision_prompt(symbol),
            req.symbol_id,
            len(symbol["required_features"]),
            len(symbol.get("forbidden_features", [])),
        )
    except Exception:
        for token in quota_tokens:
            _release_daily_quota(token)
        raise

    scored = score_observation(symbol, result)
    passed, score = scored["passed"], scored["score"]
    checks, mistakes = scored["checks"], scored["mistakes"]

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
    # GCS へのアップロードは応答をブロックしない（失敗しても判定結果は返す）。
    if ALL_JUDGMENTS_BUCKET:
        background.add_task(
            _save_to_gcs, ALL_JUDGMENTS_BUCKET, req.symbol_id, image, judgment_data, "judgments"
        )

    judgment_id = uuid.uuid4().hex
    response_data = {
        "symbol_id": req.symbol_id,
        "passed": passed,
        "score": score,
        "checks": checks,
        "mistakes": mistakes,
        "observation": result.observation,
        "ref_svg": symbol.get("ref_svg", ""),
        "judgment_id": judgment_id,
    }
    # 保留画像は同期で上げる。バックグラウンドにすると、直後の異議報告が
    # アップロードを追い越して画像を取りこぼす（_save_pending_feedback 参照）。
    if FEEDBACK_BUCKET and _record_judgment(judgment_id, req.symbol_id, judgment_data):
        _save_pending_feedback(judgment_id, image)
    return response_data


@app.post("/api/report")
def report(req: ReportRequest, request: Request) -> dict[str, bool]:
    _check_rate(request)
    if not FEEDBACK_BUCKET:
        raise HTTPException(503, "report_disabled")
    status, record = _claim_judgment(req.judgment_id)
    if status != "ok":
        logger.warning("feedback report rejected", extra={"reason": status})
        raise HTTPException({"replayed": 409, "unavailable": 503}.get(status, 404), status)
    if record:
        # 報告は既に disputed 済みでユーザーは再送できない。バックグラウンドにすると
        # Cloud Run の CPU スロットリングで保存が落ちても誰も気付けないので同期で行う。
        _promote_feedback(req.judgment_id, record)
    return {"ok": True}


@app.get("/healthz")
def healthz() -> dict[str, bool]:
    return {"ok": True}


@app.get("/readyz")
def readyz() -> dict[str, Any]:
    if not SYMBOLS:
        raise HTTPException(503, "no symbols loaded")
    keys = _gemini_api_keys()
    if not keys:
        raise HTTPException(503, "Gemini is not configured")
    available = [label for label, api_key in keys if _get_rate_limit_status(api_key) is None]
    if not available:
        raise HTTPException(503, "all Gemini API keys are rate limited")
    return {
        "ok": True,
        "symbols": len(SYMBOLS),
        "feedback_enabled": bool(FEEDBACK_BUCKET),
        "report_enabled": bool(FEEDBACK_BUCKET and _get_firestore_client() is not None),
        **_quota_status(),
        "keys_available": len(available),
        "keys_total": len(keys),
    }
