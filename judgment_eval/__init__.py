"""判定精度の評価ハーネス。

`scripts/run_judgment_eval.py` が実際に Gemini を呼び、ここのモジュールが
ケースの読み込み(`cases`)と集計・レポート生成(`summary`)を担う。集計は純粋関数に
してあるので、ネットワークなしで `python -m unittest` から検証できる。
"""
