# mise ドキュメント索引

mise の設計・収集・運用・決定・構想の入口。ここから全ファイルを辿れる。リポジトリの使い方（ローカル起動・ライセンス）は [../README.md](../README.md)。

## 読み方の約束

- **正本は1か所**。同じ内容を複数の文書に書かない。他の文書は相互リンクで参照する。
- **「実装済み」と「構想」を分ける**。`concept/` `design/` `collection/` `operations/` は実装済み（または実際に運用している）ことだけを書く。未実装・検証中は [`ideas/`](ideas/README.md) に置く。
- **進捗の数字（件数・通過率）は書かない**。台帳（`data/`）・スクリプトの出力・Issue が真実。書く場合は「◯日時点の観測」と明記する。
- **kako-jun の方針は [decisions.md](decisions.md) に日付と出典 Issue 付きで集約**する。Issue は作業の記録、仕様の正本は docs。
- 生成物（`public/globe.js`、`public/regions.js`、台帳から生成する SQL）は派生物。直接編集しない。

## 構成

| パス | 内容 |
|---|---|
| [glossary.md](glossary.md) | 用語集（独立サイト、frontier、証券取引所の比喩、範囲 など） |
| [decisions.md](decisions.md) | 決定記録（日付・内容・出典 Issue） |
| **concept/** 何を作るか | |
| [concept/overview.md](concept/overview.md) | 目指す体験、世界観、マスコット、発見の機会の方針 |
| [concept/listing-criteria.md](concept/listing-criteria.md) | 載せる基準・拒否の理由・分類の方針 |
| **design/** 設計 | |
| [design/architecture.md](design/architecture.md) | 技術構成、配信、PWA、地球儀、国旗・区画と地球の連動 |
| [design/data-model.md](design/data-model.md) | データモデル（実装済みの列・将来案） |
| [design/ui.md](design/ui.md) | 画面の仕様（一覧の並び、ピックアップ、HUD、NEW、ファビコン、PWA） |
| [design/seo.md](design/seo.md) | SEO・クローラ向けの方針 |
| **collection/** 収集 | |
| [collection/discovery-policy.md](collection/discovery-policy.md) | 発見・確認の方針（robots、レビュー、定期処理の考え方） |
| [collection/pipeline.md](collection/pipeline.md) | 独立サイトの発見パイプライン（frontier 台帳・決定論フィルタ） |
| [collection/llm-judge.md](collection/llm-judge.md) | LLM 判定工程（Claude API、手元実行）。2段階・N件まとめ・予算ガード・使い方 |
| [collection/recrawl.md](collection/recrawl.md) | 登録済みサイトの再巡回（状態遷移・変更検出・要確認の候補リスト） |
| [collection/public-bodies.md](collection/public-bodies.md) | 官公庁・自治体の名簿（公式サイト台帳） |
| [collection/procedures.md](collection/procedures.md) | 恒常的な収集手順（スクリプトの再実行方法・書式） |
| [collection/thumbnails.md](collection/thumbnails.md) | サムネイル撮影の試験（設計・作法・結果。試験用スクリプトのみ実装済み） |
| [collection/trials.md](collection/trials.md) | 試行の記録（日付つきの観測。数字はここだけ） |
| **operations/** 運用 | |
| [operations/deploy.md](operations/deploy.md) | 本番環境、デプロイ、マイグレーション |
| [operations/requests.md](operations/requests.md) | 申請（推薦・取り下げ・訂正）の受付 API、レート制限、管理レビューの手順、個人情報の保持期間 |
| [operations/jobs.md](operations/jobs.md) | 定期処理、LLM の鍵・クレジット・予算、巡回作法 |
| **ideas/** 構想（未実装・検証中） | |
| [ideas/README.md](ideas/README.md) | 構想の一覧と状態 |

## 実装済みの機能の地図（コードの場所）

| 機能 | コード |
|---|---|
| API・配信・sitemap | `src/index.js`、`src/seo.js` |
| PWA（manifest・service worker・アイコン） | `public/manifest.webmanifest`、`public/sw.js`、`public/pwa/`、`scripts/check-pwa.mjs` |
| 画面（範囲・カテゴリ階層・検索・ピックアップ・HUD） | `public/app.js`、`public/index.html`、`public/style.css` |
| 地球儀 | `src/client/globe.js` → `public/globe.js`（生成） |
| 国・区画の表 | `public/countries.js`、`public/regions.js`（生成） |
| データベース | `migrations/` |
| 収集・台帳・パイプライン | `scripts/`、`data/`（LLM 判定は `scripts/llm/`） |

## 旧パスの対応

docs の再構成（Issue #57）で移動・統合したファイルの対応。適用済みの migration やコード内の古いコメントに旧パスが残っている場合はここで引く。

| 旧 | 新 |
|---|---|
| `docs/architecture.md` | 技術構成・国旗と地球 = [design/architecture.md](design/architecture.md)、並び・ピックアップ・HUD・NEW・ファビコン = [design/ui.md](design/ui.md)、SEO = [design/seo.md](design/seo.md)、構想・マスコット = [concept/overview.md](concept/overview.md) |
| `docs/site-schema.md` | [design/data-model.md](design/data-model.md)（「運営者の所在国」「運営者の所在区画」の節を含む）。スクリーンショット構想 = [ideas/browser-and-thumbnails.md](ideas/browser-and-thumbnails.md) |
| `docs/discovery-policy.md` | [collection/discovery-policy.md](collection/discovery-policy.md)（分類の節は [concept/listing-criteria.md](concept/listing-criteria.md)） |
| `docs/collection-trial.md` | 恒常手順 = [collection/procedures.md](collection/procedures.md)、日付つき記録 = [collection/trials.md](collection/trials.md) |
| `docs/independent-sites-pipeline.md`（旧 `personal-sites-pipeline.md`） | [collection/pipeline.md](collection/pipeline.md) |
| `docs/public-bodies.md` | [collection/public-bodies.md](collection/public-bodies.md) |
| `docs/thumbnails.md` | [collection/thumbnails.md](collection/thumbnails.md) |
