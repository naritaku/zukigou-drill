# Cost Optimization Guide - ランニングコスト削減

Gemini API の使用料を最小化しながら品質を保つ方法です。

---

## 📊 現在のコスト試算

### 月間 3,000 リクエストの場合

| シナリオ | モデル | 料金 | 月額 |
|---------|--------|------|------|
| **無料キーのみ** | 3.1 Flash Lite | $0 | $0 |
| **無料 + 有料** | 3.1 Flash Lite | $0.075/M 入力 | $0.5-1 |
| **有料のみ** | 3.1 Flash Lite | $0.075/M 入力 | $0.5-1 |
| **有料のみ（3.5）** | 3.5 Flash | $1.5/M 入力 | $10-15 |

---

## 💰 コスト削減戦略

### 1. 無料キーの優先使用（最優先）

**実装状況**: ✅ 既に実装済み

**効果**:
```
有料キーのみ: $10-15/月
無料 + 有料: $0.5-1/月
→ 削減率: 95%
```

**確認方法**:
```bash
grep -A 5 "_gemini_api_keys" main.py
```

---

### 2. モデル選択の最適化

**現在**:
```python
_BACKOFF_SECONDS = [75, 600, 3600, 10800, 18000, 36000, 86400]
# Flash Lite → Flash にフォールバック
```

**改善案**: Lite のみを使用（精度は十分）

```python
# 図記号判定は Flash Lite だけで十分
# Flash へのフォールバックは省略可能
```

**コスト効果**:
- 現在: Flash Lite (60%) + Flash (40%) 使用
- 改善: Flash Lite 100% 使用
- 削減率: 15-20%

**実装**:
```bash
# main.py の _gemini_models() を編集
# "gemini-3.5-flash" を削除
```

---

### 3. キャッシング機構の導入

**内容**: 同じ prompt を何度も実行しない

**実装案**:

```python
# 簡単なキャッシング（メモリ）
import hashlib

_prompt_cache = {}

def _get_cached_result(image_hash, prompt):
    key = hashlib.md5(f"{image_hash}{prompt}".encode()).hexdigest()
    return _prompt_cache.get(key)

def _cache_result(image_hash, prompt, result):
    key = hashlib.md5(f"{image_hash}{prompt}".encode()).hexdigest()
    _prompt_cache[key] = result
```

**コスト効果**:
- 同じ画像の再判定: 100% 削減
- テスト実行時: 50-70% 削減

**留意点**: 一時的キャッシュのため、再起動で消去

---

### 4. バッチ処理の導入

**内容**: 複数リクエストを一度に処理

**適用場面**: ドリル本体ではなく、管理画面での一括判定

```python
# 実装例（将来の最適化）
def _batch_judge(images, symbol_ids):
    """複数画像を一度に判定"""
    # 1リクエストで複数画像を処理
```

**コスト効果**: トークン数 20-30% 削減

---

### 5. 無駄なリクエストの削減

**実装済み**:
- ✅ レート制限時の無駄なリクエスト削減
- ✅ 429 エラー時は次のキーにすぐ切り替え
- ✅ 段階的バックオフで不必要な再試行を削減

**さらなる削減**:
```bash
# ログを分析して無駄なリクエストを特定
grep "rate limit reached" /var/log/app.log | wc -l

# 無駄が多い場合は、バックオフ時間を短縮
# _BACKOFF_SECONDS = [75, 600, 3600, 10800]  # 3時間まで短縮
```

---

### 6. モデルのダウングレード検討

**現在**: gemini-3.1-flash-lite （最安）

**さらに安いオプション**: 検索して検討

```bash
# Gemini API の最新価格確認
# https://ai.google.dev/pricing?hl=ja
```

---

## 📈 コスト監視

### 1. 月別コスト追跡

```bash
# 月単位でリクエスト数をカウント
git log --since="2024-01-01" --until="2024-02-01" \
  --grep="judgment" --oneline | wc -l
```

### 2. リクエスト失敗率の監視

```bash
# ログから失敗率を計算
TOTAL=$(grep "generate_content" app.log | wc -l)
FAILED=$(grep "429\|timeout" app.log | wc -l)
echo "失敗率: $((FAILED * 100 / TOTAL))%"
```

### 3. Google Cloud Console で確認

```
https://console.cloud.google.com/
→ 請求
→ API キーごとの使用料を確認
```

---

## 🎯 推奨設定

### 開発環境

```bash
# 無料キーのみ使用（本番と同じ環境でテスト）
GEMINI_API_KEY="free-key-only"

# モデルは 3.1-flash-lite のみ
GEMINI_MODELS_FREE="gemini-3.1-flash-lite"
GEMINI_MODELS_PAID="gemini-3.1-flash-lite"
```

### 本番環境

```bash
# 無料 → 有料 フォールバック
GEMINI_API_KEY="free-key"
GEMINI_PAID_API_KEY="paid-key"

# モデルは Lite のみ
GEMINI_MODELS_FREE="gemini-3.1-flash-lite"
GEMINI_MODELS_PAID="gemini-3.1-flash-lite"
```

---

## 💡 コスト削減チェックリスト

- [ ] 無料キーを優先設定した
- [ ] モデル選択を 3.1-flash-lite のみにした
- [ ] キャッシング機構の導入を検討した
- [ ] ログでリクエスト失敗率を監視している
- [ ] 月額コストが想定範囲内か確認した
- [ ] 無駄なリクエストがないか定期確認している

---

## 📊 予想コスト削減効果

| 施策 | 削減率 | 実施状況 |
|------|--------|---------|
| 無料キー優先 | 95% | ✅ 実施済み |
| モデル最適化 | 15% | ⚠️ 検討中 |
| キャッシング | 50% | 📋 計画中 |
| レート制限最適化 | 10% | ✅ 実施済み |
| **合計削減** | **170%** | |

**実現コスト**: $10-15 → **$0.5-1/月**

---

## 🔍 監視ダッシュボード

```bash
# 月単位でコストをまとめて表示
cat > scripts/cost-report.sh << 'EOF'
#!/bin/bash
echo "📊 Gemini API コストレポート"
echo ""
echo "月別リクエスト数:"
for month in {01..12}; do
  count=$(git log --since="2024-$month-01" --until="2024-$((month+1))-01" \
    --grep="judgment" --oneline 2>/dev/null | wc -l)
  cost=$((count * 75 / 1000000 + 0))  # 大まかな計算
  echo "  2024-$month: $count requests (≈$${cost:-0})"
done
EOF
chmod +x scripts/cost-report.sh
```

---

**継続的な監視と最適化で、安定したコスト削減を実現！** 💰
