#!/usr/bin/env python3
"""Capture browser screenshots for PR visual review.

The script starts the FastAPI app with uvicorn, opens a page in Chromium via
Playwright, captures a screenshot, and writes a small JSON summary that can be
copied into task results or PR descriptions.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT / "artifacts" / "visual-review"


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_until_ready(url: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=1) as response:
                if response.status < 500:
                    return
        except (OSError, URLError) as exc:
            last_error = exc
        time.sleep(0.2)
    raise RuntimeError(f"server did not become ready at {url}: {last_error}")


def _safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "-" for ch in value).strip("-") or "root"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Capture a Chromium screenshot of the local FastAPI app.")
    parser.add_argument("--path", default="/", help="App path to open, for example / or /drill")
    parser.add_argument("--label", default="after", help="Screenshot label such as before or after")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="Directory for screenshots and metadata")
    parser.add_argument("--host", default="127.0.0.1", help="Host used by the temporary uvicorn server")
    parser.add_argument("--port", type=int, default=0, help="Port for uvicorn; 0 chooses a free port")
    parser.add_argument("--viewport-width", type=int, default=390, help="Browser viewport width")
    parser.add_argument("--viewport-height", type=int, default=844, help="Browser viewport height")
    parser.add_argument("--timeout", type=float, default=15, help="Seconds to wait for the local server")
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    port = args.port or _find_free_port()
    env = os.environ.copy()
    env.setdefault("GEMINI_API_KEY", "visual-review-placeholder")

    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "main:app",
            "--host",
            args.host,
            "--port",
            str(port),
        ],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        base_url = f"http://{args.host}:{port}"
        page_path = args.path if args.path.startswith("/") else f"/{args.path}"
        target_url = f"{base_url}{page_path}"
        _wait_until_ready(f"{base_url}/healthz", args.timeout)

        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Playwright is not installed. Run: python -m pip install -r dev-requirements.txt"
            ) from exc

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        output_dir = args.output_dir.resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        stem = f"{timestamp}-{_safe_name(args.label)}-{_safe_name(page_path)}"
        screenshot_path = output_dir / f"{stem}.png"
        metadata_path = output_dir / f"{stem}.json"

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": args.viewport_width, "height": args.viewport_height})
            page.goto(target_url, wait_until="networkidle")
            page.screenshot(path=screenshot_path, full_page=True)
            browser.close()

        metadata = {
            "label": args.label,
            "url": target_url,
            "path": page_path,
            "viewport": {"width": args.viewport_width, "height": args.viewport_height},
            "screenshot": str(screenshot_path.relative_to(ROOT)),
            "captured_at": timestamp,
        }
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(metadata, ensure_ascii=False, indent=2))
        return 0
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
