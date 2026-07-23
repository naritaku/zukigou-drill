# AGENTS.md

## PRレビュー向けブラウザ確認ルール

Codex が Web UI または Web アプリの挙動に知覚可能な変更を加えた場合は、PR レビュー効率化のため、可能な限りブラウザで対象ページを開いて確認してください。

1. 修正前の状態を確認できる場合は、変更前コミットまたは作業前ブランチで対象ページをブラウザ表示し、スクリーンショットを取得する。
2. 修正後の状態で同じ対象ページをブラウザ表示し、スクリーンショットを取得する。
3. 修正前後のスクリーンショットパス、確認した URL、見た目・動作の差分をタスク実行結果に記載する。
4. 環境制約によりブラウザ確認やスクリーンショット取得ができない場合は、実行したコマンド、失敗理由、代替確認内容を明記する。

このリポジトリでは `scripts/visual_review.py` を使うと、ローカルサーバー起動から Playwright によるスクリーンショット取得までを自動化できます。

```bash
python -m pip install -r requirements.txt -r dev-requirements.txt
python -m playwright install chromium
python scripts/visual_review.py --path / --label after
```

変更前後を比較する場合は、作業前に `--label before`、修正後に `--label after` を指定して同じ `--path` を撮影してください。生成物は `artifacts/visual-review/` に保存され、Git には含めません。

## 通常の検証

Python のロジックを変更した場合は、少なくとも次を実行してください。

```bash
python -m unittest
```
