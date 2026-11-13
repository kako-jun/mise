# サイトレコードとインデックスの設計案

ステータス: 構想段階。この文書は将来の拡張設計。ローカルD1にはカテゴリ・サンプルサイトのみを実装済み。巡回、紹介文生成、地理情報収集は未実装。現在のサンプルは手書きで、エージェント生成ではない。

## 単位と構造

1サイトを1レコードとし、その入口URL、表示名、選択された紹介文、巡回状態を持つ。1サイトは複数カテゴリに所属できる。サイト内の個々のページを全文検索するためのページ単位のインデックスは、将来必要なら別テーブルとして設計する。URLはサイトの識別材料であり、サイト運営者の実体や所有権を保証しない。

以下の型は SQLite/D1 を想定した論理設計であり、現行の簡易スキーマとは異なる将来案。日時はUTCのISO 8601文字列（例: `2026-11-12T00:00:00Z`）。不明値は `null`。`created_at` / `updated_at` はmise内のレコードの作成・変更日時であり、元サイトの更新日時ではない。

### `sites` — サイト本体

| 列 | 型 | 意味・制約 |
|---|---|---|
| `id` | text | 内部ID、主キー。URL変更で変えない |
| `url` | text | 訪問先の正規化済みHTTP(S) URL。必須、一意 |
| `name` | text | 表示名。必須 |
| `active_description_id` | text? | 現在表示する承認済み紹介文への参照。未設定可 |
| `source_updated_at` | text? | 元サイトの更新日時。確認できた場合のみ記録し、根拠を別途保持する。巡回日時とは別 |
| `source_updated_evidence_url` | text? | 元サイトの更新日時の出典。日時とともに保存 |
| `last_crawl_attempt_at` | text? | 最後に巡回を試みた日時 |
| `last_crawled_at` | text? | 最後に取得に成功した日時。初回成功前はnull |
| `last_crawl_status` | text? | `success` / `failed` / `blocked` 等の結果。具体的な失敗理由と再試行方針は実装時に設計 |
| `created_at`, `updated_at` | text | mise内のレコードの作成・更新日時 |

URL正規化はスキームとホストの小文字化など安全な範囲に限定する。パスの大小文字やクエリの有無で別ページになる場合があり、安易に削らない。リダイレクト後のURL、ミラー、同一サイトの判定規則は未決。URLの一意制約だけで実体の重複を完全には防げない。

現在の試作D1は `sites.description_ja`（必須）と `sites.description_en`（NULL可、migration 0010）の2列で日英の紹介文を持つ。`description_en` が無いサイトだけ、英語表示で日本語に「(Japanese)」を添える。

### `site_descriptions` — 紹介文と生成・確認履歴

| 列 | 型 | 意味・制約 |
|---|---|---|
| `id` | text | 主キー |
| `site_id` | text | `sites.id` への参照、必須 |
| `text` | text | 一文の紹介。必須 |
| `lang` | text | `ja` / `en`。同じサイトに日英それぞれ1文を用意する。英語は日本語の事実だけを訳し、情報を足さない |
| `origin` | text | `manual` / `agent`。手書きかエージェント生成か |
| `status` | text | `draft` / `approved` / `rejected` / `superseded`。公開用に選択できるのは `approved` のみ |
| `source_url` | text? | 紹介の根拠となる公開ページ。複数根拠が必要な場合は別の根拠テーブルを検討 |
| `source_checked_at` | text? | 根拠ページを確認した日時。取得できない場合はnull |
| `generator` | text? | エージェントまたは生成方法の識別子。`origin=agent` なら必須、手書きならnull |
| `created_at`, `reviewed_at` | text / text? | 作成日時と確認日時 |

`active_description_id` が参照する紹介文は同じ `site_id` に属し、`approved` でなければならない。確認者の記録や自動生成物をどの条件で公開するかは未決。状態名は確認・公開の仕組みを設計するための案で、既存のサンプルにレビューが済んだと主張するものではない。

### `categories` / `site_categories` — ディレクトリ

`categories` は `id`（主キー）、`name`、`parent_id`（親カテゴリID、トップ階層ならnull）、`sort_order` を持つ。`site_categories` は `site_id` と `category_id` の組を主キーとし、各々の外部キーを持つ。同じサイトを複数カテゴリに登録でき、同じ組の重複は不可。カテゴリを削除するときの扱い、分類の根拠や自動分類の確認フローは未決。

現在の試作はD1の14カテゴリとその子を使用する。URLの `?category=親ID&category=子ID` は表示階層を上から順に表し、日本語名を使う旧URLもIDへ変換する。複数カテゴリへの所属と、いま表示している1本の階層パスは別の概念。

