#!/bin/bash
# リポジトリ全体での矛盾検出ツール
# 環境変数、API定義、ドキュメントなどの整合性を確認

set -e

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

echo "======================================"
echo "🔍 矛盾検出ツール"
echo "======================================"
echo ""

CONSISTENCY_OK=true

# 1. 環境変数の定義一覧
echo "1️⃣  環境変数の整合性チェック..."
echo ""

# main.py で定義されている環境変数
MAIN_ENVVARS=$(grep -o 'os\.environ\.get("[^"]*"' main.py | cut -d'"' -f2 | sort -u)

# ドキュメントに記載されている環境変数
DOC_ENVVARS=$(grep -ho '\`[A-Z_]*\`' README.md deploy_memo/*.md 2>/dev/null | tr -d '`' | sort -u)

echo "📝 main.py で使用："
echo "$MAIN_ENVVARS" | head -10

echo ""
echo "📖 ドキュメントで説明："
echo "$DOC_ENVVARS" | head -10

echo ""

# 整合性チェック：main.py にある環境変数がドキュメントにあるか
echo "🔗 チェック: main.py の環境変数がドキュメント化されているか"
for var in $MAIN_ENVVARS; do
    if ! grep -q "^\`$var\`\|^## $var\|$var に" README.md deploy_memo/*.md 2>/dev/null; then
        echo "  ⚠️  $var: ドキュメント未記載"
        CONSISTENCY_OK=false
    fi
done

# 2. API エンドポイントの整合性
echo ""
echo "2️⃣  API エンドポイントの整合性チェック..."
echo ""

# main.py で定義されているエンドポイント
ENDPOINTS=$(grep -o '@app\.\(get\|post\)("[^"]*"' main.py | cut -d'"' -f2 | sort -u)

echo "📝 main.py で定義："
echo "$ENDPOINTS"

echo ""
echo "🔗 チェック: README に記載されているか"
for endpoint in $ENDPOINTS; do
    if ! grep -q "$endpoint" README.md 2>/dev/null; then
        echo "  ⚠️  $endpoint: README に記載なし"
        CONSISTENCY_OK=false
    fi
done

# 3. 関数/クラスの定義とドキュメントの矛盾
echo ""
echo "3️⃣  関数定義の矛盾チェック..."
echo ""

# main.py で定義されている関数
FUNCTIONS=$(grep -o '^def [a-z_]*(' main.py | cut -d' ' -f2 | cut -d'(' -f1 | sort -u)

echo "📝 main.py で定義："
echo "$FUNCTIONS" | head -15

# 4. テストの整合性
echo ""
echo "4️⃣  テストの整合性チェック..."
echo ""

if [ -f test_main.py ]; then
    TEST_CLASSES=$(grep -o 'class Test[a-zA-Z]*' test_main.py | cut -d' ' -f2 | sort -u)
    echo "📝 test_main.py でテストされているクラス："
    echo "$TEST_CLASSES"

    echo ""
    echo "🔗 チェック: すべてのエンドポイントがテストされているか"
    for endpoint in $ENDPOINTS; do
        endpoint_name=$(echo "$endpoint" | sed 's|/|_|g')
        if ! grep -q "$endpoint" test_main.py; then
            echo "  ⚠️  $endpoint: テストなし"
            CONSISTENCY_OK=false
        fi
    done
fi

# 5. ドキュメント間の矛盾
echo ""
echo "5️⃣  ドキュメント間の矛盾チェック..."
echo ""

# README と deploy_memo の環境変数設定が同じか
if [ -f deploy_memo/gcp-setup.md ]; then
    README_BUCKET=$(grep -o 'BUCKET=.*' README.md | head -1)
    DEPLOY_BUCKET=$(grep -o 'BUCKET=.*' deploy_memo/gcp-setup.md | head -1)

    if [ "$README_BUCKET" != "$DEPLOY_BUCKET" ]; then
        echo "  ⚠️  バケット設定が異なる"
        echo "    README: $README_BUCKET"
        echo "    deploy_memo: $DEPLOY_BUCKET"
        CONSISTENCY_OK=false
    fi
fi

# 6. バージョン情報の整合性
echo ""
echo "6️⃣  バージョン情報の整合性チェック..."
echo ""

if grep -q "gemini-3.1" main.py && ! grep -q "gemini-3.1" README.md; then
    echo "  ⚠️  main.py では gemini-3.1 を使用しているが、README に記載なし"
    CONSISTENCY_OK=false
fi

# 結果表示
echo ""
echo "======================================"
if [ "$CONSISTENCY_OK" = true ]; then
    echo "✅ すべての矛盾チェック合格"
    echo "======================================"
    exit 0
else
    echo "⚠️  矛盾が検出されました"
    echo "======================================"
    echo ""
    echo "修正が必要な項目:"
    echo "- ドキュメントの更新"
    echo "- テストの追加"
    echo "- 環境変数の確認"
    echo ""
    exit 1
fi
