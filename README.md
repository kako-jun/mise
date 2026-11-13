# mise（ミセ）

個人・企業のサイトを発見するディレクトリ型の検索エンジン。広告や大手サイトが多い検索に疲れた人が、まだ知らない独立サイトに出会える場を目指す。名前は「見せる」「店」と *mise en place* をかけている。

本番: <https://mise.llll-ll.com>

旧 Yahoo! JAPAN のディレクトリを参考に、カテゴリの階層移動、サイトの検索、ワイヤーフレームの 3D 地球を備える。日英・ライト/ダーク切替に対応し、選択はブラウザ内に保存する。言語は保存値を優先し、無ければブラウザ言語が日本語のときだけ日本語、それ以外は英語（#67）。

## ローカルで動かす

Node.js と npm が必要。

```sh
npm install
npm run db:migrate   # ローカル D1 に migrations を適用
npm run dev          # 表示されたローカル URL を開く
npm run check        # 構文確認 + API ヘッダ自己検査
```

収集・台帳のスクリプトは `uv run python3` で実行する（[docs/collection/procedures.md](docs/collection/procedures.md)）。

## 本番

Cloudflare Workers + D1。main へのマージで Workers Builds が自動デプロイし、migration はマージ前に手動で適用する。手順は [docs/operations/deploy.md](docs/operations/deploy.md)。

## ドキュメント

設計・収集・運用・決定・構想の入口は **[docs/README.md](docs/README.md)**。実装済みのことと構想（未実装）は分けて書いている。決定記録は [docs/decisions.md](docs/decisions.md)、用語は [docs/glossary.md](docs/glossary.md)。

## ライセンス

このリポジトリ自体のライセンスは未決（権利者が決める）。

## 同梱・利用している素材

- 3D 表示には同梱の Three.js を使用し、バンドルに表記を残す（[MIT License](public/THREE-LICENSE.txt)）。
- 国旗: [flag-icons](https://github.com/lipis/flag-icons)（[MIT License](public/licenses/flag-icons-MIT.txt)）。必要な国だけ `public/flags/` に置いている。
- 区画名（県・州）: 名前・座標は [Wikidata](https://www.wikidata.org/)（CC0）から `scripts/collect-regions.py` で生成した `public/regions.js`。
- フォント: [Anton](https://github.com/google/fonts/tree/main/ofl/anton)（[SIL OFL](public/licenses/Anton-OFL.txt)、ロゴ）と [Noto Serif](https://github.com/google/fonts/tree/main/ofl/notoserif)（[SIL OFL](public/licenses/NotoSerif-OFL.txt)、発音記号）。使う文字だけにサブセット化して `public/fonts/` に置いている。
- GitHub アイコン: [GitHub Octicons](https://github.com/primer/octicons)（[MIT License](public/licenses/octicons-MIT.txt)）。
- 官公庁の名簿は総務省・e-Gov・Wikidata・J-LIS を出典とする（[利用条件](docs/collection/public-bodies.md)）。
- 参考: [1996年当時のYahoo! JAPANトップページ](https://internet.watch.impress.co.jp/docs/news/755478.html)
