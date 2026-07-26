#!/bin/bash
# Cloud Run デプロイに必要な GCP 権限と、Gemini の課金上限を設定する。
# 使用方法: bash scripts/setup-gcp-guardrails.sh [runtime-sa|secrets|quota|verify|all]
#
# 何度実行しても同じ状態になる（既に設定済みの項目はそのまま）。
# 実行には gcloud のログインと、対象プロジェクトへの IAM 変更権限が必要。

set -euo pipefail

PROJECT_ID="${PROJECT_ID:-zukigou-drill-dojo}"
PROJECT_NUMBER="${PROJECT_NUMBER:-$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')}"

# Cloud Run のランタイム SA。既定の compute SA（roles/editor 付き）ではなく、
# 必要最小限のロールだけを持つ専用 SA を使う。
RUNTIME_SA_ID="${RUNTIME_SA_ID:-zukigou-drill-run}"
RUNTIME_SA="${RUNTIME_SA_ID}@${PROJECT_ID}.iam.gserviceaccount.com"
DEPLOYER_SA="github-actions-deployer@${PROJECT_ID}.iam.gserviceaccount.com"

# --source デプロイのビルドを実行する SA。これも既定では compute SA が使われる。
BUILD_SA_ID="${BUILD_SA_ID:-zukigou-drill-build}"
BUILD_SA="${BUILD_SA_ID}@${PROJECT_ID}.iam.gserviceaccount.com"
COMPUTE_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

# Cloud Run に渡すシークレット。ここに無いものはデプロイ時にスキップされる。
SECRETS=(GEMINI_API_KEY GEMINI_API_KEYS GEMINI_PAID_API_KEY)

# 1 日あたりのリクエスト上限をかけるモデルと値。
# main.py の既定の試行順（無料/有料ともに 3.1 Flash Lite → 3.5 Flash）に対応させる。
QUOTA_MODELS=(gemini-3.1-flash-lite gemini-3.5-flash)
QUOTA_LIMIT="${QUOTA_LIMIT:-1000}"

QUOTA_METRIC='generativelanguage.googleapis.com%2Fgenerate_content_paid_tier_2_requests'
QUOTA_LIMIT_PATH="projects/${PROJECT_NUMBER}/services/generativelanguage.googleapis.com/consumerQuotaMetrics/${QUOTA_METRIC}/limits/%2Fd%2Fmodel%2Fproject"

MODE="${1:-all}"

# ---------------------------------------------------------------------------
# 専用ランタイム SA
#
# GCP の既定では Cloud Run は compute SA で動き、その SA は roles/editor を持つ。
# アプリが必要なのは Firestore（レート制限の永続化）・ログ出力・シークレット参照
# だけなので、専用 SA を作って必要最小限に絞る。
#
# GCS への保存を有効にする場合は、対象バケットに objectCreator を個別に付ける
# （バケット単位。プロジェクト全体の storage 権限は不要）:
#   gcloud storage buckets add-iam-policy-binding gs://BUCKET \
#     --member "serviceAccount:${RUNTIME_SA}" --role roles/storage.objectCreator
# ---------------------------------------------------------------------------
setup_runtime_sa() {
  echo "▶ 専用ランタイム SA を用意 (${RUNTIME_SA})"
  if gcloud iam service-accounts describe "$RUNTIME_SA" --project "$PROJECT_ID" >/dev/null 2>&1; then
    echo "  - SA: 既存"
  else
    gcloud iam service-accounts create "$RUNTIME_SA_ID" \
      --project "$PROJECT_ID" \
      --display-name "zukigou-drill Cloud Run runtime" \
      --quiet >/dev/null
    echo "  - SA: 作成"
  fi

  # 作成直後の SA は IAM 側にまだ伝播しておらず、そのままロールを付けると
  # "Service account does not exist" で失敗する。見えるまで待つ。
  local i
  for i in $(seq 1 12); do
    if gcloud iam service-accounts describe "$RUNTIME_SA" --project "$PROJECT_ID" >/dev/null 2>&1; then
      break
    fi
    sleep 5
  done

  # datastore.user : Firestore のドキュメント読み書き（rate_limits/{key}）
  # logging.logWriter : アプリログの書き込み
  for role in roles/datastore.user roles/logging.logWriter; do
    for i in $(seq 1 6); do
      if gcloud projects add-iam-policy-binding "$PROJECT_ID" \
        --member "serviceAccount:${RUNTIME_SA}" \
        --role "$role" --condition=None --quiet >/dev/null 2>&1; then
        echo "  - ${role}: OK"
        break
      fi
      if [ "$i" -eq 6 ]; then
        echo "  - ${role}: 付与に失敗（伝播待ちの可能性。再実行してください）" >&2
        return 1
      fi
      sleep 5
    done
  done

  # デプロイ側がこの SA としてサービスを動かすための actAs
  if gcloud iam service-accounts describe "$DEPLOYER_SA" --project "$PROJECT_ID" >/dev/null 2>&1; then
    gcloud iam service-accounts add-iam-policy-binding "$RUNTIME_SA" \
      --project "$PROJECT_ID" \
      --member "serviceAccount:${DEPLOYER_SA}" \
      --role roles/iam.serviceAccountUser --quiet >/dev/null
    echo "  - deployer の actAs: OK"
  else
    echo "  - deployer SA が無いので actAs はスキップ"
  fi
}

