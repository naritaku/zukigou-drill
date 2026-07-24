# 自動コードレビュー統合ガイド

PR 作成時に Claude Code が自動的にコードレビューを実行し、結果を PR コメントに追加する機能です。

## 🎯 フロー

```
実装 → テスト → PR 作成
          ↓
    自動コードレビュー開始
          ↓
    レビュー結果を PR コメント追加
          ↓
    人間が確認 & マージ
```

---

## 📋 3つの実装パターン

### パターン 1: シンプル版（テスト + レビュー告知）

**使用コマンド:**
```bash
bash scripts/create-pr-with-validation.sh "Feature: My feature"
```

**動作:**
- ✅ テスト実行
- ✅ PR 作成
- ✅ テスト結果コメント追加
- 📝 レビュー実行中の告知コメント追加

**適用シーン:**
- 小規模な変更
- 緊急性が低い変更

---

### パターン 2: 拡張版（テスト + ビジュアル + レビュー）

**使用コマンド:**
```bash
bash scripts/create-pr-with-visual-review.sh "UI: My UI change"
```

**動作:**
- ✅ テスト実行
- ✅ ビジュアルスクショ取得
- ✅ PR 作成
- ✅ テスト結果 + ビジュアルコメント追加
- 📝 レビュー実行中の告知コメント追加

**適用シーン:**
- HTML/CSS 変更
- UI 関連の変更

---

### パターン 3: 高度な自動統合版（フル自動）

**使用コマンド:**
```bash
bash scripts/create-pr-with-auto-review.sh "Feature: My feature"
```

**動作:**
- ✅ テスト実行
- ✅ PR 作成
- ✅ テスト結果コメント追加
- ⏳ Claude Code が自動コードレビュー実行
- 📝 レビュー結果を自動コメント追加（2-5 分後）

**適用シーン:**
- 本番運用
- 完全自動化が必要な場合

---

## 🚀 セットアップ方法

### 1. ローカル実装 + テスト

```bash
git checkout -b feature/my-feature

# 実装
code main.py

# テスト確認
python3 -m unittest
```

### 2. PR 作成 + 自動レビュー開始

```bash
bash scripts/create-pr-with-auto-review.sh "Feature: My feature title"
```

**出力例:**
```
✨ PR 作成完了 - レビュー自動開始
📍 PR URL: https://github.com/naritaku/zukigou-drill/pull/42

🤖 自動処理進行中：
  ✅ テスト実行: 完了
  ✅ PR 作成: 完了
  ⏳ コードレビュー: 実行中...

⏱️  レビュー結果は 2-5 分で PR コメントに表示されます
```

### 3. GitHub で確認

```
PR ページを開く
  ↓
コメント1: テスト結果 ✅
  ↓
コメント2: Claude Code レビュー（2-5分後） 📝
  ↓
指摘対応 or マージ
```

---

## 🔍 自動レビューの内容

Claude Code は以下の観点からコードレビューを実施します：

### 自動チェック項目

- **機能性**: コードが意図通りに動作しているか
- **スタイル**: コード規約に従っているか
- **セキュリティ**: セキュリティ脆弱性がないか
- **パフォーマンス**: パフォーマンス問題がないか
- **テスト**: テストが十分か
- **ドキュメント**: ドキュメントが最新か

### レビュー結果の例

```markdown
## 🔍 Code Review Results

### ✅ PASS Items
- コード品質: OK
- テストカバレッジ: 完全
- セキュリティ: 問題なし

### ⚠️ SHOULD Items
- コメントを追加すると可読性が向上
- エラーハンドリングをより詳細に

### 📋 Checklist
- [x] テスト実行
- [x] ローカルレビュー
- [ ] マージ（人間による確認後）
```

---

## 🔄 手動レビューとの組み合わせ

### フロー

```
自動レビュー（2-5分）
  ↓
人間によるレビュー（オプション）
  ↓
指摘対応
  ↓
マージ
```

### 対応方法

**自動レビューで MUST 指摘がある場合:**
```bash
# 修正
code main.py

# 再テスト
python3 -m unittest

# 修正をコミット
git add .
git commit -m "fix: address review comments"
git push origin feature/my-feature
```

PR は自動的に再レビューされます。

---

## ⚙️ カスタマイズ

### レビュー対象のファイルを限定

`.github/workflows/auto-review-on-pr.yml` で設定：

```yaml
on:
  pull_request:
    paths:
      - 'main.py'
      - 'test_*.py'
```

### レビュー実行の条件を変更

```yaml
# テスト失敗時はレビューしない
- name: Skip review on test failure
  if: failure()
  run: exit 0
```

### コメント内容をカスタマイズ

`scripts/create-pr-with-auto-review.sh` の `PR_BODY` 部分を編集。

---

## 🐛 トラブルシューティング

### レビューコメントが表示されない

```bash
# 1. PR を確認
gh pr view <PR-NUMBER> --web

# 2. GitHub Actions ワークフローを確認
gh run list --workflow auto-review-on-pr.yml

# 3. ログを確認
gh run view <RUN-ID> --log
```

### テスト失敗で PR が作成できない

```bash
# ローカルで再度テスト実行
python3 -m unittest -v

# 失敗原因を特定
python3 -m unittest test_main.JudgeEndpointTest -v
```

### 自動レビューが実行されない

```bash
# GitHub Actions が有効か確認
ls -la .github/workflows/

# ワークフロー構文をチェック
gh workflow view auto-review-on-pr.yml
```

---

## 📊 実行時間の目安

| ステップ | 時間 |
|---------|------|
| テスト実行 | 5-10秒 |
| PR 作成 | 2-3秒 |
| 自動レビュー開始 | 即座 |
| レビュー実行（CI/CD） | 2-5 分 |
| **合計（PR マージまで）** | **5-10 分** |

---

## 💡 ベストプラクティス

### 1. テストは必ず実行
```bash
python3 -m unittest
```
失敗時は修正してから PR 作成。

### 2. コミットメッセージは明確に
```
feat: Add exponential backoff for rate limiting
fix: Correct TypedDict definition
docs: Update API key setup guide
```

### 3. レビュー指摘を確認
- MUST 指摘は必ず対応
- SHOULD 指摘は検討して対応
- MAY 指摘は任意

### 4. マージの判断基準
```
✅ 全テスト合格
✅ 自動レビュー OK
✅ 人間による確認完了
  → マージ OK
```

---

## 🎯 効果

| 項目 | 効果 |
|------|------|
| **品質** | 自動チェックで基本的な問題を検出 |
| **速度** | PR 作成から確認まで完全自動化 |
| **透明性** | すべてのチェック結果が GitHub に記録 |
| **手戻り削減** | 人間は最終確認のみ |

---

## 📞 関連ドキュメント

- `.claude/PR-WORKFLOW.md` - PR パイプライン完全ガイド
- `.claude/VALIDATION.md` - ローカル検証プロセス
- `.claude/SETUP.md` - Claude Code 統合ガイド

---

**完全自動化で、品質と速度の両立を実現！** 🚀
