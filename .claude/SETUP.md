# Claude Code 自動検証セットアップ

このプロジェクトでは、変更内容を自動的に検証・テスト・改善するパイプラインを構築できます。

## 🚀 クイックスタート

### パターン A: 単発の検証（推奨）

```bash
bash scripts/validate.sh
```

このコマンドで：
1. Python テスト実行 (8/8 合格確認)
2. コード品質確認
3. 環境変数テンプレート確認

### パターン B: Claude Code での自動ループ検証

Claude Code コマンドプロンプトで：

```
/loop validate-and-improve
```

このループ内では以下を自動実行：

1. **テスト実行**
   ```bash
   python3 -m unittest
   ```

2. **失敗時の自動修正**
   - テスト失敗原因を特定
   - 修正を提案・実装
   - 再テスト

3. **ローカルレビュー**
   ```bash
   /local-review main HEAD
   ```

4. **レビュー指摘への対応**
   - MUST 指摘：必ず修正
   - SHOULD 指摘：改善推奨
   - MAY 指摘：軽微なため任意

5. **最終確認**
   - 全テスト合格
   - 全指摘対応完了
   - コミット準備完了

## 📋 ワークフロー

```
変更 → テスト実行
   ↓
   テスト失敗？ → YES → 修正 → テスト再実行 ↻
   ↓ NO
   レビュー実行
   ↓
   MUST 指摘？ → YES → 修正 → レビュー再実行 ↻
   ↓ NO
   コミット準備完了
   ↓
   git add / commit / push
```

## 🔧 手動ステップ

以下の手順で、段階的に検証できます：

### 1. テスト実行（必須）
```bash
python3 -m unittest -v
```

**AGENTS.md 要件：** Python ロジック変更時は必ず実行

### 2. ローカルレビュー
```bash
/local-review main HEAD
```

**確認項目：**
- MUST 指摘がないか
- SHOULD/MAY 指摘を確認

### 3. 修正・改善
レビュー指摘に基づいて修正

### 4. 再検証
テスト → レビューを再度実行

### 5. コミット
```bash
git add .
git commit -m "..."
```

## 🎯 各機能の詳細

### テスト
- **対象：** Python ロジック（main.py）
- **テストスイート：** test_main.py
- **実行コマンド：** `python3 -m unittest`
- **要件：** 8/8 合格

### レビュー
- **対象：** 全ファイル（Python / Markdown / JSON）
- **観点：** リポジトリ規約（AGENTS.md） + 汎用観点
- **実行コマンド：** `/local-review main HEAD`
- **出力：** MUST / SHOULD / MAY 指摘

### ドキュメント
- **AGENTS.md** → コミット前検証要件
- **VALIDATION.md** → 検証プロセス詳細
- **deploy_memo/gcp-setup.md** → デプロイ手順
- **README.md** → 運用方法

## 💡 Tips

### テストが落ちたとき
```bash
python3 -m unittest test_main.TestClassName.test_method_name -v
```
特定のテストのみ実行して原因特定

### レビュー結果を再確認
```bash
/local-review main HEAD
```
同じ差分に対して再度レビュー実行

### CI/CD（参考）
本リポジトリのデプロイは GitHub Actions で自動化されています。
- `.github/workflows/deploy-cloud-run.yml`

---

## 📝 チェックリスト（コミット前）

- [ ] `python3 -m unittest` で 8/8 合格
- [ ] `/local-review` で MUST 指摘がない
- [ ] SHOULD 指摘は対応 or 妥当性記載
- [ ] ドキュメント更新（必要に応じて）
- [ ] コミットメッセージは英語
- [ ] `git push origin <branch>`

---

**最後に：** 自動化の目的は「品質向上」と「反復スピード加速」です。
チェックリストを満たさないコミットは Push しないことで、本番環境の品質を保ちます。
