#!/bin/bash
# GitHub Issue 検索・一覧表示ツール
# 使用方法: bash scripts/find-issues.sh [検索条件]

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

REPO=$(git remote get-url origin | sed 's/.*github.com[:/]\(.*\)\.git$/\1/')

echo "======================================"
echo "🔍 Issue 検索ツール"
echo "======================================"
echo "リポジトリ: $REPO"
echo ""

# 検索フィルタを定義
SEARCH_TYPE="${1:-open}"

case "$SEARCH_TYPE" in
  open)
    echo "📋 オープンな issue 一覧："
    gh issue list --repo "$REPO" --state open --limit 20
    ;;
  closed)
    echo "✅ クローズされた issue 一覧："
    gh issue list --repo "$REPO" --state closed --limit 20
    ;;
  all)
    echo "📝 全 issue 一覧："
    gh issue list --repo "$REPO" --limit 30
    ;;
  bug)
    echo "🐛 バグ issue："
    gh issue list --repo "$REPO" --state open --label bug --limit 20
    ;;
  feature)
    echo "✨ 機能リクエスト issue："
    gh issue list --repo "$REPO" --state open --label feature --limit 20
    ;;
  help)
    echo "🆘 ヘルプが必要な issue："
    gh issue list --repo "$REPO" --state open --label "help wanted" --limit 20
    ;;
  *)
    echo "使用方法:"
    echo "  bash scripts/find-issues.sh [検索タイプ]"
    echo ""
    echo "検索タイプ:"
    echo "  open       - オープンな issue（デフォルト）"
    echo "  closed     - クローズされた issue"
    echo "  all        - すべての issue"
    echo "  bug        - バグ関連"
    echo "  feature    - 機能リクエスト"
    echo "  help       - ヘルプが必要な issue"
    echo ""
    echo "例:"
    echo "  bash scripts/find-issues.sh open"
    echo "  bash scripts/find-issues.sh bug"
    exit 0
    ;;
esac

echo ""
echo "💡 詳細情報を表示:"
echo "  gh issue view <NUMBER>"
echo ""