# ---------------------------------------------------------------------------
# ビルド専用 SA
#
# `gcloud run deploy --source` のビルドも、既定では compute SA（roles/editor）で
# 実行される。ワークフローは --build-service-account でこの SA を明示する。
# ---------------------------------------------------------------------------
setup_build_sa() {
  echo "▶ ビルド専用 SA を用意 (${BUILD_SA})"
  if gcloud iam service-accounts describe "$BUILD_SA" --project "$PROJECT_ID" >/dev/null 2>&1; then
    echo "  - SA: 既存"
  else
    gcloud iam service-accounts create "$BUILD_SA_ID" \
      --project "$PROJECT_ID" \
      --display-name "zukigou-drill Cloud Build" \
      --quiet >/dev/null
    echo "  - SA: 作成"
  fi

  local i
  for i in $(seq 1 12); do
    if gcloud iam service-accounts describe "$BUILD_SA" --project "$PROJECT_ID" >/dev/null 2>&1; then
      break
    fi
    sleep 5
  done

  # cloudbuild.builds.builder : ビルド実行（ソース取得・ログ・イメージ push を含む）
  # artifactregistry.writer   : cloud-run-source-deploy リポジトリへの push
  # logging.logWriter         : ビルドログの書き込み
  for role in roles/cloudbuild.builds.builder roles/artifactregistry.writer roles/logging.logWriter; do
    for i in $(seq 1 6); do
      if gcloud projects add-iam-policy-binding "$PROJECT_ID" \
        --member "serviceAccount:${BUILD_SA}" \
        --role "$role" --condition=None --quiet >/dev/null 2>&1; then
        echo "  - ${role}: OK"
        break
      fi
      if [ "$i" -eq 6 ]; then
        echo "  - ${role}: 付与に失敗（伝播待ちの可能性。再実行してください）" >&2
        return 1
      fi
      sleep 5
    done
  done

  if gcloud iam service-accounts describe "$DEPLOYER_SA" --project "$PROJECT_ID" >/dev/null 2>&1; then
    gcloud iam service-accounts add-iam-policy-binding "$BUILD_SA" \
      --project "$PROJECT_ID" \
      --member "serviceAccount:${DEPLOYER_SA}" \
      --role roles/iam.serviceAccountUser --quiet >/dev/null
    echo "  - deployer の actAs: OK"
  fi
}

