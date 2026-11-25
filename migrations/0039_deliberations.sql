-- オンライン補強パイプライン(deliberate 段、合議)の記録。3 匹の猫(白猫=shiro・黒猫=kuro・よもぎ猫=yomogi)の票と理由。
-- 1 URL 1 行(identity_key は url_progress / url_candidates と同じ)。status: running(claim 中。lease_until を過ぎたら再 claim 可)/
-- decided(2/3 以上で承認か却下に決まった)/ meeting(割れた・保留多数・どれかの猫が失敗 = 会議中。人の判断待ち)。
-- 非公開: 公開 API・SSR は絶対に SELECT しない(判定理由を含む)。docs/design/data-model.md 参照。外部キーは使わない(リポの流儀)。
-- 戻し方: DROP TABLE deliberations;（他から参照されない）
CREATE TABLE IF NOT EXISTS deliberations (
  identity_key TEXT PRIMARY KEY,
  status TEXT NOT NULL CHECK (status IN ('running','decided','meeting')),
  decision TEXT CHECK (decision IS NULL OR decision IN ('approve','reject')),
  input_hash TEXT,                  -- この票が見た入力(合議の版・モデル・url_candidates.input_hash。review_reason は含めない: この段自身が書き換えるため)。変われば再合議
  shiro_vote TEXT, shiro_reason TEXT, shiro_model TEXT,
  kuro_vote TEXT, kuro_reason TEXT, kuro_model TEXT,
  yomogi_vote TEXT, yomogi_reason TEXT, yomogi_model TEXT,
  lease_until TEXT,                 -- running の claim の期限(UTC ISO8601)。この値が claim の合言葉も兼ねる
  decided_at TEXT,
  updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_deliberations_status ON deliberations(status);
