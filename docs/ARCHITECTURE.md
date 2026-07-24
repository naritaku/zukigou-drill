# システムアーキテクチャ

配線用図記号ドリルの詳細な技術設計。

## 全体構成

```
┌─────────────────────────────────────────────────────────────┐
│ ユーザー（スマートフォン）                                   │
└──────────────────────┬──────────────────────────────────────┘
                       │
        [HTML5 Canvas + JavaScript]
                       │
        ┌──────────────▼──────────────┐
        │ 画像処理                     │
        │ - 白背景化                   │
        │ - トリミング                 │
        │ - 正規化 (512px)             │
        │ - PNG エンコード              │
        └──────────────┬──────────────┘
                       │
                    POST /api/judge
                       │
        ┌──────────────▼────────────────────────┐
        │ Cloud Run (FastAPI)                  │
        │                                      │
        │ ┌──────────────────────────────────┐ │
        │ │ Rate Limiting (Firestore)        │ │
        │ │ - キーごとのバックオフ管理       │ │
        │ │ - 指数バックオフ (75s→24h)       │ │
        │ │ - クラウド再起動後も状態保持     │ │
        │ └──────────────────────────────────┘ │
        │                                      │
        │ ┌──────────────────────────────────┐ │
        │ │ ルーブリック判定エンジン          │ │
        │ │ - symbols.json ロード             │ │
        │ │ - 必須特徴・禁止特徴・類似判定    │ │
        │ └──────────────────────────────────┘ │
        │                                      │
        │ ┌──────────────────────────────────┐ │
        │ │ Gemini API 呼び出し               │ │
        │ │ - 複数キーフォールバック          │ │
        │ │ - 複数モデルの試行                │ │
        │ │ - JSON Schema 固定採点            │ │
        │ └──────────────────────────────────┘ │
        └──────────────┬────────────────────────┘
                       │
                    JSON
                    {
                      passed: bool,
                      score: float,
                      checks: {...},
                      mistakes: [...]
                    }
                       │
        ┌──────────────▼────────────────────┐
        │ Cloud Storage (GCS) - 任意         │
        │ - ALL_JUDGMENTS_BUCKET              │
        │   (全判定データ、30日で自動削除)   │
        │ - FEEDBACK_BUCKET                   │
        │   (異議報告のみ、90日で自動削除)   │
        └─────────────────────────────────────┘

                      JSON
                       │
        ┌──────────────▼──────────────┐
        │ ブラウザ UI レンダリング      │
        │ - 合否表示                   │
        │ - スコア表示                 │
        │ - 不足特徴の詳細表示         │
        │ - お手本との比較表示         │
        └──────────────────────────────┘
```

---

## API エンドポイント

### GET /

ランディングページ。記号カテゴリー一覧・練習開始。

**応答**: HTML

### GET /drill

練習画面。Canvas 指描き → /api/judge 送信。

**応答**: HTML

### GET /api/question

出題 API。`verified: true` のランダムな記号を返す。

**応答**:
```json
{
  "symbol_id": "tel-handset",
  "name": "加入電話機",
  "features": "二重円にフック付き...",
  "category": "電話設備",
  "similar": [...]
}
```

### POST /api/judge

採点 API。

**リクエスト**:
```json
{
  "image": "base64-encoded-png",
  "symbol_id": "tel-handset"
}
```

**レスポンス**:
```json
{
  "passed": true,
  "score": 0.95,
  "symbol_id": "tel-handset",
  "checks": {
    "double_circle": true,
    "hook_shape": true,
    "no_fill": true,
    "similar_single_circle": false
  },
  "mistakes": [],
  "observation": "..."
}
```

**エラーレスポンス**:
- 400: 画像形式/サイズ検証エラー
- 429: Rate limit（API キー一時停止、バックオフ中）
- 503: Gemini API 不可（全キー枯渇 or フォールバック試行中）

### GET /healthz

プロセス生存確認。常に 200 OK。

### GET /readyz

準備状態確認。

- 200: 全て OK
- 503: Gemini API キー設定なし or すべてのキーが rate limited 中

---

## AI 判定フロー

### 1. 入力画像の検証

```python
# PNG 形式チェック
# サイズチェック (MAX_IMAGE_BYTES)
# ピクセル数チェック (MAX_IMAGE_PIXELS)
# 解像度チェック (MAX_IMAGE_DIM)
# インク量チェック (MIN_INK_PIXELS)
#   → 白紙判定で API 呼び出しスキップ
```

