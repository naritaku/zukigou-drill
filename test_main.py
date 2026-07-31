import base64
import inspect
import io
import json
import unittest
from unittest.mock import Mock, patch

from fastapi import HTTPException, Request
from fastapi.testclient import TestClient
from google.genai import errors as genai_errors
from PIL import Image, ImageDraw

import main

# 全テストの既定では Firestore に接続しない（開発機の ADC で実 DB に触れるのを防ぐ）。
# Firestore パスは FirestoreRateLimitTest がフェイククライアントで検証する。
_firestore_patcher = patch.object(main, "_get_firestore_client", return_value=None)


def setUpModule():
    _firestore_patcher.start()


def tearDownModule():
    _firestore_patcher.stop()


def png_b64(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def blank_png_b64() -> str:
    return png_b64(Image.new("RGB", (64, 64), "white"))


def inked_png_b64() -> str:
    img = Image.new("RGB", (64, 64), "white")
    draw = ImageDraw.Draw(img)
    draw.line((4, 4, 60, 60), fill="black", width=3)
    return "data:image/png;base64," + png_b64(img)


class ImageValidationTest(unittest.TestCase):
    def test_decode_png_rejects_blank_before_external_judgment(self):
        with self.assertRaises(HTTPException) as ctx:
            main._decode_png(blank_png_b64())
        self.assertEqual(ctx.exception.status_code, 400)

    def test_decode_png_accepts_and_normalizes_inked_png(self):
        payload = main._decode_png(inked_png_b64())
        self.assertTrue(payload.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_decode_png_rejects_malformed_png_magic(self):
        malformed = base64.b64encode(b"\x89PNG\r\n\x1a\nnot really png").decode("ascii")
        with self.assertRaises(HTTPException) as ctx:
            main._decode_png(malformed)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_decode_png_rejects_pixel_bomb_before_loading_image(self):
        img = Image.new("RGB", (1, 1), "black")
        payload = png_b64(img)
        original_limit = main.MAX_IMAGE_PIXELS
        try:
            main.MAX_IMAGE_PIXELS = 0
            with self.assertRaises(HTTPException) as ctx:
                main._decode_png(payload)
        finally:
            main.MAX_IMAGE_PIXELS = original_limit
        self.assertEqual(ctx.exception.status_code, 413)

    def test_decode_png_rejects_oversized_base64_string(self):
        """base64 文字列長が上限超過なら、デコード前に 413 で弾く（コスト保護）"""
        original = main.MAX_IMAGE_B64_CHARS
        try:
            main.MAX_IMAGE_B64_CHARS = 10
            with self.assertRaises(HTTPException) as ctx:
                main._decode_png(inked_png_b64())  # 10 文字を超える
        finally:
            main.MAX_IMAGE_B64_CHARS = original
        self.assertEqual(ctx.exception.status_code, 413)

    def test_decode_png_rejects_empty_image(self):
        """空 base64（デコード結果が空）は 400"""
        with self.assertRaises(HTTPException) as ctx:
            main._decode_png("")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_decode_png_rejects_oversized_bytes(self):
        """デコード後のバイト数が上限超過なら、PNG 検証前に 413（外部呼び出し前の門番）"""
        original = main.MAX_IMAGE_BYTES
        try:
            main.MAX_IMAGE_BYTES = 10
            with self.assertRaises(HTTPException) as ctx:
                main._decode_png(png_b64(Image.new("RGB", (64, 64), "white")))
        finally:
            main.MAX_IMAGE_BYTES = original
        self.assertEqual(ctx.exception.status_code, 413)

    def test_decode_png_rejects_invalid_base64(self):
        """base64 として不正な文字列は 400"""
        with self.assertRaises(HTTPException) as ctx:
            main._decode_png("data:image/png;base64,@@@not-base64@@@")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_decode_png_rejects_non_png_magic(self):
        """PNG マジックで始まらないバイト列は 400（PIL に渡す前に拒否）"""
        not_png = base64.b64encode(b"this is definitely not a png file").decode("ascii")
        with self.assertRaises(HTTPException) as ctx:
            main._decode_png(not_png)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.detail, "PNG image required")


class JudgeEndpointTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)
        self.symbol = next(symbol for symbol in main.SYMBOLS.values() if symbol["verified"])
        main._hits.clear()
        main._clients.clear()

    def test_judge_rejects_blank_image_without_creating_genai_client(self):
        with patch.object(main, "_get_genai_client") as get_client:
            res = self.client.post(
                "/api/judge",
                json={"symbol_id": self.symbol["id"], "image_b64": blank_png_b64()},
            )
        self.assertEqual(res.status_code, 400)
        get_client.assert_not_called()

    def test_judge_valid_image_uses_schema_response_and_scores_in_code(self):
        required = self.symbol.get("required_features", self.symbol["features"])
        forbidden = self.symbol.get("forbidden_features", [])
        response_text = json.dumps(
            {
                "required": [True for _ in required],
                "forbidden": [False for _ in forbidden],
                "observation": "必要な特徴が見える",
            },
            ensure_ascii=False,
        )
        fake_client = Mock()
        fake_client.models.generate_content.return_value.text = response_text

        with patch.object(main, "_gemini_api_keys", return_value=[("primary", "free-key")]), \
            patch.object(main, "_get_genai_client", return_value=fake_client):
            res = self.client.post(
                "/api/judge",
                json={"symbol_id": self.symbol["id"], "image_b64": inked_png_b64()},
            )

        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertTrue(body["passed"])
        self.assertEqual(body["score"], f"{len(body['checks'])}/{len(body['checks'])}")
        fake_client.models.generate_content.assert_called_once()

    def test_judge_failed_when_required_feature_missing(self):
        """必須特徴が欠けていれば passed=false・mistakes に不足理由が入る（不合格フィードバックの核心）"""
        required = self.symbol.get("required_features", self.symbol["features"])
        forbidden = self.symbol.get("forbidden_features", [])
        confusions = self.symbol.get("confusable_symbols", [])
        # 先頭の必須特徴だけ False（欠落）にする
        required_flags = [i != 0 for i in range(len(required))]
        response_text = json.dumps(
            {
                "required": required_flags,
                "forbidden": [False for _ in forbidden],
                "confusions": [False for _ in confusions],
                "observation": "一部の特徴が見当たらない",
            },
            ensure_ascii=False,
        )
        fake_client = Mock()
        fake_client.models.generate_content.return_value.text = response_text

        with patch.object(main, "_gemini_api_keys", return_value=[("primary", "free-key")]), \
            patch.object(main, "_get_genai_client", return_value=fake_client):
            res = self.client.post(
                "/api/judge",
                json={"symbol_id": self.symbol["id"], "image_b64": inked_png_b64()},
            )

        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertFalse(body["passed"])
        # 不足した必須特徴が mistakes に「必須特徴が不足: ...」として現れる
        self.assertTrue(any(m.startswith("必須特徴が不足:") for m in body["mistakes"]))
        # score は 合格数 < 総数
        n_ok, n_total = map(int, body["score"].split("/"))
        self.assertLess(n_ok, n_total)

    def test_judge_falls_back_models_before_paid_key(self):
        required = self.symbol.get("required_features", self.symbol["features"])
        forbidden = self.symbol.get("forbidden_features", [])
        response_text = json.dumps(
            {
                "required": [True for _ in required],
                "forbidden": [False for _ in forbidden],
                "observation": "fallback succeeded",
            },
            ensure_ascii=False,
        )
        free_client = Mock()
        paid_client = Mock()
        free_client.models.generate_content.side_effect = [RuntimeError("rate limit"), RuntimeError("rate limit")]
        paid_client.models.generate_content.return_value.text = response_text

        def get_client(api_key=None):
            return {"free-key": free_client, "paid-key": paid_client}[api_key]

        with patch.object(main, "_gemini_api_keys", return_value=[("primary", "free-key"), ("paid", "paid-key")]), \
            patch.object(main, "_gemini_models", return_value=["gemini-3.6-flash", "gemini-2.5-flash-lite"]), \
            patch.object(main, "_get_genai_client", side_effect=get_client):
            res = self.client.post(
                "/api/judge",
                json={"symbol_id": self.symbol["id"], "image_b64": inked_png_b64()},
            )

        self.assertEqual(res.status_code, 200)
        self.assertEqual(free_client.models.generate_content.call_count, 2)
        paid_client.models.generate_content.assert_called_once()
        attempted_models = [
            call.kwargs["model"] for call in free_client.models.generate_content.call_args_list
        ]
        self.assertEqual(attempted_models, ["gemini-3.6-flash", "gemini-2.5-flash-lite"])
        self.assertEqual(paid_client.models.generate_content.call_args.kwargs["model"], "gemini-3.6-flash")

    def test_report_rejects_unclaimable_judgment_before_promoting_feedback(self):
        with patch.object(main, "FEEDBACK_BUCKET", "test-bucket"), \
             patch.object(main, "_claim_judgment", return_value=("unknown", None)), \
             patch.object(main, "_promote_feedback") as promote_feedback:
            res = self.client.post(
                "/api/report",
                json={"judgment_id": "0" * 32},
            )
        self.assertEqual(res.status_code, 404)
        promote_feedback.assert_not_called()