# ---------------------------------------------------------------------------
# 既定 compute SA の editor 剥がし
#
# GCP は既定の compute SA に roles/editor を付ける。ランタイムとビルドの両方を
# 専用 SA に移したあとであれば不要になる。**移行とデプロイ成功を確認してから**
# 実行する（先に外すとビルドが失敗する）。
# ---------------------------------------------------------------------------
harden_compute_sa() {
  echo "▶ 既定 compute SA から roles/editor を外す (${COMPUTE_SA})"

  local current_runtime
  current_runtime="$(gcloud run services describe zukigou-drill --project "$PROJECT_ID" \
    --region "${REGION:-asia-northeast1}" \
    --format='value(spec.template.spec.serviceAccountName)' 2>/dev/null || true)"
  if [ "$current_runtime" = "$COMPUTE_SA" ] || [ -z "$current_runtime" ]; then
    echo "  ! 稼働中サービスがまだ compute SA を使っています（${current_runtime:-取得失敗}）。" >&2
    echo "    runtime-sa と build-sa を適用し、デプロイ成功を確認してから実行してください。" >&2
    return 1
  fi
  echo "  - 稼働中のランタイム SA: ${current_runtime}（compute SA ではない）"

  if ! gcloud projects get-iam-policy "$PROJECT_ID" \
    --flatten="bindings[].members" \
    --filter="bindings.members:${COMPUTE_SA} AND bindings.role:roles/editor" \
    --format="value(bindings.role)" | grep -q editor; then
    echo "  - roles/editor: 既に付いていない"
    return 0
  fi

  gcloud projects remove-iam-policy-binding "$PROJECT_ID" \
    --member "serviceAccount:${COMPUTE_SA}" \
    --role roles/editor --quiet >/dev/null
  echo "  - roles/editor: 削除"
  echo "    ※ 次回のデプロイが通ることを確認してください。問題が出たら以下で戻せます:"
  echo "      gcloud projects add-iam-policy-binding $PROJECT_ID \\"
  echo "        --member serviceAccount:${COMPUTE_SA} --role roles/editor"
}

# ---------------------------------------------------------------------------
# シークレットの参照権
#
# シークレットを読むのは Cloud Run の「ランタイム SA」。使うシークレットごとに
# secretAccessor が必要で、無いとリビジョン作成が Permission denied on secret で
# 失敗する（デプロイのビルドは通るので、失敗が最後まで分からない）。
# ---------------------------------------------------------------------------
grant_secrets() {
  echo "▶ シークレットの参照権を付与 (ランタイム SA: ${RUNTIME_SA})"
  for name in "${SECRETS[@]}"; do
    if ! gcloud secrets describe "$name" --project "$PROJECT_ID" >/dev/null 2>&1; then
      echo "  - ${name}: Secret Manager に無いのでスキップ"
      continue
    fi
    gcloud secrets add-iam-policy-binding "$name" \
      --project "$PROJECT_ID" \
      --member "serviceAccount:${RUNTIME_SA}" \
      --role roles/secretmanager.secretAccessor \
      --quiet >/dev/null
    echo "  - ${name}: OK"
  done
}

# ---------------------------------------------------------------------------
# Gemini の 1 日あたりリクエスト上限
#
# Cloud Billing の「予算とアラート」は通知だけで課金を止めない。実際に止めるのは
# このクォータ上限。上限に達すると 429 が返り、アプリはそのキーをバックオフして
# 次のキーへ回し、最終的に 503 を返す（課金は増えない）。
#
# 注意: 上限はプロジェクト単位でモデルごと。鍵単位ではない。有料キーが別プロジェクト
# 発行なら、そのプロジェクトでも同じ設定が必要。
#
# force=true は 10% を超える引き下げを許可するため（既定値 350,000 → 1,000）。
# ---------------------------------------------------------------------------
set_quota() {
  echo "▶ Gemini の 1 日あたり上限を ${QUOTA_LIMIT} に設定"
  local token
  token="$(gcloud auth print-access-token)"
  for model in "${QUOTA_MODELS[@]}"; do
    local response
    response="$(curl -sS -X POST \
      -H "Authorization: Bearer ${token}" \
      -H "Content-Type: application/json" \
      "https://serviceusage.googleapis.com/v1beta1/${QUOTA_LIMIT_PATH}/consumerOverrides?force=true" \
      -d "{\"overrideValue\":\"${QUOTA_LIMIT}\",\"dimensions\":{\"model\":\"${model}\"}}")"
    if printf '%s' "$response" | grep -q '"error"'; then
      # 同じ値の override が既にあると ALREADY_EXISTS で返る（実害なし）。
      echo "  - ${model}: $(printf '%s' "$response" | python3 -c 'import sys,json;print(json.load(sys.stdin)["error"]["message"][:120])')"
    else
      echo "  - ${model}: override 作成を受理（反映まで数十秒）"
    fi
  done
}