### 2. ルーブリック構築

```python
symbol = symbols[symbol_id]  # symbols.json から読み込み
rubric = {
  "required_features": symbol["required_features"],  # 必須特徴（全て true 必須）
  "forbidden_features": symbol["forbidden_features"],  # 禁止特徴（全て false 必須）
  "confusable_symbols": symbol["confusable_symbols"]  # 類似記号判定
}
```

### 3. Gemini vision 呼び出し

**JSON Schema 強制**で各項目を独立判定：

```json
{
  "type": "object",
  "properties": {
    "double_circle": { "type": "boolean" },
    "hook_shape": { "type": "boolean" },
    "no_fill": { "type": "boolean" },
    "similar_single_circle": { "type": "boolean" }
  },
  "required": ["double_circle", "hook_shape", "no_fill", "similar_single_circle"]
}
```

**プロンプト例**:
```
画像を見て、以下の項目を true/false で答えてください：

required_features:
1. 二重円がある: true/false
2. フック形状がある: true/false
3. 塗りつぶしがない: true/false

forbidden_features:
4. 単一円である: true/false

confusable_symbols:
5. 単一円（類似記号の特徴）が見えるか: true/false
```

### 4. 採点ロジック

```python
def judge(image_b64, symbol_id):
  # 1. 画像検証
  if invalid(image): return 400
  
  # 2. キャッシュ確認 (Firestore)
  rate_limit_status = firestore.get(symbol_id)
  if rate_limit_status.is_backoff():
    return 429, backoff_seconds
  
  # 3. API キー試行順序
  for key in [GEMINI_API_KEY, GEMINI_API_KEYS, GEMINI_PAID_API_KEY]:
    for model in MODELS[key.tier]:
      try:
        observation = gemini.vision(image, rubric, model, key)
        checks = parse_schema(observation)  # JSON Schema チェック
        break
      except rate_limit:
        firestore.mark_rate_limited(key.label, backoff_seconds)
        continue
      except other_error:
        continue
  
  # 4. 採点決定
  passed = all(checks[f] for f in required_features) \
        and not any(checks[f] for f in forbidden_features)
  
  score = calculate_score(checks, required_features)
  
  # 5. GCS 保存（オプション）
  if ALL_JUDGMENTS_BUCKET:
    gcs.save_judgment(image, symbol_id, checks, score)
  
  return {
    "passed": passed,
    "score": score,
    "checks": checks,
    "mistakes": [f for f in required_features if not checks[f]],
    "observation": observation
  }
```

---

## API キーとモデルの管理

### 優先順位

1. **GEMINI_API_KEY** (無料枠)
   - モデル: `GEMINI_MODELS_FREE`
   - タイムアウト: 短い
   - リトライ: 積極的

2. **GEMINI_API_KEYS** (追加無料キー)
   - モデル: `GEMINI_MODELS_FREE`
   - 1 の枯渇時に試行

3. **GEMINI_PAID_API_KEY** (有料枠)
   - モデル: `GEMINI_MODELS_PAID`
   - 最後の手段

### バックオフ戦略

API キーが 429 (rate limit) を返すと、Firestore に以下を記録：

```json
{
  "key_label": "primary",
  "consecutive_failures": 3,
  "backoff_seconds": 600,
  "timestamp": "2025-01-20T12:34:56Z",
  "expires_at": "2025-01-20T12:44:56Z"
}
```

指数バックオフ: 75s → 600s → 3600s → 10800s → 18000s → 36000s → 86400s

バックオフ中の呼び出しは 429 を即座に返し、別のキーへ自動フォールバック。

---

## Firestore スキーマ

### `rate_limits` コレクション

各 API キーのバックオフ状態を永続化。Cloud Run 再起動後も状態が保持される。

```
rate_limits/{key_label}
├─ key_label (string): キーの識別子（"primary", "gemini-api-keys-0", "paid"）
├─ consecutive_failures (integer): 連続失敗回数
├─ backoff_seconds (integer): 現在のバックオフ秒数
├─ timestamp (timestamp): 最後の失敗時刻
└─ expires_at (timestamp): バックオフ終了予定時刻

例:
{
  "key_label": "primary",
  "consecutive_failures": 3,
  "backoff_seconds": 600,
  "timestamp": Timestamp(2025-01-20T12:00:00Z),
  "expires_at": Timestamp(2025-01-20T12:10:00Z)
}
```