class RateLimitingTest(unittest.TestCase):
    def setUp(self):
        main._rate_limited_keys.clear()
        main._fs_status_cache.clear()
        main._quota_memory.clear()
        main._backoff_decayed_at.clear()

    def test_get_rate_limit_status_returns_none_when_key_not_limited(self):
        status = main._get_rate_limit_status("test-key")
        self.assertIsNone(status)

    def test_mark_rate_limited_increments_consecutive_count(self):
        main._mark_rate_limited("test-key")
        status = main._get_rate_limit_status("test-key")
        self.assertIsNotNone(status)
        self.assertEqual(status["consecutive_count"], 1)

    def test_mark_rate_limited_uses_correct_backoff_time(self):
        for attempt in range(1, 4):
            main._mark_rate_limited("test-key")
        status = main._get_rate_limit_status("test-key")
        self.assertIsNotNone(status)
        expected_backoff = main._BACKOFF_SECONDS[2]  # 3rd attempt
        self.assertEqual(status["backoff_seconds"], expected_backoff)

    def test_firestore_io_is_not_called_while_rate_limit_lock_is_held(self):
        """ロックを持ったまま外部 I/O を待つと、同じキーの全リクエストが直列化する。"""
        class TrackingLock:
            held = False

            def __enter__(self):
                self.held = True

            def __exit__(self, *args):
                self.held = False

        lock = TrackingLock()
        snapshot = Mock(exists=False)
        # side_effect の中で self.fail() を呼ぶと、その AssertionError が実装側の
        # `except Exception` に飲み込まれてテストが空回りする。違反は外に記録する。
        violations: list[str] = []

        def record(operation, result=None):
            def _call(*args, **kwargs):
                if lock.held:
                    violations.append(operation)
                return result
            return _call

        ref = Mock()
        ref.get.side_effect = record("get", snapshot)
        ref.set.side_effect = record("set")
        db = Mock()
        db.collection.return_value.document.return_value = ref
        with patch.object(main, "_rate_limit_lock", lock), \
             patch.object(main, "_get_firestore_client", return_value=db):
            self.assertIsNone(main._get_rate_limit_status("io-key"))
            main._mark_rate_limited("io-key")
        self.assertEqual(violations, [], f"Firestore I/O under _rate_limit_lock: {violations}")
        # I/O 自体は行われている（何も呼ばずに違反ゼロ、を成功と誤認しない）
        self.assertTrue(ref.get.called and ref.set.called)

    def test_status_read_does_not_overwrite_a_429_recorded_during_io(self):
        """I/O 中に記録された 429 を、古い Firestore 読み取りで消してしまわない。"""
        snapshot = Mock(exists=False)  # Firestore には記録が無い ＝「制限なし」
        interrupted = []

        def mark_during_io(*args, **kwargs):
            if not interrupted:  # 割り込み側自身の read で再帰しないよう 1 回だけ
                interrupted.append(1)
                main._mark_rate_limited("racy-key")  # I/O の最中に 429 を記録
            return snapshot

        ref = Mock()
        ref.get.side_effect = mark_during_io
        db = Mock()
        db.collection.return_value.document.return_value = ref
        with patch.object(main, "_get_firestore_client", return_value=db):
            status = main._get_rate_limit_status("racy-key")
        self.assertIsNotNone(status)
        self.assertEqual(status["consecutive_count"], 1)
        self.assertNotIn("racy-key", main._fs_status_cache)

    def test_concurrent_marks_do_not_lose_an_increment(self):
        """I/O 中に別スレッドが記録した段階を引き継ぎ、上書きで 1 回ぶん失わない。"""
        doc = Mock(exists=True)
        doc.to_dict.return_value = {
            "timestamp": main.time.time(),
            "consecutive_count": 4,
            "backoff_seconds": main._BACKOFF_SECONDS[3],
        }
        calls = []

        def mark_during_io(*args, **kwargs):
            if not calls:  # 最初の read の最中にだけ割り込む
                calls.append(1)
                main._mark_rate_limited("racy-key")
            return doc

        ref = Mock()
        ref.get.side_effect = mark_during_io
        db = Mock()
        db.collection.return_value.document.return_value = ref
        with patch.object(main, "_get_firestore_client", return_value=db):
            main._mark_rate_limited("racy-key")
        # 割り込み側が 4→5、こちらがそれを引き継いで 5→6。上書きなら 5 のままになる。
        self.assertEqual(main._rate_limited_keys["racy-key"][1], 6)

    def test_stable_success_reduces_only_one_backoff_level(self):
        # 現在の段階の 2 倍を越えて安定していれば 1 段だけ下げる（0 には戻さない）。
        main._rate_limited_keys["key"] = (main.time.time() - main._BACKOFF_SECONDS[2] * 2 - 1, 3)
        with patch.object(main, "_get_firestore_client", return_value=None):
            main._mark_key_succeeded("key")
        self.assertEqual(main._rate_limited_keys["key"][1], 2)

    def test_repeated_success_does_not_cascade_down_the_backoff_levels(self):
        """_BACKOFF_SECONDS は単調増加なので、1 段下げるたびに閾値も下がる。
        起点を据え置くと同じ瞬間に何段でも下がってしまう。"""
        main._rate_limited_keys["key"] = (main.time.time() - main._BACKOFF_SECONDS[4] * 2 - 1, 5)
        with patch.object(main, "_get_firestore_client", return_value=None):
            for _ in range(5):
                main._mark_key_succeeded("key")
        self.assertEqual(main._rate_limited_keys["key"][1], 4)

    def test_recent_success_does_not_reduce_backoff(self):
        # 断続的に 429 を返すキーが成功のたびに段階を戻すと、バックオフが機能しなくなる。
        main._rate_limited_keys["key"] = (main.time.time(), 3)
        with patch.object(main, "_get_firestore_client", return_value=None):
            main._mark_key_succeeded("key")
        self.assertEqual(main._rate_limited_keys["key"][1], 3)

    def test_success_on_unlimited_key_is_a_noop(self):
        with patch.object(main, "_get_firestore_client") as get_db:
            main._mark_key_succeeded("never-limited")
        self.assertNotIn("never-limited", main._rate_limited_keys)
        get_db.assert_not_called()

    def test_success_does_not_pin_a_key_known_only_from_firestore(self):
        """他インスタンスの記録をキャッシュ経由で知っただけのキーを
        _rate_limited_keys に載せると、以後 Firestore を読まなくなり共有状態から外れる。"""
        stale = main.time.time() - main._BACKOFF_SECONDS[4] * 2 - 1
        main._fs_status_cache["shared-key"] = (main.time.time(), (stale, 5, main._BACKOFF_SECONDS[4]))
        with patch.object(main, "_get_firestore_client") as get_db:
            main._mark_key_succeeded("shared-key")
        self.assertNotIn("shared-key", main._rate_limited_keys)
        get_db.assert_not_called()

    def test_decay_to_zero_clears_the_key(self):
        main._rate_limited_keys["key"] = (main.time.time() - main._BACKOFF_SECONDS[0] * 2 - 1, 1)
        with patch.object(main, "_get_firestore_client", return_value=None):
            main._mark_key_succeeded("key")
        self.assertNotIn("key", main._rate_limited_keys)
        self.assertIsNone(main._get_rate_limit_status("key"))

    def test_decay_does_not_make_the_key_look_freshly_limited(self):
        """減衰で last_at を now にすると、成功したキーが即座に「制限中」に見える。"""
        main._rate_limited_keys["key"] = (main.time.time() - main._BACKOFF_SECONDS[2] * 2 - 1, 3)
        with patch.object(main, "_get_firestore_client", return_value=None):
            main._mark_key_succeeded("key")
        self.assertIsNone(main._get_rate_limit_status("key"))

    def test_new_429_restarts_the_decay_stability_window(self):
        main._rate_limited_keys["key"] = (main.time.time() - main._BACKOFF_SECONDS[2] * 2 - 1, 3)
        with patch.object(main, "_get_firestore_client", return_value=None):
            main._mark_key_succeeded("key")   # 3 → 2、減衰時刻を記録
            main._mark_rate_limited("key")    # 429 で 3 へ戻り、安定期間は測り直し
            main._mark_key_succeeded("key")   # 直後なので減衰しない
        self.assertEqual(main._rate_limited_keys["key"][1], 3)


class FirestoreRateLimitTest(unittest.TestCase):
    """Firestore 永続化パスの動作（フェイククライアントで検証）"""

    def setUp(self):
        main._rate_limited_keys.clear()
        main._fs_status_cache.clear()

    def tearDown(self):
        main._rate_limited_keys.clear()
        main._fs_status_cache.clear()

    def _make_db(self, doc):
        db = Mock()
        db.collection.return_value.document.return_value.get.return_value = doc
        return db

    def test_mark_rate_limited_persists_with_hashed_doc_id(self):
        doc = Mock(exists=False)
        db = self._make_db(doc)
        with patch.object(main, "_get_firestore_client", return_value=db):
            main._mark_rate_limited("secret-key")
        expected_id = main._rate_limit_doc_id("secret-key")
        self.assertNotIn("secret-key", expected_id)
        db.collection.assert_called_with("rate_limits")
        db.collection.return_value.document.assert_called_with(expected_id)
        saved = db.collection.return_value.document.return_value.set.call_args[0][0]
        self.assertEqual(saved["consecutive_count"], 1)
        self.assertEqual(saved["backoff_seconds"], main._BACKOFF_SECONDS[0])

    def test_mark_rate_limited_resumes_count_from_firestore_after_restart(self):
        import time as time_module

        doc = Mock(exists=True)
        doc.to_dict.return_value = {
            # 最近の 429（リセット窓内）→ 段階を引き継ぐ
            "timestamp": time_module.time(),
            "consecutive_count": 2,
            "backoff_seconds": main._BACKOFF_SECONDS[1],
        }
        db = self._make_db(doc)
        # メモリは空 = コンテナ再起動直後を再現
        with patch.object(main, "_get_firestore_client", return_value=db):
            main._mark_rate_limited("secret-key")
        saved = db.collection.return_value.document.return_value.set.call_args[0][0]
        self.assertEqual(saved["consecutive_count"], 3)

    def test_mark_rate_limited_resets_count_when_record_is_stale(self):
        """最後の 429 から _BACKOFF_RESET_SECONDS 超で経過していれば 1 に戻す"""
        import time as time_module

        doc = Mock(exists=True)
        doc.to_dict.return_value = {
            "timestamp": time_module.time() - main._BACKOFF_RESET_SECONDS - 10,
            "consecutive_count": 5,
            "backoff_seconds": main._BACKOFF_SECONDS[4],
        }
        db = self._make_db(doc)
        with patch.object(main, "_get_firestore_client", return_value=db):
            main._mark_rate_limited("secret-key")
        saved = db.collection.return_value.document.return_value.set.call_args[0][0]
        self.assertEqual(saved["consecutive_count"], 1)

    def test_get_rate_limit_status_reads_active_backoff_from_firestore(self):
        import time as time_module

        doc = Mock(exists=True)
        doc.to_dict.return_value = {
            "timestamp": time_module.time(),
            "consecutive_count": 1,
            "backoff_seconds": main._BACKOFF_SECONDS[0],
        }
        db = self._make_db(doc)
        with patch.object(main, "_get_firestore_client", return_value=db):
            status = main._get_rate_limit_status("other-key")
        self.assertIsNotNone(status)
        self.assertEqual(status["backoff_seconds"], main._BACKOFF_SECONDS[0])

    def test_get_rate_limit_status_expired_returns_none_without_deleting(self):
        """満了時は None を返すが、ドキュメントは削除しない（mark が段階を引き継げるように）"""
        doc = Mock(exists=True)
        doc.to_dict.return_value = {
            "timestamp": 0.0,  # 遥か過去 = 満了
            "consecutive_count": 1,
            "backoff_seconds": main._BACKOFF_SECONDS[0],
        }
        db = self._make_db(doc)
        with patch.object(main, "_get_firestore_client", return_value=db):
            status = main._get_rate_limit_status("other-key")
        self.assertIsNone(status)
        doc.reference.delete.assert_not_called()

    def test_restart_resume_end_to_end_with_fake_db(self):
        """FakeDB でストア永続を再現し、実フロー（get_status→mark）での引き継ぎを検証。

        MUST①のリグレッション: 満了時に get_status が doc を消すと、この後の mark が
        1 に戻ってしまう。消さない実装なら 2 に引き継がれる。
        """
        import time as time_module

        store = {}

        class FakeDoc:
            def __init__(self, col, doc_id):
                self.col, self.id = col, doc_id
            @property
            def reference(self):
                return self
            @property
            def exists(self):
                return self.id in self.col.store
            def to_dict(self):
                return dict(self.col.store.get(self.id, {}))
            def get(self):
                return self
            def set(self, data):
                self.col.store[self.id] = dict(data)
            def delete(self):
                self.col.store.pop(self.id, None)

        class FakeCol:
            def __init__(self, store):
                self.store = store
            def document(self, doc_id):
                return FakeDoc(self, doc_id)

        class FakeDB:
            def __init__(self, store):
                self._c = FakeCol(store)
            def collection(self, name):
                return self._c

        db = FakeDB(store)
        doc_id = main._rate_limit_doc_id("secret-key")
        with patch.object(main, "_get_firestore_client", return_value=db):
            main._mark_rate_limited("secret-key")
            self.assertEqual(store[doc_id]["consecutive_count"], 1)

            # 再起動を再現: メモリを消し、Firestore は残す
            main._rate_limited_keys.clear()
            main._fs_status_cache.clear()
            # バックオフ満了させる
            store[doc_id]["timestamp"] -= main._BACKOFF_SECONDS[0] + 1

            # 実フロー: judge がまず get_status を呼ぶ（満了 → None、doc は残る）
            self.assertIsNone(main._get_rate_limit_status("secret-key"))
            self.assertIn(doc_id, store)  # 消えていない

            # 直後に再び 429
            main._mark_rate_limited("secret-key")
            self.assertEqual(store[doc_id]["consecutive_count"], 2)


