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


if __name__ == "__main__":
    unittest.main()
