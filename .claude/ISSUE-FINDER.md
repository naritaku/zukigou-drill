# Issue Finder - GitHub Issue 検索ツール

リポジトリのオープン issue、バグ、機能リクエストなどを素早く検索・一覧表示するツールです。

## 🚀 使用方法

### コマンド

```bash
bash scripts/find-issues.sh [検索タイプ]
```

### 検索タイプ

| タイプ | 説明 | 例 |
|--------|------|-----|
| `open` | オープンな issue（デフォルト） | `bash scripts/find-issues.sh open` |
| `closed` | クローズされた issue | `bash scripts/find-issues.sh closed` |
| `all` | すべての issue | `bash scripts/find-issues.sh all` |
| `bug` | バグ関連 | `bash scripts/find-issues.sh bug` |
| `feature` | 機能リクエスト | `bash scripts/find-issues.sh feature` |
| `help` | ヘルプが必要な issue | `bash scripts/find-issues.sh help` |

---

## 📋 使用例

### 1. オープンな issue を表示

```bash
bash scripts/find-issues.sh open
```

**出力例：**
```
====================================
🔍 Issue 検索ツール
====================================
リポジトリ: naritaku/zukigou-drill

📋 オープンな issue 一覧：
#42  Feature: Add exponential backoff      about 1 hour ago
#41  Bug: Rate limit handling              about 2 days ago
#40  docs: Update deploy instructions      about 1 week ago
```

### 2. バグのみを表示

```bash
bash scripts/find-issues.sh bug
```

### 3. 詳細情報を表示

```bash
# issue 一覧から取得
bash scripts/find-issues.sh open

# 詳細を確認
gh issue view 42
```

---

## 🔄 Claude Code での使用

### パターン 1: Issue を探して対応

```
バグ issue を探して、対応を実装してください
```

Claude Code が以下を実行：
1. `bash scripts/find-issues.sh bug` で bug issue を取得
2. 各 issue の詳細を確認
3. 対応を実装
4. PR を作成

### パターン 2: 特定ラベルの issue を処理

```
help wanted ラベルが付いた issue を確認して、対応を実装してください
```

---

## 📖 gh コマンド直接使用

### よく使うコマンド

```bash
# オープンな issue の一覧
gh issue list --state open

# バグラベルの issue
gh issue list --label bug

# 特定のユーザーが作成した issue
gh issue list --creator username

# タイトルで検索
gh issue list --search "title:keyword"

# 詳細情報を表示
gh issue view 42

# issue にコメント追加
gh issue comment 42 --body "This is a comment"

# issue をクローズ
gh issue close 42

# issue を再オープン
gh issue reopen 42
```

---

## 🎯 使用シーン

### 1. 開発タスクの発見

```bash
# 対応待ちの issue を確認
bash scripts/find-issues.sh open

# 優先度の高い issue をピックアップ
# → 対応を実装
```

### 2. バグ修正

```bash
# バグ issue のみ確認
bash scripts/find-issues.sh bug

# 修正を実装
# → テスト実行
# → PR 作成
```

### 3. ドキュメント整備

```bash
# ドキュメント関連の issue
gh issue list --label docs

# 古い issue の確認と更新
```

---

## 🤖 Claude Code での完全自動化

### Issue → 対応 → PR まで自動

```
以下のコマンドで issue を確認して、対応してください：
bash scripts/find-issues.sh feature

確認後、対応を実装して PR を作成してください
```

Claude Code が以下を自動実行：
1. Issue 一覧取得
2. 詳細確認
3. 実装
4. テスト実行
5. PR 作成＆レビュー

---

## 💡 Tips

### Issue を素早く開く

```bash
# issue #42 をブラウザで開く
gh issue view 42 --web
```

### Issue に紐付けるコミット

```bash
# issue を参照するコミット
git commit -m "fix: Resolve issue #42

Implements exponential backoff for rate limiting"
```

### 複数 issue の一括操作

```bash
# すべてのバグを確認
for issue in $(gh issue list --label bug --json number -q '.[].number'); do
  echo "Issue #$issue"
  gh issue view $issue
done
```

---

## 🔗 関連コマンド

```bash
# PR 一覧
gh pr list

# PR の詳細
gh pr view 42

# Issue と PR の統計
gh issue list --state closed | wc -l
```

---

## 📊 Issue 統計

```bash
# オープンな issue の数
gh issue list --state open | wc -l

# バグの数
gh issue list --state open --label bug | wc -l

# 機能リクエストの数
gh issue list --state open --label feature | wc -l
```

---

**Issue Finder で効率よく開発タスクを管理！** 🚀
