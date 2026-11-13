# 構想（未実装・検証中）

ここに書くのは**実装済みではない**こと。実装が main にマージされ本番で動いたら、該当部分を `concept/` `design/` `collection/` `operations/` の文書に移し、ここからは削る。決定した方針は [decisions.md](../decisions.md) に日付つきで集約している。

| 構想 | 状態 | 出典 Issue / PR |
|---|---|---|
| [surf-feed.md](surf-feed.md) サーフモード・縦フィード・長い紹介文 | モックアップ（PR #54、未マージ） | #52 |
| [browser-and-thumbnails.md](browser-and-thumbnails.md) 画面内でサイトを開く・サムネイル・3D 町 | 実測と撮影の試験のみ。撮影の試験用スクリプトは実装済み（PR #56）。画面内パネル・R2・D1・カード統合は構想 | #41, #6 |
| [scope-and-search.md](scope-and-search.md) 国の範囲、エンドレススクロール、検索の拡張、PWA | 設計合意または調査前 | #42, #51, #31, #9, #10 |
| [requests.md](requests.md) 推薦・取り下げ・訂正の申請 | 方針合意、未実装 | #4, #29 |
| [exchange-and-deliberation.md](exchange-and-deliberation.md) 証券取引所の比喩、状態遷移、審議コーナー、初期発見の喜び、見たサイトの累計 | 設計案、未実装（PR なし） | #53, #55, #58, #2 |
| [llm-judging.md](llm-judging.md) LLM 判定工程 | 設計案、未実装 | #48 |

この他、共有サービス配下の個別ユーザー領域の扱い（#50）、官公庁のカテゴリ設計、継続巡回の運用（#2）が未決。
