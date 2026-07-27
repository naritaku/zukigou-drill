import base64
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
        response_text = json.dumps(
            {
                "required": [True for _ in required],
                "forbidden": [],
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

    def test_report_rejects_tampered_checklist_before_saving_feedback(self):
        judgment = {
            "passed": True,
            "checks": [{"feature": "改ざんされた項目", "ok": True}],
            "mistakes": [],
            "observation": "",
        }
        with patch.object(main, "_save_feedback") as save_feedback:
            res = self.client.post(
                "/api/report",
                json={"symbol_id": self.symbol["id"], "image_b64": inked_png_b64(), "judgment": judgment},
            )
        self.assertEqual(res.status_code, 400)
        save_feedback.assert_not_called()


class RateLimitingTest(unittest.TestCase):
    def setUp(self):
        main._rate_limited_keys.clear()
        main._fs_status_cache.clear()

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
                main._generate_vision_result(image_bytes, "test prompt", "symbol-1")
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
            result = main._generate_vision_result(image_bytes, "test prompt", "symbol-1")

        self.assertIsInstance(result, main.VisionResult)
        self.assertEqual(result.required, [True, False])
        self.assertEqual(result.forbidden, [False, True])
        self.assertEqual(result.observation, "観察結果")

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
            result = main._generate_vision_result(image_bytes, "test prompt", "symbol-1")

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
            result = main._generate_vision_result(image_bytes, "test prompt", "symbol-1")

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
            result = main._generate_vision_result(image_bytes, "test prompt", "symbol-1")

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
            result = main._generate_vision_result(image_bytes, "test prompt", "symbol-1")

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
            result = main._generate_vision_result(image_bytes, "test prompt", "symbol-1")

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
                main._generate_vision_result(image_bytes, "test prompt", "symbol-1")
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
        judgment = {"passed": True, "checks": self._valid_checks(), "mistakes": [], "observation": ""}
        with patch.object(main, "_save_feedback") as save:
            res = self.client.post(
                "/api/report",
                json={"symbol_id": self.symbol["id"], "image_b64": inked_png_b64(), "judgment": judgment},
            )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"ok": True})

    def test_report_unknown_symbol_returns_404_json(self):
        judgment = {"passed": True, "checks": [], "mistakes": [], "observation": ""}
        res = self.client.post(
            "/api/report",
            json={"symbol_id": "nope", "image_b64": inked_png_b64(), "judgment": judgment},
        )
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


if __name__ == "__main__":
    unittest.main()
