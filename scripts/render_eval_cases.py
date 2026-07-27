#!/usr/bin/env python3
"""symbols.json の `ref_svg`（お手本）を PNG に焼いて、評価ケースの土台を作る。

    python3 scripts/render_eval_cases.py

生成されるのは「教科書どおりの理想解答」であって手描きではない。したがって
これで測れるのは **下限** だけ ——「お手本すら通らない記号」はどう頑張っても
手描きでは通らない。手描きケースは実機で描いたものを
`judgment_eval/cases/<id>/` に追加していく（docs/EVALUATION.md 参照）。

依存は dev-requirements.txt の Playwright のみ（新規依存なし）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CASES_DIR = ROOT / "judgment_eval" / "cases"
CASE_PREFIX = "ref-"
# ドリルのクライアント側正規化(白背景・余白付き 512px)に合わせる。
CANVAS_PX = 512
PADDING_PX = 24

PAGE_TEMPLATE = """<!DOCTYPE html><html><head><meta charset="utf-8"><style>
  html, body {{ margin: 0; padding: 0; background: #fff; }}
  #stage {{
    width: {canvas}px; height: {canvas}px; box-sizing: border-box; padding: {padding}px;
    background: #fff; display: flex; align-items: center; justify-content: center;
  }}
  #stage svg {{ width: 100%; height: 100%; }}
</style></head><body><div id="stage">{svg}</div></body></html>"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--symbol", action="append", default=[], help="この symbol_id だけ再生成する(複数可)")
    args = parser.parse_args(argv)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("❌ Playwright が必要です: pip install -r dev-requirements.txt", file=sys.stderr)
        return 1

    import main as app

    targets = [
        symbol for symbol in app.SYMBOLS.values()
        if symbol.get("verified") and symbol.get("ref_svg")
        and (not args.symbol or symbol["id"] in set(args.symbol))
    ]
    if not targets:
        print("❌ 対象の記号がありません", file=sys.stderr)
        return 1

    missing_svg = [s["id"] for s in app.SYMBOLS.values() if s.get("verified") and not s.get("ref_svg")]
    if missing_svg:
        print(f"⚠️  ref_svg が無いためケースを作れない記号: {', '.join(missing_svg)}")

    CASES_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": CANVAS_PX, "height": CANVAS_PX}, device_scale_factor=1)
        for symbol in targets:
            case_dir = CASES_DIR / f"{CASE_PREFIX}{symbol['id']}"
            case_dir.mkdir(parents=True, exist_ok=True)
            page.set_content(PAGE_TEMPLATE.format(canvas=CANVAS_PX, padding=PADDING_PX, svg=symbol["ref_svg"]))
            page.locator("#stage").screenshot(path=str(case_dir / "image.png"), type="png")
            (case_dir / "case.json").write_text(
                json.dumps(
                    {
                        "symbol_id": symbol["id"],
                        "expect": "pass",
                        "source": "rendered-reference",
                        "note": f"{symbol['name']} のお手本 SVG をそのまま描画した理想解答",
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            print(f"  ✅ {case_dir.relative_to(ROOT)}")
        browser.close()

    print(f"\n{len(targets)} 件のケースを生成しました → {CASES_DIR.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