# ---------------------------------------------------------------------------
# 反映確認
# ---------------------------------------------------------------------------
verify() {
  echo "▶ 現在の実効上限"
  local token
  token="$(gcloud auth print-access-token)"
  curl -sS -H "Authorization: Bearer ${token}" \
    "https://serviceusage.googleapis.com/v1beta1/projects/${PROJECT_NUMBER}/services/generativelanguage.googleapis.com/consumerQuotaMetrics/${QUOTA_METRIC}" \
    | python3 -c '
import sys, json
metric = json.load(sys.stdin)
for limit in metric.get("consumerQuotaLimits", []):
    unit = limit.get("unit", "")
    for bucket in limit.get("quotaBuckets", []):
        model = (bucket.get("dimensions") or {}).get("model", "")
        if not model.startswith("gemini-3."):
            continue
        override = (bucket.get("consumerOverride") or {}).get("overrideValue")
        mark = "  <- override" if override else ""
        default = bucket.get("defaultLimit")
        effective = bucket.get("effectiveLimit")
        print("  %-24s %-24s 既定 %s -> 実効 %s%s" % (unit, model, default, effective, mark))
'
  echo "▶ シークレットの参照権 (${RUNTIME_SA})"
  for name in "${SECRETS[@]}"; do
    if ! gcloud secrets describe "$name" --project "$PROJECT_ID" >/dev/null 2>&1; then
      echo "  - ${name}: 未作成"
    elif gcloud secrets get-iam-policy "$name" --project "$PROJECT_ID" --format=json \
      | grep -q "$RUNTIME_SA"; then
      echo "  - ${name}: 付与済み"
    else
      echo "  - ${name}: 未付与（デプロイが失敗する）"
    fi
  done

  echo "▶ ランタイム SA のプロジェクトロール"
  gcloud projects get-iam-policy "$PROJECT_ID" \
    --flatten="bindings[].members" \
    --filter="bindings.members:${RUNTIME_SA}" \
    --format="value(bindings.role)" | sed 's/^/  - /'

  echo "▶ ビルド SA のプロジェクトロール"
  gcloud projects get-iam-policy "$PROJECT_ID" \
    --flatten="bindings[].members" \
    --filter="bindings.members:${BUILD_SA}" \
    --format="value(bindings.role)" | sed 's/^/  - /'

  echo "▶ 既定 compute SA のプロジェクトロール（roles/editor が無いのが望ましい）"
  gcloud projects get-iam-policy "$PROJECT_ID" \
    --flatten="bindings[].members" \
    --filter="bindings.members:${COMPUTE_SA}" \
    --format="value(bindings.role)" | sed 's/^/  - /'

  echo "▶ 稼働中サービスのランタイム SA"
  gcloud run services describe zukigou-drill --project "$PROJECT_ID" \
    --region "${REGION:-asia-northeast1}" \
    --format="value(spec.template.spec.serviceAccountName)" | sed 's/^/  - /'
}

case "$MODE" in
  runtime-sa)       setup_runtime_sa ;;
  build-sa)         setup_build_sa ;;
  harden-compute-sa) harden_compute_sa ;;
  secrets)          grant_secrets ;;
  quota)            set_quota ;;
  verify)           verify ;;
  # all に harden-compute-sa は含めない。専用 SA でのデプロイ成功を確認したあとに
  # 明示的に実行する運用にする。
  all)              setup_runtime_sa; echo; setup_build_sa; echo; grant_secrets; echo; set_quota; echo; verify ;;
  *)                echo "使用方法: bash scripts/setup-gcp-guardrails.sh [runtime-sa|build-sa|harden-compute-sa|secrets|quota|verify|all]" >&2; exit 2 ;;
esac
