# mise（ミセ）

個人・企業のサイトを発見するディレクトリ。名前は「見せる」「店」と *mise en place* をかけています。

旧Yahoo! JAPANのディレクトリを参考に、カテゴリの階層移動、サンプル検索、ワイヤーフレームの3D地球を試せます。日英・ライト/ダーク切替に対応し、選択はブラウザ内に保存します。言語の既定は英語です（保存値があればそれを優先。ブラウザ言語の自動判定はしません）。サイト紹介の英訳がない場合は日本語であると明記します。

## ローカルで動かす

Node.jsとnpmが必要です。

```sh
npm install
npm run db:migrate
npm run dev
```

表示されたローカルURLを開きます。`/sitemap.xml` と `/robots.txt` も配信します（SEO方針は [構想と技術方針](docs/architecture.md#seoクローラ向けの方針)）。マイグレーションでローカルD1にカテゴリと9件のサンプルを登録します。`GET /api/categories` と `GET /api/sites` はローカルD1のデータを返します。検索は取得したサンプルをブラウザ内で絞り込みます。クローラーや本格的な検索インデックスは未実装です。

`wrangler.jsonc` のDB IDはローカル開発用の仮値で、本番DBやデプロイ先は設定していません。

- [構想と技術方針](docs/architecture.md)
- [サイトレコードの設計案](docs/site-schema.md)
- [独立サイトの発見パイプライン](docs/independent-sites-pipeline.md)（frontier台帳・決定論フィルタ・実行方法）
- 3D表示には同梱の Three.js を使用し、バンドルに表記を残す（[MIT License](public/THREE-LICENSE.txt)）
- 参考: [1996年当時のYahoo! JAPANトップページ](https://internet.watch.impress.co.jp/docs/news/755478.html)

- 国旗: [flag-icons](https://github.com/lipis/flag-icons)（[MIT License](public/licenses/flag-icons-MIT.txt)）。必要な国だけ `public/flags/` に置いている。サイト行の旗にカーソルを乗せる（スマホはタップ）と地球がその国へ向く（[国旗と地球の連動](docs/architecture.md#国旗と地球の連動)）。
- 区画名（県・州）: 名前・座標は [Wikidata](https://www.wikidata.org/)（CC0）から `scripts/collect-regions.py` で生成した `public/regions.js`。
- GitHubアイコン: [GitHub Octicons](https://github.com/primer/octicons)（[MIT License](public/licenses/octicons-MIT.txt)）。
