"""評価ハーネスの単体テスト。API は呼ばない。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from judgment_eval.cases import CaseError, load_cases
from judgment_eval.summary import render_markdown, summarize

SYMBOLS: dict[str, dict[str, Any]] = {
    "earth": {
        "id": "earth",
        "name": "接地極",
        "required_features": ["縦線が1本ある", "水平線が3本ある"],
        "forbidden_features": ["水平線が3本以外である"],
    },
    "bell": {
        "id": "bell",
        "name": "ベル",
        "required_features": ["四角形の枠がある"],
        "forbidden_features": [],
    },
}


def _write_case(root: Path, case_id: str, meta: dict[str, Any], *, image: bool = True) -> None:
    case_dir = root / case_id
    case_dir.mkdir(parents=True)
    (case_dir / "case.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    if image:
        (case_dir / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n")


class LoadCasesTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_derives_ground_truth_for_pass_cases(self) -> None:
        _write_case(self.root, "ref-earth", {"symbol_id": "earth", "expect": "pass", "source": "rendered-reference"})
        (case,) = load_cases(SYMBOLS, self.root)
        self.assertEqual(case["case_id"], "ref-earth")
        self.assertEqual(case["expected_required"], [True, True])
        self.assertEqual(case["expected_forbidden"], [False])
        self.assertTrue(case["image_path"].is_file())

    def test_fail_case_without_ground_truth_is_verdict_only(self) -> None:
        _write_case(self.root, "hand-earth-01", {"symbol_id": "earth", "expect": "fail", "source": "handwritten"})
        (case,) = load_cases(SYMBOLS, self.root)
        self.assertIsNone(case["expected_required"])

    def test_fail_case_with_ground_truth_is_kept(self) -> None:
        _write_case(self.root, "hand-earth-02", {
            "symbol_id": "earth", "expect": "fail", "source": "handwritten",
            "expected_required": [True, False], "expected_forbidden": [True],
        })
        (case,) = load_cases(SYMBOLS, self.root)
        self.assertEqual(case["expected_required"], [True, False])

    def test_rejects_ground_truth_that_contradicts_expect(self) -> None:
        _write_case(self.root, "bad", {
            "symbol_id": "earth", "expect": "fail", "source": "handwritten",
            "expected_required": [True, True], "expected_forbidden": [False],
        })
        with self.assertRaisesRegex(CaseError, "imply"):
            load_cases(SYMBOLS, self.root)

    def test_rejects_wrong_length_ground_truth(self) -> None:
        _write_case(self.root, "bad", {
            "symbol_id": "earth", "expect": "fail", "source": "handwritten",
            "expected_required": [False], "expected_forbidden": [False],
        })
        with self.assertRaisesRegex(CaseError, "expected_required"):
            load_cases(SYMBOLS, self.root)

    def test_rejects_unknown_symbol(self) -> None:
        _write_case(self.root, "bad", {"symbol_id": "nope", "expect": "pass", "source": "handwritten"})
        with self.assertRaisesRegex(CaseError, "unknown symbol_id"):
            load_cases(SYMBOLS, self.root)

    def test_rejects_missing_image(self) -> None:
        _write_case(self.root, "bad", {"symbol_id": "earth", "expect": "pass", "source": "handwritten"}, image=False)
        with self.assertRaisesRegex(CaseError, "image.png"):
            load_cases(SYMBOLS, self.root)

    def test_rejects_invalid_expect_and_source(self) -> None:
        _write_case(self.root, "bad", {"symbol_id": "earth", "expect": "maybe", "source": "handwritten"})
        with self.assertRaisesRegex(CaseError, "expect must be"):
            load_cases(SYMBOLS, self.root)

    def test_rejects_empty_directory(self) -> None:
        with self.assertRaisesRegex(CaseError, "no cases"):
            load_cases(SYMBOLS, self.root)

    def test_rejects_missing_directory(self) -> None:
        with self.assertRaisesRegex(CaseError, "not found"):
            load_cases(SYMBOLS, self.root / "absent")


def _case(case_id: str, symbol_id: str, expect: str, source: str = "rendered-reference",
          required: list[bool] | None = None, forbidden: list[bool] | None = None) -> dict[str, Any]:
    return {
        "case_id": case_id, "symbol_id": symbol_id, "expect": expect, "source": source,
        "note": "", "expected_required": required, "expected_forbidden": forbidden,
        "image_path": Path("image.png"),
    }


def _record(case: dict[str, Any], run: int, passed: bool | None,
            required: list[bool] | None = None, forbidden: list[bool] | None = None) -> dict[str, Any]:
    return {
        "case_id": case["case_id"], "symbol_id": case["symbol_id"], "expect": case["expect"],
        "source": case["source"], "run": run, "passed": passed,
        "required": required or [], "forbidden": forbidden or [], "error": None if passed is not None else "boom",
    }


class SummarizeTest(unittest.TestCase):
    def test_false_ng_and_false_ok_rates(self) -> None:
        good = _case("g", "earth", "pass", required=[True, True], forbidden=[False])
        bad = _case("b", "earth", "fail", required=[False, True], forbidden=[True])
        records = [
            _record(good, 0, True, [True, True], [False]),
            _record(good, 1, False, [True, False], [False]),  # 偽 NG
            _record(bad, 0, False, [False, True], [True]),
            _record(bad, 1, True, [True, True], [False]),      # 偽 OK
        ]
        summary = summarize([good, bad], records, SYMBOLS)
        self.assertAlmostEqual(summary["verdict"]["expect_pass"]["false_ng_rate"], 0.5)
        self.assertAlmostEqual(summary["verdict"]["expect_fail"]["false_ok_rate"], 0.5)
        self.assertAlmostEqual(summary["verdict"]["accuracy"], 0.5)

    def test_api_errors_are_excluded_from_rates(self) -> None:
        good = _case("g", "earth", "pass", required=[True, True], forbidden=[False])
        records = [_record(good, 0, True, [True, True], [False]), _record(good, 1, None)]
        summary = summarize([good], records, SYMBOLS)
        self.assertEqual(summary["api_errors"], 1)
        self.assertEqual(summary["verdict"]["expect_pass"]["n"], 1)
        self.assertEqual(summary["verdict"]["expect_pass"]["false_ng_rate"], 0.0)

    def test_flaky_case_detection(self) -> None:
        good = _case("g", "earth", "pass", required=[True, True], forbidden=[False])
        stable = _case("s", "bell", "pass", required=[True], forbidden=[])
        records = [
            _record(good, 0, True, [True, True], [False]),
            _record(good, 1, False, [True, False], [False]),
            _record(stable, 0, True, [True], []),
            _record(stable, 1, True, [True], []),
        ]
        summary = summarize([good, stable], records, SYMBOLS)
        self.assertEqual([f["case_id"] for f in summary["flaky_cases"]], ["g"])
        self.assertAlmostEqual(summary["flaky_rate"], 0.5)

    def test_feature_errors_are_ranked_and_named(self) -> None:
        good = _case("g", "earth", "pass", required=[True, True], forbidden=[False])
        records = [
            _record(good, 0, False, [True, False], [False]),
            _record(good, 1, False, [True, False], [True]),
        ]
        summary = summarize([good], records, SYMBOLS)
        top = summary["feature_errors"][0]
        self.assertEqual((top["symbol_id"], top["kind"], top["index"]), ("earth", "required", 1))
        self.assertEqual(top["feature"], "水平線が3本ある")
        self.assertAlmostEqual(top["error_rate"], 1.0)
        # 誤りゼロの項目は載せない
        self.assertNotIn(("required", 0), [(f["kind"], f["index"]) for f in summary["feature_errors"]])

    def test_short_observation_array_counts_as_not_observed(self) -> None:
        """Gemini が短い配列を返した場合、欠けた要素は false 扱いで誤りに数える。"""
        good = _case("g", "earth", "pass", required=[True, True], forbidden=[False])
        summary = summarize([good], [_record(good, 0, False, [True], [False])], SYMBOLS)
        self.assertEqual(summary["feature_errors"][0]["index"], 1)

    def test_cases_without_ground_truth_are_excluded_from_feature_errors(self) -> None:
        blind = _case("b", "earth", "fail", source="handwritten")
        summary = summarize([blind], [_record(blind, 0, False, [False, False], [True])], SYMBOLS)
        self.assertEqual(summary["feature_errors"], [])
        self.assertEqual(summary["verdict"]["expect_fail"]["correct"], 1)

    def test_by_source_split(self) -> None:
        rendered = _case("r", "earth", "pass", required=[True, True], forbidden=[False])
        hand = _case("h", "bell", "pass", "handwritten", required=[True], forbidden=[])
        records = [_record(rendered, 0, True, [True, True], [False]), _record(hand, 0, False, [False], [])]
        summary = summarize([rendered, hand], records, SYMBOLS)
        self.assertEqual(summary["by_source"]["rendered-reference"]["expect_pass"]["false_ng_rate"], 0.0)
        self.assertEqual(summary["by_source"]["handwritten"]["expect_pass"]["false_ng_rate"], 1.0)

    def test_worst_cases_lists_only_failing_pass_cases(self) -> None:
        good = _case("g", "earth", "pass", required=[True, True], forbidden=[False])
        stable = _case("s", "bell", "pass", required=[True], forbidden=[])
        records = [
            _record(good, 0, False, [True, False], [False]),
            _record(stable, 0, True, [True], []),
        ]
        summary = summarize([good, stable], records, SYMBOLS)
        self.assertEqual([c["case_id"] for c in summary["worst_cases"]], ["g"])


class RenderMarkdownTest(unittest.TestCase):
    def test_report_contains_headline_numbers(self) -> None:
        good = _case("g", "earth", "pass", required=[True, True], forbidden=[False])
        records = [
            _record(good, 0, True, [True, True], [False]),
            _record(good, 1, False, [True, False], [False]),
        ]
        summary = summarize([good], records, SYMBOLS)
        report = render_markdown(summary, {"model": "m", "blind": False, "repeat": 2})
        self.assertIn("偽 NG 率", report)
        self.assertIn("50.0%", report)
        self.assertIn("水平線が3本ある", report)
        self.assertIn("ブレ", report)
        self.assertIn("production", report)

    def test_blind_condition_is_labelled(self) -> None:
        good = _case("g", "bell", "pass", required=[True], forbidden=[])
        summary = summarize([good], [_record(good, 0, True, [True], [])], SYMBOLS)
        report = render_markdown(summary, {"model": "m", "blind": True, "repeat": 1})
        self.assertIn("blind", report)
        self.assertNotIn("## ブレ", report)


class RepositoryCasesTest(unittest.TestCase):
    """リポジトリに入っている実ケースが常に読める状態であることを担保する。"""

    def test_bundled_cases_load(self) -> None:
        import main

        cases = load_cases(main.SYMBOLS)
        self.assertGreaterEqual(len(cases), 1)
        verified = {s["id"] for s in main.SYMBOLS.values() if s.get("verified") and s.get("ref_svg")}
        rendered = {c["symbol_id"] for c in cases if c["source"] == "rendered-reference"}
        self.assertEqual(verified - rendered, set(), "お手本ケースが無い出題記号がある")


if __name__ == "__main__":
    unittest.main()
