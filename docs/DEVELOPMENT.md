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
- バックオフ状態は Firestore に永続化され、Cloud Run 再起動後も復元される
  （Firestore 不達時はインスタンス内メモリにフォールバック）

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
- `RATE_LIMIT`: 既定 20（`RATE_WINDOW` あたりの最大リクエスト数）
- `RATE_WINDOW`: 既定 60（秒単位のウィンドウ）
- `TRUST_FORWARDED_FOR`: 既定 1。`X-Forwarded-For` の**末尾**をクライアント識別子に使う。
  Cloud Run やロードバランサ配下ではこれが必要（TCP 接続元はプロキシになるため、
  無効にすると全ユーザーが 1 つのレート制限バケットを共有してしまう）。
  プロキシを介さず直接公開する場合のみ `0` にする。

**画像検証:**
- `MAX_IMAGE_BYTES`: 既定 1000000
- `MAX_IMAGE_B64_CHARS`: 既定 1500000
- `MAX_IMAGE_PIXELS`: 既定 4000000
- `MAX_IMAGE_DIM`: 既定 1024（超過分はサーバー側で縮小）
- `MIN_INK_PIXELS`: 既定 20（未満なら白紙として 400 で拒否）
- `INK_THRESHOLD`: 既定 245（この輝度未満をインクとみなす）

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

1. **Workload Identity Federation**（サービスアカウントキーを持たずに GitHub から認証する）

   プール・プロバイダ・サービスアカウントのいずれかが実在しないと、
   `google-github-actions/auth` が `invalid_target` で失敗する。

   ```bash
   PROJECT_ID=zukigou-drill-dojo
   PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
   REPO=naritaku/zukigou-drill
   SA="github-actions-deployer@${PROJECT_ID}.iam.gserviceaccount.com"

   # デプロイ用サービスアカウント
   gcloud iam service-accounts create github-actions-deployer \
     --project "$PROJECT_ID" --display-name "GitHub Actions deployer"

   # 必要ロール（run.admin: サービス更新 / cloudbuild・storage・artifactregistry:
   # --source デプロイのビルドと push / secretmanager.viewer: シークレット存在確認
   # / logging.viewer: ビルドログ取得）
   for ROLE in roles/run.admin roles/cloudbuild.builds.editor roles/storage.admin \
               roles/artifactregistry.writer roles/secretmanager.viewer roles/logging.viewer; do
     gcloud projects add-iam-policy-binding "$PROJECT_ID" \
       --member "serviceAccount:${SA}" --role "$ROLE" --condition=None
   done

   # ランタイム SA として Cloud Run を動かす権限（actAs）
   gcloud iam service-accounts add-iam-policy-binding \
     "${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
     --project "$PROJECT_ID" \
     --member "serviceAccount:${SA}" --role roles/iam.serviceAccountUser

   # プールとプロバイダ（このリポジトリからの認証のみ許可）
   gcloud iam workload-identity-pools create github \
     --project "$PROJECT_ID" --location global --display-name "GitHub Actions"
   gcloud iam workload-identity-pools providers create-oidc github-repo \
     --project "$PROJECT_ID" --location global --workload-identity-pool github \
     --display-name "GitHub repo" \
     --issuer-uri "https://token.actions.githubusercontent.com" \
     --attribute-mapping "google.subject=assertion.sub,attribute.repository=assertion.repository" \
     --attribute-condition "assertion.repository=='${REPO}'"
   gcloud iam service-accounts add-iam-policy-binding "$SA" \
     --project "$PROJECT_ID" --role roles/iam.workloadIdentityUser \
     --member "principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/github/attribute.repository/${REPO}"
   ```

