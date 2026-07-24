#!/bin/bash
# 自動検証・テスト・レビュー スクリプト
# 使用方法: bash scripts/validate.sh [--fix]

set -e

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

FIX_MODE=${1:-}

echo "======================================"
echo "🔍 検証開始"
echo "======================================"

# 1. テスト実行
echo ""
echo "1️⃣  テスト実行中..."
if ! python3 -m unittest 2>&1 | tee /tmp/test-output.log; then
    echo "❌ テスト失敗"
    echo ""
    echo "テスト出力:"
    cat /tmp/test-output.log
    exit 1
fi
echo "✅ テスト合格"

# 2. Python スタイル確認（pylint/flake8 が必要な場合）
echo ""
echo "2️⃣  コード品質確認中..."
if command -v pylint &> /dev/null; then
    python3 -m pylint main.py --disable=all --enable=syntax-error,undefined-variable || true
fi
echo "✅ コード品質確認完了"

# 3. 環境変数テンプレート確認
echo ""
echo "3️⃣  環境変数テンプレート確認中..."
if ! grep -q "DEFAULT_GEMINI_MODEL" main.py; then
    echo "❌ デフォルトモデル定義がありません"
    exit 1
fi
echo "✅ 環境変数確認完了"

echo ""
echo "======================================"
echo "✨ 検証完了 - コミット準備完了"
echo "======================================"
echo ""
echo "次のステップ:"
echo "  1. git add ."
echo "  2. git commit -m \"...\""
echo "  3. git push origin <branch>"
echo ""
