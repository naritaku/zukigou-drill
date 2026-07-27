# 判定精度の評価

このアプリの価値は「判定が当たること」にある。にもかかわらず、これまで精度を測る
仕組みが無く、README の「判定の正確さ」を支える数字が存在しなかった。ここで導入する
ハーネスは、その数字を継続的に出すためのもの。

`python -m unittest` は Gemini をモックするので**判定の当たり外れは一切見ていない**。
両者は目的が違う（前者は壊れていないこと、後者は当たっていること）。

## 何を測るか

| 指標 | 意味 | なぜ見るか |
| --- | --- | --- |
| **偽 NG 率** | 合格すべき解答が落ちた割合 | 必須+禁止の AND 判定は項目数だけ誤りが積み上がる。6 項目 × 各 95% なら理論上 27% が落ちる。学習意欲を最も削ぐ失敗 |
| **偽 OK 率** | 不合格にすべき解答が通った割合 | 「課題は〇〇です」と正解を先に渡している以上、確証バイアスが乗りうる |
| **ブレ** | temperature=0 でも判定が揺れた割合 | 決定的なのは採点だけで、観察は決定的ではない |
| **特徴単位の誤り率** | どの特徴文が観察されにくいか | 直す対象を `symbols.json` の行単位で名指しする |

## 使い方

```bash
# 0. ケース定義だけ検証（API を呼ばない・無料）
python3 scripts/run_judgment_eval.py --dry-run

# 1. API キーを読み込む
source scripts/load-env.sh

# 2. 本番と同じ条件で 1 周（54 判定 ≒ $0.03）
python3 scripts/run_judgment_eval.py

# 3. ブレも測る（3 回ずつ ≒ $0.08）
python3 scripts/run_judgment_eval.py --repeat 3

# 4. 記号名を伏せた対照条件と比べる
python3 scripts/run_judgment_eval.py --blind

# 特定の記号だけ／モデルを固定して
python3 scripts/run_judgment_eval.py --symbol earth --symbol bell
python3 scripts/run_judgment_eval.py --model gemini-3.5-flash
```

結果は `artifacts/eval/<UTC タイムスタンプ>/` に `report.md`（人が読む）と
`results.json`（差分を取る）が出る。`artifacts/` は gitignore 済み。

判定は本番と同じ経路を通る（`main._validate_and_prepare_png` →
`main.build_vision_prompt` → `main._generate_vision_result` →
`main.score_observation`）。評価専用の実装は持たない。持つと、測っているものが
本番とずれる。

## ケースの中身

1 ケース = 1 ディレクトリ。

```
judgment_eval/cases/<case_id>/
├── case.json
└── image.png
```

```json
{
  "symbol_id": "earth",
  "expect": "pass",
  "source": "handwritten",
  "note": "水平線を2本しか描いていない",
  "expected_required": [true, false, true],
  "expected_forbidden": [true, false, false, false]
}
```

- `expect` — この解答が本来受けるべき判定。
- `source` — `rendered-reference`（お手本 SVG の描画）か `handwritten`（実機で指描き）。
  レポートはこの 2 つを分けて集計する。
- `expected_required` / `expected_forbidden` — 特徴単位の真値。**省略可**だが、
  書くと「どの特徴文で間違えたか」の集計に入る。`expect: "pass"` のときは
  「必須は全 true・禁止は全 false」と一意に決まるので自動補完される。
- 真値と `expect` が矛盾していたら読み込み時に落ちる。測定器が黙って狂うのを防ぐため。

## ベースライン

測定結果のスナップショットは `judgment_eval/baselines/` に置く。判定に影響する変更
（`symbols.json` の文言、プロンプト、モデル）を入れたら直近のベースラインと比較する。

- [2026-07-28 — お手本ケース 54 件](../judgment_eval/baselines/2026-07-28-rendered-reference.md)
  — 偽 NG 率 **0.0%**、特徴単位の誤り **0/326**。ただし全て理想解答であり手描き精度ではない。

## 同梱されているケースの限界（重要）

現在入っている 54 件はすべて `rendered-reference`、つまり **`symbols.json` の
`ref_svg` をそのまま描画した「教科書どおりの理想解答」** であって手描きではない。

```bash
python3 scripts/render_eval_cases.py   # 再生成（Playwright を使う）
```

したがってこれで測れるのは **下限（floor test）** だけ：

> お手本すら通らない記号は、手描きでは絶対に通らない。

逆に、ここで偽 NG 率 0% でも「手描きで通る」ことの保証には**ならない**。
実運用の精度を知るには手描きケースが要る。ここの偽 NG 率は、手描き精度の
楽観的な上界だと理解すること。

## 手描きケースの増やし方

1. デプロイ済みアプリ（またはローカル）でその記号を描く。
2. 判定前に canvas を右クリック（スマホなら長押し）で PNG を保存。
   `ALL_JUDGMENTS_BUCKET` を有効にしている場合は GCS に貯まっている実解答も使える。
3. `judgment_eval/cases/hand-<symbol_id>-<連番>/` に `image.png` と `case.json` を置く。
4. `python3 scripts/run_judgment_eval.py --dry-run` で定義を検証。
5. `python3 -m unittest` を通す（同梱ケースが読めることを担保するテストがある）。

**1 記号あたり最低 2 件**（正しい解答 1 件・典型的なミス 1 件）が目標。
54 記号 × 2 = 108 件揃えば、以下が実測で判断できるようになる：

- AND 判定を緩めるべきか（偽 NG 率が実用に耐えない水準か）
- 禁止特徴を減らすべきか（必須の裏返しになっている項目が誤りを増やしていないか）
- 記号名を伏せるべきか（`--blind` との差分）

`common_mistakes` に既に典型ミスが文章で書いてあるので、ミス側のケースは
それを描き起こせばよい。
