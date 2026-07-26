# 配線用図記号ドリル 📝

電気通信工事施工管理技士試験の設備系統図学習向け Web ドリル。スマホで指描きした JIS C 0303 記号を AI が判定し、フィードバックが得られる。

**ログイン不要・成績保存なし・URL 共有だけで利用可能**

Google AI Dojo Season 2 提出作品

---

## 🎯 特徴

### 判定の正確さ: コード + AI の組み合わせ

従来の LLM 判定のみでは判定がぶれる。本アプリは：

1. **ルーブリック定義**: 記号ごとに必須特徴・禁止特徴を `symbols.json` で定義
2. **AI の観察**: Gemini vision が各特徴を**独立した true/false で観察**（JSON Schema 固定・temperature 0）
3. **確定的な採点**: 必須特徴がすべて true、禁止特徴がすべて false の場合のみ合格

→ 不合格時に「どの特徴が不足しているか」が構造的に分かる

### インフラ

- **DB なし、状態なし** → スケーラブル
- **Cloud Run scale to zero** → コスト最小化
- **複数 API キー対応** → 無料枠と有料枠の自動フォールバック

---

## 🏗️ システムアーキテクチャ

![architecture](docs/architecture.svg)

```
[ブラウザ] landing.html / drill.html / standards.html (canvas 指描き)
     │  → PNG 正規化・トリミング・512px 化
     ▼
[Cloud Run] FastAPI ── [Secret Manager]  API キー（無料 → 有料）
     │  検証 + ルーブリック          └─ [Firestore] レート制限のバックオフを永続化
     ▼
[Gemini 3.1 Flash Lite]
     │  各特徴を true/false で観察
     ▼
[決定的採点] ─ ─ ─ ▶ [Cloud Storage] 判定ログ（任意・既定は無効）
```

---

## 🚀 クイックスタート

### Web で試す

デプロイ済みサービス: https://zukigou-drill-vnoxzmytga-an.a.run.app

<div style="margin: 16px 0; padding: 24px; background: #f3f4f6; border-radius: 4px; display: inline-block;">

![QR Code](https://api.qrserver.com/v1/create-qr-code/?size=200x200&margin=25&data=https://zukigou-drill-vnoxzmytga-an.a.run.app)

</div>

### ローカルで実行

```bash
pip install -r requirements.txt
GEMINI_API_KEY="your-free-key" uvicorn main:app --host 0.0.0.0 --port 8080
```

詳細は [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) を参照

---

## 🛠️ 技術スタック

- **Frontend**: HTML5 Canvas + Vanilla JS
- **Backend**: FastAPI + Python
- **AI**: Gemini vision (free tier + paid tier fallback)
- **Infrastructure**: Cloud Run + Firestore + Cloud Storage（任意）
- **CI/CD**: GitHub Actions + Workload Identity Federation

---

## 📚 ドキュメント

- [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) — 開発・デプロイ手順
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — システム詳細設計
- [docs/requirements.md](docs/requirements.md) — 要件書

---

## 📊 収録範囲

電気通信工事施工管理技士 第二次検定の設備系統図に頻出の **9 カテゴリ・全 54 記号**：

| カテゴリ | 記号数 |
|---|---|
| テレビ共聴 | 13 |
| 共通・配線材料 | 10 |
| 電話設備 | 7 |
| 警報・呼出・表示 | 7 |
| インターホン | 5 |
| 放送設備 | 4 |
| LAN・情報設備 | 4 |
| 電気時計設備 | 3 |
| 映像設備 | 1 |

各記号は JIS C 0303:2000 規格票と照合済み。最新の一覧は [/standards](https://zukigou-drill-vnoxzmytga-an.a.run.app/standards) で確認できる。

---

## 🔒 プライバシー

- ユーザー認証・成績保存なし
- 判定画像は通常保存しない
- Gemini API 呼び出しのみ外部連携

---

## 📄 免責

本アプリの判定は練習支援用であり、図記号としての正しさを保証しない。最終的な正誤の確認は JIS C 0303:2000 規格票によること。
