# 配線用図記号ドリル

電気通信工事施工管理技士 第二次検定の設備系統図に登場する JIS C 0303「構内電気設備の配線用図記号」を、スマホの指描きで練習できる Web ドリル。手描きした記号を AI が特徴ごとに判定し、不合格ならお手本と並べて差分を確認できる。

Google AI Dojo Season 2 提出作品。ログイン不要・成績保存なし・URL 共有だけで使える。

## 設計思想: 判定はコード、観察のみ LLM

手描きスケッチの合否を LLM に丸投げすると判定がブレる。本アプリでは:

1. 記号ごとに、`required_features`（必須特徴）、`forbidden_features`（存在してはいけない特徴）、`confusable_symbols`（類似記号）を `symbols.json` に定義
2. Gemini vision は各項目を独立に **true/false** で観察する（JSON Schema 強制・temperature 0）
3. 合否・スコアはコード側で計算し、必須特徴が全て存在し、禁止特徴と類似記号判定が全て false の場合だけ合格とする

これにより、不合格時に「どの特徴が欠けているか」を特定したフィードバックが構造から得られる。

## アーキテクチャ

![architecture](docs/architecture.svg)

```
[ブラウザ] landing.html / drill.html(canvas 指描き)
     │  描画を白背景化→トリミング→512px に正規化して POST
     ▼
[Cloud Run] FastAPI (main.py)
     ├─ GET  /              ランディング
     ├─ GET  /drill         ドリル画面
     ├─ GET  /api/question  ランダム出題(verified のみ)
     └─ POST /api/judge     ルーブリック→Gemini 観察→決定的採点
                │
                ▼
         [Gemini 2.5 Flash vision]
```

DB なし。状態なし。scale to zero。

## ローカル実行

```bash
pip install -r requirements.txt
GEMINI_API_KEY="your-key" uvicorn main:app --host 0.0.0.0 --port 8080
# → http://localhost:8080
```

## デプロイ (Cloud Run)

デプロイは GitHub Actions の手動実行で行う。GitHub の **Actions → Deploy Cloud Run → Run workflow** を開き、`main` ブランチを選んで実行する。必要に応じて `gemini_model` と `max_instances` を入力する。

初回だけ、GitHub Actions から Google Cloud に認証するための事前設定が必要。Google Cloud 側で Secret Manager と Workload Identity Federation を設定し、GitHub リポジトリの **Settings → Secrets and variables → Actions → Variables** に次の Variables を登録する。

- `GCP_WORKLOAD_IDENTITY_PROVIDER`: Workload Identity Provider のリソース名
- `GCP_SERVICE_ACCOUNT`: デプロイ用サービスアカウントのメールアドレス

例:

```text
GCP_WORKLOAD_IDENTITY_PROVIDER=projects/123456789/locations/global/workloadIdentityPools/github/providers/github-repo
GCP_SERVICE_ACCOUNT=github-actions-deployer@zukigou-drill-dojo.iam.gserviceaccount.com
```

`GEMINI_API_KEY` は GitHub Secrets ではなく Google Cloud Secret Manager の `GEMINI_API_KEY` に登録しておく。workflow はこの Secret を Cloud Run の環境変数として参照する。

## 記号の追加方法

`symbols.json` にエントリを追加する。判定精度のため、次の3種類を分けて定義する。

- `required_features`: 対象記号に必ず存在する構造。本数、接続、貫通、内外、方向、塗りつぶし、文字を個別の文に分ける。
- `forbidden_features`: 対象記号には存在してはいけない構造。類似記号との決定的な差分を優先して書く。
- `confusable_symbols`: 誤認しやすい記号名。Geminiに対象記号との二者択一ではなく、類似記号の決定的特徴が見えるかを独立判定させる。

各チェックは「画像を見て true/false で答えられる文」にし、「きれい」「十分」「それらしい」のような主観語は避ける。線の歪みは許容し、本数・接続関係・貫通・塗り・文字など識別に必要な位相を厳密にする。

参考書・規格票で形状を確認できたものだけ `"verified": true` にする。出題対象は verified のみ。`ref_svg` は不合格時の比較表示に利用する。

## 収録範囲

第二次検定 設問 2 の出題実績(設備系統図中の JIS 記号の名称・機能を問う形式)に基づき、電話設備・インターホン・テレビ共聴・LAN 情報・放送設備の 5 カテゴリから選定。

## docs/

- `requirements.md` — 要件ドキュメント(確定要件・スコープ・スケジュール)
- `stitch-prompts.md` — UI モック生成に使った Stitch プロンプト

## 判定改善のための匿名フィードバック保存

環境変数 `FEEDBACK_BUCKET` に GCS バケット名を設定すると、**ユーザーが「判定に納得できない」を押した場合だけ**、個人と紐づかない形で `disputed/` プレフィックスに保存する。通常の判定画像と判定結果は保存しない。

- 保存するもの: 画像 / symbol_id / 判定結果 / 日付(日単位)
- 保存しないもの: IP・セッション情報・秒精度の時刻・ユーザー識別子
- バケットにはライフサイクルルール(例: 90 日で自動削除)の設定を推奨
- 未設定(既定)では一切保存しない

```bash
# 有効化の例
gcloud storage buckets create gs://zukigou-feedback --location=asia-northeast1
echo '{"rule":[{"action":{"type":"Delete"},"condition":{"age":90}}]}' > /tmp/lc.json
gcloud storage buckets update gs://zukigou-feedback --lifecycle-file=/tmp/lc.json
gcloud run services update zukigou-drill --set-env-vars FEEDBACK_BUCKET=zukigou-feedback
```

## 公開運用時の保護

- APIはPNG形式と最大サイズをサーバー側で検証します。
- Geminiの応答はJSON SchemaとPydantic strict modeで検証します。
- 判定画像は通常保存しません。`FEEDBACK_BUCKET` 設定時も、「判定に納得できない」の明示報告だけを匿名保存します。
- 組み込みレート制限は単一インスタンス向けの補助機能です。一般公開では Cloud Armor、Cloud Run の `max-instances`、Gemini API quotaを併用してください。

主な環境変数:

```text
GEMINI_API_KEY          必須。Gemini APIキー
GEMINI_MODEL            既定: gemini-2.5-flash
FEEDBACK_BUCKET         任意。異議報告の保存先GCSバケット
MAX_IMAGE_BYTES         既定: 1000000
MAX_IMAGE_B64_CHARS     既定: 1500000
RATE_LIMIT              既定: 20
RATE_WINDOW             既定: 60秒
```

ヘルスチェック:

- `GET /healthz`: プロセス生存確認
- `GET /readyz`: 問題データとGemini設定の準備確認

## 免責

本アプリの判定は Gemini による練習支援であり、図記号としての正しさを保証するものではない。ルーブリックは JIS C 0303:2000 を参照して作成しているが、最終的な正誤の確認は規格票によること。