2. **GitHub リポジトリ Settings → Secrets and variables → Actions → Variables** に登録
   （上で作ったリソース名と一致させる）：

   ```bash
   gh variable set GCP_WORKLOAD_IDENTITY_PROVIDER \
     --body "projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/github/providers/github-repo"
   gh variable set GCP_SERVICE_ACCOUNT --body "$SA"
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

4. **専用ランタイム SA を作り、シークレットの参照権を付与**

   GCP の既定では Cloud Run は compute SA で動き、その SA は **`roles/editor`**
   （プロジェクトのほぼ全権）を持つ。アプリが必要なのは Firestore・ログ出力・
   シークレット参照だけなので、専用 SA に絞る。ワークフローは
   `--service-account zukigou-drill-run@...` で明示的にこの SA を指定する。

   シークレットを読むのはランタイム SA なので、使うシークレットごとに
   `secretAccessor` が必要（無いとリビジョン作成が `Permission denied on secret`
   で失敗する。ビルドは通るため最後まで気付きにくい）。

   ```bash
   bash scripts/setup-gcp-guardrails.sh runtime-sa   # SA 作成 + 最小ロール + actAs
   bash scripts/setup-gcp-guardrails.sh secrets      # シークレットごとの参照権
   bash scripts/setup-gcp-guardrails.sh verify       # 付与状況と実効ロールを確認
   ```

   付与するロールは Firestore 用の `roles/datastore.user` とログ用の
   `roles/logging.logWriter` のみ。GCS 保存を有効にする場合は、対象バケットに
   `roles/storage.objectCreator` を**バケット単位で**追加する。

   > **注**: シークレットは環境変数への値コピーではなく `--set-secrets` でマウントする。
   > ワークフローは Secret Manager に**存在するものだけ**を渡すため、未作成のキーを
   > 気にせず実行できる。

### 手動デプロイ

GitHub の **Actions → Deploy Cloud Run → Run workflow** を実行（`main` のみ）。

パラメータ（既定値）：
- `gemini_models_free` / `gemini_models_paid`: 試行するモデルの順序
  （既定: `gemini-3.1-flash-lite,gemini-3.5-flash`）
- `max_instances`: Cloud Run の最大インスタンス数（既定: `2`）
- `rate_limit` / `rate_window`: IP あたりのレート制限（既定: 20 回 / 60 秒）
- `all_judgments_bucket` / `feedback_bucket`: 空ならデータ保存は無効

### ローカルからのデプロイ

CI が使えないときはワークフローと同じ内容を手元から実行できる。`--set-env-vars` と
`--set-secrets` は**指定した集合で置き換わる**ので、必要な値はすべて書く。

```bash
gcloud run deploy zukigou-drill --source . \
  --project zukigou-drill-dojo --region asia-northeast1 \
  --allow-unauthenticated --max-instances 2 \
  --set-secrets "GEMINI_API_KEY=GEMINI_API_KEY:latest,GEMINI_PAID_API_KEY=GEMINI_PAID_API_KEY:latest" \
  --set-env-vars "^|^GEMINI_MODELS_FREE=gemini-3.1-flash-lite,gemini-3.5-flash|GEMINI_MODELS_PAID=gemini-3.1-flash-lite,gemini-3.5-flash|RATE_LIMIT=20|RATE_WINDOW=60"

# 疎通確認（/healthz は Google のフロントエンドが握るため 404 になる。/readyz を見る）
curl -fsS https://zukigou-drill-vnoxzmytga-an.a.run.app/readyz
```

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
```

バケット名は **Deploy Cloud Run ワークフローの `all_judgments_bucket` 入力**に指定する。
`gcloud run services update` で手動設定しても、次回デプロイの `--set-env-vars` が
環境変数一式を置き換えるため消える。

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
```

同様に、バケット名は **Deploy Cloud Run ワークフローの `feedback_bucket` 入力**に指定する。

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

API キーが 429（RESOURCE_EXHAUSTED）を返した場合、そのキーを一定時間スキップし、
連続回数に応じて待機時間を延ばす：

- 1 回目失敗: 75 秒待機
- 2 回目: 10 分
- 3 回目: 1 時間
- ...最大 24 時間

バックオフ中のキーは `/api/judge` の試行対象から外れ、次のキーへフォールバックする。
全キーがバックオフ中の場合、`/readyz` は 503 を返す。

**Firestore による永続化**: バックオフ状態は `rate_limits` コレクションに保存される
（キーごとに `timestamp` / `consecutive_count` / `backoff_seconds`）。これにより
Cloud Run の再起動・スケールアウトをまたいでも状態が復元され、無駄な再試行と 429 の
再発を防ぐ。Firestore に到達できない場合はインスタンス内メモリへ自動フォールバックし、
判定自体は継続する（機能低下のみで停止しない）。

Firestore のセットアップ（データベース作成・IAM）は
[deploy_memo/gcp-setup.md](../deploy_memo/gcp-setup.md) を参照。

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
