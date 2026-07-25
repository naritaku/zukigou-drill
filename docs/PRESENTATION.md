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

![bg right:40% fit](bookToApp.png)

試験前に効率よく記号を覚えたいけど…

- 分厚い参考書は持ち運びたくない
- スマホで手を動かして、リアルタイムにフィードバックがほしい
- 間違いを即座に指摘してもらい、その場で修正したい

→ 24/7、自分のペースで練習できる学習相手がいたら。

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
<div style="display: flex; align-items: flex-end; justify-content: center;">

<div style="padding: 24px; background: #ffffff; border-radius: 4px;">

![w:250](https://api.qrserver.com/v1/create-qr-code/?size=500x500&margin=50&data=https://zukigou-drill-vnoxzmytga-an.a.run.app)

</div>

</div>
</div>

---

# スマホだけで安く・早く・簡単に

<div style="display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 1.2em; margin-top: 1.2em;">

<div style="background: #ffffff; border-top: 4px solid #003e89; border-radius: 4px; padding: 24px;">

### ✍️ 指で描いて即判定

Canvas に指描きするだけ。リアルタイムに合否フィードバック

</div>

<div style="background: #ffffff; border-top: 4px solid #003e89; border-radius: 4px; padding: 24px;">

### 🔍 間違いの理由が明確

「必須特徴が不足」「禁止特徴あり」を構造的に提示。その場で修正できる

</div>

<div style="background: #ffffff; border-top: 4px solid #003e89; border-radius: 4px; padding: 24px;">

### 🎯 判定がぶれない

LLM 単独では出力がぶれる。ルーブリック × AI 観察 × コード採点の 3 層で解決

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

![w:980](architecture.svg)

観察だけが Gemini、採点は Cloud Run 上のコード<br>Firestore は Geminiのレート制限管理に利用

---

# インフラ：スケーラブルかつ低コスト

<div class="columns">
<div>

### 成績データを保存しない設計
- 1 リクエスト = 1 判定の独立処理
- Cloud Run が **scale to zero**
- 無負荷時のコストはほぼ $0

<br>

### 複数 API キー対応
- **無料枠のキーから順に**試行
- 枯渇時は有料枠へ **自動フォールバック**

</div>
<div>

### Firestore でレート制限を管理
- 回復まで制限中のキーに**アクセスしない**
- **API 鍵ごと**に指数バックオフ
- **Cloud Run 再起動後も状態を維持**

<br>

### 判定を改善するデータ収集機能
- 収集機能はON/OFF切り替え可能
- **判定条件（ルーブリック）改善**の材料に

</div>
</div>

---

# コスト試算：判定数に比例するだけ

| サービス | 1 日 100 判定<br>（月 3,042） | 1 日 1,000 判定<br>（月 30,417） |
|---|---|---|
| **Gemini** 入力 1,575 + 出力 40 トークン/判定 | **$1.38** | **$13.80** |
| **Cloud Run** scale to zero | **$0.03** | **$0.31** |
| **Cloud Storage** 全判定ログ保存（任意）| **$0.03** | **$0.30** |
| **Firestore** レート制限のみ（無料枠内）| **$0** | **$0** |
| **合計** | **≈ $1.4 / 月** | **≈ $14.4 / 月** |

実コストはほぼ Gemini のみで、無料枠のキーで回す現状は実質 **$0**。

<div style="font-size: 0.7em; color: #5a6270; margin-top: 0.8em;">

出典: [Gemini API 料金](https://ai.google.dev/gemini-api/docs/pricing)（入力 $0.25 / 出力 $1.50 per 1M）、[Cloud 料金計算ツールの保存見積](https://cloud.google.com/products/calculator?dl=CjhDaVJpWmpFNVptTTVZeTA1WTJZMExUUXlNbVl0T1dReU9DMW1OemcwT0dVNU1EWXpOVElRQVE9PRASGiQ4NDREQzRFOS03QUQyLTQyMDAtODc2RC05M0M3NkFFQjk4Qzk)

</div>

---

# 技術スタック

| レイヤー | 採用技術 |
|---|---|
| **フロントエンド** | HTML5 Canvas + Vanilla JavaScript |
| **バックエンド** | FastAPI + Python |
| **AI** | Gemini API（Flash Lite / 画像認識） |
| **インフラ** | Cloud Run + Firestore + Cloud Storage（任意） |
| **CI / CD** | GitHub Actions + Workload Identity Federation |
| **監視** | Cloud Logging（Cloud Run 標準メトリクス） |

---

# 収録範囲

電気通信工事施工管理技士 第二次検定の設備系統図に頻出の 6 カテゴリ：

- テレビ共聴 8 / 共通・配線材料 6 / 電話設備 5 / LAN・情報設備 3 / 放送設備 2 / インターホン 2

全 26 記号を JIS C 0303:2000 の規格票と照合済み（学習がてら随時拡張予定）

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
- **成果**: JIS C 0303 記号 26 個に対応。現在は無料枠内で実質 $0、1,000 判定/日でも月 $15 未満
- **いま試せます**: ログイン不要・URL 共有だけで利用可能

---

# リンク

**デモサイト**: https://zukigou-drill-vnoxzmytga-an.a.run.app

<div style="margin: 24px auto; padding: 32px; background: #eef2fa; border-radius: 4px; display: flex; justify-content: center; align-items: center;">

![QR Code](https://api.qrserver.com/v1/create-qr-code/?size=250x250&margin=25&data=https://zukigou-drill-vnoxzmytga-an.a.run.app)

</div>

- GitHub: https://github.com/naritaku/zukigou-drill
- ドキュメント: [DEVELOPMENT.md](DEVELOPMENT.md) / [ARCHITECTURE.md](ARCHITECTURE.md) / [requirements.md](requirements.md)