### `site_locations` — 場所の主張と根拠

サイトに関連する場所を0件以上記録する。候補の場所は運営者が公開した情報または推定であり、確定した住所を意味しない。複数候補のうち表示するものの選び方は未決。

| 列 | 型 | 意味・制約 |
|---|---|---|
| `id` | text | 主キー |
| `site_id` | text | `sites.id` への参照 |
| `location_role` | text | 何の場所か。初期候補は `operator`（運営所在地）/ `service_area`（対象地域）/ `unknown`。混同しない |
| `country_code` | text? | ISO 3166-1 alpha-2 の2文字国コード。不明ならnull |
| `latitude`, `longitude` | real? | 緯度・経度。両方nullか両方有効値（緯度−90〜90、経度−180〜180）。座標の粒度と扱いは未決 |
| `basis` | text | `explicit`（当事者の公開情報に明記）/ `inferred`（推定）/ `unknown`（不明） |
| `evidence_url` | text? | 根拠となる公開ページ。`explicit` / `inferred` なら必須 |
| `evidence_note` | text? | 何が書かれていたか、またはどう推定したかの簡潔な説明。`inferred` なら必須 |
| `checked_at` | text? | 根拠を確認した日時。`explicit` / `inferred` なら必須 |

`basis=unknown` では国・座標・根拠をnullとし、不明を0や架空の座標で埋めない。国コードの形式だけでなく実在する国コードかどうかはアプリ側または参照テーブルで検証する。サイトのドメイン、言語、サーバーやIPの所在だけで運営者の所在地を確定しない。たとえ所在地の推定に使うとしても `inferred` として根拠を示し、運営所在地として表示できるかは別途判断する。非公開の個人住所や精密座標をむやみに収集しない。

## レコード例（架空・説明用）

```json
{
  "site": {
    "id": "site-001",
    "url": "https://example.com/",
    "name": "サンプル工房",
    "active_description_id": "desc-001",
    "source_updated_at": null,
    "source_updated_evidence_url": null,
    "last_crawl_attempt_at": null,
    "last_crawled_at": null,
    "last_crawl_status": null,
    "created_at": "2026-11-12T00:00:00Z",
    "updated_at": "2026-11-12T00:00:00Z"
  },
  "description": {
    "id": "desc-001", "site_id": "site-001",
    "text": "作品を紹介するサイト。", "origin": "manual", "status": "approved",
    "source_url": "https://example.com/", "source_checked_at": "2026-11-12T00:00:00Z",
    "generator": null, "created_at": "2026-11-12T00:00:00Z", "reviewed_at": "2026-11-12T00:00:00Z"
  },
  "category_ids": ["art", "local"],
  "location": {
    "id": "loc-001", "site_id": "site-001", "location_role": "unknown",
    "country_code": null, "latitude": null, "longitude": null, "basis": "unknown",
    "evidence_url": null, "evidence_note": null, "checked_at": null
  }
}
```

例はデータ形状の説明であり、実際にサイトを確認・登録した記録ではない。所在地が分からなければ `site_locations` 自体を0件にすることも可能。

## 未決事項

- 利用DB、ID方式、スキーマ移行、トランザクションと外部キーの強制方法。
- サイトとページの境界、正規URL・重複・リダイレクトの扱い。
- カテゴリ名変更時の共有URL互換性、複数分類や更新時の確認方法。
- 紹介文の生成タイミング、複数出典、承認・差し替え・削除の運用。
- 所在地の取得対象、推定の許容範囲、精度、位置情報の公開基準と削除対応。
- 元サイトの更新日時の根拠の持ち方、巡回頻度・削除やアクセス拒否の扱い。

## サイトのスクリーンショット（将来構想）

巡回時にサイトのトップページを撮影し、一覧にサムネイルを表示する構想がある。サイト単位に画像を管理し、少なくとも撮影元URL、最終取得日時、取得状態（未取得・成功・失敗）を記録する。成功した画像と直近の取得失敗は両立するため、最後の成功日時と最後の試行日時・結果は別に持つ。画像の保存先と失敗詳細、保持期間、古い画像の再取得・置き換え・削除条件は今後設計する。

サイトの巡回ポリシーを尊重する。画像を保存・表示する際の負荷、費用、許諾や公開・削除方針は未決。Cloudflare R2 と Browser Rendering は保存・撮影の候補であり、採用は確定していない。現在はスクリーンショットの撮影・公開は行っていない。
