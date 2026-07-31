"""評価結果の集計とレポート生成（純粋関数のみ・ネットワークなし）。

測りたいのは次の 4 つ:

1. 偽 NG 率  — 正しく描いた解答が落ちる割合。必須+禁止の AND 判定は項目数だけ
               誤りが積み上がるため、ここが実用上の主要な失敗モードになりうる。
2. 偽 OK 率  — 誤った解答が通る割合。
3. ブレ      — temperature=0 でも同一画像の判定が揺れる割合。
4. 特徴単位の誤り — どの特徴文が観察されにくいか。symbols.json の文言を
               直す対象を名指しするための数字。
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any


def _rate(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else numerator / denominator


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def _verdict_block(records: list[dict[str, Any]]) -> dict[str, Any]:
    scored = [r for r in records if r.get("passed") is not None]
    expect_pass = [r for r in scored if r["expect"] == "pass"]
    expect_fail = [r for r in scored if r["expect"] == "fail"]
    judged_pass = sum(1 for r in expect_pass if r["passed"])
    judged_fail = sum(1 for r in expect_fail if not r["passed"])
    return {
        "scored": len(scored),
        "expect_pass": {
            "n": len(expect_pass),
            "correct": judged_pass,
            "false_ng_rate": _rate(len(expect_pass) - judged_pass, len(expect_pass)),
        },
        "expect_fail": {
            "n": len(expect_fail),
            "correct": judged_fail,
            "false_ok_rate": _rate(len(expect_fail) - judged_fail, len(expect_fail)),
        },
        "accuracy": _rate(judged_pass + judged_fail, len(scored)),
    }


def summarize(
    cases: list[dict[str, Any]],
    records: list[dict[str, Any]],
    symbols: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """1 回の評価実行をまとめる。

    `records` は (ケース × 繰り返し回数) 分の判定結果。API 呼び出しに失敗した
    レコードは `passed` が None で、合否の母数から除外し件数だけ報告する。
    """
    api_errors = [r for r in records if r.get("passed") is None]

    by_source: dict[str, dict[str, Any]] = {}
    for source in sorted({r["source"] for r in records}):
        by_source[source] = _verdict_block([r for r in records if r["source"] == source])

    # ブレ: 同一ケースの判定が繰り返し間で割れたもの
    verdicts_by_case: dict[str, list[bool]] = defaultdict(list)
    for record in records:
        if record.get("passed") is not None:
            verdicts_by_case[record["case_id"]].append(bool(record["passed"]))
    flaky = [
        {"case_id": case_id, "pass_runs": sum(v), "runs": len(v)}
        for case_id, v in sorted(verdicts_by_case.items())
        if len(v) > 1 and 0 < sum(v) < len(v)
    ]
    repeated = [v for v in verdicts_by_case.values() if len(v) > 1]

    # 特徴単位の誤り: 真値のあるケースだけを母数にする
    truth_by_case = {
        c["case_id"]: (c["expected_required"], c["expected_forbidden"])
        for c in cases
        if c["expected_required"] is not None
    }
    tally: dict[tuple[str, str, int], list[int]] = defaultdict(lambda: [0, 0])  # [観察回数, 誤り回数]
    for record in records:
        if record.get("passed") is None or record["case_id"] not in truth_by_case:
            continue
        expected_required, expected_forbidden = truth_by_case[record["case_id"]]
        for kind, expected, observed in (
            ("required", expected_required, record.get("required") or []),
            ("forbidden", expected_forbidden, record.get("forbidden") or []),
        ):
            for index, truth in enumerate(expected):
                seen = observed[index] if index < len(observed) else False
                counter = tally[(record["symbol_id"], kind, index)]
                counter[0] += 1
                if bool(seen) != truth:
                    counter[1] += 1

    feature_errors = []
    for (symbol_id, kind, index), (total, wrong) in tally.items():
        if not wrong:
            continue
        features = symbols[symbol_id]["required_features"] if kind == "required" else symbols[symbol_id].get("forbidden_features", [])
        feature_errors.append({
            "symbol_id": symbol_id,
            "kind": kind,
            "index": index,
            "feature": features[index] if index < len(features) else "",
            "n": total,
            "wrong": wrong,
            "error_rate": wrong / total,
        })
    feature_errors.sort(key=lambda item: (-item["error_rate"], -item["wrong"], item["symbol_id"]))

    # 落ちやすいケース（合格すべきなのに落ちたもの）
    worst_cases: list[dict[str, Any]] = []
    for case in cases:
        runs = verdicts_by_case.get(case["case_id"], [])
        if case["expect"] != "pass" or not runs or all(runs):
            continue
        worst_cases.append({
            "case_id": case["case_id"],
            "symbol_id": case["symbol_id"],
            "pass_runs": sum(runs),
            "runs": len(runs),
        })
    worst_cases.sort(key=lambda item: (item["pass_runs"] / item["runs"], item["case_id"]))

    return {
        "cases": len(cases),
        "records": len(records),
        "api_errors": len(api_errors),
        "verdict": _verdict_block(records),
        "by_source": by_source,
        "flaky_cases": flaky,
        "flaky_rate": _rate(len(flaky), len(repeated)),
        "feature_errors": feature_errors,
        "worst_cases": worst_cases,
    }


def render_markdown(summary: dict[str, Any], meta: dict[str, Any]) -> str:
    """人が読むレポート。PR や課題管理にそのまま貼れる Markdown。"""
    verdict = summary["verdict"]
    lines = [
        "# 判定精度レポート",
        "",
        "| 項目 | 値 |",
        "| --- | --- |",
        f"| モデル | `{meta.get('model', '-')}` |",
        f"| プロンプト条件 | {'blind(記号名を伏せる)' if meta.get('blind') else 'production(記号名を伝える)'} |",
        f"| ケース数 | {summary['cases']} |",
        f"| 繰り返し | {meta.get('repeat', 1)} 回 |",
        f"| 判定回数 | {summary['records']}（API 失敗 {summary['api_errors']}） |",
        "",
        "## 合否",
        "",
        "| 指標 | 母数 | 値 |",
        "| --- | ---: | ---: |",
        f"| 偽 NG 率（合格すべき解答が落ちた） | {verdict['expect_pass']['n']} | **{_pct(verdict['expect_pass']['false_ng_rate'])}** |",
        f"| 偽 OK 率（不合格にすべき解答が通った） | {verdict['expect_fail']['n']} | **{_pct(verdict['expect_fail']['false_ok_rate'])}** |",
        f"| 正解率 | {verdict['scored']} | {_pct(verdict['accuracy'])} |",
        "",
    ]

    if len(summary["by_source"]) > 1:
        lines += ["### 解答の出所別", "", "| source | 偽 NG 率 | 偽 OK 率 |", "| --- | ---: | ---: |"]
        for source, block in summary["by_source"].items():
            lines.append(
                f"| {source} | {_pct(block['expect_pass']['false_ng_rate'])} | {_pct(block['expect_fail']['false_ok_rate'])} |"
            )
        lines.append("")

    if meta.get("repeat", 1) > 1:
        lines += [
            "## ブレ（temperature=0）",
            "",
            f"判定が繰り返し間で割れたケース: **{len(summary['flaky_cases'])}** 件（{_pct(summary['flaky_rate'])}）",
            "",
        ]
        for item in summary["flaky_cases"][:20]:
            lines.append(f"- `{item['case_id']}` — {item['pass_runs']}/{item['runs']} 回で合格")
        if summary["flaky_cases"]:
            lines.append("")

    if summary["worst_cases"]:
        lines += ["## 落ちたケース（合格すべき解答）", "", "| ケース | 記号 | 合格回数 |", "| --- | --- | ---: |"]
        for item in summary["worst_cases"][:30]:
            lines.append(f"| `{item['case_id']}` | {item['symbol_id']} | {item['pass_runs']}/{item['runs']} |")
        lines.append("")

    if summary["feature_errors"]:
        lines += [
            "## 誤りの多い特徴文",
            "",
            "symbols.json の文言を直す優先順位。誤り率が高い項目ほど、記号の正しさではなく"
            "「文の伝わりにくさ」で落としている可能性が高い。",
            "",
            "| 記号 | 種別 | # | 誤り率 | 特徴文 |",
            "| --- | --- | ---: | ---: | --- |",
        ]
        for item in summary["feature_errors"][:30]:
            lines.append(
                f"| {item['symbol_id']} | {item['kind']} | {item['index']} | "
                f"{_pct(item['error_rate'])} ({item['wrong']}/{item['n']}) | {item['feature']} |"
            )
        lines.append("")

    return "\n".join(lines)