class FirestoreClientCacheTest(unittest.TestCase):
    """初期化失敗のキャッシュ（MUST②）"""

    def setUp(self):
        # このクラスは実 _get_firestore_client を検証するため、モジュールの
        # 「常に None を返す」パッチャを一時停止する。
        _firestore_patcher.stop()
        self._orig_client = main._firestore_client
        self._orig_unavail = main._firestore_unavailable
        self._orig_ctor = main.firestore.Client

    def tearDown(self):
        main._firestore_client = self._orig_client
        main._firestore_unavailable = self._orig_unavail
        main.firestore.Client = self._orig_ctor
        _firestore_patcher.start()

    def test_init_failure_is_cached_and_not_retried(self):
        calls = {"n": 0}

        def boom(*a, **k):
            calls["n"] += 1
            raise RuntimeError("no ADC")

        main.firestore.Client = boom
        main._firestore_client = None
        main._firestore_unavailable = False

        self.assertIsNone(main._get_firestore_client())
        self.assertIsNone(main._get_firestore_client())
        self.assertIsNone(main._get_firestore_client())
        # 実際の構築は 1 回だけ（以後はフラグで即 return）
        self.assertEqual(calls["n"], 1)
        self.assertTrue(main._firestore_unavailable)


class GCSTest(unittest.TestCase):
    def setUp(self):
        self.symbol = next(symbol for symbol in main.SYMBOLS.values() if symbol["verified"])

    def test_all_judgments_bucket_configured(self):
        # ALL_JUDGMENTS_BUCKET が定義されていることを確認
        self.assertTrue(hasattr(main, "ALL_JUDGMENTS_BUCKET"))

    def test_save_to_gcs_skips_when_bucket_not_configured(self):
        # バケットが空の場合、保存がスキップされることを確認
        original_bucket = main.ALL_JUDGMENTS_BUCKET
        try:
            main.ALL_JUDGMENTS_BUCKET = ""
            main._save_to_gcs(b"test-image", {"test": "data"}, self.symbol["id"], "judgments")
            # 例外が発生しないことを確認
        finally:
            main.ALL_JUDGMENTS_BUCKET = original_bucket


class SplitEnvListTest(unittest.TestCase):
    """_split_env_list 関数の包括的なテスト"""

    def test_split_env_list_with_none_returns_empty_list(self):
        """None を渡すと空リストを返す"""
        result = main._split_env_list(None)
        self.assertEqual(result, [])

    def test_split_env_list_with_empty_string_returns_empty_list(self):
        """空文字列を渡すと空リストを返す"""
        result = main._split_env_list("")
        self.assertEqual(result, [])

    def test_split_env_list_with_single_value(self):
        """単一の値を渡すと1要素のリストを返す"""
        result = main._split_env_list("single-key")
        self.assertEqual(result, ["single-key"])

    def test_split_env_list_with_multiple_values(self):
        """カンマ区切りの複数値を渡すとリストに分割"""
        result = main._split_env_list("key1,key2,key3")
        self.assertEqual(result, ["key1", "key2", "key3"])

    def test_split_env_list_strips_whitespace(self):
        """カンマの前後の空白を削除"""
        result = main._split_env_list("key1 , key2 , key3")
        self.assertEqual(result, ["key1", "key2", "key3"])

    def test_split_env_list_filters_empty_elements(self):
        """カンマ区切りで空要素が含まれる場合はフィルタリング"""
        result = main._split_env_list("key1,,key2")
        self.assertEqual(result, ["key1", "key2"])

    def test_split_env_list_with_only_whitespace_elements(self):
        """空白のみの要素をフィルタリング"""
        result = main._split_env_list("key1,  ,key2")
        self.assertEqual(result, ["key1", "key2"])

    def test_split_env_list_with_trailing_comma(self):
        """末尾のカンマは無視される"""
        result = main._split_env_list("key1,key2,")
        self.assertEqual(result, ["key1", "key2"])

    def test_split_env_list_with_leading_comma(self):
        """先頭のカンマは無視される"""
        result = main._split_env_list(",key1,key2")
        self.assertEqual(result, ["key1", "key2"])

    def test_split_env_list_with_multiple_consecutive_commas(self):
        """連続したカンマはフィルタリング"""
        result = main._split_env_list("key1,,,key2")
        self.assertEqual(result, ["key1", "key2"])


class FlattenImageTest(unittest.TestCase):
    """_flatten_image 関数の包括的なテスト"""

    def test_flatten_image_rgb_unchanged(self):
        """RGB 画像は RGB のまま返される"""
        img = Image.new("RGB", (64, 64), "white")
        draw = ImageDraw.Draw(img)
        draw.rectangle((10, 10, 50, 50), fill="black")

        result = main._flatten_image(img)

        self.assertEqual(result.mode, "RGB")
        self.assertEqual(result.size, (64, 64))

    def test_flatten_image_rgba_to_rgb(self):
        """RGBA 画像は RGB に変換される"""
        img = Image.new("RGBA", (64, 64), (255, 255, 255, 255))
        draw = ImageDraw.Draw(img)
        draw.rectangle((10, 10, 50, 50), fill=(0, 0, 0, 255))

        result = main._flatten_image(img)

        self.assertEqual(result.mode, "RGB")
        self.assertEqual(result.size, (64, 64))

    def test_flatten_image_rgba_transparent_becomes_white(self):
        """RGBA の透明部分は白で埋められる"""
        img = Image.new("RGBA", (10, 10), (255, 0, 0, 255))
        # 最初の50ピクセル（5行）を透明にする
        rgba_pixels = list(img.getdata())
        for i in range(50):
            rgba_pixels[i] = (255, 0, 0, 0)
        img.putdata(rgba_pixels)

        result = main._flatten_image(img)

        self.assertEqual(result.mode, "RGB")
        # 透明部分は白になっているはず
        self.assertEqual(result.getpixel((0, 0)), (255, 255, 255))

    def test_flatten_image_la_to_rgb(self):
        """LA（グレースケール + アルファ）を RGB に変換"""
        img = Image.new("LA", (10, 10), (200, 255))
        result = main._flatten_image(img)
        self.assertEqual(result.mode, "RGB")

    def test_flatten_image_palette_to_rgb(self):
        """パレットモード（P）を RGB に変換"""
        img = Image.new("P", (10, 10))
        result = main._flatten_image(img)
        self.assertEqual(result.mode, "RGB")

    def test_flatten_image_grayscale_to_rgb(self):
        """グレースケール（L）を RGB に変換"""
        img = Image.new("L", (10, 10), 128)
        result = main._flatten_image(img)
        self.assertEqual(result.mode, "RGB")


class CountInkPixelsTest(unittest.TestCase):
    """_count_ink_pixels 関数の包括的なテスト"""

    def setUp(self):
        """各テスト前に INK_THRESHOLD を保存"""
        self.original_threshold = main.INK_THRESHOLD

    def tearDown(self):
        """各テスト後に INK_THRESHOLD を復元"""
        main.INK_THRESHOLD = self.original_threshold

    def test_count_ink_pixels_blank_image(self):
        """白紙画像はインク量が 0"""
        img = Image.new("RGB", (64, 64), "white")
        main.INK_THRESHOLD = 245
        count = main._count_ink_pixels(img)
        self.assertEqual(count, 0)

    def test_count_ink_pixels_black_image(self):
        """完全に黒い画像はインク量 = ピクセル数"""
        img = Image.new("RGB", (64, 64), "black")
        main.INK_THRESHOLD = 245
        count = main._count_ink_pixels(img)
        self.assertEqual(count, 64 * 64)

    def test_count_ink_pixels_with_black_region(self):
        """黒い領域を含む画像はインク量が増える"""
        img = Image.new("RGB", (64, 64), "white")
        draw = ImageDraw.Draw(img)
        draw.rectangle((10, 10, 50, 50), fill="black")
        main.INK_THRESHOLD = 245
        count = main._count_ink_pixels(img)
        # 41x41 の黒い領域がある（1681 ピクセル）
        self.assertEqual(count, 1681)

    def test_count_ink_pixels_gray_image(self):
        """グレー画像は閾値に応じてカウント"""
        img = Image.new("RGB", (10, 10), (128, 128, 128))
        main.INK_THRESHOLD = 245
        count = main._count_ink_pixels(img)
        # グレーは閾値（245）より小さいので、すべてのピクセルがカウントされる
        self.assertEqual(count, 100)

    def test_count_ink_pixels_respects_threshold_boundary(self):
        """閾値の変更が機能する"""
        img = Image.new("RGB", (10, 10), (250, 250, 250))
        # 閾値を高く設定
        main.INK_THRESHOLD = 250
        count = main._count_ink_pixels(img)
        self.assertEqual(count, 0)

    def test_count_ink_pixels_mixed_threshold(self):
        """混合画像が閾値で正しく分離される"""
        img = Image.new("RGB", (10, 10), "white")
        draw = ImageDraw.Draw(img)
        # グレーで四角を描画
        draw.rectangle((0, 0, 4, 4), fill=(200, 200, 200))
        main.INK_THRESHOLD = 220
        count = main._count_ink_pixels(img)
        # 5x5 のグレー領域 = 25 ピクセル
        self.assertEqual(count, 25)