---

## Cloud Storage スキーマ

### ALL_JUDGMENTS_BUCKET

**目的**: 全判定データの収集・分析

**保存パス**: `judgments/{date}/{symbol_id}/{uuid}.json`

```
{
  "symbol_id": "tel-handset",
  "passed": true,
  "score": 0.95,
  "checks": {...},
  "mistakes": [],
  "image_url": "gs://bucket/judgments/.../image.png",
  "date": "2025-01-20"
}
```

**ライフサイクル**: 30 日で自動削除

### FEEDBACK_BUCKET

**目的**: 異議報告データの収集（学習データ化）

**保存パス**: `disputed/{date}/{symbol_id}/{uuid}.json`

保存対象: ユーザーが「判定に納得できない」を押した場合のみ

**ライフサイクル**: 90 日で自動削除

---

## セキュリティ設計

### プライバシー保護

- ユーザー認証なし（ログイン機能なし）
- セッション追跡なし
- IP アドレス記録なし
- 成績保存なし

### 判定データの最小化

- 通常: 判定結果のみ返却、画像は保存しない
- 異議報告時のみ画像を保存（且つ 90 日で削除）

### API キーの保護

- キーはコード内に記載しない
- Google Cloud Secret Manager から環境変数経由でロード
- Workload Identity Federation で GitHub Actions との認証

### 入力検証

- PNG 形式必須
- サイズ・ピクセル数の上限チェック
- 白紙判定で無駄な API 呼び出し防止

### 出力検証

- Gemini API 応答は JSON Schema + Pydantic strict mode で検証
- 予期しない型は拒否

---

## スケーラビリティ

### Stateless 設計

- データベース接続なし（Firestore は読み書きのみ、永続的なセッション管理なし）
- セッション状態なし
- キャッシュ戦略なし（毎回 symbols.json をロード）

→ **Cloud Run scale to zero** が可能。使用量に応じた自動スケール。

### 同時実行性

- 複数インスタンスで安全に並行実行可能
- Firestore のアトミック操作でレート制限状態を一元管理

### コスト最適化

- **無料キー優先**: Gemini API 無料枠の 1500 req/分を活用
- **段階的フォールバック**: 無料枠枯渇後に有料キーへ移行
- **Cloud Run scale to zero**: 利用なしで月額 $0
- **GCS ライフサイクル**: 古いデータの自動削除

**想定コスト**:
- Gemini API: ~$0.50-1/月（無料枠メイン）
- Cloud Run: ~$0.10/月（scale to zero）
- Firestore: ~$0.05/月（レート制限状態管理のみ）
- **合計: $0.65-1.15/月**

---

## デプロイメント

### GitHub Actions ワークフロー

**トリガー**:
- PR 作成・更新
- 手動デプロイ
- スケジュール（3 時間ごと）

**ステップ**:
1. チェックアウト
2. Python 環境構築
3. 依存関係インストール
4. テスト実行
5. Cloud Run へデプロイ（Workload Identity Federation 認証）

### 環境変数の参照

```yaml
env:
  GEMINI_API_KEY: ${GCP_SECRET_GEMINI_API_KEY}
  ALL_JUDGMENTS_BUCKET: zukigou-all-judgments
```

---

## 監視・ロギング

### Cloud Logging

**標準ログ**:
- リクエスト: メソッド、パス、ステータス、レスポンスタイム
- エラー: スタックトレース、API エラー詳細
- レート制限: キー、バックオフ秒数、試行履歴

**ログレベル**:
- INFO: リクエスト処理
- WARN: フォールバック、バックオフ開始
- ERROR: API キー枯渇、処理失敗

### ヘルスチェック

```
GET /healthz → 200 (プロセス生存)
GET /readyz  → 200/503 (API キー設定・利用可能性)
```

Cloud Run の自動再起動トリガー: `/readyz` が 503 を 3 回連続で返す

---

## 今後の拡張可能性

- [ ] ユーザー成績追跡（別アプリ or 外部 DB）
- [ ] 複数言語対応
- [ ] モデル精度検証パイプライン
- [ ] A/B テスト (ルーブリック変更の影響測定)
- [ ] 記号の自動生成（Gemini でルーブリック提案）
