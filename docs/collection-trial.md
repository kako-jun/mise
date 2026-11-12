# 小規模なサイト収集の試行

2026-11-12 に、固定した2つの公開サイトのトップページを取得した。`scripts/collect-sample.py` は robots.txt を確認した後に各トップページを取得し、タイトルと meta description を `data/collection-sample.json` に記録する。アクセス間に待機を入れ、リンクをたどらない。robots.txt が 404/410 の場合は規則なしとして扱い、それ以外の取得失敗、リダイレクト、noindex、サイズ超過は取得・登録を中止する。取得結果とUTC時刻はJSONを参照。

2件の紹介文とカテゴリは収集結果を元に作成した試作用の案であり、自動公開の承認済みデータではない。`migrations/0002_collected_examples.sql` は試作用のD1に登録する。エージェントによる定期収集・自動分類・紹介生成・重複判定・審査フローは未実装。

再実行: `uv run python3 scripts/collect-sample.py > data/collection-sample.json`。実行するたび実サイトへアクセスする。試行用の固定シード限定であり、このスクリプトを任意URLの大量収集や定期巡回に使用しない。
