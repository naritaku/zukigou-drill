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

### 安く・早く・簡単に判定できる

<div class="columns">
<div>

**指描き → 即座に判定**
- スマートフォン Canvas で描画
- PNG に正規化
- リアルタイム判定＆フィードバック

**「どこが間違ったか」が明確**
- 不合格時に理由を構造的に説明
- 「必須特徴が不足」「禁止特徴あり」が分かる

</div>
<div>

**判定の正確さを確保**
- LLM のみでは判定がぶれる
- ルーブリック定義 + AI 観察 + コード採点
- 3 層構造で再現性を確保

</div>
</div>

---

# デモ：その場でお試しください

<div class="columns">
<div>

### 体験の流れ（30 秒）

1. 記号を選ぶ
2. 指で描く
3. 「判定」をタップ
4. 合否と「どこが違うか」が即表示

**ログイン不要・インストール不要**

いま QR コードからアクセスできます →

</div>
<div style="display: flex; align-items: center; justify-content: center;">

<div style="padding: 24px; background: #ffffff; border-radius: 4px;">

![w:250](https://api.qrserver.com/v1/create-qr-code/?size=250x250&margin=25&data=https://zukigou-drill-vnoxzmytga-an.a.run.app)

</div>

</div>
</div>

---

# 確定的採点エンジン

観察（AI）と 採点（コード）を分離することで、再現性のある合否判定を実現。

```text
① ルーブリック定義  (symbols.json)
    必須特徴 : 「二重円か？」「フック付きか？」
    禁止特徴 : 「塗りつぶしはないか？」
    類似記号 : 「他記号と区別できるか？」

② Gemini Flash Lite で観察  (Temperature 0 / JSON Schema 固定)
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

![w:1080](architecture.svg)

3 コンポーネントのみ。観察は AI、採点はコード、状態はレート制限だけ。

---

# インフラ：スケーラブルかつ低コスト

<div class="columns">
<div>

### 成績データを保存しない設計
- 1 リクエスト = 1 判定の独立処理
- Cloud Run が **scale to zero**
- 無負荷時のコストはほぼゼロ
- 状態は右記のレート制限のみ

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

# コスト試算：月 1000 判定での概算

- **Cloud Run**: リクエスト課金 ≈ **$0.02/月**（scale to zero）
- **Gemini API**: 無料枠 1 日 100 回（月 3000 回相当）→ **$0**
- **Firestore**: 無料枠（読み取り 5 万回/日）内 → **$0**
- **合計**: **実質 月 $1 未満**

→ 無料枠を超えても有料キーへ自動フォールバック。スケールしても限界費用は数ドル。

---

# 技術スタック

| レイヤー | 採用技術 |
|---|---|
| **フロントエンド** | HTML5 Canvas + Vanilla JavaScript |
| **バックエンド** | FastAPI + Python |
| **AI** | Gemini API（Flash Lite / 画像認識） |
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

# クイックスタート

Web で試す: https://zukigou-drill-vnoxzmytga-an.a.run.app

ローカルで実行:

```bash
pip install -r requirements.txt
GEMINI_API_KEY="your-free-key" uvicorn main:app --host 0.0.0.0 --port 8080
```

詳細は [DEVELOPMENT.md](DEVELOPMENT.md) を参照

---

# まとめ

配線用図記号ドリル

- **課題**: 参考書を持ち歩かず、スマホで手を動かして覚えたい
- **解法**: AI の観察とコードの採点を分離した確定的判定
- **成果**: JIS C 0303 記号 26 個に対応、実質 $0 で運用中
- **いま試せます**: ログイン不要・URL 共有だけで利用可能

---

# リンク

**デモサイト**: https://zukigou-drill-vnoxzmytga-an.a.run.app

<div style="margin: 24px auto; padding: 32px; background: #eef2fa; border-radius: 4px; display: flex; justify-content: center; align-items: center;">

![QR Code](https://api.qrserver.com/v1/create-qr-code/?size=250x250&margin=25&data=https://zukigou-drill-vnoxzmytga-an.a.run.app)

</div>

- GitHub: https://github.com/naritaku/zukigou-drill
- ドキュメント: [DEVELOPMENT.md](DEVELOPMENT.md) / [ARCHITECTURE.md](ARCHITECTURE.md) / [requirements.md](requirements.md)
