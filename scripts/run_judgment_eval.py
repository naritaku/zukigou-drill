#!/usr/bin/env python3
"""判定精度を実測する。実際に Gemini を呼ぶので API キーと課金が必要。

    bash scripts/load-env.sh && python3 scripts/run_judgment_eval.py --dry-run
    python3 scripts/run_judgment_eval.py                 # 本番と同じ条件で 1 周
    python3 scripts/run_judgment_eval.py --repeat 3      # ブレも測る
    python3 scripts/run_judgment_eval.py --blind         # 記号名を伏せた対照条件

判定ロジックは main.py の `build_vision_prompt` / `score_observation` を
そのまま使う。評価用に別実装を持つと、測っているものが本番とずれる。
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 1 判定あたりのおおよその実測コスト(USD)。画像は解像度によらず約 1,090 トークン。
COST_PER_JUDGMENT_USD = 0.0005


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cases-dir", type=Path, default=None, help="ケースのディレクトリ")
    parser.add_argument("--repeat", type=int, default=1, help="各ケースを何回判定するか(ブレの測定用)")
    parser.add_argument("--blind", action="store_true", help="プロンプトから記号名を伏せる対照条件")
    parser.add_argument("--symbol", action="append", default=[], help="この symbol_id のケースだけ実行(複数可)")
    parser.add_argument("--source", choices=("rendered-reference", "handwritten"), help="出所で絞り込む")
    parser.add_argument("--limit", type=int, default=0, help="先頭 N ケースだけ実行(動作確認用)")
    parser.add_argument("--model", default="", help="このモデルに固定する(既定は main.py のフォールバック順)")
    parser.add_argument("--concurrency", type=int, default=4, help="同時実行数")
    parser.add_argument("--out", type=Path, default=ROOT / "artifacts" / "eval", help="出力先ディレクトリ")
    parser.add_argument("--dry-run", action="store_true", help="API を呼ばずにケース定義の検証だけ行う")
    return parser.parse_args(argv)


def _select(cases: list[dict[str, Any]], args: argparse.Namespace) -> list[dict[str, Any]]:
    selected = cases
    if args.symbol:
        selected = [c for c in selected if c["symbol_id"] in set(args.symbol)]
    if args.source:
        selected = [c for c in selected if c["source"] == args.source]
    if args.limit > 0:
        selected = selected[: args.limit]
    return selected


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    if args.model:
        # main._gemini_models() は呼び出し毎に環境変数を読むので、import 前後どちらでもよい。
        os.environ["GEMINI_MODELS_FREE"] = args.model
        os.environ["GEMINI_MODELS_PAID"] = args.model

    import main as app
    from judgment_eval import cases as case_loader
    from judgment_eval.summary import render_markdown, summarize

    try:
        cases = case_loader.load_cases(app.SYMBOLS, args.cases_dir)
    except case_loader.CaseError as exc:
        print(f"❌ ケース定義が不正です: {exc}", file=sys.stderr)
        return 1

    selected = _select(cases, args)
    if not selected:
        print("❌ 条件に合うケースがありません", file=sys.stderr)
        return 1

    total_calls = len(selected) * max(1, args.repeat)
    with_truth = sum(1 for c in selected if c["expected_required"] is not None)
    print(f"ケース {len(selected)} 件（特徴単位の真値あり {with_truth} 件） × {args.repeat} 回 = {total_calls} 判定")
    print(f"推定コスト: 約 ${total_calls * COST_PER_JUDGMENT_USD:.2f}")

    if args.dry_run:
        print("✅ ケース定義は妥当（--dry-run のため API は呼んでいない）")
        return 0

    if not app._gemini_api_keys():
        print("❌ GEMINI_API_KEY が未設定です。`source scripts/load-env.sh` を先に実行してください。", file=sys.stderr)
        return 1

    jobs = [(case, run) for case in selected for run in range(args.repeat)]
    records: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        futures = {pool.submit(_judge_once, app, case, run, args.blind): (case, run) for case, run in jobs}
        for done, future in enumerate(concurrent.futures.as_completed(futures), start=1):
            records.append(future.result())
            print(f"\r  {done}/{len(jobs)}", end="", file=sys.stderr, flush=True)
    print("", file=sys.stderr)

    summary = summarize(selected, records, app.SYMBOLS)
    meta = {
        "model": args.model or "(main.py のフォールバック順)",
        "blind": args.blind,
        "repeat": args.repeat,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }

    out_dir = args.out / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.json").write_text(
        json.dumps({"meta": meta, "summary": summary, "records": records}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    report = render_markdown(summary, meta)
    (out_dir / "report.md").write_text(report, encoding="utf-8")

    print()
    print(report)
    print(f"\n📄 {out_dir}/report.md")
    return 0


def _judge_once(app: Any, case: dict[str, Any], run: int, blind: bool) -> dict[str, Any]:
    """1 ケースを 1 回判定する。本番と同じ前処理・プロンプト・採点を通す。"""
    symbol = app.SYMBOLS[case["symbol_id"]]
    record: dict[str, Any] = {
        "case_id": case["case_id"],
        "symbol_id": case["symbol_id"],
        "expect": case["expect"],
        "source": case["source"],
        "run": run,
        "passed": None,
        "error": None,
    }
    try:
        image = app._validate_and_prepare_png(case["image_path"].read_bytes())
        result = app._generate_vision_result(
            image, app.build_vision_prompt(symbol, blind=blind), case["symbol_id"]
        )
        scored = app.score_observation(symbol, result)
        record.update({
            "passed": scored["passed"],
            "score": scored["score"],
            "mistakes": scored["mistakes"],
            "required": list(result.required),
            "forbidden": list(result.forbidden),
            "observation": result.observation,
        })
    except Exception as exc:  # API 失敗は測定不能として記録し、他ケースは続行する
        record["error"] = f"{type(exc).__name__}: {exc}"
    return record


if __name__ == "__main__":
    raise SystemExit(main())
