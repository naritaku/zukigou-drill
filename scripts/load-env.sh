#!/bin/bash
# .env.local ファイルをロードして環境変数を設定

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || dirname "$0"/..)"
ENV_FILE="$REPO_ROOT/.env.local"

if [ ! -f "$ENV_FILE" ]; then
    echo "❌ エラー: $ENV_FILE が見つかりません"
    echo ""
    echo "以下のコマンドで初期設定してください："
    echo "  1. Gemini API キーを取得: https://ai.google.dev/pricing?hl=ja"
    echo "  2. .env.local を作成:"
    echo "     export GEMINI_API_KEY='your-api-key'"
    exit 1
fi

# .env.local をロード
set -a
source "$ENV_FILE"
set +a

# 検証
if [ -z "$GEMINI_API_KEY" ]; then
    echo "❌ エラー: GEMINI_API_KEY が設定されていません"
    exit 1
fi

echo "✅ 環境変数をロードしました"
echo "   - GEMINI_API_KEY: $(echo $GEMINI_API_KEY | head -c 20)..."
[ -n "$GEMINI_PAID_API_KEY" ] && echo "   - GEMINI_PAID_API_KEY: 設定済み"
[ -n "$ALL_JUDGMENTS_BUCKET" ] && echo "   - ALL_JUDGMENTS_BUCKET: $ALL_JUDGMENTS_BUCKET"
[ -n "$FEEDBACK_BUCKET" ] && echo "   - FEEDBACK_BUCKET: $FEEDBACK_BUCKET"
