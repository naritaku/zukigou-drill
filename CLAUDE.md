# CLAUDE.md

配線用図記号の描画ドリル。手描き PNG を Gemini に観察させ、合否はコード側で決める
（FastAPI + Cloud Run、1 サービス構成）。日本語で応答・PR 作成すること。

## 環境（ここを外すと必ずハマる）

システムの `python3` は **3.9** で、コードが使う `datetime.UTC` が無いためテストが落ちる。
本番は `python:3.12-slim`。ローカルは 3.12 以降の venv を使う:

```bash
python3.13 -m venv .venv && .venv/bin/pip install -r requirements.txt -r dev-requirements.txt
.venv/bin/python -m unittest        # 全テスト
.venv/bin/python -m ruff check .    # lint
```

`.venv/` は gitignore 済み。pytest は `.env.local` の読み取りで collection が失敗するので
**`unittest` を使う**。API キーは `source scripts/load-env.sh`（`.env.local` を読む）。

## コミット前の検証

- **Python を変更**: TDD でテストを書き、`python -m unittest` と `ruff check .` を通す
- **symbols.json を変更**: JSON として妥当で、図記号と判断基準が矛盾しないこと。
  図記号や説明を変えたら `standards.html` の該当箇所をスクショして確認する。
  `required_features` / `forbidden_features` / `ref_svg` を変えたら、さらに判定精度を測る:

  ```bash
  python3 scripts/render_eval_cases.py --symbol <id>   # お手本ケースを再生成
  python3 scripts/run_judgment_eval.py --symbol <id>   # 精度が落ちていないこと
  ```
- **html/css を変更**: `python scripts/visual_review.py --path /drill --label after` で
  スクショし、スマホ縦画面と PC 横画面のどちらでも崩れないこと

## リポジトリ地図

| 場所 | 中身 |
| --- | --- |
| `main.py` | 全サーバーロジック。設定 → レート制限 → 日次クォータ → Gemini 呼び出し → 画像検証 → GCS/Firestore → エンドポイントの順に並ぶ |
| `test_main.py` | サーバーのテスト。Gemini はモックなので**判定の当たり外れは見ていない** |
| `judgment_eval/` + `scripts/run_judgment_eval.py` | 判定精度の測定。当たり外れはこちらの担当（`docs/EVALUATION.md`） |
| `symbols.json` | 記号の定義とルーブリック（必須特徴・禁止特徴）。採点エンジンに記号固有コードは無い |
| `drill.html` / `landing.html` / `standards.html` | 画面。ビルド無しで `main.py` が配信する |
| `docs/ARCHITECTURE.md` | 構成・API・エラー契約 |
| `docs/DEVELOPMENT.md` | 環境変数・デプロイ・運用の注意 |
| `.claude/REVIEW-PERSPECTIVES.md` | レビュー観点（`/local-review`・PR レビューで使う） |

## ワークフロー

1. タスクを立てる → 2. 実装 → 3. 検証（上記）→ 4. `gh pr create` で PR → 5. 人間が確認してマージ

- **`git push` は私が実行すると拒否される**。`! git push origin <branch>` の形で人間に頼み、
  `git ls-remote --heads origin <branch>` で上がったことを確認してから次へ進む
- PR は `gh pr create` で作る。`scripts/create-pr-*.sh` は廃止済み
- マージはマージコミット方式（squash しない）
