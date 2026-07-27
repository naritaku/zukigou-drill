# AGENTS.md

- 日本語で応答、PRの作成を行うこと。コミットは英語でもよし。

## 大まかなワークフロー

1. 人間-開発タスクをcodexで作る
2. AI-タスクの分析、実行計画、確認項目を立てる
3. AI-実装
4. AI-PR作成
5. 人間-PRの確認、マージ
6. 人間-1-5を繰り返した後、1日に一回デプロイし直し、動作確認。

## コミット前の検証

- サーバーサイド(Python)の変更
  - 変更箇所はTDDでテストを書いて、遠るようにしてください。
  - Python のロジックを変更した場合は、少なくとも次を実行してください。

    ```bash
    python -m unittest
    ```

- Jsonの変更
  - jsonとしてのルールに則れていること
  - 図記号と判断基準に矛盾がないこと
    - 図記号、説明を変更した場合は`standards.html`の該当する図記号と説明をスクショしてスクショを分析すること
  - `symbols.json`の`required_features`/`forbidden_features`/`ref_svg`を変更した場合
    - `python3 scripts/render_eval_cases.py --symbol <id>`でお手本ケースを再生成
    - `python3 scripts/run_judgment_eval.py --symbol <id>`で判定精度が落ちていないこと（[docs/EVALUATION.md](docs/EVALUATION.md)）

- html/cssの変更
  - 変更点をスクショして期待している見た目になっていること
    - スマホの縦画面/PCなどのでかい横画面どちらでも違和感がないこと
