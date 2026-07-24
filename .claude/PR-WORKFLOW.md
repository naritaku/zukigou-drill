# 自動 PR 作成ワークフロー

このガイドでは、ローカル検証 → 自動 PR 作成 → マージ準備完了までの完全な自動化パイプラインを説明します。

## 🎯 ワークフロー概要

```
ローカル実装
    ↓
テスト実行 ✅
    ↓
ビジュアル確認 📸
    ↓
PR 自動作成 🚀
    ↓
検証コメント自動生成 📝
    ↓
マージ準備完了 ✨
    ↓
人間が GitHub で確認 & マージ 👤
```

---

## 📋 パターン A: シンプル（テストのみ）

### 1. ローカルで実装・テスト

```bash
# 編集
code main.py

# ローカルテスト
python3 -m unittest
```

### 2. PR 自動作成 + テスト結果コメント

```bash
bash scripts/create-pr-with-validation.sh \
  "Feature: Add rate limiting backoff" \
  "実装: 段階的バックオフ機能"
```

**自動実行内容：**
- ✅ テスト実行確認（8/8 合格）
- ✅ 変更ファイル一覧作成
- ✅ PR 作成
- ✅ テスト結果コメント自動追加

**GitHub での見た目：**
```
PR タイトル: Feature: Add rate limiting backoff
PR 説明: 実装内容 + テスト統計
コメント: テスト結果詳細
```

---

## 📸 パターン B: 拡張（ビジュアル確認 + 自動判定）

### 1. ローカルで実装・テスト

```bash
# HTML/CSS 変更の場合
code landing.html

# テスト実行
python3 -m unittest
```

### 2. ビジュアル確認 + PR 作成

```bash
bash scripts/create-pr-with-visual-review.sh \
  "UI: Improve button styling"
```

**自動実行内容：**
- ✅ テスト実行確認
- ✅ ビジュアルスクショ取得（ブラウザ自動開く）
- ✅ PR 作成
- ✅ テスト結果コメント追加
- ✅ スクショをコメントで追加

**GitHub での見た目：**
```
PR タイトル: UI: Improve button styling
PR 説明: 変更内容 + テスト統計
コメント1: テスト結果詳細
コメント2: ビジュアル確認スクショ + 判定
```

---

## 🔄 フル自動化ループ（Claude Code 内）

Claude Code プロンプトで指示すると、すべて自動実行できます：

### パターン 1: テスト → 修正 → PR

```
変更点をテストして、問題があれば修正し、PR を作成してください
```

**Claude Code が自動実行：**
1. テスト実行
2. 失敗時は原因特定 & 修正
3. 再テスト
4. `bash scripts/create-pr-with-validation.sh` 実行
5. PR 作成

### パターン 2: テスト → ビジュアル確認 → PR

```
HTML変更をテストして、ビジュアル確認をしてから PR を作成してください
```

**Claude Code が自動実行：**
1. テスト実行
2. ビジュアルスクショ取得
3. Claude で スクショ分析
4. 分析結果をコメント候補に
5. `bash scripts/create-pr-with-visual-review.sh` 実行
6. PR 作成＋コメント追加

---

## 📊 PR コメントの内容

### テスト結果コメント
```
✅ テスト実行
- ステータス: 合格
- 結果: 8/8 テスト合格
- 実行時刻: 2026-07-24 15:30:45
```

### ビジュアル確認コメント
```
📸 ビジュアル確認結果
[スクショ画像]
判定: ✅ 期待通りの見た目
- レイアウト: OK
- カラー: OK
- レスポンシブ: OK（PC/モバイル確認）
```

---

## 🚀 実行手順（ステップバイステップ）

### ステップ 1: 実装＆ローカルテスト

```bash
# ブランチ切る
git checkout -b feature/my-feature

# 実装する
# ... edit files ...

# テスト実行
python3 -m unittest

# OK なら次へ
```

### ステップ 2: PR 作成（2 つのコマンドから選択）

**シンプル版：**
```bash
bash scripts/create-pr-with-validation.sh "Feature: My feature"
```

**ビジュアル確認版：**
```bash
bash scripts/create-pr-with-visual-review.sh "UI: My UI change"
```

### ステップ 3: GitHub で確認＆マージ

1. 表示される PR URL にアクセス
2. テスト結果コメントを確認
3. ビジュアルスクショを確認（HTML/CSS 変更時）
4. 「Merge pull request」をクリック

**以上！** 人間は最終確認とマージボタンを押すだけ。

---

## 🔧 カスタマイズ

### PR タイトルを指定

```bash
bash scripts/create-pr-with-validation.sh \
  "Feature: Add exponential backoff for rate limiting"
```

### PR 説明を指定

```bash
bash scripts/create-pr-with-validation.sh \
  "Feature: Add exponential backoff" \
  "段階的バックオフ: 75秒 → 10分 → 1時間 → ..."
```

### ビジュアルレビューのパスを指定

`scripts/create-pr-with-visual-review.sh` 内の以下を編集：
```bash
python3 scripts/visual_review.py --path "/" --label after
```

例：特定ページのみ確認
```bash
python3 scripts/visual_review.py --path "/standards" --label after
```

---

## 📋 チェックリスト

### コミット前の確認
- [ ] `python3 -m unittest` で 8/8 合格
- [ ] ブランチが main でないことを確認
- [ ] `bash scripts/create-pr-with-validation.sh` または
- [ ] `bash scripts/create-pr-with-visual-review.sh` を実行
- [ ] PR が作成され、コメントが追加されたことを確認

### GitHub マージ前の確認
- [ ] PR の説明を読む
- [ ] テスト結果コメントを確認
- [ ] ビジュアルスクショを確認（該当時）
- [ ] コードレビュー
- [ ] 「Merge pull request」をクリック

---

## 🎯 利点

| 項目 | 利点 |
|------|------|
| **時間短縮** | テスト → PR 作成 → コメント追加まで自動化 |
| **品質保証** | すべての PR がテスト合格・ビジュアル確認済み |
| **透明性** | テスト結果・スクショがコメントに自動記載 |
| **マージ判断** | 人間は最終確認のみで、すべて合格済み PR をマージ |
| **監査** | GitHub に全テスト結果・ビジュアル確認記録が残る |

---

## 🐛 トラブルシューティング

### PR 作成に失敗する

```bash
# GitHub CLI の認証確認
gh auth status

# 必要に応じて再認証
gh auth login
```

### ビジュアルレビューが失敗する

```bash
# Playwright インストール
python3 -m playwright install chromium

# サーバーが起動しているか確認
lsof -i :8080
```

### テストが失敗する

```bash
# 個別テスト実行で原因特定
python3 -m unittest test_main.JudgeEndpointTest.test_judge_valid_image -v
```

---

## 📞 サポート

スクリプトの詳細は以下を参照：
- テスト実行: `.claude/VALIDATION.md`
- PR 作成: `scripts/create-pr-with-validation.sh`
- ビジュアル: `scripts/create-pr-with-visual-review.sh`

---

**次のステップ:**

1. ローカルで実装
2. `bash scripts/create-pr-with-validation.sh "..."` 実行
3. GitHub で PR 確認＆マージ

**以上で、完全自動化パイプラインの完成です！** 🚀