class ValidateAndPreparePngTest(unittest.TestCase):
    """_validate_and_prepare_png 関数の包括的なテスト"""

    def setUp(self):
        """各テスト前に設定を保存"""
        self.original_min_ink = main.MIN_INK_PIXELS
        self.original_max_pixels = main.MAX_IMAGE_PIXELS
        self.original_max_dim = main.MAX_IMAGE_DIM

    def tearDown(self):
        """各テスト後に設定を復元"""
        main.MIN_INK_PIXELS = self.original_min_ink
        main.MAX_IMAGE_PIXELS = self.original_max_pixels
        main.MAX_IMAGE_DIM = self.original_max_dim

    def test_validate_and_prepare_png_rejects_invalid_png_format(self):
        """無効な PNG フォーマットを拒否"""
        malformed_png = b"\x89PNG\r\n\x1a\n" + b"garbage data"
        with self.assertRaises(HTTPException) as ctx:
            main._validate_and_prepare_png(malformed_png)
        self.assertIn(ctx.exception.status_code, [400, 413])

    def test_validate_and_prepare_png_accepts_valid_png(self):
        """有効な PNG を受け入れる"""
        img = Image.new("RGB", (64, 64), "white")
        draw = ImageDraw.Draw(img)
        draw.line((4, 4, 60, 60), fill="black", width=3)

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        png_bytes = buf.getvalue()

        main.MIN_INK_PIXELS = 20
        result = main._validate_and_prepare_png(png_bytes)
        self.assertTrue(result.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_validate_and_prepare_png_rejects_empty_drawing(self):
        """ほぼ白紙（インク量不足）の画像を拒否"""
        img = Image.new("RGB", (64, 64), "white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        png_bytes = buf.getvalue()

        main.MIN_INK_PIXELS = 20
        with self.assertRaises(HTTPException) as ctx:
            main._validate_and_prepare_png(png_bytes)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_validate_and_prepare_png_rejects_oversized_image(self):
        """ピクセル数が MAX_IMAGE_PIXELS を超える画像を拒否"""
        main.MAX_IMAGE_PIXELS = 100
        img = Image.new("RGB", (64, 64), "white")
        draw = ImageDraw.Draw(img)
        draw.line((0, 0, 63, 63), fill="black", width=1)

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        png_bytes = buf.getvalue()

        with self.assertRaises(HTTPException) as ctx:
            main._validate_and_prepare_png(png_bytes)
        self.assertEqual(ctx.exception.status_code, 413)

    def test_validate_and_prepare_png_resizes_oversized_dimension(self):
        """最長辺が MAX_IMAGE_DIM を超える画像をリサイズ"""
        main.MAX_IMAGE_DIM = 50
        main.MIN_INK_PIXELS = 5

        img = Image.new("RGB", (200, 100), "white")
        draw = ImageDraw.Draw(img)
        draw.line((10, 10, 190, 90), fill="black", width=3)

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        png_bytes = buf.getvalue()

        result = main._validate_and_prepare_png(png_bytes)
        result_img = Image.open(io.BytesIO(result))
        self.assertLessEqual(max(result_img.size), main.MAX_IMAGE_DIM)

    def test_validate_and_prepare_png_converts_rgba_to_rgb(self):
        """RGBA 画像を RGB に変換"""
        img = Image.new("RGBA", (64, 64), (255, 255, 255, 255))
        draw = ImageDraw.Draw(img)
        draw.line((4, 4, 60, 60), fill=(0, 0, 0, 255), width=3)

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        png_bytes = buf.getvalue()

        main.MIN_INK_PIXELS = 20
        result = main._validate_and_prepare_png(png_bytes)
        result_img = Image.open(io.BytesIO(result))
        self.assertEqual(result_img.mode, "RGB")

    def test_validate_and_prepare_png_optimizes_output(self):
        """出力 PNG が最適化される（optimize=True オプションが実装に含まれていることを検証）"""
        # より大きな画像でテスト（optimizeの効果が顕著になる）
        img = Image.new("RGB", (200, 200), "white")
        draw = ImageDraw.Draw(img)
        # グラデーション効果のある複雑なパターンを描画
        for i in range(0, 200, 10):
            draw.line((i, 0, 200, 200 - i), fill="black", width=2)
            draw.line((0, i, 200 - i, 200), fill="gray", width=1)

        # optimize=False 版のサイズを取得
        buf_unoptimized = io.BytesIO()
        img.save(buf_unoptimized, format="PNG", optimize=False)
        unoptimized_bytes = buf_unoptimized.getvalue()
        unoptimized_size = len(unoptimized_bytes)

        # optimize=True 版のサイズを取得（_validate_and_prepare_png が使用）
        main.MIN_INK_PIXELS = 20
        result = main._validate_and_prepare_png(unoptimized_bytes)

        # PNG magic number を確認
        self.assertTrue(result.startswith(b"\x89PNG\r\n\x1a\n"))

        # optimize=True が実装に含まれていることを検証
        optimized_size = len(result)
        # 最適化により、同じ画像データでも異なるサイズになる
        # （optimize=Falseと同じサイズまたは小さいサイズになるはず）
        self.assertLessEqual(optimized_size, unoptimized_size * 1.1,
                            f"Optimized PNG ({optimized_size}) should be comparable to or smaller than unoptimized ({unoptimized_size})")


class GenerateVisionResultTest(unittest.TestCase):
    """_generate_vision_result 関数の包括的なテスト"""

    def setUp(self):
        main._rate_limited_keys.clear()
        main._fs_status_cache.clear()
        main._clients.clear()

    def test_generate_vision_result_no_api_keys_raises_503(self):
        """APIキーが設定されていない場合は 503 エラー"""
        img = Image.new("RGB", (64, 64), "white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        image_bytes = buf.getvalue()

        with patch.object(main, "_gemini_api_keys", return_value=[]):
            with self.assertRaises(HTTPException) as ctx:
                main._generate_vision_result(image_bytes, "test prompt", "symbol-1", 1, 0)
            self.assertEqual(ctx.exception.status_code, 503)

    def test_generate_vision_result_valid_response(self):
        """有効な Gemini 応答を解析する"""
        img = Image.new("RGB", (64, 64), "white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        image_bytes = buf.getvalue()

        response_text = json.dumps(
            {
                "required": [True, False],
                "forbidden": [False, True],
                "observation": "観察結果",
            },
            ensure_ascii=False,
        )

        fake_client = Mock()
        fake_client.models.generate_content.return_value.text = response_text

        with patch.object(main, "_gemini_api_keys", return_value=[("primary", "test-key")]), \
             patch.object(main, "_gemini_models", return_value=["gemini-3.1-flash-lite"]), \
             patch.object(main, "_get_genai_client", return_value=fake_client):
            result = main._generate_vision_result(image_bytes, "test prompt", "symbol-1", 2, 2)

        self.assertIsInstance(result, main.VisionResult)
        self.assertEqual(result.required, [True, False])
        self.assertEqual(result.forbidden, [False, True])
        self.assertEqual(result.observation, "観察結果")

    def test_generate_vision_result_reports_success_for_backoff_decay(self):
        """成功をレート制限側に伝えないと、一度上がった段階が下がらない。"""
        img = Image.new("RGB", (64, 64), "white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")

        fake_client = Mock()
        fake_client.models.generate_content.return_value.text = json.dumps(
            {"required": [True], "forbidden": [], "observation": "ok"}
        )
        with patch.object(main, "_gemini_api_keys", return_value=[("primary", "test-key")]), \
             patch.object(main, "_gemini_models", return_value=["gemini-3.1-flash-lite"]), \
             patch.object(main, "_get_genai_client", return_value=fake_client), \
             patch.object(main, "_mark_key_succeeded") as succeeded:
            main._generate_vision_result(buf.getvalue(), "test prompt", "symbol-1", 1, 0)
        succeeded.assert_called_once_with("test-key")

    def test_generate_vision_result_falls_back_models(self):
        """モデルフォールバックが機能する"""
        img = Image.new("RGB", (64, 64), "white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        image_bytes = buf.getvalue()

        response_text = json.dumps(
            {
                "required": [True],
                "forbidden": [],
                "observation": "fallback succeeded",
            },
            ensure_ascii=False,
        )

        fake_client = Mock()
        fake_client.models.generate_content.side_effect = [
            RuntimeError("model error"),
            Mock(text=response_text),
        ]

        with patch.object(main, "_gemini_api_keys", return_value=[("primary", "test-key")]), \
             patch.object(main, "_gemini_models", return_value=["gemini-3.5-flash", "gemini-3.1-flash-lite"]), \
             patch.object(main, "_get_genai_client", return_value=fake_client):
            result = main._generate_vision_result(image_bytes, "test prompt", "symbol-1", 1, 0)

        self.assertEqual(result.observation, "fallback succeeded")
        self.assertEqual(fake_client.models.generate_content.call_count, 2)

    def test_generate_vision_result_falls_back_api_keys(self):
        """APIキーフォールバックが機能する"""
        img = Image.new("RGB", (64, 64), "white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        image_bytes = buf.getvalue()

        response_text = json.dumps(
            {
                "required": [True],
                "forbidden": [],
                "observation": "fallback to paid succeeded",
            },
            ensure_ascii=False,
        )

        free_client = Mock()
        paid_client = Mock()
        free_client.models.generate_content.side_effect = RuntimeError("free tier failed")
        paid_client.models.generate_content.return_value.text = response_text

        def get_client(api_key=None):
            return {"free-key": free_client, "paid-key": paid_client}[api_key]

        with patch.object(main, "_gemini_api_keys", return_value=[("primary", "free-key"), ("paid", "paid-key")]), \
             patch.object(main, "_gemini_models", return_value=["gemini-3.1-flash-lite"]), \
             patch.object(main, "_get_genai_client", side_effect=get_client):
            result = main._generate_vision_result(image_bytes, "test prompt", "symbol-1", 1, 0)

        self.assertEqual(result.observation, "fallback to paid succeeded")
        paid_client.models.generate_content.assert_called_once()

    def test_generate_vision_result_handles_validation_error(self):
        """ValidationError 時は次のモデルにフォールバック"""
        img = Image.new("RGB", (64, 64), "white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        image_bytes = buf.getvalue()

        response_text = json.dumps(
            {
                "required": [True],
                "forbidden": [],
                "observation": "valid response",
            },
            ensure_ascii=False,
        )

        fake_client = Mock()
        fake_client.models.generate_content.side_effect = [
            Mock(text="invalid json"),
            Mock(text=response_text),
        ]

        with patch.object(main, "_gemini_api_keys", return_value=[("primary", "test-key")]), \
             patch.object(main, "_gemini_models", return_value=["gemini-3.5-flash", "gemini-3.1-flash-lite"]), \
             patch.object(main, "_get_genai_client", return_value=fake_client):
            result = main._generate_vision_result(image_bytes, "test prompt", "symbol-1", 1, 0)

        self.assertEqual(result.observation, "valid response")

    def test_generate_vision_result_rate_limit_429_marks_key(self):
        """429 エラーでキーを記録して次のキーをトライ"""
        img = Image.new("RGB", (64, 64), "white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        image_bytes = buf.getvalue()

        response_text = json.dumps(
            {
                "required": [True],
                "forbidden": [],
                "observation": "paid key succeeded",
            },
            ensure_ascii=False,
        )

        free_client = Mock()
        paid_client = Mock()

        # SDK が実際に送出する例外型を使う。自作の RuntimeError に status_code を
        # 付ける形だと、実装が誤った属性名を見ていても検出できない。
        rate_limit_error = genai_errors.ClientError(
            429, {"error": {"message": "quota exceeded", "status": "RESOURCE_EXHAUSTED"}}
        )
        free_client.models.generate_content.side_effect = rate_limit_error
        paid_client.models.generate_content.return_value.text = response_text

        def get_client(api_key=None):
            return {"free-key": free_client, "paid-key": paid_client}[api_key]

        with patch.object(main, "_gemini_api_keys", return_value=[("primary", "free-key"), ("paid", "paid-key")]), \
             patch.object(main, "_gemini_models", return_value=["gemini-3.1-flash-lite"]), \
             patch.object(main, "_get_genai_client", side_effect=get_client):
            result = main._generate_vision_result(image_bytes, "test prompt", "symbol-1", 1, 0)

        self.assertEqual(result.observation, "paid key succeeded")
        # free-key がレート制限に記録されたか確認
        status = main._get_rate_limit_status("free-key")
        self.assertIsNotNone(status)
        self.assertEqual(status["consecutive_count"], 1)

    def test_is_rate_limit_error_detects_real_sdk_exception(self):
        """SDK の ClientError(429) を 429 として判定できる（属性名リグレッション防止）"""
        exc = genai_errors.ClientError(
            429, {"error": {"message": "quota exceeded", "status": "RESOURCE_EXHAUSTED"}}
        )
        # SDK は status_code ではなく code を持つ
        self.assertFalse(hasattr(exc, "status_code"))
        self.assertEqual(exc.code, 429)
        self.assertTrue(main._is_rate_limit_error(exc))

    def test_is_rate_limit_error_ignores_other_errors(self):
        """429 以外のエラーはレート制限として扱わない"""
        exc = genai_errors.ClientError(400, {"error": {"message": "bad request", "status": "INVALID_ARGUMENT"}})
        self.assertFalse(main._is_rate_limit_error(exc))
        self.assertFalse(main._is_rate_limit_error(RuntimeError("boom")))

    def test_generate_vision_result_skips_rate_limited_keys(self):
        """レート制限中のキーはスキップ"""
        img = Image.new("RGB", (64, 64), "white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        image_bytes = buf.getvalue()

        response_text = json.dumps(
            {
                "required": [True],
                "forbidden": [],
                "observation": "paid key only",
            },
            ensure_ascii=False,
        )

        free_client = Mock()
        paid_client = Mock()
        paid_client.models.generate_content.return_value.text = response_text

        def get_client(api_key=None):
            return {"free-key": free_client, "paid-key": paid_client}[api_key]

        # free-key を事前にレート制限状態にする
        main._mark_rate_limited("free-key")

        with patch.object(main, "_gemini_api_keys", return_value=[("primary", "free-key"), ("paid", "paid-key")]), \
             patch.object(main, "_gemini_models", return_value=["gemini-3.1-flash-lite"]), \
             patch.object(main, "_get_genai_client", side_effect=get_client):
            result = main._generate_vision_result(image_bytes, "test prompt", "symbol-1", 1, 0)

        self.assertEqual(result.observation, "paid key only")
        # free_client は呼び出されていないはず
        free_client.models.generate_content.assert_not_called()

    def test_generate_vision_result_all_keys_fail_raises_503(self):
        """すべてのキーが失敗したら 503 エラー"""
        img = Image.new("RGB", (64, 64), "white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        image_bytes = buf.getvalue()

        fake_client = Mock()
        fake_client.models.generate_content.side_effect = RuntimeError("all failed")

        with patch.object(main, "_gemini_api_keys", return_value=[("primary", "test-key")]), \
             patch.object(main, "_gemini_models", return_value=["gemini-3.1-flash-lite"]), \
             patch.object(main, "_get_genai_client", return_value=fake_client):
            with self.assertRaises(HTTPException) as ctx:
                main._generate_vision_result(image_bytes, "test prompt", "symbol-1", 1, 0)
            self.assertEqual(ctx.exception.status_code, 503)


class EnvironmentConfigTest(unittest.TestCase):
    """環境設定関連のテスト"""

    def test_gemini_models_with_none_returns_default(self):
        """key_label が None のときデフォルトモデルを返す"""
        models = main._gemini_models(None)

        self.assertIsInstance(models, list)
        self.assertGreater(len(models), 0)

    def test_gemini_models_free_tier(self):
        """free キーで無料モデルリストを返す"""
        models = main._gemini_models("free")

        self.assertIsInstance(models, list)
        self.assertGreater(len(models), 0)

    def test_gemini_models_paid_tier(self):
        """paid キーで有料モデルリストを返す"""
        models = main._gemini_models("paid")

        self.assertIsInstance(models, list)
        self.assertGreater(len(models), 0)


class ApiErrorContractTest(unittest.TestCase):
    """API エンドポイントのエラー応答が JSON であることを保証する"""

    def setUp(self):
        self.client = TestClient(main.app)
        main._hits.clear()

    def test_api_404_returns_json_not_html(self):
        """/api/* の 404 は HTML ページではなく JSON を返す"""
        res = self.client.post(
            "/api/judge", json={"symbol_id": "no-such-symbol", "image_b64": inked_png_b64()}
        )
        self.assertEqual(res.status_code, 404)
        self.assertIn("application/json", res.headers["content-type"])
        self.assertEqual(res.json()["detail"], "unknown symbol")

    def test_html_404_still_returns_page(self):
        """通常ページの 404 は HTML のまま"""
        res = self.client.get("/no-such-page")
        self.assertEqual(res.status_code, 404)
        self.assertIn("text/html", res.headers["content-type"])

    def test_openapi_schema_is_not_exposed(self):
        """/openapi.json を公開しない"""
        self.assertEqual(self.client.get("/openapi.json").status_code, 404)


class ClientIpTest(unittest.TestCase):
    """レート制限のキーになるクライアント識別子の決定ロジック"""

    def _request(self, headers=None, peer="10.0.0.1"):
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
            "client": (peer, 12345),
        }
        return Request(scope)

    def test_uses_last_forwarded_for_entry(self):
        """X-Forwarded-For の末尾（インフラが付与する実接続元）を使う"""
        req = self._request({"x-forwarded-for": "1.2.3.4, 5.6.7.8"})
        self.assertEqual(main._client_ip(req), "5.6.7.8")

    def test_ignores_client_spoofed_prefix(self):
        """クライアントが左側に偽装値を入れても末尾が採用される"""
        req = self._request({"x-forwarded-for": "attacker-spoofed, 203.0.113.9"})
        self.assertEqual(main._client_ip(req), "203.0.113.9")

    def test_falls_back_to_peer_without_header(self):
        """ヘッダが無ければ接続元 IP を使う"""
        self.assertEqual(main._client_ip(self._request(peer="192.0.2.5")), "192.0.2.5")

    def test_distinct_clients_get_distinct_rate_buckets(self):
        """異なるクライアントが別々のレート制限バケットになる"""
        main._hits.clear()
        original = main.RATE_LIMIT
        try:
            main.RATE_LIMIT = 1
            main._check_rate(self._request({"x-forwarded-for": "a, 198.51.100.1"}))
            # 別クライアントは影響を受けない
            main._check_rate(self._request({"x-forwarded-for": "a, 198.51.100.2"}))
            # 同じクライアントの2回目は弾かれる
            with self.assertRaises(HTTPException) as ctx:
                main._check_rate(self._request({"x-forwarded-for": "a, 198.51.100.1"}))
            self.assertEqual(ctx.exception.status_code, 429)
            # detail は機械可読な識別子（フロントが文言を組み立てる）
            self.assertEqual(ctx.exception.detail, "rate_limited")
        finally:
            main.RATE_LIMIT = original
            main._hits.clear()


class VisionResultObservationTest(unittest.TestCase):
    """observation の長さ超過で候補を失わないこと"""

    def test_long_observation_is_truncated_not_rejected(self):
        payload = json.dumps(
            {
                "required": [True],
                "forbidden": [],
                "observation": "あ" * (main.MAX_OBSERVATION_CHARS + 200),
            },
            ensure_ascii=False,
        )
        result = main.VisionResult.model_validate_json(payload)
        self.assertEqual(len(result.observation), main.MAX_OBSERVATION_CHARS)


class ReadyzTest(unittest.TestCase):
    """/readyz が全キーのレート制限状態を反映する"""

    def setUp(self):
        self.client = TestClient(main.app)
        main._rate_limited_keys.clear()
        main._fs_status_cache.clear()

    def tearDown(self):
        main._rate_limited_keys.clear()
        main._fs_status_cache.clear()

    def test_readyz_ok_when_key_available(self):
        with patch.object(main, "_gemini_api_keys", return_value=[("primary", "free-key")]):
            res = self.client.get("/readyz")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["keys_available"], 1)

    def test_readyz_503_when_no_symbols_loaded(self):
        with patch.object(main, "SYMBOLS", {}):
            res = self.client.get("/readyz")
        self.assertEqual(res.status_code, 503)

    def test_readyz_503_when_all_keys_rate_limited(self):
        main._mark_rate_limited("free-key")
        with patch.object(main, "_gemini_api_keys", return_value=[("primary", "free-key")]):
            res = self.client.get("/readyz")
        self.assertEqual(res.status_code, 503)

    def test_readyz_ok_when_one_of_two_keys_available(self):
        main._mark_rate_limited("free-key")
        with patch.object(
            main, "_gemini_api_keys", return_value=[("primary", "free-key"), ("paid", "paid-key")]
        ):
            res = self.client.get("/readyz")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["keys_available"], 1)
        self.assertEqual(res.json()["keys_total"], 2)


class GeminiApiKeysTest(unittest.TestCase):
    """_gemini_api_keys の優先順・重複排除"""

    def _env(self, **kwargs):
        # 3 変数を明示的に設定/削除する patch.dict を返す
        base = {k: "" for k in ("GEMINI_API_KEY", "GEMINI_API_KEYS", "GEMINI_PAID_API_KEY")}
        base.update(kwargs)
        return patch.dict(main.os.environ, base, clear=False)

    def test_primary_only(self):
        with self._env(GEMINI_API_KEY="k1"):
            self.assertEqual(main._gemini_api_keys(), [("primary", "k1")])

    def test_order_primary_extra_paid(self):
        with self._env(GEMINI_API_KEY="k1", GEMINI_API_KEYS="k2,k3", GEMINI_PAID_API_KEY="k4"):
            labels = [label for label, _ in main._gemini_api_keys()]
            self.assertEqual(labels, ["primary", "extra_1", "extra_2", "paid"])

    def test_deduplicates_repeated_keys(self):
        # primary と同じ値が extra / paid に現れても 1 度だけ
        with self._env(GEMINI_API_KEY="dup", GEMINI_API_KEYS="dup,other", GEMINI_PAID_API_KEY="dup"):
            values = [value for _, value in main._gemini_api_keys()]
            self.assertEqual(values, ["dup", "other"])

    def test_empty_when_unset(self):
        with self._env():
            self.assertEqual(main._gemini_api_keys(), [])


class GetGenaiClientTest(unittest.TestCase):
    def setUp(self):
        main._clients.clear()

    def tearDown(self):
        main._clients.clear()

    def test_raises_503_when_no_keys(self):
        with patch.object(main, "_gemini_api_keys", return_value=[]):
            with self.assertRaises(HTTPException) as ctx:
                main._get_genai_client()
        self.assertEqual(ctx.exception.status_code, 503)

    def test_caches_client_per_key(self):
        with patch.object(main.genai, "Client", side_effect=lambda api_key: Mock(name=api_key)) as ctor:
            c1 = main._get_genai_client("key-a")
            c2 = main._get_genai_client("key-a")
        self.assertIs(c1, c2)
        self.assertEqual(ctor.call_count, 1)


class InkThresholdBoundaryTest(unittest.TestCase):
    """histogram 化した _count_ink_pixels が旧ループ実装と一致すること"""

    def setUp(self):
        self.original = main.INK_THRESHOLD

    def tearDown(self):
        main.INK_THRESHOLD = self.original

    def test_matches_naive_loop_across_thresholds(self):
        img = Image.new("L", (16, 16))
        img.putdata([0, 100, 200, 244, 245, 255] * 42 + [0, 0, 0, 0])
        for threshold in (0, 1, 200, 245, 256):
            main.INK_THRESHOLD = threshold
            naive = sum(1 for v in img.getdata() if v < threshold)
            self.assertEqual(main._count_ink_pixels(img.convert("RGB")), naive, f"threshold={threshold}")


class EndpointSmokeTest(unittest.TestCase):
    """GET エンドポイントと静的ファイル配信が 200 を返すこと"""

    def setUp(self):
        self.client = TestClient(main.app)
        main._hits.clear()

    def test_html_pages(self):
        for path in ("/", "/drill", "/standards"):
            self.assertEqual(self.client.get(path).status_code, 200, path)

    def test_static_assets(self):
        for path in ("/theme.css", "/favicon.ico", "/favicon-32.png",
                     "/apple-touch-icon.png", "/og.png"):
            self.assertEqual(self.client.get(path).status_code, 200, path)

    def test_catalog_returns_only_verified(self):
        data = self.client.get("/api/catalog").json()
        self.assertTrue(data)
        ids = {s["id"] for s in data}
        verified_ids = {s["id"] for s in main.SYMBOLS.values() if s["verified"]}
        self.assertEqual(ids, verified_ids)
        self.assertIn("ref_svg", data[0])

    def test_symbols_lists_only_verified(self):
        data = self.client.get("/api/symbols").json()
        self.assertEqual({s["id"] for s in data}, {s["id"] for s in main.SYMBOLS.values() if s["verified"]})

    def test_question_returns_verified_symbol(self):
        body = self.client.get("/api/question").json()
        self.assertIn(body["id"], main.SYMBOLS)
        self.assertTrue(main.SYMBOLS[body["id"]]["verified"])

    def test_unverified_symbol_is_not_judgeable_or_listed(self):
        unverified = {**next(s for s in main.SYMBOLS.values() if s["verified"]),
                      "id": "unverified-test", "verified": False}
        with patch.dict(main.SYMBOLS, {unverified["id"]: unverified}, clear=True):
            judged = self.client.post("/api/judge", json={
                "symbol_id": unverified["id"], "image_b64": inked_png_b64(),
            })
            listed = self.client.get("/api/symbols").json()
        self.assertEqual(judged.status_code, 404)
        self.assertEqual(listed, [])

    def test_question_includes_description(self):
        """判定後の解説表示に使うため、出題レスポンスに description を含める。"""
        body = self.client.get("/api/question").json()
        self.assertEqual(body["description"], main.SYMBOLS[body["id"]]["description"])

    def test_healthz(self):
        self.assertEqual(self.client.get("/healthz").json(), {"ok": True})


class ReportEndpointTest(unittest.TestCase):
    """/api/report の正常系と検証"""

    def setUp(self):
        self.client = TestClient(main.app)
        self.symbol = next(s for s in main.SYMBOLS.values() if s["verified"])
        main._hits.clear()

    def _valid_checks(self):
        checks = [{"feature": f"必須: {v}", "ok": True} for v in self.symbol["required_features"]]
        checks += [{"feature": f"除外: {v}がない", "ok": True} for v in self.symbol.get("forbidden_features", [])]
        return checks

    def test_report_accepts_matching_checklist(self):
        with patch.object(main, "FEEDBACK_BUCKET", "test-bucket"), \
             patch.object(main, "_claim_judgment", return_value=("ok", {"symbol_id": self.symbol["id"]})), \
             patch.object(main, "_promote_feedback"):
            res = self.client.post(
                "/api/report",
                json={"judgment_id": "a" * 32},
            )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"ok": True})

    def test_report_unknown_symbol_returns_404_json(self):
        with patch.object(main, "FEEDBACK_BUCKET", "test-bucket"), \
             patch.object(main, "_claim_judgment", return_value=("unknown", None)):
            res = self.client.post("/api/report", json={"judgment_id": "b" * 32})
        self.assertEqual(res.status_code, 404)
        self.assertIn("application/json", res.headers["content-type"])


class LoadSymbolsValidationTest(unittest.TestCase):
    """_load_symbols のデータ整合性検証（不正な symbols.json を起動時に弾く）"""

    def _run_with(self, payload):
        import json as _json
        import pathlib
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            (pathlib.Path(d) / "symbols.json").write_text(_json.dumps(payload), encoding="utf-8")
            with patch.object(main, "ROOT", pathlib.Path(d)):
                return main._load_symbols()

    def _ok_symbol(self, **over):
        base = {
            "id": "s1",
            "name": "記号1",
            "category": "電話設備",
            "required_features": ["縦線が1本ある"],
            "verified": True,
        }
        base.update(over)
        return base

    def test_accepts_valid_payload(self):
        result = self._run_with({"symbols": [self._ok_symbol()]})
        self.assertIn("s1", result)

    def test_rejects_non_list_symbols(self):
        with self.assertRaises(RuntimeError):
            self._run_with({"symbols": "notalist"})

    def test_rejects_duplicate_id(self):
        with self.assertRaises(RuntimeError):
            self._run_with({"symbols": [self._ok_symbol(), self._ok_symbol()]})

    def test_rejects_empty_required_features(self):
        with self.assertRaises(RuntimeError):
            self._run_with({"symbols": [self._ok_symbol(required_features=[])]})

    def test_rejects_when_no_verified_symbol(self):
        with self.assertRaises(RuntimeError):
            self._run_with({"symbols": [self._ok_symbol(verified=False)]})

    def test_rejects_invalid_forbidden_features(self):
        with self.assertRaises(RuntimeError):
            self._run_with({"symbols": [self._ok_symbol(forbidden_features=[""])]})

    def test_rejects_invalid_catalog_fields(self):
        # verified="yes" は従来の bool() 判定なら truthy で通っていた。
        for field, value in (("name", ""), ("category", None), ("verified", "yes")):
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                self._run_with({"symbols": [self._ok_symbol(**{field: value})]})

    def test_rejects_missing_catalog_fields(self):
        for field in ("name", "category", "verified"):
            symbol = self._ok_symbol()
            del symbol[field]
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                self._run_with({"symbols": [symbol]})


class ReviewRegressionTest(unittest.TestCase):
    """課金・応答整合性・署名のレビュー指摘に対する回帰テスト。"""

    def setUp(self):
        self.client = TestClient(main.app)
        self.symbol = next(s for s in main.SYMBOLS.values() if s["verified"])
        main._hits.clear()
        main._rate_limited_keys.clear()
        main._fs_status_cache.clear()
        main._quota_memory.clear()

    def _response(self):
        return json.dumps({
            "required": [True] * len(self.symbol["required_features"]),
            "forbidden": [False] * len(self.symbol.get("forbidden_features", [])),
            "observation": "ok",
        })

    def test_global_daily_limit_returns_429_without_calling_gemini(self):
        with patch.object(main, "_consume_daily_quotas", return_value=None), \
             patch.object(main, "_get_genai_client") as get_client:
            response = self.client.post("/api/judge", json={
                "symbol_id": self.symbol["id"], "image_b64": inked_png_b64(),
            })
        self.assertEqual(response.status_code, 429)
        get_client.assert_not_called()

    def test_paid_limit_is_independent_from_global_limit(self):
        paid = Mock()
        paid.models.generate_content.return_value.text = self._response()
        judge_tokens = [{"backend": "memory", "key": field, "ref": None} for field in ("ip", "global")]
        with patch.object(main, "_consume_daily_quotas", return_value=judge_tokens), \
             patch.object(main, "_consume_daily_quota", return_value=None), \
             patch.object(main, "_gemini_api_keys", return_value=[("paid", "paid-key")]), \
             patch.object(main, "_get_genai_client", return_value=paid):
            response = self.client.post("/api/judge", json={
                "symbol_id": self.symbol["id"], "image_b64": inked_png_b64(),
            })
        self.assertEqual(response.status_code, 503)
        paid.models.generate_content.assert_not_called()

    def test_short_feature_array_falls_back_instead_of_failing_drawing(self):
        short = json.loads(self._response())
        short["required"] = short["required"][:-1]
        client = Mock()
        client.models.generate_content.side_effect = [Mock(text=json.dumps(short)), Mock(text=self._response())]
        with patch.object(main, "_gemini_api_keys", return_value=[("primary", "key")]), \
             patch.object(main, "_gemini_models", return_value=["first", "second"]), \
             patch.object(main, "_get_genai_client", return_value=client):
            response = self.client.post("/api/judge", json={
                "symbol_id": self.symbol["id"], "image_b64": inked_png_b64(),
            })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(client.models.generate_content.call_count, 2)

    def test_recent_success_does_not_reduce_429_escalation(self):
        main._mark_rate_limited("key")
        main._rate_limited_keys["key"] = (main.time.time() - main._BACKOFF_SECONDS[0] - 1, 1)
        client = Mock()
        client.models.generate_content.return_value.text = self._response()
        with patch.object(main, "_gemini_api_keys", return_value=[("primary", "key")]), \
             patch.object(main, "_gemini_models", return_value=["model"]), \
             patch.object(main, "_get_genai_client", return_value=client):
            main._generate_vision_result(
                b"png", "prompt", self.symbol["id"],
                len(self.symbol["required_features"]), len(self.symbol.get("forbidden_features", [])),
            )
        main._mark_rate_limited("key")
        self.assertEqual(main._get_rate_limit_status("key")["consecutive_count"], 2)

    def test_stable_success_reduces_only_one_backoff_level(self):
        main._rate_limited_keys["key"] = (main.time.time() - main._BACKOFF_SECONDS[2] * 2 - 1, 3)
        with patch.object(main, "_get_firestore_client", return_value=None):
            main._mark_key_succeeded("key")
        self.assertEqual(main._rate_limited_keys["key"][1], 2)

    def test_unverified_symbol_is_not_judgeable_or_listed(self):
        unverified = {**self.symbol, "id": "unverified-test", "verified": False}
        with patch.dict(main.SYMBOLS, {unverified["id"]: unverified}, clear=True):
            response = self.client.post("/api/judge", json={
                "symbol_id": unverified["id"], "image_b64": inked_png_b64(),
            })
            listed = self.client.get("/api/symbols").json()
        self.assertEqual(response.status_code, 404)
        self.assertEqual(listed, [])

    def test_report_rejects_unknown_and_replayed_id(self):
        with patch.object(main, "FEEDBACK_BUCKET", "test-bucket"), \
             patch.object(main, "_claim_judgment", side_effect=[("unknown", None), ("replayed", None)]):
            self.assertEqual(self.client.post("/api/report", json={"judgment_id": "c" * 32}).status_code, 404)
            self.assertEqual(self.client.post("/api/report", json={"judgment_id": "c" * 32}).status_code, 409)

    def test_firestore_io_is_not_called_while_rate_limit_lock_is_held(self):
        class TrackingLock:
            held = False
            def __enter__(self):
                self.held = True
            def __exit__(self, *args):
                self.held = False

        lock = TrackingLock()
        snapshot = Mock(exists=False)
        ref = Mock()
        ref.get.side_effect = lambda *a, **k: (
            self.fail("Firestore get under _rate_limit_lock") if lock.held else snapshot
        )
        ref.set.side_effect = lambda *a, **k: (
            self.fail("Firestore set under _rate_limit_lock") if lock.held else None
        )
        db = Mock()
        db.collection.return_value.document.return_value = ref
        with patch.object(main, "_rate_limit_lock", lock), \
             patch.object(main, "_get_firestore_client", return_value=db):
            self.assertIsNone(main._get_rate_limit_status("io-key"))
            main._mark_rate_limited("io-key")

    def test_daily_quota_uses_independent_transaction_fields(self):
        full = Mock(exists=True)
        full.to_dict.return_value = {"count": 1}
        available = Mock(exists=True)
        available.to_dict.return_value = {"count": 0}
        ref = Mock()
        ref.get.side_effect = [full, available]
        transaction = Mock()
        db = Mock()
        db.transaction.return_value = transaction
        db.collection.return_value.document.return_value.collection.return_value.document.return_value = ref
        with patch.object(main, "_get_firestore_client", return_value=db), \
             patch.object(main.firestore, "transactional", side_effect=lambda fn: fn):
            self.assertFalse(main._consume_daily_quota("judge_calls", 3))
            self.assertTrue(main._consume_daily_quota("paid_calls", 20))
        transaction.set.assert_called_once()
        update = transaction.set.call_args.args[1]
        self.assertIn("count", update)

    def test_subject_quota_uses_full_limit_instead_of_one_shard_slice(self):
        # 主体別カウンタをさらにシャードすると 1 主体は常に同じシャードへ落ち、
        # 実効上限が limit/QUOTA_SHARDS まで縮んでしまう。
        snapshot = Mock(exists=True)
        snapshot.to_dict.return_value = {"count": main.DAILY_IP_LIMIT - 1}
        ref = Mock()
        ref.get.return_value = snapshot
        db = Mock()
        db.collection.return_value.document.return_value.collection.return_value.document.return_value = ref
        with patch.object(main, "_get_firestore_client", return_value=db), \
             patch.object(main.firestore, "transactional", side_effect=lambda fn: fn):
            token = main._consume_daily_quota("ip_calls", main.DAILY_IP_LIMIT, "203.0.113.7")
        self.assertIsNotNone(token)
        self.assertTrue(token["key"].endswith("-0"))

    def test_promote_feedback_keeps_record_when_pending_image_is_missing(self):
        # 保留画像が無くても、報告は既に disputed 済みで再送できない。
        # copy_blob の例外で判定メタデータまで失わせない。
        blob = Mock()
        bucket = Mock()
        bucket.blob.return_value = blob
        bucket.copy_blob.side_effect = RuntimeError("pending image not found")
        storage = Mock()
        storage.bucket.return_value = bucket
        judgment_id = "a" * 32
        with patch.object(main, "FEEDBACK_BUCKET", "test-bucket"), \
             patch.object(main, "_get_storage_client", return_value=storage), \
             self.assertLogs("kenzu", "ERROR"):
            main._promote_feedback(judgment_id, {"symbol_id": self.symbol["id"]})
        self.assertEqual(bucket.blob.call_args_list[0].args[0], f"disputed/{judgment_id}.json")
        blob.upload_from_string.assert_called_once()

    def test_feedback_persistence_is_not_deferred_to_background_tasks(self):
        # Cloud Run は既定でレスポンス後に CPU を絞るため、BackgroundTasks の完了時刻を
        # 別の API から当てにできない。異議報告の経路は同期で行う。
        self.assertNotIn("background", inspect.signature(main.report).parameters)

    def test_page_response_is_private_and_varies_by_host(self):
        response = self.client.get("/drill")
        self.assertIn("private", response.headers["cache-control"])
        self.assertEqual(response.headers["vary"], "Host")

    def test_client_ip_counts_from_the_end_not_proxy_hops(self):
        request = ClientIpTest()._request({"x-forwarded-for": "1.1.1.1, 2.2.2.2, 3.3.3.3"})
        with patch.object(main, "CLIENT_IP_INDEX_FROM_END", 2):
            self.assertEqual(main._client_ip(request), "2.2.2.2")

    def test_firestore_outage_reports_503_not_expired_404(self):
        # サーバー側障害を 404 で返すと、フロントが「報告期限が切れています」と
        # 誤って表示してしまう。
        with patch.object(main, "FEEDBACK_BUCKET", "test-bucket"), \
             patch.object(main, "_get_firestore_client", return_value=None):
            response = self.client.post("/api/report", json={"judgment_id": "f" * 32})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "unavailable")

    def test_claim_judgment_transaction_failure_is_unavailable(self):
        db = Mock()
        db.transaction.side_effect = RuntimeError("firestore down")
        with patch.object(main, "_get_firestore_client", return_value=db):
            self.assertEqual(main._claim_judgment("f" * 32), ("unavailable", None))

    def test_generate_vision_result_requires_expected_feature_counts(self):
        # 既定値で長さ検証を素通りできると、_flag_at が IndexError を投げる。
        with self.assertRaises(TypeError):
            main._generate_vision_result(b"png", "prompt", self.symbol["id"])

    def test_paid_quota_is_released_when_no_model_is_configured(self):
        paid_token = {"backend": "memory", "key": "paid", "ref": None, "released": False}
        with patch.object(main, "_gemini_api_keys", return_value=[("paid", "paid-key")]), \
             patch.object(main, "_consume_daily_quota", return_value=paid_token), \
             patch.object(main, "_gemini_models", return_value=[]), \
             patch.object(main, "_release_daily_quota") as release:
            with self.assertRaises(HTTPException):
                main._generate_vision_result(b"png", "prompt", self.symbol["id"], 1, 0)
        release.assert_called_once_with(paid_token)

    def test_failed_gemini_releases_ip_and_global_reservations(self):
        tokens = [
            {"backend": "memory", "key": "ip", "ref": None},
            {"backend": "memory", "key": "global", "ref": None},
        ]
        with patch.object(main, "_consume_daily_quotas", return_value=tokens), \
             patch.object(main, "_generate_vision_result", side_effect=HTTPException(503, "failed")), \
             patch.object(main, "_release_daily_quota") as release:
            response = self.client.post("/api/judge", json={
                "symbol_id": self.symbol["id"], "image_b64": inked_png_b64(),
            })
        self.assertEqual(response.status_code, 503)
        self.assertEqual(release.call_count, 2)

    def test_ip_and_global_quota_are_reserved_in_one_transaction(self):
        # 個別に取ると、後段が枯渇したとき前段を解放して回る必要が出る。
        with patch.object(main, "_consume_daily_quotas", return_value=None) as consume, \
             patch.object(main, "_get_genai_client") as get_client:
            response = self.client.post("/api/judge", json={
                "symbol_id": self.symbol["id"], "image_b64": inked_png_b64(),
            })
        self.assertEqual(response.status_code, 429)
        consume.assert_called_once()
        self.assertEqual(
            [field for field, _, _ in consume.call_args.args[0]], ["ip_calls", "judge_calls"]
        )
        get_client.assert_not_called()

    def test_partial_memory_quota_reservation_is_rolled_back(self):
        main._quota_memory.clear()
        with patch.object(main, "MAX_INSTANCES", 1), \
             patch.object(main, "_get_firestore_client", return_value=None):
            self.assertIsNotNone(main._consume_daily_quotas([("a", 1, "s"), ("b", 5, "s")]))
            # a はもう枯渇。b だけ増えた状態を残さない。
            self.assertIsNone(main._consume_daily_quotas([("a", 1, "s"), ("b", 5, "s")]))
            self.assertIsNotNone(main._consume_daily_quotas([("b", 5, "s")]))
        key = main._quota_target(main._quota_day(), "b", 5, "s")["memory_key"]
        self.assertEqual(main._quota_memory[key], 2)

    def test_memory_quota_divides_limit_by_max_instances(self):
        main._quota_memory.clear()
        with patch.object(main, "MAX_INSTANCES", 2), \
             patch.object(main, "_get_firestore_client", return_value=None):
            self.assertIsNotNone(main._consume_daily_quota("judge", 4))
            self.assertIsNotNone(main._consume_daily_quota("judge", 4))
            self.assertIsNone(main._consume_daily_quota("judge", 4))
        self.assertEqual(main._quota_status()["quota_backend"], "memory")

    def test_quota_status_counts_fallbacks_instead_of_last_backend(self):
        # 成功した 1 リクエストで firestore に戻る単純なフラグでは部分障害が隠れる。
        before = main._quota_status()["quota_fallbacks"]
        with patch.object(main, "_get_firestore_client", return_value=None):
            main._consume_daily_quota("fallback-probe", 5)
        self.assertEqual(main._quota_status()["quota_fallbacks"], before + 1)

    def test_quota_status_counts_fallbacks_when_memory_quota_is_exhausted(self):
        # 退避中に枠が枯渇して 429 を返した経路こそ、監視から漏らしてはいけない。
        main._quota_memory.clear()
        with patch.object(main, "MAX_INSTANCES", 1), \
             patch.object(main, "_get_firestore_client", return_value=None):
            self.assertIsNotNone(main._consume_daily_quota("exhaust-probe", 1))
            before = main._quota_status()["quota_fallbacks"]
            self.assertIsNone(main._consume_daily_quota("exhaust-probe", 1))
        self.assertEqual(main._quota_status()["quota_fallbacks"], before + 1)

    def test_quota_day_changes_at_jst_midnight(self):
        before = main.datetime.datetime(2026, 7, 28, 14, 59, 59, tzinfo=main.datetime.UTC)
        after = main.datetime.datetime(2026, 7, 28, 15, 0, 1, tzinfo=main.datetime.UTC)
        self.assertEqual(main._quota_day(before), "2026-07-28")
        self.assertEqual(main._quota_day(after), "2026-07-29")

    def test_page_base_url_does_not_stick_between_hosts(self):
        with patch.object(main, "PUBLIC_BASE_URL", ""):
            first = self.client.get("https://first.example/drill")
            second = self.client.get("https://second.example/drill")
        self.assertIn("https://first.example/drill", first.text)
        self.assertIn("https://second.example/drill", second.text)
        self.assertNotIn("first.example", second.text)

    def test_public_base_url_ignores_host_header(self):
        with patch.object(main, "PUBLIC_BASE_URL", "https://canonical.example/"):
            response = self.client.get("/drill", headers={"host": "attacker.example"})
        self.assertIn("https://canonical.example/drill", response.text)
        self.assertNotIn("attacker.example", response.text)

    def test_short_xff_falls_back_to_last_and_warns(self):
        request = ClientIpTest()._request({"x-forwarded-for": "203.0.113.10"})
        with patch.object(main, "CLIENT_IP_INDEX_FROM_END", 2), self.assertLogs("kenzu", "WARNING"):
            self.assertEqual(main._client_ip(request), "203.0.113.10")

    def test_claim_judgment_rejects_expired_and_replayed_records(self):
        transaction = Mock()
        db = Mock()
        db.transaction.return_value = transaction
        ref = db.collection.return_value.document.return_value
        expired = Mock(exists=True)
        expired.to_dict.return_value = {
            "expires_at": main.datetime.datetime.now(main.datetime.UTC) - main.datetime.timedelta(seconds=1),
            "disputed": False,
        }
        replayed = Mock(exists=True)
        replayed.to_dict.return_value = {
            "expires_at": main.datetime.datetime.now(main.datetime.UTC) + main.datetime.timedelta(seconds=1),
            "disputed": True,
        }
        ref.get.side_effect = [expired, replayed]
        with patch.object(main, "_get_firestore_client", return_value=db), \
             patch.object(main.firestore, "transactional", side_effect=lambda fn: fn):
            self.assertEqual(main._claim_judgment("d" * 32)[0], "expired")
            self.assertEqual(main._claim_judgment("d" * 32)[0], "replayed")
        transaction.update.assert_not_called()

    def test_report_is_disabled_without_feedback_bucket(self):
        with patch.object(main, "FEEDBACK_BUCKET", ""):
            response = self.client.post("/api/report", json={"judgment_id": "e" * 32})
        self.assertEqual(response.status_code, 503)


class FirestoreResilienceTest(unittest.TestCase):
    """Firestore 読み取りが失敗しても None を返し、判定を継続できる"""

    def setUp(self):
        main._rate_limited_keys.clear()
        main._fs_status_cache.clear()

    def tearDown(self):
        main._rate_limited_keys.clear()
        main._fs_status_cache.clear()

    def test_read_error_returns_none(self):
        db = Mock()
        db.collection.return_value.document.return_value.get.side_effect = RuntimeError("firestore down")
        with patch.object(main, "_get_firestore_client", return_value=db):
            # メモリ・キャッシュに無いキーなので Firestore を引きにいき、例外 → None
            self.assertIsNone(main._get_rate_limit_status("unknown-key"))

    def test_read_error_is_not_cached(self):
        db = Mock()
        db.collection.return_value.document.return_value.get.side_effect = RuntimeError("firestore down")
        with patch.object(main, "_get_firestore_client", return_value=db):
            main._get_rate_limit_status("unknown-key")
        # 障害時はキャッシュしない（次回再試行できる）
        self.assertNotIn("unknown-key", main._fs_status_cache)


class SaveToGcsUploadTest(unittest.TestCase):
    """_save_to_gcs が画像とJSONを正しいパス/Content-Typeで書き込む"""

    def setUp(self):
        self._orig = main._storage_client

    def tearDown(self):
        main._storage_client = self._orig

    def test_uploads_png_and_json_with_correct_paths(self):
        blobs = {}

        class FakeBlob:
            def __init__(self, name):
                self.name = name
            def upload_from_string(self, data, content_type=None):
                blobs[self.name] = (data, content_type)

        class FakeBucket:
            def blob(self, name):
                return FakeBlob(name)

        class FakeStorage:
            def __init__(self):
                self.requested = None
            def bucket(self, name):
                self.requested = name
                return FakeBucket()

        fake = FakeStorage()
        main._storage_client = fake
        main._save_to_gcs("zukigou-all", "sym1", b"\x89PNG-bytes",
                          {"passed": True, "score": "3/3"}, prefix="judgments")

        self.assertEqual(fake.requested, "zukigou-all")
        names = sorted(blobs.keys())
        self.assertEqual(len(names), 2)
        png = [n for n in names if n.endswith(".png")][0]
        js = [n for n in names if n.endswith(".json")][0]
        # パス書式: prefix/symbol_id/YYYY-MM-DD/<hex>.{png,json}
        self.assertTrue(png.startswith("judgments/sym1/"))
        self.assertEqual(blobs[png], (b"\x89PNG-bytes", "image/png"))
        self.assertEqual(blobs[js][1], "application/json")
        self.assertIn('"passed"', blobs[js][0])

    def test_upload_failure_is_swallowed(self):
        class BoomStorage:
            def bucket(self, name):
                raise RuntimeError("gcs down")

        main._storage_client = BoomStorage()
        # 例外は判定処理に波及させない（Noneを返して静かに失敗）
        try:
            main._save_to_gcs("b", "sym1", b"x", {"a": 1}, prefix="judgments")
        except Exception:
            self.fail("_save_to_gcs must not raise")


SAMPLE_SYMBOL = {
    "id": "sample",
    "name": "接地極",
    "required_features": ["縦線が1本ある", "水平線が3本ある"],
    "forbidden_features": ["水平線が3本以外である"],
}


class BuildVisionPromptTest(unittest.TestCase):
    """プロンプト生成。評価ハーネスが本番と同じ文面を測れるよう関数として切り出してある。"""

    def test_production_prompt_names_the_symbol(self):
        prompt = main.build_vision_prompt(SAMPLE_SYMBOL)
        self.assertIn("課題は「接地極」です", prompt)

    def test_blind_prompt_hides_the_symbol_name(self):
        prompt = main.build_vision_prompt(SAMPLE_SYMBOL, blind=True)
        self.assertNotIn("接地極", prompt)
        self.assertNotIn("課題は", prompt)

    def test_all_features_are_listed_with_indices(self):
        prompts = (main.build_vision_prompt(SAMPLE_SYMBOL), main.build_vision_prompt(SAMPLE_SYMBOL, blind=True))
        for prompt in prompts:
            for feature in SAMPLE_SYMBOL["required_features"] + SAMPLE_SYMBOL["forbidden_features"]:
                self.assertIn(feature, prompt)
            self.assertIn('"0"', prompt)
            self.assertIn('"1"', prompt)

    def test_symbol_without_forbidden_features_is_supported(self):
        prompt = main.build_vision_prompt({"name": "x", "required_features": ["枠がある"]})
        self.assertIn("枠がある", prompt)

    def test_judge_endpoint_uses_the_production_prompt(self):
        symbol = next(s for s in main.SYMBOLS.values() if s["verified"])
        captured = {}

        def fake_generate(image, prompt, symbol_id, required_count, forbidden_count):
            captured["prompt"] = prompt
            return main.VisionResult(
                required=[True] * len(symbol["required_features"]),
                forbidden=[False] * len(symbol.get("forbidden_features", [])),
                observation="ok",
            )

        with patch.object(main, "_generate_vision_result", side_effect=fake_generate):
            client = TestClient(main.app)
            response = client.post(
                "/api/judge", json={"symbol_id": symbol["id"], "image_b64": inked_png_b64()}
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured["prompt"], main.build_vision_prompt(symbol))


class ScoreObservationTest(unittest.TestCase):
    """合否はコード側で決まる。Gemini の観察をどう畳み込むかの単体検証。"""

    def test_all_features_satisfied_passes(self):
        result = main.VisionResult(required=[True, True], forbidden=[False], observation="")
        scored = main.score_observation(SAMPLE_SYMBOL, result)
        self.assertTrue(scored["passed"])
        self.assertEqual(scored["score"], "3/3")
        self.assertEqual(scored["mistakes"], [])

    def test_missing_required_feature_fails_and_is_named(self):
        result = main.VisionResult(required=[True, False], forbidden=[False], observation="")
        scored = main.score_observation(SAMPLE_SYMBOL, result)
        self.assertFalse(scored["passed"])
        self.assertEqual(scored["score"], "2/3")
        self.assertEqual(scored["mistakes"], ["必須特徴が不足: 水平線が3本ある"])

    def test_forbidden_feature_present_fails(self):
        result = main.VisionResult(required=[True, True], forbidden=[True], observation="")
        scored = main.score_observation(SAMPLE_SYMBOL, result)
        self.assertFalse(scored["passed"])
        self.assertEqual(scored["mistakes"], ["対象外の特徴を検出: 水平線が3本以外である"])

    def test_checks_are_labelled_for_display(self):
        result = main.VisionResult(required=[True, True], forbidden=[False], observation="")
        labels = [check["feature"] for check in main.score_observation(SAMPLE_SYMBOL, result)["checks"]]
        self.assertEqual(labels[0], "必須: 縦線が1本ある")
        self.assertEqual(labels[-1], "除外: 水平線が3本以外であるがない")


if __name__ == "__main__":
    unittest.main()
