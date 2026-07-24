---
marp: true
theme: default
paginate: true
footer: "配線用図記号ドリル | Google AI Dojo Season 2"
style: |
  section {
    background: #f3f4f6;
    color: #1a1c1e;
    font-family: "Noto Sans JP", "Hiragino Kaku Gothic ProN", sans-serif;
    font-size: 26px;
    line-height: 1.6;
    padding: 60px 70px;
  }
  section h1 {
    font-size: 2.4em;
    margin-bottom: 0.4em;
    line-height: 1.25;
    color: #003e89;
  }
  section h2 {
    font-size: 1.5em;
    margin-bottom: 0.6em;
    color: #003e89;
    border-bottom: 2px solid #003e89;
    padding-bottom: 0.3em;
  }
  section h3 {
    font-size: 1.05em;
    color: #003e89;
    margin-bottom: 0.3em;
    font-weight: 700;
  }
  strong { color: #003e89; font-weight: 700; }
  code {
    background-color: #eef2fa;
    color: #003e89;
    padding: 2px 8px;
    border-radius: 2px;
    font-size: 0.9em;
    font-family: "JetBrains Mono", ui-monospace, monospace;
  }
  pre {
    background-color: #eef2fa;
    color: #1a1c1e;
    border-left: 3px solid #003e89;
    border-radius: 2px;
    font-size: 0.72em;
    line-height: 1.5;
    padding: 12px;
  }
  ul li, ol li { margin-bottom: 0.35em; }
  table {
    font-size: 0.85em;
    border-collapse: collapse;
  }
  table th {
    background-color: #eef2fa;
    color: #003e89;
    padding: 8px 14px;
    border-bottom: 2px solid #003e89;
    font-weight: 700;
  }
  table td { 
    padding: 8px 14px;
    border-bottom: 1px solid #dfe3e8;
  }
  .columns {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 2.4em;
  }
  .lead {
    display: flex;
    flex-direction: column;
    justify-content: center;
    text-align: center;
  }
  footer { color: #5a6270; font-size: 14px; }
---

# 配線用図記号ドリル

電気通信工事施工管理技士試験の設備系統図学習向け Web ドリル。スマホで指描きした JIS C 0303 記号を AI が判定し、フィードバックが得られる。

**ログイン不要・成績保存なし・URL 共有だけで利用可能**

[Google AI Dojo Season 2](https://developers-jp.googleblog.com/2026/06/google-ai-dojo-ai.html) 提出作品

powered by [@naritaku](https://github.com/naritaku/)

---

# 課題

試験前に効率よく記号を覚えたいけど…

- 分厚い参考書は持ち運びたくない
- スマホで手を動かして、リアルタイムにフィードバックがほしい
- 間違いを即座に指摘してもらい、その場で修正したい

→ 24/7、自分のペースで練習できる学習相手がいたら。

---

# ソリューション：本アプリの特徴

### 判定の正確さ: コード + AI の組み合わせ

従来の LLM 判定のみでは判定がぶれる。本アプリは：

1. **ルーブリック定義**: 記号ごとに必須特徴・禁止特徴・類似記号を `symbols.json` で定義
2. **AI の観察**: Gemini vision が各特徴を**独立した true/false で観察**（JSON Schema 固定・temperature 0）
3. **確定的な採点**: 必須特徴がすべて満たされ、禁止特徴・類似記号がすべて false の場合のみ合格

→ 不合格時に「どの特徴が不足しているか」が構造的に分かる

---

# 確定的採点エンジン

観察（AI）と 採点（コード）を分離することで、再現性のある合否判定を実現。

```text
① ルーブリック定義  (symbols.json)
    必須特徴 : 「二重円か？」「フック付きか？」
    禁止特徴 : 「塗りつぶしはないか？」
    類似記号 : 「他記号と区別できるか？」

② Gemini vision で観察  (Temperature 0 / JSON Schema 固定)
    各特徴を独立した true / false で判定
    → 同じ入力なら同じ出力

③ コードで確定的に採点
    必須特徴 = すべて true
    禁止特徴 = すべて false
    類似記号 = すべて false
    → 3 条件を満たせば「合格」
```

---

# システムアーキテクチャ

```text
[ スマートフォン ]
    HTML5 Canvas + JS で指描き
    画像を正規化・トリミング・PNG 化
        ↓
[ Cloud Run ] FastAPI + Python
    Rate Limiting (Firestore)
    ルーブリック採点エンジン
    Gemini API 呼び出し
        ↓
[ Gemini Flash Lite ]
    各特徴を true / false で観察
        ↓
[ JSON レスポンス ]
    { passed, score, checks, mistakes }
        ↓
[ ブラウザ UI ]
    合否・スコア・不足特徴を表示
```

---

# インフラ：スケーラブルかつ低コスト

<div class="columns">
<div>

### DB を持たないステートレス設計
- 1 リクエスト = 1 判定の独立処理
- Cloud Run が **scale to zero**
- 無負荷時のコストはほぼゼロ

### 複数 API キー対応
- 無料枠（1 日 100 回）を優先
- 上限時は有料枠へ **自動フォールバック**

</div>
<div>

### Firestore によるレート制限
- キーごとに **指数バックオフ** を管理
  （75 秒 → 最大 24 時間）
- Cloud Run 再起動後も状態を保持

### 任意：学習データ保存（オプトイン）
- 全判定ログ：30 日で自動削除
- 異議報告：90 日で自動削除

</div>
</div>

---

# 技術スタック

| レイヤー | 採用技術 |
|---|---|
| **フロントエンド** | HTML5 Canvas + Vanilla JavaScript |
| **バックエンド** | FastAPI + Python |
| **AI** | Google Gemini vision API |
| **インフラ** | Cloud Run + Cloud Storage + Firestore |
| **CI / CD** | GitHub Actions + Workload Identity Federation |
| **監視** | Cloud Logging + Cloud Monitoring |

---

# 収録範囲

電気通信工事施工管理技士 第二次検定の出題実績に基づく 5 カテゴリ：

- 電話設備 / インターホン / テレビ共聴 / LAN 情報 / 放送設備

各記号は JIS C 0303:2000 規格票で検証済み（現在 26 個、学習がてら随時拡張予定）

---

# プライバシー・セキュリティ

- ユーザー認証・成績保存なし
- 判定画像は通常保存しない
- Gemini API 呼び出しのみ外部連携
- ルーブリックは `symbols.json` で全公開
- JSON Schema + Pydantic strict mode で検証

⚠️ 免責：本アプリの判定は Gemini による練習支援であり、図記号としての正しさを保証しない。最終的な正誤の確認は規格票によること。

---

# 実装の工夫 ①：複数 Gemini キーの自動切替

```python
# キーの優先順位
#   1. GEMINI_API_KEY        最優先
#   2. GEMINI_API_KEYS       複数キーを順に試行
#   3. GEMINI_PAID_API_KEY   最後の手段（有料枠）

while True:
    try:
        return call_gemini(current_key, model)
    except QuotaExceededError:
        current_key = next_api_key()   # 次のキーへ
```

→ **無料枠が尽きても自動で有料枠にフォールバックし、サービスが止まらない。**

---

# 実装の工夫 ②：Firestore でのレート制限

```python
# キーごとに指数バックオフを管理
if rate_limited:
    backoff = calc_exponential_backoff(consecutive_failures)
    #   1 回目 :  75 秒
    #   2 回目 : 150 秒
    #   上限   :  24 時間
    store_in_firestore(key, backoff_until=now + backoff)

@app.on_event("startup")
def restore_rate_limits():
    for key, until in firestore.fetch_all_keys():
        apply_backoff(key, until)   # 再起動後も制限を復元
```

→ **インスタンス再起動をまたいでも制限状態が引き継がれる。**

---

# クイックスタート

Web で試す: https://zukigou-drill-dojo.run.app

ローカルで実行:

```bash
pip install -r requirements.txt
GEMINI_API_KEY="your-free-key" uvicorn main:app --host 0.0.0.0 --port 8080
```

詳細は [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) を参照

---

# 開発・運用

## GitHub Actions オートメーション

- **自動デプロイ** — Cloud Run へ本番デプロイ
- **スケーリング管理** — max_instances パラメータ化
- **Secret 管理** — Workload Identity Federation で安全化

---

# 提出のねらい

- 学習支援の現場化 — 実際の試験対策に直結
- Gemini vision の新用途 — 医療・工業以外での活用例
- 確定的 AI — LLM の不確実性を構造的に解決
- GCP ベストプラクティス — Firestore・Cloud Run・IAM の連携

## 今後の展開

- 他の技能試験対応 — 建築・電気・配管など
- モバイルアプリ化 — iOS/Android ネイティブ版
- 学習分析 — 学習者の弱点を可視化
- 学校向け管理画面 — 教育機関での活用

---

# コスト試算

## 月 1000 ユーザーでの概算

- **Cloud Run**: $0.000015/リクエスト × 1000 判定/月 = **$0.015/月**
- **Gemini API**: 無料枠 月 1500 リクエスト + 超過時は有料
- **Firestore**: 読み書き操作数による従量課金、見積 **$1～5/月**
- **合計**: **月 $5～10** の低コスト運用

→ **スケールしても限界費用が低い。**

---

# まとめ

配線用図記号ドリル

- **課題**: 試験対策の採点ループが遅い
- **解法**: AI と コード を分離した確定的採点
- **成果**: 26 個の JIS 記号対応、Firestore・複数キーで堅牢性確保
- **展開**: 他試験・ネイティブアプリへの横展開予定

---

# リンク

**デモサイト**: https://zukigou-drill-vnoxzmytga-an.a.run.app

<div style="margin: 24px auto; padding: 32px; background: #eef2fa; border-radius: 4px; display: flex; justify-content: center; align-items: center;">

![QR Code](https://api.qrserver.com/v1/create-qr-code/?size=250x250&margin=25&data=https://zukigou-drill-vnoxzmytga-an.a.run.app)

</div>

- GitHub: https://github.com/naritaku/zukigou-drill
- ドキュメント: [DEVELOPMENT.md](docs/DEVELOPMENT.md) / [ARCHITECTURE.md](docs/ARCHITECTURE.md) / [requirements.md](docs/requirements.md)
