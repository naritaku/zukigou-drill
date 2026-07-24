---
marp: true
theme: default
paginate: true
footer: "配線用図記号ドリル | Google AI Dojo Season 2"
style: |
  section {
    background: linear-gradient(135deg, #1a2a6c 0%, #2c5364 100%);
    color: #f5f7fa;
    font-family: "Segoe UI", "Noto Sans JP", sans-serif;
    font-size: 26px;
    line-height: 1.6;
    padding: 60px 70px;
  }
  section h1 {
    font-size: 2.4em;
    margin-bottom: 0.4em;
    line-height: 1.25;
  }
  section h2 {
    font-size: 1.5em;
    margin-bottom: 0.6em;
    color: #7fdbff;
  }
  section h3 {
    font-size: 1.05em;
    color: #ffd479;
    margin-bottom: 0.3em;
  }
  strong { color: #7fdbff; }
  code {
    background-color: rgba(255, 255, 255, 0.14);
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 0.9em;
  }
  pre {
    background-color: rgba(0, 0, 0, 0.28);
    border-radius: 8px;
    font-size: 0.72em;
    line-height: 1.5;
  }
  ul li, ol li { margin-bottom: 0.35em; }
  table {
    font-size: 0.85em;
    border-collapse: collapse;
  }
  table th {
    background-color: rgba(127, 219, 255, 0.18);
    padding: 8px 14px;
  }
  table td { padding: 8px 14px; }
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
  footer { color: rgba(245, 247, 250, 0.6); font-size: 14px; }
---

<!-- _class: lead -->

# 配線用図記号ドリル

## AI が判定する設備系統図の学習 Web アプリ

電気通信工事施工管理技士 試験対策向け
**スマホ指描き → Gemini 判定 → 即フィードバック**

Google AI Dojo Season 2 提出作品

---

# 課題：従来の学習は「採点ループが遅い」

施工管理技士試験では **JIS C 0303** の設備系統図記号（電話・LAN・テレビ共聴など）を正確に描けることが求められる。しかし従来の学習には次の壁があった。

- **採点に人手が必要** — 記号の完全性チェックを自分で判断できない
- **フィードバックが遅い** — 実時間の指摘がなく、練習の反復が回らない
- **「なぜ不合格か」が曖昧** — 個別指導がないと改善点が分からない

→ 結果として **試験対策の学習効率が上がらない**。

---

# ソリューション：配線用図記号ドリル

<div class="columns">
<div>

## 学習者の体験

1. スマホを開く（**ログイン不要**）
2. 記号を指で描く
3. **数秒で判定 + フィードバック**
4. どの特徴が足りないかを把握
5. その場で反復練習

</div>
<div>

## 4 つの価値

- **即座なフィードバック**
  判定は秒単位で完了
- **客観的な採点**
  ルーブリックで基準を明文化
- **プライバシー重視**
  ログイン・成績保存なし
- **URL 共有だけで利用可**
  インストール不要

</div>
</div>

---

<!-- _class: lead -->

# 技術的コア

## LLM の「判定のぶれ」を
## コード側で封じ込める

---

# 従来 LLM 判定の問題と、本アプリの解法

<div class="columns">
<div>

## 従来 LLM 判定の課題

- 同じ画像でも **判定がぶれる**
- 「なぜ不合格か」が **不透明**
- 採点基準が **AI 任せ**

</div>
<div>

## 本アプリの方針

- 特徴ごとに **true / false で観察**
- 合否は **コードで確定的に計算**
- 基準は **`symbols.json` で公開**

</div>
</div>

観察（AI）と 採点（コード）を **分離** することで、再現性のある合否判定を実現する。

---

# 確定的採点エンジンの 3 ステップ

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

**判定（AI）と採点（コード）を分けることが再現性の鍵。**

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

# 対応記号：5 カテゴリ・26 個（現在実装）

電気通信工事施工管理技士試験の出題実績をベースに収録。

- **電話設備** — 加入電話機、交換機、接地極
- **テレビ共聴** — テレビジョンアンテナ、分配器
- **LAN・情報** — ネットワーク機器、コネクタ
- **放送設備** — FM ラジオ、受信機
- **インターホン** — ドア機器、内線

各記号は **JIS C 0303:2000 規格票** で検証済み。

→ **今後 100+ 記号への拡張を予定中**

---

# セキュリティとプライバシー

<div class="columns">
<div>

## ユーザーデータ保護

- **ログイン不要**
  個人情報を収集しない
- **成績保存なし**
  サーバー側にユーザー管理を持たない
- **判定画像は原則保存しない**
  保存はオプトインのみ

</div>
<div>

## 採点の信頼性

- **ルーブリックは全公開**
  `symbols.json` で誰でも確認可
- **JSON Schema 検証**
  AI 出力は固定スキーマで検証

</div>
</div>

> ⚠️ 免責：Gemini の判定は練習支援用。最終確認は規格票で行うこと。

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

# デモ：実際の操作フロー

<div class="columns">
<div>

1. **カテゴリ選択**
   記号カテゴリを選んで練習開始
2. **指描き**
   Canvas に記号を描く
3. **判定（秒単位）**

```text
✅ 合格！
スコア   : 95 点
不足特徴 : なし
```

</div>
<div>

4. **フィードバック**
   - 必須特徴の充足状況
   - 類似記号との比較
   - 具体的な改善点
5. **次の問題へ**
   URL 共有ですぐ再開

</div>
</div>

---

# 学習効果：ビフォー / アフター

| 観点 | 従来 | 本アプリ |
|---|---|---|
| 採点スピード | 数分〜数時間 | **数秒** |
| フィードバック | 「不合格」のみ | **不足特徴まで明示** |
| 学習者の負担 | 先生に見てもらう | **24 / 7 自動採点** |
| 対策効率 | 試行錯誤に時間 | **即座の改善ループ** |

---

# 開発・運用の自動化

<div class="columns">
<div>

## GitHub Actions

- **自動デプロイ** — Cloud Run へ本番反映
- **自動レビュー** — PR 時に敵対的レビュー
- **スケール管理** — `max_instances` をパラメータ化
- **Secret 管理** — Workload Identity Federation で鍵レス化

</div>
<div>

## ローカル開発

```bash
pip install -r requirements.txt
GEMINI_API_KEY="..." \
  uvicorn main:app
# http://localhost:8080
```

詳細は `docs/DEVELOPMENT.md` を参照。

</div>
</div>

---

# Google AI Dojo での位置づけ

<div class="columns">
<div>

## 提出のねらい

- **学習支援の実装** — 実際の試験対策に直結
- **vision の新用途** — 医療・工業以外での活用例
- **確定的 AI** — LLM の不確実性を構造で解決
- **GCP 連携** — Firestore・Cloud Run・IAM の実践

</div>
<div>

## 今後の展開

- **他試験へ横展開** — 建築・電気・配管など
- **ネイティブアプリ化** — iOS / Android
- **学習分析** — 弱点の可視化
- **教育機関向け管理画面**

</div>
</div>

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

<div class="lead">

## 配線用図記号ドリル

**課題** — 試験対策の採点ループが遅い

**解法** — AI と コード を分離した確定的採点 + 即時フィードバック

**成果** — 26 個の JIS 記号に対応、Firestore・複数キーで堅牢性を確保

**展開** — 他試験・ネイティブアプリへの横展開を予定

</div>

---

# 参考資料

- **デプロイ済みサイト**: https://zukigou-drill-dojo.run.app
- **GitHub リポジトリ**: https://github.com/naritaku/zukigou-drill
- **ドキュメント**:
  - [DEVELOPMENT.md](docs/DEVELOPMENT.md) — セットアップ・デプロイ
  - [ARCHITECTURE.md](docs/ARCHITECTURE.md) — 技術詳細
  - [requirements.md](docs/requirements.md) — 要件書
