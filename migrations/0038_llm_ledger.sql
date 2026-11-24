-- オンライン補強パイプライン(judge 段)の LLM 費用台帳。正本は本番 D1 のみ(ローカルの jsonl 台帳は使わない)。
-- 1 リクエスト 1 行。呼び出し前に kind='reserve'(最悪見積りの費用)を入れ、応答後に同じ行を kind='call'(実費・トークン数)へ
-- 確定する。API が明示的に拒否した時だけ kind='release'(cost_usd=0)。応答が曖昧(通信断など)なら reserve のまま残す。
-- 月(UTC) 合計 = SUM(cost_usd)(reserve も計上)。上限 100 USD/月は scripts/enrich/judge_stage.py の MONTHLY_CAP_USD。
-- 戻し方: DROP TABLE llm_ledger;（他から参照されない）
CREATE TABLE IF NOT EXISTS llm_ledger (
  id TEXT PRIMARY KEY,
  ts TEXT NOT NULL,                       -- UTC ISO8601 (YYYY-MM-DDTHH:MM:SSZ)。月の集計は substr(ts,1,7)
  run TEXT,
  kind TEXT NOT NULL CHECK (kind IN ('reserve','call','release')),
  model TEXT,
  mode TEXT,                              -- single
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0,
  cache_read_input_tokens INTEGER NOT NULL DEFAULT 0,
  cost_usd REAL NOT NULL DEFAULT 0,
  note TEXT
);
CREATE INDEX IF NOT EXISTS idx_llm_ledger_ts ON llm_ledger(ts);
