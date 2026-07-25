# 開発ガイド

ローカル開発環境構築、デプロイ、トラブルシューティング。

## ローカル実行

### 最小構成

```bash
pip install -r requirements.txt
GEMINI_API_KEY="your-free-key" uvicorn main:app --host 0.0.0.0 --port 8080
# → http://localhost:8080
```

### 推奨設定（複数キー・段階的フォールバック）

複数の API キーを用意して、無料枠の配額切れに対応。詳細は [ARCHITECTURE.md#api-keyとモデルの管理](ARCHITECTURE.md#api-keyとモデルの管理) を参照。

```bash
GEMINI_API_KEY="your-free-key" \
GEMINI_API_KEYS="additional-free-key-1,additional-free-key-2" \
GEMINI_PAID_API_KEY="your-paid-key" \
GEMINI_MODELS_FREE="gemini-3.1-flash-lite,gemini-3.5-flash" \
GEMINI_MODELS_PAID="gemini-3.1-flash-lite,gemini-3.5-flash" \
uvicorn main:app --host 0.0.0.0 --port 8080
```

**試行順序（自動フォールバック）:**
1. `GEMINI_API_KEY` → `GEMINI_MODELS_FREE` で試行
2. `GEMINI_API_KEYS` の各キー → `GEMINI_MODELS_FREE` で試行
3. `GEMINI_PAID_API_KEY` → `GEMINI_MODELS_PAID` で試行（最終手段）

**フォールバック機構:**
- 任意のキーが rate limit（429）を返すと、自動的に次のキーへ移行
- バックオフ戦略: 75秒 → 10分 → 1時間 → ... → 24時間
- Firestore に状態が永続化され、Cloud Run 再起動後も保持

### 環境変数リファレンス

**Gemini API 設定:**
- `GEMINI_API_KEY`: 必須。優先度 1（Google AI Studio の無料キー）
- `GEMINI_API_KEYS`: 任意。追加キー（カンマ区切り）
- `GEMINI_PAID_API_KEY`: 任意。有料キー（最後のフォールバック）
- `GEMINI_MODELS_FREE`: 無料キー用モデルリスト（カンマ区切り）
- `GEMINI_MODELS_PAID`: 有料キー用モデルリスト（カンマ区切り）

**GCS（判定データ保存）:**
- `ALL_JUDGMENTS_BUCKET`: GCS バケット（全判定保存・品質監視用）
- `FEEDBACK_BUCKET`: GCS バケット（異議報告保存・学習データ用）

**通信制限:**
- `RATE_LIMIT`: 既定 20（秒間リクエスト数上限）
- `RATE_WINDOW`: 既定 60（秒単位のウィンドウ）

**画像検証:**
- `MAX_IMAGE_BYTES`: 既定 1000000
- `MAX_IMAGE_B64_CHARS`: 既定 1500000
- `MAX_IMAGE_PIXELS`: 既定 4000000
- `MAX_IMAGE_DIM`: 既定 1024
- `MIN_INK_PIXELS`: 既定 20

---

## PR レビュー向けビジュアル確認

Web UI の修正前後を比較する場合：

```bash
pip install -r requirements.txt -r dev-requirements.txt
python -m playwright install chromium
python -m playwright install-deps chromium  # Linux の場合
python scripts/visual_review.py --path / --label after
```

生成物は `artifacts/visual-review/` に保存（`.gitignore` で除外）

対象ページ指定例：
```bash
python scripts/visual_review.py --path /drill --label after
```

---

## テスト

### ユニットテスト実行

```bash
python -m unittest discover -s . -p 'test_*.py' -v
```

**必須基準**:
- すべてのテストが PASS (0 failures)
- 画像検証・採点ロジック・Gemini API 呼び出しをカバー

### カバレッジ確認（推奨）

```bash
pip install coverage
coverage run -m unittest discover
coverage report --include='main.py'
```

**目標**: 80% 以上

### 手動検証（PR マージ前）

1. **ヘルスチェック確認**:
   ```bash
   curl http://localhost:8080/healthz
   ```
   → 期待: 200 OK

2. **準備状態確認**:
   ```bash
   curl http://localhost:8080/readyz
   ```
   → 期待: GEMINI_API_KEY 設定時は 200、未設定時は 503

3. **採点 API の動作確認**:
   - ブラウザで http://localhost:8080 を開く
   - /drill ページで記号を手描き
   - 採点結果が正しく返ることを確認
   - 不合格時に不足特徴が表示されることを確認

### テスト失敗時のデバッグ

```bash
# 特定のテストクラスのみ実行
python -m unittest test_main.RateLimitingTest -v

# 詳細スタックトレース付き
python -m unittest test_main -v 2>&1 | tail -50

# ログレベル設定
LOGLEVEL=DEBUG python -m unittest discover -v
```

---

## デプロイ (Cloud Run)

### 初回セットアップ

GitHub Actions が Google Cloud に認証するための準備。

1. **Google Cloud 側の設定**（[deploy_memo/gcp-setup.md](../deploy_memo/gcp-setup.md) を参照）
   - Secret Manager 初期化
   - Workload Identity Federation 設定

2. **GitHub リポジトリ Settings → Secrets and variables → Actions → Variables** に登録：

   ```text
   GCP_WORKLOAD_IDENTITY_PROVIDER=projects/123456789/locations/global/workloadIdentityPools/github/providers/github-repo
   GCP_SERVICE_ACCOUNT=github-actions-deployer@zukigou-drill-dojo.iam.gserviceaccount.com
   ```

3. **Gemini API キーを Secret Manager に登録**

   **単一キー（最小構成）:**
   ```bash
   gcloud secrets create GEMINI_API_KEY \
     --replication-policy="automatic" \
     --data-file=- <<< "your-free-key"
   ```

   **複数キー（推奨、フォールバック対応）:**
   ```bash
   # 無料キー（優先度 1）
   gcloud secrets create GEMINI_API_KEY \
     --replication-policy="automatic" \
     --data-file=- <<< "free-key-1"

   # 追加の無料キー（優先度 2、カンマ区切り）
   gcloud secrets create GEMINI_API_KEYS \
     --replication-policy="automatic" \
     --data-file=- <<< "free-key-2,free-key-3"

   # 有料キー（優先度 3、最後のフォールバック）
   gcloud secrets create GEMINI_PAID_API_KEY \
     --replication-policy="automatic" \
     --data-file=- <<< "paid-key"
   ```

4. **Cloud Run サービスアカウントに IAM 権限を付与**

   ```bash
   # サービスアカウント取得
   SA="cloud-run-service-account@YOUR_PROJECT.iam.gserviceaccount.com"

   # 各 Secret に対して Secret Accessor 権限を付与
   for secret in GEMINI_API_KEY GEMINI_API_KEYS GEMINI_PAID_API_KEY; do
     gcloud secrets add-iam-policy-binding $secret \
       --member=serviceAccount:${SA} \
       --role=roles/secretmanager.secretAccessor \
       --project=YOUR_PROJECT
   done
   ```

5. **Cloud Run にシークレットをマウント**

   Cloud Run サービス更新時に環境変数として参照：
   ```bash
   gcloud run services update zukigou-drill \
     --set-env-vars GEMINI_API_KEY=GEMINI_API_KEY,GEMINI_API_KEYS=GEMINI_API_KEYS,GEMINI_PAID_API_KEY=GEMINI_PAID_API_KEY \
     --project=YOUR_PROJECT \
     --region=asia-northeast1
   ```

   > **注**: 値は Secret Manager からの参照（環境変数名で指定）ではなく、runtime に Secret Manager から値をロード

### 手動デプロイ

GitHub の **Actions → Deploy Cloud Run → Run workflow** を実行

パラメータ：
- `gemini_model`: デプロイ先で使用するモデル（既定: `gemini-2.5-flash`）
- `max_instances`: Cloud Run の最大インスタンス数（既定: 1）

### カスタムドメイン設定

Cloud Run サービスはデフォルトで `https://zukigou-drill-XXXXXX.a.run.app` という自動生成 URL を取得する。カスタムドメイン `zukigou-drill-dojo.run.app` にマッピングするには：

#### Cloud Console での設定

1. [Cloud Console](https://console.cloud.google.com/run) を開く
2. `zukigou-drill` サービスを選択
3. **ドメインマッピング** タブ → **ドメインを追加**
4. ドメイン `zukigou-drill-dojo.run.app` を入力
5. DNS レコードを確認・設定（Cloud DNS または外部 DNS プロバイダー）

#### CLI での設定

```bash
gcloud beta run domain-mappings create \
  --service=zukigou-drill \
  --domain=zukigou-drill-dojo.run.app \
  --project=zukigou-drill-dojo \
  --region=asia-northeast1
```

---

## 記号の追加

`symbols.json` にエントリを追加する際の注意。

### 定義の役割分け

- **`features`**: 学習者・UI 表示向けの短い説明（採点には使わない）
- **`required_features`**: 採点用ルーブリックの正本。必須特徴を個別の文で定義
- **`forbidden_features`**: 記号に存在してはいけない構造。類似記号との差分を優先
- **`confusable_symbols`**: 誤認しやすい記号と判断基準

### チェックリスト

- [ ] 各チェック項目が「画像を見て true/false で答えられる」文か
- [ ] 「きれい」「十分」など主観語を避けた
- [ ] `ref_svg` の全構成要素が `required_features` または `forbidden_features` に対応
- [ ] 参考書・規格票で形状を確認し、`"verified": true` をセット

### スケジュール

将来的に `features` を `required_features` から自動生成し、同じ情報の手動二重管理を廃止予定。

---

## デプロイ後のトラブルシューティング

クラウド上で「判定サービスが使えない」と表示される場合の診断。

### 1. サービス・リビジョン確認

```bash
gcloud run services describe zukigou-drill \
  --project zukigou-drill-dojo \
  --region asia-northeast1 \
  --format='value(status.url,status.latestReadyRevisionName)'
```

### 2. エラーログ確認

```bash
gcloud logging read \
  'resource.type="cloud_run_revision" AND resource.labels.service_name="zukigou-drill" AND resource.labels.location="asia-northeast1" AND severity>=ERROR' \
  --project zukigou-drill-dojo \
  --freshness=2h \
  --limit=50 \
  --format='table(timestamp,severity,resource.labels.revision_name,textPayload,jsonPayload.message)'
```

### 3. /api/judge ログ確認

```bash
gcloud logging read \
  'resource.type="cloud_run_revision" AND resource.labels.service_name="zukigou-drill" AND resource.labels.location="asia-northeast1" AND (httpRequest.requestUrl:"/api/judge" OR textPayload:"judge" OR jsonPayload.message:"judge")' \
  --project zukigou-drill-dojo \
  --freshness=30m \
  --limit=100 \
  --format='table(timestamp,severity,httpRequest.status,httpRequest.requestUrl,textPayload,jsonPayload.message)'
```

### 4. ヘルスチェック確認

```bash
SERVICE_URL="$(gcloud run services describe zukigou-drill \
  --project zukigou-drill-dojo \
  --region asia-northeast1 \
  --format='value(status.url)')"

# プロセス生存確認
curl -i "$SERVICE_URL/healthz"

# 設定・準備確認（/readyz が 503 ならば API キー未設定の可能性）
curl -i "$SERVICE_URL/readyz"

# 環境変数確認
gcloud run services describe zukigou-drill \
  --project zukigou-drill-dojo \
  --region asia-northeast1 \
  --format='yaml(spec.template.spec.containers[0].env)'
```

### 5. Secret Manager & IAM 確認

```bash
# 複数キーの構成確認
for key in GEMINI_API_KEY GEMINI_API_KEYS GEMINI_PAID_API_KEY; do
  echo "=== $key ==="
  gcloud secrets describe $key --project zukigou-drill-dojo 2>&1 | head -5
done

# Service Account 取得
SA=$(gcloud run services describe zukigou-drill \
  --project zukigou-drill-dojo \
  --region asia-northeast1 \
  --format='value(spec.template.spec.serviceAccountName)')

# 各キーの IAM 権限確認
for key in GEMINI_API_KEY GEMINI_API_KEYS GEMINI_PAID_API_KEY; do
  echo "=== $key ==="
  gcloud secrets get-iam-policy $key --project zukigou-drill-dojo \
    --filter="bindings.members:${SA}" 2>&1 || echo "Not granted"
done
```

### 6. API キー有効性確認

ログに `quota`、`permission`、`model not found`、`API key invalid` がないか確認。必要に応じて以下を見直す：
- API キーの有効期限
- Google Cloud プロジェクトの割り当て
- workflow の `gemini_model` パラメータ

### 7. 再現確認を記録

修正後に以下を実施し、issue or PR に記録：
- `/readyz` と `/healthz` の状態確認
- ブラウザからの判定操作再実行
- 確認した URL、時刻、該当リビジョン、ログ抜粋、実施した対処

---

## 判定データの GCS 保存

### 全判定の保存（品質監視用）

環境変数 `ALL_JUDGMENTS_BUCKET` を設定すると、全ての判定の画像と結果を保存：

- 保存内容: 画像 / symbol_id / 判定結果（passed / score / checks / mistakes / observation）/ 日付（日単位）
- 非保存: IP・セッション情報・秒精度時刻・ユーザー ID
- ライフサイクルルール推奨: 30 日で自動削除
- 設定:

```bash
gcloud storage buckets create gs://zukigou-all-judgments --location=asia-northeast1
echo '{"rule":[{"action":{"type":"Delete"},"condition":{"age":30}}]}' > /tmp/lc.json
gcloud storage buckets update gs://zukigou-all-judgments --lifecycle-file=/tmp/lc.json
gcloud run services update zukigou-drill \
  --set-env-vars ALL_JUDGMENTS_BUCKET=zukigou-all-judgments
```

### 異議報告の保存（学習データ用）

環境変数 `FEEDBACK_BUCKET` を設定すると、「判定に納得できない」の報告のみ保存：

- 保存内容: 画像 / symbol_id / 判定結果 / 日付（日単位）
- 非保存: IP・セッション情報・秒精度時刻・ユーザー ID
- ライフサイクルルール推奨: 90 日で自動削除
- 設定:

```bash
gcloud storage buckets create gs://zukigou-feedback --location=asia-northeast1
echo '{"rule":[{"action":{"type":"Delete"},"condition":{"age":90}}]}' > /tmp/lc.json
gcloud storage buckets update gs://zukigou-feedback --lifecycle-file=/tmp/lc.json
gcloud run services update zukigou-drill \
  --set-env-vars FEEDBACK_BUCKET=zukigou-feedback
```

---

## セキュリティ・制限機構

### サーバー側の検証

- 画像: PNG 形式、サイズ・ピクセル数・インク量の上限をチェック
- 白紙判定: 白紙に近い画像は Gemini API 呼び出しをスキップ
- Gemini 応答: JSON Schema + Pydantic strict mode で検証

### API キーの優先順位

1. `GEMINI_API_KEY` 最優先
2. `GEMINI_API_KEYS` の各キー
3. `GEMINI_PAID_API_KEY` 最後の手段

### 組み込みレート制限

単一インスタンス向けの補助機能。一般公開時は以下を併用：
- Cloud Armor
- Cloud Run の `max-instances`
- Gemini API quota

### Rate Limiting の自動フォールバック

API キーが連続して失敗した場合、指数バックオフを自動適用：

- 1 回目失敗: 75 秒待機
- 2 回目: 10 分
- 3 回目: 1 時間
- ...最大 24 時間

状態は Firestore に永続化され、Cloud Run の再起動後も保持される。

---

## 自動コード審査・マージシステム

### 概要

3 時間ごとに自動でコードレビュー・敵対的レビューを実行。軽微な修正（`[minor]` ラベル）は条件付きで自動マージ。

### セットアップ

```bash
# 必須: GitHub personal access token（repo 権限）を環境変数に設定
export GITHUB_TOKEN="your-token"

# PR 作成・自動レビュー・マージは GitHub Actions workflow で自動実行
# 3 時間ごとの :17, :27, :47 分に実行
```

### [minor] ラベルの定義

ランタイムに影響しないファイルのみが変更された場合に自動付与：

- ✅ ドキュメント、README、設定例
- ❌ `main.py`、`requirements.txt`、`.github/workflows/*`、`scripts/*`

実装ファイル変更を含む PR は標準レビュープロセス（人間の確認）が必須。

詳細は [../.claude/REVIEW-PERSPECTIVES.md](../.claude/REVIEW-PERSPECTIVES.md) 参照。
