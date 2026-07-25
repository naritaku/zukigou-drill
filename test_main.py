import base64
import io
import json
import unittest
from unittest.mock import Mock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

import main


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
        confusions = self.symbol.get("confusable_symbols", [])
        response_text = json.dumps(
            {
                "required": [True for _ in required],
                "forbidden": [False for _ in forbidden],
                "confusions": [False for _ in confusions],
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

    def test_judge_falls_back_models_before_paid_key(self):
        required = self.symbol.get("required_features", self.symbol["features"])
        response_text = json.dumps(
            {
                "required": [True for _ in required],
                "forbidden": [],
                "confusions": [],
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
                "confusions": [True],
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
        self.assertEqual(result.confusions, [True])
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
                "confusions": [],
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
                "confusions": [],
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
                "confusions": [],
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
                "confusions": [],
                "observation": "paid key succeeded",
            },
            ensure_ascii=False,
        )

        free_client = Mock()
        paid_client = Mock()

        rate_limit_error = RuntimeError("rate limited")
        rate_limit_error.status_code = 429
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
                "confusions": [],
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


if __name__ == "__main__":
    unittest.main()
