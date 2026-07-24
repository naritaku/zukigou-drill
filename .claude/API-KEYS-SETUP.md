# API キー設定ガイド

このプロジェクトで自動化パイプラインを実行するために必要な API キーの取得・設定方法です。

## 🔑 必要な API キー

### 1. Gemini API キー（必須）

図記号判定用の Vision API を使用するため、Gemini API キーが必須です。

#### 取得方法

1. **Google AI Studio にアクセス**
   - URL: https://ai.google.dev/pricing?hl=ja
   - または: https://aistudio.google.com/app/apikey

2. **「API キーを作成」をクリック**
   - 新しいプロジェクトを作成するか、既存プロジェクトを選択

3. **API キーをコピー**
   - 形式: `AIzaSy...`

4. **.env.local に設定**
   ```bash
   export GEMINI_API_KEY="AIzaSy..."
   ```

#### 無料枠について（重要）

**Gemini API 無料枠:**
- 月間 15,000 リクエスト
- Vision: 月間 4,000 リクエスト
- レート制限: 1分間 15 リクエスト

**このプロジェクトの使用量:**
- 1判定 = 1 リクエスト + 約 2000 トークン
- 月間 3000-4000 リクエストで十分なテスト・開発が可能

### 2. Gemini 有料キー（オプション）

無料枠では足りない場合、または本番環境での安定性が必要な場合。

#### 取得方法

1. **Google Cloud Console にアクセス**
   - URL: https://console.cloud.google.com/

2. **請求情報を設定**
   - プロジェクトを作成
   - 支払い方法を登録

3. **Gemini API を有効化**
   - API ライブラリから検索
   - 有効にする

4. **API キーを作成**
   - 認証情報 → API キーを作成

5. **.env.local に設定**
   ```bash
   export GEMINI_PAID_API_KEY="AIzaSy..."
   ```

#### 有料プランの価格（参考）

| モデル | 入力 | 出力 |
|--------|------|------|
| gemini-3.1-flash-lite | $0.075/M | $0.3/M |
| gemini-3.5-flash | $1.5/M | $9/M |

**月 2000-3000 リクエストの場合:**
- 3.1 Flash Lite のみ: 約 $0.5-1
- ハイブリッド（無料 + 有料）: 約 $0.3-0.5

---

## 🔧 セットアップ手順

### ステップ 1: Gemini API キーを取得

上記「Gemini API キー（必須）」を参照

### ステップ 2: `.env.local` を編集

```bash
# テンプレートを確認
cat .env.local

# エディタで編集
nano .env.local
# または
vim .env.local
```

内容例：
```
GEMINI_API_KEY=AIzaSyXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
GEMINI_PAID_API_KEY=AIzaSyYYYYYYYYYYYYYYYYYYYYYYYYYYYYYY
```

### ステップ 3: 環境変数を確認

```bash
source scripts/load-env.sh
```

出力例：
```
✅ 環境変数をロードしました
   - GEMINI_API_KEY: AIzaSyXXXXXXXXXX...
   - GEMINI_PAID_API_KEY: 設定済み
```

### ステップ 4: テスト実行で検証

```bash
# 環境変数をロード
source scripts/load-env.sh

# テスト実行
python3 -m unittest
```

---

## 🌐 GitHub CLI 認証（確認）

gh コマンド用の認証は既に設定済みです。

### 確認方法

```bash
gh auth status
```

出力例：
```
github.com
  ✓ Logged in to github.com account naritaku (keyring)
  - Active account: true
  - Git operations protocol: https
  - Token scopes: 'repo', 'workflow'
```

### 必要なスコープ

PR 自動作成に必要な最小限のスコープ：
- `repo` → PR 作成・コメント追加
- `workflow` → CI/CD トリガー（オプション）

---

## ☁️ GCS バケット設定（オプション）

全判定データを保存する場合のみ必要です。

### GCS セットアップ

詳細は `deploy_memo/gcp-setup.md` を参照。

```bash
# バケット作成例
gcloud storage buckets create gs://zukigou-all-judgments \
  --location=asia-northeast1

# .env.local に設定
echo 'ALL_JUDGMENTS_BUCKET=zukigou-all-judgments' >> .env.local
```

---

## 🔒 セキュリティ確認

### ✅ `.env.local` は .gitignore に含まれています

```bash
# 確認
git status .env.local
```

出力：
```
.env.local is ignored (in .gitignore)
```

### ⚠️ API キーをコミットしない

```bash
# 誤ってコミットされていないか確認
git log --all --full-history -S "GEMINI_API_KEY" | head -5
```

---

## 🚀 動作確認

### 完全なセットアップ確認

```bash
#!/bin/bash
# 全項目を確認

echo "1️⃣  環境変数確認..."
source scripts/load-env.sh

echo ""
echo "2️⃣  テスト実行..."
python3 -m unittest

echo ""
echo "3️⃣  gh コマンド確認..."
gh auth status

echo ""
echo "✨ セットアップ完了！"
```

---

## 📝 チェックリスト

- [ ] Gemini API キーを取得
- [ ] `.env.local` に設定
- [ ] `source scripts/load-env.sh` で確認
- [ ] `python3 -m unittest` で テスト実行
- [ ] `gh auth status` で GitHub 認証確認
- [ ] `.env.local` がコミットされていないことを確認

---

## 🆘 トラブルシューティング

### `GEMINI_API_KEY が設定されていない` エラー

```bash
# .env.local に GEMINI_API_KEY が設定されているか確認
grep GEMINI_API_KEY .env.local

# 環境変数がロードされているか確認
echo $GEMINI_API_KEY
```

### Gemini API がエラーを返す

```bash
# API キーが正しいか確認（curl）
curl -s https://generativelanguage.googleapis.com/v1beta/models/gemini-pro:generateContent \
  -H "Content-Type: application/json" \
  -H "x-goog-api-key: $GEMINI_API_KEY" \
  -d '{"contents":[{"parts":[{"text":"test"}]}]}' | jq .

# API キーが無効な場合：
# "API key not valid" エラーが返される
```

### テストが失敗する

```bash
# 特定のテストのみ実行
python3 -m unittest test_main.JudgeEndpointTest.test_judge_valid_image -v

# 詳細なエラーメッセージを表示
python3 -m unittest test_main.JudgeEndpointTest -v
```

---

## 💡 ベストプラクティス

1. **定期的にキーをローテーション**
   - 3ヶ月ごとに新しいキーを生成

2. **複数キーを設定**
   ```bash
   export GEMINI_API_KEY="free-key"
   export GEMINI_PAID_API_KEY="paid-key"
   ```

3. **キーのスコープを最小化**
   - Gemini API のみを許可

4. **ログを監視**
   ```bash
   # 異常なリクエストがないか確認
   tail -f /tmp/test-result.log
   ```

---

**セットアップが完了したら、PR 自動作成パイプラインが使用可能です！** 🚀
