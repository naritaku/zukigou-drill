"""評価ケースの読み込みと検証。

1 ケース = 1 ディレクトリ:

    judgment_eval/cases/<case_id>/case.json
    judgment_eval/cases/<case_id>/image.png

case.json のスキーマ:

    symbol_id          出題記号 ID (symbols.json の id)
    expect             "pass" | "fail"  — この解答が本来受けるべき判定
    source             "rendered-reference" | "handwritten"
    note               自由記述（何を意図した解答か）
    expected_required  省略可。各必須特徴が画像に存在するか(真値)
    expected_forbidden 省略可。各禁止特徴が画像に存在するか(真値)

`expected_*` は特徴単位の誤りを集計するための真値。expect="pass" のケースでは
「必須は全て true・禁止は全て false」しかありえないので省略時に自動補完する。
expect="fail" では、どの特徴を欠いた解答なのかを人が書く必要がある（省略した
場合は合否だけを採点し、特徴単位の集計からは除外する）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CASES_DIR = Path(__file__).resolve().parent / "cases"

VALID_EXPECT = ("pass", "fail")
VALID_SOURCE = ("rendered-reference", "handwritten")


class CaseError(ValueError):
    """ケース定義が壊れている。"""


def load_cases(
    symbols: dict[str, dict[str, Any]],
    cases_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """ケースを ID 順に読み込む。1 件でも壊れていれば例外にする。

    評価は「測定器」なので、壊れたケースを黙って読み飛ばすと精度の数字が
    静かに歪む。ここでは早く失敗させる。
    """
    directory = cases_dir if cases_dir is not None else CASES_DIR
    if not directory.is_dir():
        raise CaseError(f"cases directory not found: {directory}")

    cases: list[dict[str, Any]] = []
    for case_dir in sorted(p for p in directory.iterdir() if p.is_dir()):
        cases.append(_load_case(case_dir, symbols))
    if not cases:
        raise CaseError(f"no cases found in {directory}")
    return cases


def _load_case(case_dir: Path, symbols: dict[str, dict[str, Any]]) -> dict[str, Any]:
    case_id = case_dir.name
    meta_path = case_dir / "case.json"
    image_path = case_dir / "image.png"

    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CaseError(f"{case_id}: case.json could not be read: {exc}") from exc
    if not isinstance(meta, dict):
        raise CaseError(f"{case_id}: case.json must be an object")
    if not image_path.is_file():
        raise CaseError(f"{case_id}: image.png is missing")

    symbol_id = meta.get("symbol_id")
    if symbol_id not in symbols:
        raise CaseError(f"{case_id}: unknown symbol_id {symbol_id!r}")
    symbol = symbols[symbol_id]

    expect = meta.get("expect")
    if expect not in VALID_EXPECT:
        raise CaseError(f"{case_id}: expect must be one of {VALID_EXPECT}, got {expect!r}")
    source = meta.get("source")
    if source not in VALID_SOURCE:
        raise CaseError(f"{case_id}: source must be one of {VALID_SOURCE}, got {source!r}")

    n_required = len(symbol["required_features"])
    n_forbidden = len(symbol.get("forbidden_features", []))

    expected_required = meta.get("expected_required")
    expected_forbidden = meta.get("expected_forbidden")
    if expected_required is None and expected_forbidden is None and expect == "pass":
        # 合格すべき解答の真値は一意に決まる。
        expected_required = [True] * n_required
        expected_forbidden = [False] * n_forbidden

    if expected_required is not None or expected_forbidden is not None:
        expected_required = _check_bools(case_id, "expected_required", expected_required, n_required)
        expected_forbidden = _check_bools(case_id, "expected_forbidden", expected_forbidden, n_forbidden)
        implied = "pass" if all(expected_required) and not any(expected_forbidden) else "fail"
        if implied != expect:
            raise CaseError(
                f"{case_id}: expected_required/expected_forbidden imply {implied!r} but expect is {expect!r}"
            )

    return {
        "case_id": case_id,
        "symbol_id": symbol_id,
        "expect": expect,
        "source": source,
        "note": str(meta.get("note", "")),
        "expected_required": expected_required,
        "expected_forbidden": expected_forbidden,
        "image_path": image_path,
    }


def _check_bools(case_id: str, field: str, values: Any, expected_len: int) -> list[bool]:
    if values is None:
        values = []
    if not isinstance(values, list) or not all(isinstance(v, bool) for v in values):
        raise CaseError(f"{case_id}: {field} must be a list of booleans")
    if len(values) != expected_len:
        raise CaseError(f"{case_id}: {field} must have {expected_len} entries, got {len(values)}")
    return values
