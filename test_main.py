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


class ImageProcessingTest(unittest.TestCase):
    """画像処理関数のテスト"""

    def test_flatten_image_rgb_unchanged(self):
        """RGB 画像は変換されない"""
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

    def test_count_ink_pixels_blank_image(self):
        """白紙画像はインク量が少ない"""
        img = Image.new("RGB", (64, 64), "white")

        count = main._count_ink_pixels(img)

        self.assertEqual(count, 0)

    def test_count_ink_pixels_with_black_region(self):
        """黒い領域を含む画像はインク量が増える"""
        img = Image.new("RGB", (64, 64), "white")
        draw = ImageDraw.Draw(img)
        draw.rectangle((10, 10, 50, 50), fill="black")

        count = main._count_ink_pixels(img)

        # 40x40 の黒い領域がある（1600 ピクセル）
        self.assertGreater(count, 1000)


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
