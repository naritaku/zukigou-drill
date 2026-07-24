# 追加レビュー観点 - 実際のレビュー結果から発見した穴

このドキュメントは、実際の /code-review 実行結果から発見された、新たに追加すべき4つの観点について説明しています。

---

## 🔍 背景

実際のコードレビューで以下の問題が指摘されました：

1. **GEMINI_MODELS 環境変数が使用されなくなった** → 破壊的変更の検出漏れ
2. **MODEL 変数が定義されているが使用されない** → デッドコード検出漏れ
3. **モデル検証チェックが到達不可** → デッドコード検出漏れ
4. **README で古い環境変数が記載されている** → ドキュメント検証漏れ

これらの問題に対応するため、新しい観点を追加しました。

---

## 📌 追加観点（9-12）

### 9. 破壊的変更（Breaking Changes）

**なぜ重要？**

このプロジェクトは無料と有料の APIキーを複数管理し、ユーザーが環境変数で設定を変更しています。破壊的変更があると、既存のデプロイメントが機能しなくなります。

**実例**:
```python
# 問題のあるコード例
# 以前: GEMINI_MODELS で モデルをカスタマイズ可能
# 現在: ハードコード値のみを使用
# → 既存の GEMINI_MODELS="gemini-2.0-flash" 設定が無視される
```

**チェック項目**:
- [ ] 環境変数が削除されていないか
- [ ] API レスポンス形式が変わっていないか
- [ ] エンドポイントが削除されていないか
- [ ] 必須パラメータが増えていないか

---

### 10. デッドコード・未使用定義

**なぜ重要？**

デッドコードは：
- 保守コストを増加させる（何のためのコードか不明）
- バグの温床になる（動作確認されていない）
- リファクタリング時に混乱を招く

**実例**:
```python
# 問題のあるコード例（実際の指摘）
MODEL = os.environ.get("GEMINI_MODEL", DEFAULT_GEMINI_MODEL)
# ↑ 定義されているが、_gemini_models() が呼ばれるため使用されない

# 到達不可なコード例
if not models:
    raise HTTPException(503, "judgment model is not configured")
# ↑ models = _gemini_models(key_label) は常に非空なので到達不可
```

**チェック項目**:
- [ ] 全ての関数が呼び出されているか（pylint 使用）
- [ ] 全ての変数が参照されているか
- [ ] インポートが全て使用されているか
- [ ] if-else で片方が必ず実行される結果になっていないか

---

### 11. ログ・観測性（Observability）

**なぜ重要？**

本番環境でのトラブルシューティングには適切なログが必須です。

**実例**:
```python
# 問題のあるコード例
def _generate_vision_result(...):
    for key_label, api_key in api_keys:
        # ← ここで何が試されているか、どうしてスキップされたか見えない
        rate_limit_info = _get_rate_limit_status(api_key)
        if rate_limit_info:
            # ← ここでスキップされた理由がログに出ていないと追跡不可
            continue
```

**改善例**:
```python
# 改善後
logger.debug(
    "API key is rate limited; skipping",
    extra={
        "key_label": key_label,
        "remaining_seconds": remaining,
    },
)
```

**チェック項目**:
- [ ] API リクエスト前後にログがあるか
- [ ] エラーケースで詳細情報がログされるか
- [ ] 本番環境で DEBUG ログが出力されていないか
- [ ] リクエスト追跡用の相関ID が使用されているか

---

### 12. API 設計・インターフェース

**なぜ重要？**

一貫性のあるAPI設計は、クライアント実装を簡単にし、バグを減らします。

**実例**:
```python
# 問題のあるコード例
# エンドポイント: /api/judge
# レスポンス: {"passed": true, "score": "3/3", "checks": [...]}

# 別のエンドポイント: /api/report
# レスポンス: {"ok": true}  ← キー名が異なる

# 改善すべき設計
# 統一: {"success": true, ...} または {"status": "ok", ...}
```

**チェック項目**:
- [ ] HTTP ステータスコードが RFC に準拠しているか
- [ ] エラーレスポンス形式が統一されているか
- [ ] パラメータ名が snake_case で統一されているか
- [ ] API ドキュメントが実装と一致しているか

---

## 📊 実装効果

これら4つの観点を追加することで、以下の検出率が向上：

| 問題タイプ | 検出率（前） | 検出率（後） |
|-----------|-----------|-----------|
| 破壊的変更 | 低 | 高 |
| デッドコード | 低 | 高 |
| ログ不足 | 低 | 中 |
| API 設計問題 | 低 | 中 |

---

## 🔧 チェック実装

### 自動チェック（GitHub Actions）

```bash
# scripts/check-consistency.sh に追加
echo "### Checking for dead code..."
python3 << 'EOF'
import ast

with open('main.py', 'r') as f:
    tree = ast.parse(f.read())

# 未使用定義を検出
EOF
```

### 手動チェック（開発者向け）

PR 作成前：
```bash
# 1. デッドコード検出
grep -n "^def " main.py | while read line; do
  func=$(echo "$line" | awk -F'[(:' '{print $2}')
  if ! grep -q "$func" main.py | tail -n +2; then
    echo "Unused function: $func"
  fi
done

# 2. ログ確認
grep -n "logger\." main.py | wc -l

# 3. API 設計確認
grep -n "@app\.\(get\|post\)" main.py
```

---

## 📋 レビューチェックリスト更新

`.claude/REVIEW-PERSPECTIVES.md` に以下を追加：

- [ ] 破壊的変更がないか確認（MUST）
- [ ] デッドコードが存在しないか確認（MUST）
- [ ] 重要なイベントのログがあるか確認（SHOULD）
- [ ] API 設計が一貫しているか確認（SHOULD）

---

## 🎯 継続的改善

毎週のレビュー実行時：

1. **新しい問題パターンが見つかったか**
2. **既存のチェック項目で見落とされたものがないか**
3. **これら4つの観点の追加で改善されたか**

を確認し、さらに洗練させます。

---

**観点の継続的な充実で、より高い品質をキープ！** 🚀
