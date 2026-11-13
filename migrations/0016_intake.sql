-- 申請の受付（推薦 add / 取り下げ remove / 訂正 correct）。Issue #4・#29。
-- 受付のみ。承認・却下は管理者が wrangler d1 execute で行う（docs/operations/requests.md）。
CREATE TABLE IF NOT EXISTS requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  type TEXT NOT NULL CHECK (type IN ('add','remove','correct')),
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected')),
  url TEXT NOT NULL,            -- 正規化済み(origin + path、クエリ・フラグメントなし)
  host TEXT NOT NULL,           -- 小文字ホスト名
  normalized_key TEXT NOT NULL, -- 比較キー(www.・スキーム・末尾スラッシュ・大文字小文字を同一視)。レビュー時のグルーピング用
  reason TEXT,                  -- 理由・訂正内容(最大 1000 字、プレーンテキスト)
  contact TEXT,                 -- 連絡先(メールまたは https URL)。決定の 90 日後に NULL にする
  ip_hash TEXT,                 -- レート制限用の IP ハッシュ(IPv6 は /64)。生 IP は保存しない。7 日後に NULL にする
  net_hash TEXT,                -- 同 /48(IPv6)の緩い上限用。IPv4 は ip_hash と同じ。7 日後に NULL にする
  note TEXT,                    -- 管理者メモ(blocklisted など)。blocklisted の自動却下は連絡先を保存しない
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  decided_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_requests_status ON requests (status, created_at);
CREATE INDEX IF NOT EXISTS idx_requests_key ON requests (normalized_key, type, status);
CREATE INDEX IF NOT EXISTS idx_requests_ip ON requests (ip_hash, created_at);

-- 拒否リスト: 取り下げ承認・robots/noindex で外した対象。巡回・推薦で再追加しない。
-- path_prefix が '' ならホスト全体(登録ドメイン配下を含む)、それ以外はそのパス配下だけ
-- (ameblo.jp/foo のようなパス型のユーザー領域の取り下げで、サービス全体を拒否しないため)。小文字・先頭 '/'・末尾 '/' なし。
CREATE TABLE IF NOT EXISTS blocked_hosts (
  host TEXT NOT NULL,           -- 小文字・www. なし
  path_prefix TEXT NOT NULL DEFAULT '',
  reason TEXT NOT NULL,         -- 'removal_request' | 'robots' | 'noindex' | 'manual'
  request_id INTEGER REFERENCES requests(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (host, path_prefix)
);
