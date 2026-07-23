import base64
import io
import unittest

from fastapi import HTTPException
from PIL import Image, ImageDraw

import main


def png_b64(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


class ImageValidationTest(unittest.TestCase):
    def test_decode_png_rejects_blank_before_external_judgment(self):
        img = Image.new("RGB", (64, 64), "white")
        with self.assertRaises(HTTPException) as ctx:
            main._decode_png(png_b64(img))
        self.assertEqual(ctx.exception.status_code, 400)

    def test_decode_png_accepts_and_normalizes_inked_png(self):
        img = Image.new("RGB", (64, 64), "white")
        draw = ImageDraw.Draw(img)
        draw.line((4, 4, 60, 60), fill="black", width=3)
        payload = main._decode_png("data:image/png;base64," + png_b64(img))
        self.assertTrue(payload.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_decode_png_rejects_malformed_png_magic(self):
        malformed = base64.b64encode(b"\x89PNG\r\n\x1a\nnot really png").decode("ascii")
        with self.assertRaises(HTTPException) as ctx:
            main._decode_png(malformed)
        self.assertEqual(ctx.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
