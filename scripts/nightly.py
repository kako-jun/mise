#!/usr/bin/env python3
"""夜間ジョブ（Issue #124）。発見 -> 取得 -> LLM 判定 -> 自動承認 -> 投入 SQL -> 整合チェック -> (任意で)本番反映。

  uv run python3 scripts/nightly.py [--date YYYY-MM-DD] [--max-add N(既定 0=上限なし)] [--dry-run] [--apply-prod]

運用の正本は docs/operations/nightly.md。
- 本番 D1 に触れるのは --apply-prod のときだけ（wrangler の呼び出しは prod 段の 1 か所に閉じている）。既定は SQL と報告を作るだけ。
- 各段は冪等・再開可能。同じ --date で再実行すると、成功済みの段は飛ばして失敗した段から続ける（バッチの未精算も再開される）。
- 決定論の段（発見・取得・承認・SQL）に LLM は使わない。LLM は判定の 1 工程（scripts/llm/pipeline.py）だけ。鍵は環境変数のみ。
- --dry-run は外部アクセス・API・書き込みをせず、計画と見積りだけ出す。
Exit code: 0 正常（LLM 未解決が残る partial を含む） / 1 段の失敗（status.json に記録） / 75 多重起動（このスクリプトの内側のロックだけで防ぐ。外側で flock しない）。
"""
import argparse
import datetime
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _d in ("scripts/llm", "scripts/pipeline"):
    sys.path.insert(0, os.path.join(ROOT, _d))
import judge  # noqa: E402
import filelock  # noqa: E402
import export_candidates  # noqa: E402

SCHEMA = 1
PY = sys.executable
EST_USD_PER_CANDIDATE = 0.01      # --dry-run だけの粗い概算（実行時は pipeline.py --dry-run のオフライン見積りを使う）
ANOMALY_FACTOR = 5                # 追加数が直近 7 夜の平均のこの倍を超えたら本番反映を止める
ANOMALY_MIN_NIGHTS = 3            # 履歴がこれ未満の夜は比較しない
ANOMALY_MIN_ADDED = 50            # 小さい数どうしの比は異常と見なさない
AFTER_LLM = ("import", "approve", "sql", "gate")   # llm 段をやり直した夜は、これらも成功済みでもやり直す
STAGE_NAMES = ["discover", "triage", "fetch", "judge", "candidates", "llm", "import", "approve", "sql", "gate", "prod", "favicon"]


class StageFailed(Exception):
    pass


# ------------------------------------------------------------------ 外部コマンド（テストはここを差し替える）
def run_cmd(cmd, env=None, timeout=None):
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, env=env, timeout=timeout)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


# ------------------------------------------------------------------ 台帳（読み取り専用）
def ro_connect(path):
    if not os.path.exists(path):
        return None
    con = sqlite3.connect("file:" + urllib.parse.quote(os.path.abspath(path)) + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def snapshot(db_path):
    """台帳の件数（状態別・却下理由別・LLM 審査状態別）。台帳が無ければ空。"""
    out = {"state": {}, "rejected": {}, "review": {}}
    con = ro_connect(db_path)
    if con is None:
        return out
    try:
        out["state"] = {r[0]: r[1] for r in con.execute("SELECT state, count(*) FROM frontier GROUP BY 1")}
        out["rejected"] = {r[0] or "-": r[1] for r in con.execute("SELECT reject_reason, count(*) FROM frontier WHERE state='rejected' GROUP BY 1")}
        try:
            out["review"] = {r[0]: r[1] for r in con.execute("SELECT review_state, count(*) FROM llm_judgement GROUP BY 1")}
        except sqlite3.OperationalError:
            pass
    finally:
        con.close()
    return out


def unjudged_candidates(db_path):
    """今夜の判定対象 = state='candidate' で llm_judgement がまだ無いもの（judge.py 入力の形）。"""
    con = ro_connect(db_path)
    if con is None:
        return []
    try:
        try:
            judged = {export_candidates.cand_id(r[0]) for r in con.execute("SELECT key FROM llm_judgement")}
        except sqlite3.OperationalError:
            judged = set()
    finally:
        con.close()
    return [c for c in export_candidates.export(db_path) if c["id"] not in judged]


def month_spent(ledger_path, month):
    return judge.Ledger(ledger_path, 0, 0).month_total(month)


def ledger_total(ledger_path):
    return judge.Ledger(ledger_path, 0, 0).total()


# ------------------------------------------------------------------ 状態・履歴
def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
        f.write("\n")
    os.replace(tmp, path)


def history_added(nightly_dir, date, n=7):
    """date より前の夜の追加数（新しい順に最大 n 夜）。"""
    rows = []
    try:
        names = sorted(d for d in os.listdir(nightly_dir) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d) and d < date)
    except OSError:
        return []
    for d in reversed(names):
        st = read_json(os.path.join(nightly_dir, d, "status.json"))
        if isinstance(st, dict) and isinstance(st.get("added"), int):
            rows.append(st["added"])
        if len(rows) >= n:
            break
    return rows


def is_anomalous(added, past):
    if len(past) < ANOMALY_MIN_NIGHTS or added < ANOMALY_MIN_ADDED:
        return False
    return added > ANOMALY_FACTOR * (sum(past) / len(past))


# ------------------------------------------------------------------ 実行本体
class Night:
    def __init__(self, a):
        self.a = a
        self.date = a.date
        self.month = a.date[:7]
        self.var = os.path.abspath(a.var_dir)
        self.nightly = os.path.join(self.var, "nightly")
        self.dir = os.path.join(self.nightly, a.date)
        self.db = os.path.abspath(a.db)
        self.status_path = os.path.join(self.dir, "status.json")
        prev = read_json(self.status_path, {}) if not a.dry_run else {}
        self.prev = {s["name"]: s for s in prev.get("stages", []) if isinstance(s, dict)}
        self.status = {"schema": SCHEMA, "date": a.date, "started_at": utcnow(), "finished_at": None,
                       "mode": {"dry_run": a.dry_run, "apply_prod": a.apply_prod, "judge_mode": a.judge_mode},
                       "ok": None, "stages": [], "candidates_tonight": 0, "cost_usd": 0.0, "month_spent_usd": 0.0,
                       "budget_month_usd": a.budget_month, "added": 0, "rejected_by_reason": {}, "held": 0,
                       "approved": 0, "prod_applied": False, "bookmark": None, "guards": [], "next_action": ""}
        for k in ("candidates_tonight", "cost_usd", "added", "approved", "prod_applied", "bookmark"):  # 再開時は成功済みの段の結果を引き継ぐ
            if k in prev:
                self.status[k] = prev[k]
        self.failed = False
        self.stop = False          # 以降の段は進めない（整合違反・失敗）
        self.exported_keys = []
        self.partial = False       # llm 段が未解決を残した（本番反映は止める）
        self.llm_ran = False       # 今回 llm 段を実際に動かした
        self.from_idx = STAGE_NAMES.index(a.from_stage) if a.from_stage else None

    def spent(self):
        """当月の台帳合計。台帳が壊れていれば 0 を返して理由を guards に残す（llm 段は自分で失敗する）。"""
        try:
            return month_spent(self.a.ledger, self.month)
        except judge.LedgerError as e:
            msg = f"台帳が読めない: {e}"[:300]
            if msg not in self.status["guards"]:
                self.status["guards"].append(msg)
            return 0.0

    # -- 段の枠
    def stage(self, name, fn, resumable=True):
        rec = {"name": name, "status": "ok", "seconds": 0.0, "count": {}, "note": ""}
        if self.stop:
            rec.update(status="skipped", note="前の段で停止")
            self.status["stages"].append(rec)
            return
        old = self.prev.get(name)
        if name in AFTER_LLM and self.llm_ran:
            resumable = False
        if resumable and old and old.get("status") == "ok":
            old = dict(old, note=("再実行: 成功済みのため飛ばした " + (old.get("note") or "")).strip())
            self.status["stages"].append(old)
            return
        t0 = time.monotonic()
        try:
            res = fn() or {}
            rec.update(res)
        except StageFailed as e:
            rec.update(status="failed", note=str(e)[:600])
            self.failed = True
            self.stop = True
        except (Exception, judge.LedgerError) as e:   # 台帳の破損(SystemExit 系)・想定外の例外も段の失敗として記録する
            rec.update(status="failed", note=f"{type(e).__name__}: {e}"[:600])
            self.failed = True
            self.stop = True
        if name == "llm" and rec["status"] in ("ok", "partial"):
            self.llm_ran = True
        if rec["status"] == "partial":
            self.partial = True
        rec["seconds"] = round(time.monotonic() - t0, 2)
        self.status["stages"].append(rec)
        self.save()

    def save(self):
        write_json(self.status_path, self.status)

    def cmd(self, name, argv, env=None, ok_codes=(0,)):
        rc, out = run_cmd(argv, env=env)
        if rc not in ok_codes:
            tail = "\n".join(out.strip().splitlines()[-6:])
            raise StageFailed(f"{name}: exit {rc}\n{tail}")
        return out

    def run_py(self, name, script, *args, ok_codes=(0,)):
        return self.cmd(name, [PY, os.path.join(ROOT, script), *map(str, args)], ok_codes=ok_codes)

    # -- 各段
    def s_discover(self):
        a = self.a
        out = self.run_py("discover-links", "scripts/pipeline/run.py", "discover-links", "--db", self.db, "--max-links", a.max_links)
        note = "links: " + out.strip()[-120:]
        if a.discover_limit > 0:  # discover-cc の失敗は続行できる（取得済みの台帳で後段が進む）
            rc, o = run_cmd([PY, os.path.join(ROOT, "scripts/pipeline/run.py"), "discover-cc", "--sample-every", "1000",
                             "--limit", str(a.discover_limit), "--db", self.db])
            note += " / cc: " + (o.strip()[-120:] if rc == 0 else f"失敗(exit {rc}、続行)")
            if rc != 0:
                self.status["guards"].append("discover-cc 失敗(続行)")
        if a.wikidata_leaves > 0:  # Wikidata の公式サイト(#128)。失敗しても続行(取得済みの台帳で後段が進む)
            leaves, how = wikidata_leaf_order(a.wikidata_leaves, db=self.db)
            if not leaves:
                note += f" / wikidata: 0 葉({how}、スキップ)"
                self.status["guards"].append(f"discover-wikidata スキップ({how})")
                return {"note": note}
            rc, o = run_cmd([PY, os.path.join(ROOT, "scripts/pipeline/run.py"), "discover-wikidata", "--db", self.db,
                             "--leaves", ",".join(leaves), "--limit-leaves", str(a.wikidata_leaves)])
            note += f" / wikidata({how}: {','.join(leaves)}): " + (o.strip()[-120:] if rc == 0 else (f"中断(exit {rc}、続行): " + o.strip()[-100:] if rc == WIKIDATA_ABORT_EXIT else f"失敗(exit {rc}、続行)"))
            if rc == WIKIDATA_ABORT_EXIT:
                self.status["guards"].append("Wikidata が中断した(429/503・Retry-After。続行)")
            elif rc != 0:
                self.status["guards"].append("discover-wikidata 失敗(続行)")
        return {"note": note}

    def s_triage(self):
        return {"note": self.run_py("triage", "scripts/pipeline/run.py", "triage", "--db", self.db).strip()[-200:]}

    def s_fetch(self):
        return {"note": self.run_py("fetch", "scripts/pipeline/run.py", "fetch", "--db", self.db, "--limit", self.a.fetch_limit, "--workers", 3).strip()[-200:]}

    def s_judge(self):
        return {"note": self.run_py("judge", "scripts/pipeline/run.py", "judge", "--db", self.db).strip()[-300:]}

    def unsettled_ids(self):
        """未精算の LLM バッチ(日付に関係なく var/llm-batches の pipeline-*)が抱えている候補 id。"""
        ids = set()
        for _p, st in judge.unsettled_states(os.path.join(self.var, "llm-batches")):
            if os.path.basename(_p).startswith("pipeline-"):
                for ch in st.get("chunks") or []:
                    ids.update(x["id"] for x in ch if isinstance(x, dict) and "id" in x)
        return ids

    def s_candidates(self):
        allc = unjudged_candidates(self.db)
        must = self.unsettled_ids()    # 前夜の未精算バッチの候補は上限に関係なく今夜の入力に含める(pipeline.py が id で引くため)
        first = [c for c in allc if c["id"] in must]
        cands = first + [c for c in allc if c["id"] not in must][:max(0, self.a.judge_limit - len(first))]
        lost = must - {c["id"] for c in first}
        if lost:
            self.status["guards"].append(f"未精算バッチの候補 {len(lost)} 件が台帳の candidate に無い(状態が変わった)。pipeline.py が引けない場合は ledger_release.py で整理する")
        self.status["candidates_tonight"] = len(cands)
        with open(self.p("candidates.jsonl"), "w", encoding="utf-8") as f:
            for c in cands:
                f.write(json.dumps(c, ensure_ascii=False) + "\n")
        return {"count": {"candidates": len(cands)}}

    def p(self, name):
        return os.path.join(self.dir, name)

    def s_llm(self):
        a = self.a
        n = self.status["candidates_tonight"]
        if n == 0 and not self.pending_batch():
            return {"status": "skipped", "note": "今夜の判定対象 0 件"}
        spent = month_spent(a.ledger, self.month)    # 台帳が壊れていれば LedgerError -> 段の失敗として記録される
        remaining = a.budget_month - spent
        base = [PY, os.path.join(ROOT, "scripts/llm/pipeline.py"), self.p("candidates.jsonl"), "--model", "haiku", "--describe-model", "sonnet",
                "--mode", "batch" if a.judge_mode == "batch" else "single", "--var-dir", self.var, "--ledger", a.ledger]
        rc, out = run_cmd(base + ["--dry-run"])    # オフラインの見積り（API は呼ばない）
        m = re.search(r"TOTAL expected \$([0-9.]+), worst case \$([0-9.]+)", out)
        if rc != 0 or not m:
            raise StageFailed("llm: 費用見積りに失敗\n" + "\n".join(out.strip().splitlines()[-4:]))
        expected, worst = float(m.group(1)), float(m.group(2))
        if spent + worst > a.budget_month:
            self.status["guards"].append(f"予算ガード: 当月 ${spent:.2f} + 最悪 ${worst:.2f} > ${a.budget_month:.2f}。LLM 段を実行しない")
            return {"status": "skipped", "note": f"予算ガード(当月 ${spent:.2f}, 最悪 ${worst:.2f})", "count": {"estimate_expected_usd": expected, "estimate_worst_usd": worst}}
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise StageFailed("llm: ANTHROPIC_API_KEY が環境変数に無い（~/.config/mise/anthropic.env を読み込む。値は出さない）")
        total_now = ledger_total(a.ledger)
        rc, out = run_cmd(base + ["--budget-run", f"{remaining:.4f}", "--budget-total", f"{total_now + remaining:.4f}", "--out", self.p("results.json"),
                                  "--poll-seconds", "60"])
        if rc == 3:
            self.status["guards"].append("予算ガード: pipeline.py が予算超過で停止")
        if rc not in (0, 2):
            raise StageFailed(f"llm: exit {rc}\n" + "\n".join(out.strip().splitlines()[-5:]))
        cost = month_spent(a.ledger, self.month) - spent
        self.status["cost_usd"] = round(cost, 4)
        res = {"count": {"cost_usd": round(cost, 4), "unresolved": 1 if rc == 2 else 0}}
        if rc == 2:   # 未解決あり = 未完了。同じ --date で再実行すると、この段から続きを再開する
            res.update(status="partial", note="未解決あり(同じ --date で再実行すると続きから)")
        return res

    def pending_batch(self):
        """未精算の LLM バッチ状態があるか(常在の *.lock などでは true にならない)。"""
        return any(os.path.basename(p).startswith("pipeline-") for p, _ in judge.unsettled_states(os.path.join(self.var, "llm-batches")))

    def s_import(self):
        res = self.p("results.json")
        if not os.path.exists(res):
            return {"status": "skipped", "note": "結果ファイルなし"}
        out = self.cmd("import", [PY, os.path.join(ROOT, "scripts/llm/import_results.py"), res, "--input", self.p("candidates.jsonl"), "--db", self.db], ok_codes=(0, 2))
        return {"note": out.strip()[-300:]}

    def s_approve(self):
        out = self.run_py("approve", "scripts/llm/approve_policy.py", "--db", self.db, "--apply")
        m = re.search(r"approve=(\d+), hold=(\d+), reject_stale=(\d+)", out)
        cnt = {"approve": int(m.group(1)), "hold": int(m.group(2)), "reject_stale": int(m.group(3))} if m else {}
        return {"count": cnt}

    def select_export(self):
        """承認済みのうち未出荷を key 順に --max-add 件まで。残りは出荷用の台帳コピーで pending に戻す（本物の台帳は変えない）。"""
        shipped = set(read_keys(os.path.join(self.nightly, "shipped.txt")))
        src = ro_connect(self.db)
        if src is None:
            raise StageFailed("sql: 台帳がない")
        dst = sqlite3.connect(self.p("export.sqlite"))
        try:
            src.backup(dst)
        finally:
            src.close()
        rows = dst.execute("SELECT j.key FROM llm_judgement j JOIN frontier f ON f.key=j.key WHERE j.review_state='approved' AND f.state='candidate' ORDER BY j.key").fetchall()
        eligible = [r[0] for r in rows if r[0] not in shipped]
        # --max-add 0 は上限なし(既定)。1 夜の量は fetch/judge の上限・予算ガード・前日比の異常ガードで抑える
        keep, cut = (eligible, []) if self.a.max_add == 0 else (eligible[:self.a.max_add], eligible[self.a.max_add:])
        if cut:
            self.status["guards"].append(f"1夜の追加上限 {self.a.max_add} 件: 承認済み {len(eligible)} 件のうち {len(cut)} 件は翌夜以降")
        drop = [r[0] for r in rows if r[0] in shipped] + cut
        dst.executemany("UPDATE llm_judgement SET review_state='pending' WHERE key=?", [(k,) for k in drop])
        dst.commit()
        dst.close()
        return keep

    def s_sql(self):
        keep = self.select_export()
        sql_path = self.p("approved-sites.sql")
        if os.path.exists(sql_path):
            os.remove(sql_path)
        if not keep:
            return {"status": "skipped", "note": "承認 0 件のため SQL を作らない", "count": {"exported": 0}}
        self.exported_keys = keep
        with open(self.p("keys.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(keep) + "\n")
        rc, out = run_cmd([PY, os.path.join(ROOT, "scripts/llm/export_approved_sql.py"), "--db", self.p("export.sqlite"), "--out", sql_path])
        if rc == 3:
            self.status["guards"].append("整合チェック違反: 出荷 SQL の生成で停止")
            raise StageFailed("sql: 整合チェック違反\n" + out.strip()[-600:])
        if rc != 0:
            raise StageFailed(f"sql: exit {rc}\n" + out.strip()[-400:])
        m = re.search(r"exported=(\d+)", out)
        n = int(m.group(1)) if m else 0
        self.status["added"] = n
        self.status["approved"] = len(keep)
        if n == 0 and os.path.exists(sql_path):
            os.remove(sql_path)
            self.exported_keys = []
            os.remove(self.p("keys.txt"))
            return {"status": "skipped", "note": "書き出せる承認 0 件", "count": {"exported": 0}}
        # 出荷済みとして記録するのは SQL に実際に入った key だけ(カテゴリ無し・重複などで外れたものを除く)
        with open(sql_path, encoding="utf-8") as f:
            in_sql = {ln[3:].strip() for ln in f.read().splitlines() if ln.startswith("-- ")}
        self.exported_keys = [k for k in keep if k in in_sql]
        if n and not self.exported_keys:
            raise StageFailed("sql: 出荷 SQL から key を読めない(出力形式の変更?)")
        with open(self.p("keys.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(self.exported_keys) + "\n")
        return {"count": {"exported": n}, "note": out.strip()[-200:]}

    def s_gate(self):
        sql_path = self.p("approved-sites.sql")
        if not os.path.exists(sql_path):
            return {"status": "skipped", "note": "SQL なし"}
        import integrity
        cur = self.a.current_db
        if cur and not os.path.exists(cur):
            raise StageFailed(f"gate: --current-db が無い: {cur}")
        with open(sql_path, encoding="utf-8") as f:
            found = integrity.gate(f.read(), current=cur) if cur else integrity.gate(f.read())
        if found:
            self.status["guards"].append("整合チェック違反で停止")
            raise StageFailed("gate: 整合チェック違反\n" + integrity.format_violations(found))
        return {"note": "違反なし(" + (f"本番再現 DB {os.path.basename(cur)} 上" if cur else "空 DB 上。--current-db 未指定") + ")"}

    def s_prod(self):
        a = self.a
        sql_path = self.p("approved-sites.sql")
        if not a.apply_prod:
            return {"status": "skipped", "note": "--apply-prod なし(本番には触れない)"}
        if not os.path.exists(sql_path):
            return {"status": "skipped", "note": "SQL なし"}
        if self.partial:
            self.status["guards"].append("llm 段に未解決が残るため本番反映を止めた(再実行で続きを解決してから)")
            return {"status": "skipped", "note": "LLM 未解決あり(完了後に反映)"}
        if self.status["prod_applied"]:
            raise StageFailed("prod: この夜(--date)は本番へ投入済み。同じ --date での再投入は拒否する(bookmark は保持)。"
                              "整合 error 後の対処は docs/operations/nightly.md の「ロールバック」を見る")
        if is_anomalous(self.status["added"], history_added(self.nightly, self.date)):
            self.status["guards"].append(f"前日比の異常: 追加 {self.status['added']} 件が直近平均の {ANOMALY_FACTOR} 倍超。本番反映を止めた")
            return {"status": "skipped", "note": "異常ガード(人の確認待ち)"}
        # 投入前に本番の整合を読み取りだけで確認する。前の夜の error が残っているなら重ねて投入しない
        rc, out = run_cmd([PY, os.path.join(ROOT, "scripts/check-data-integrity.py"), "--remote", "--db", a.d1_name])
        with open(self.p("prod-integrity-before.log"), "w", encoding="utf-8") as f:
            f.write(out)
        if rc != 0:
            self.status["guards"].append("投入前の本番整合チェックで error。投入しなかった")
            raise StageFailed("prod: 投入前の本番整合チェックが error のため投入しない(nightly.md「ロールバック」参照)\n" + out.strip()[-400:])
        npx = [shutil.which("npx") or "npx", "wrangler", "d1"]   # Windows は npx.exe/npx.cmd を PATH から解決する
        bm_path = self.p("bookmark.txt")
        if os.path.exists(bm_path):   # 投入に失敗した夜の再実行: 最初に取った bookmark を上書きしない
            with open(bm_path, encoding="utf-8") as f:
                self.status["bookmark"] = parse_bookmark(f.read())
        else:
            rc, out = run_cmd(npx + ["time-travel", "info", a.d1_name, "--json"])
            if rc != 0:
                raise StageFailed("prod: Time Travel の bookmark が取れないため反映しない\n" + out.strip()[-300:])
            with open(bm_path, "w", encoding="utf-8") as f:
                f.write(out)
            self.status["bookmark"] = parse_bookmark(out)
        rc, out = run_cmd(npx + ["execute", a.d1_name, "--remote", "--yes", "--file", sql_path])
        with open(self.p("prod-execute.log"), "w", encoding="utf-8") as f:
            f.write(out)
        if rc != 0:
            raise StageFailed("prod: 本番への投入が失敗(途中まで入った可能性。bookmark で戻せる)\n" + out.strip()[-400:])
        self.status["prod_applied"] = True
        with open(os.path.join(self.nightly, "shipped.txt"), "a", encoding="utf-8") as f:
            f.write("\n".join(self.exported_keys or read_keys(self.p("keys.txt"))) + "\n")
        rc, out = run_cmd([PY, os.path.join(ROOT, "scripts/check-data-integrity.py"), "--remote", "--db", a.d1_name])
        with open(self.p("prod-integrity.log"), "w", encoding="utf-8") as f:
            f.write(out)
        if rc != 0:
            self.status["guards"].append("本番の整合チェックで error。bookmark で復元を検討")
            raise StageFailed("prod: 反映後の整合チェックが error。bookmark " + str(self.status["bookmark"]) + "\n" + out.strip()[-500:])
        return {"note": "反映済み・整合 OK", "count": {"added": self.status["added"]}}

    def s_favicon(self):
        """ファビコンの取得(#125)。既定は無効(--favicon-limit 0)。有効にしてもローカルへ取得するだけで、SQL は作らず、本番 D1・R2 には触れない。
        失敗しても夜間ジョブ全体は失敗にしない(guards に残して続行)。"""
        a = self.a
        if a.favicon_limit <= 0:
            return {"status": "skipped", "note": "--favicon-limit 0(既定)で無効"}
        fdb = os.path.join(self.var, "favicons.sqlite")
        script = os.path.join(ROOT, "scripts/favicon/favicon.py")
        recrawl_db = os.path.join(self.var, "recrawl.sqlite")
        steps = []
        if os.path.exists(recrawl_db):
            steps.append(("import", ["--db", fdb, "import", "--recrawl", recrawl_db]))
        steps.append(("run", ["--db", fdb, "run", "--apply", "--limit", str(a.favicon_limit), "--store-dir", os.path.join(self.var, "favicons")]))
        notes = []
        for name, argv in steps:
            # ここだけ python3 でなく python: 実行機 kako-rog(Windows)の venv に python3.exe は無い(Store の偽 python3 を拾うと Pillow が無く毎晩失敗する)。uv run python は Linux でも動く
            # 夜間ジョブ本体は Pillow を持たないので、favicon.py だけ --with pillow で起動する(無ければ favicon.py が非 0 で終わり、ここで失敗として残る)
            rc, out = run_cmd(["uv", "run", "--with", "pillow", "python", script, *argv])
            if rc != 0:
                self.status["guards"].append(f"favicon {name} 失敗(続行)")
                return {"note": f"{name}: 失敗(exit {rc}、続行): " + out.strip()[-150:]}
            notes.append(f"{name}: " + out.strip()[-80:])
        return {"note": " / ".join(notes)}

    # -- 全体
    def run(self):
        os.makedirs(self.dir, exist_ok=True)
        before = snapshot(self.db)
        self.status["month_spent_usd"] = round(self.spent(), 4)
        table = [("discover", self.s_discover), ("triage", self.s_triage), ("fetch", self.s_fetch), ("judge", self.s_judge),
                 ("candidates", self.s_candidates), ("llm", self.s_llm), ("import", self.s_import), ("approve", self.s_approve),
                 ("sql", self.s_sql), ("gate", self.s_gate), ("prod", self.s_prod), ("favicon", self.s_favicon)]
        llm_prev_ok = (self.prev.get("llm") or {}).get("status") == "ok"
        for i, (name, fn) in enumerate(table):
            force = self.from_idx is not None and i >= self.from_idx      # --from-stage: ここから先は成功済みでもやり直す
            # candidates は llm 段が済むまでだけ作り直す(済んだ後に作り直すと import の入力 candidates.jsonl が縮む)
            self.stage(name, fn, resumable=not force and (name != "candidates" or llm_prev_ok))
        after = snapshot(self.db)
        self.status["rejected_by_reason"] = {k: v - before["rejected"].get(k, 0) for k, v in after["rejected"].items() if v > before["rejected"].get(k, 0)}
        self.status["held"] = after["review"].get("pending", 0)
        self.status["month_spent_usd"] = round(self.spent(), 4)
        self.status["ok"] = not self.failed
        self.status["finished_at"] = utcnow()
        self.status["next_action"] = self.next_action()
        self.save()
        with open(self.p("report.md"), "w", encoding="utf-8") as f:
            f.write(render_report(self.status))
        return 1 if self.failed else 0

    def next_action(self):
        failed = [s for s in self.status["stages"] if s["status"] == "failed"]
        if failed:
            return f"段 {failed[0]['name']} の失敗を直し、同じ --date {self.date} で再実行する(成功済みの段は飛ばされる)。docs/operations/nightly.md のトラブルシュート参照"
        if any(s["status"] == "partial" for s in self.status["stages"]):
            return f"LLM 判定に未解決が残っている。同じ --date {self.date} で再実行すると続きから再開する(本番反映はそれまで止まる)"
        if self.status["prod_applied"]:
            return "本番の件数と保留サンプルを朝の手順で確認する"
        if self.status["added"]:
            return "report.md と approved-sites.sql を確認し、問題なければ --apply-prod を付けた夜に切り替える(または手動で投入)"
        return "追加なし。保留・却下の理由を確認する"


def read_keys(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().split()
    except OSError:
        return []


def parse_bookmark(text):
    m = re.search(r'"bookmark"\s*:\s*"([^"]+)"', text) or re.search(r"bookmark[^\w]+([0-9A-Za-z_-]{16,})", text)
    return m.group(1) if m else "(出力から取れなかった。bookmark.txt を見る)"


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def render_report(st):
    L = [f"# 夜間ジョブ報告 {st['date']}", "",
         f"- 結果: {('未完了あり(再実行で続き)' if any(s['status'] == 'partial' for s in st['stages']) else '成功') if st['ok'] else '失敗あり'} / モード: dry_run={st['mode']['dry_run']} apply_prod={st['mode']['apply_prod']} judge={st['mode']['judge_mode']}",
         f"- 今夜の判定対象: {st['candidates_tonight']} 件 / 追加(SQL): {st['added']} 件 / 本番反映: {'済' if st['prod_applied'] else 'していない'}",
         f"- 費用: 今夜 ${st['cost_usd']:.4f} / 当月 ${st['month_spent_usd']:.2f} / 予算 ${st['budget_month_usd']:.2f}",
         f"- 保留(pending): {st['held']} 件", f"- Time Travel bookmark: {st['bookmark'] or '-'}", "", "## 段", "",
         "| 段 | 結果 | 秒 | 件数 | メモ |", "|---|---|---|---|---|"]
    for s in st["stages"]:
        note = (s.get("note") or "").replace("\n", " / ").replace("|", "\\|")[:160]
        L.append(f"| {s['name']} | {s['status']} | {s['seconds']} | {json.dumps(s.get('count') or {}, ensure_ascii=False)} | {note} |")
    L += ["", "## 却下理由別(今夜の増分)", ""]
    L += [f"- {k}: {v}" for k, v in sorted(st["rejected_by_reason"].items(), key=lambda kv: -kv[1])] or ["- なし"]
    L += ["", "## ガードの発動", ""] + ([f"- {g}" for g in st["guards"]] or ["- なし"])
    L += ["", "## 次の一手", "", st["next_action"], ""]
    return "\n".join(L)


# ------------------------------------------------------------------ Wikidata の葉の順(#128)
WIKIDATA_ABORT_EXIT = 3                            # run.py discover-wikidata が 429/503 で中断したときの終了コード
LEAF_TARGET = 100                                  # 非官公庁の各葉の最低ライン(#124)
CATEGORIES_URL = "https://mise.llll-ll.com/api/categories"   # 読み取りのみ


def wikidata_done_leaves(rows, db_path):
    """対応表の行がすべて source_cursor で done の葉の集合。台帳が無ければ空。"""
    import discover_wikidata
    con = ro_connect(db_path) if db_path else None
    if con is None:
        return set()
    try:
        cur = {k: v for k, v in con.execute("SELECT cursor_key, cursor_value FROM source_cursor WHERE source=?", (discover_wikidata.SOURCE,))}
    except sqlite3.Error:
        return set()
    finally:
        con.close()
    by_leaf = {}
    for r in rows:
        by_leaf.setdefault(r["leaf_id"], []).append(cur.get(discover_wikidata.cursor_name(r)) == "done")
    return {l for l, flags in by_leaf.items() if all(flags)}


def wikidata_leaf_order(n, fetch=None, map_path=None, db=None):
    """今夜 Wikidata から取る葉を最大 n 個。対応表の行が全部 done の葉は除く。残りのうち本番の site_count が LEAF_TARGET 未満の葉を少ない順に
    (不足の葉が無ければ 0 件=スキップ)。API が取れなければ対応表の順(固定)。全葉が完了なら 0 件。"""
    import discover_wikidata
    rows = discover_wikidata.load_map(map_path or discover_wikidata.MAP_PATH)
    done = wikidata_done_leaves(rows, db)
    leaves = [l for l in dict.fromkeys(r["leaf_id"] for r in rows) if l not in done]
    if not leaves:
        return [], "完了"
    try:
        if fetch is None:
            import urllib.request
            req = urllib.request.Request(CATEGORIES_URL, headers={"User-Agent": "MiseResearchBot/0.1 (+https://github.com/kako-jun/mise)"})
            fetch = lambda: json.load(urllib.request.urlopen(req, timeout=15))
        counts = {c["id"]: c["site_count"] for c in fetch()}
        short = [l for l in leaves if counts.get(l, 0) < LEAF_TARGET]
        short.sort(key=lambda l: counts.get(l, 0))   # 安定ソート: 同数なら対応表の順
        return short[:n], ("不足順" if short else "不足なし")
    except Exception:
        return leaves[:n], "固定順(API 不可)"


# ------------------------------------------------------------------ 計画（--dry-run）
def plan(a):
    snap = snapshot(a.db)
    month = a.date[:7]
    spent = month_spent(a.ledger, month) if os.path.exists(a.ledger) else 0.0
    cands = unjudged_candidates(a.db)
    n = min(len(cands), a.judge_limit)
    est = n * EST_USD_PER_CANDIDATE
    L = [f"[dry-run] 夜間ジョブ計画 {a.date}（外部アクセス・API・書き込みなし）", f"台帳: {a.db} ({'あり' if os.path.exists(a.db) else 'なし'})",
         f"台帳の状態: {json.dumps(snap['state'], ensure_ascii=False)}", f"LLM 審査状態: {json.dumps(snap['review'], ensure_ascii=False)}", "段:"]
    L.append(f"  1 discover   discover-links 常に / discover-cc 上限 {a.discover_limit}" + ("(無効)" if a.discover_limit <= 0 else ""))
    L.append(f"  1 discover   + discover-wikidata 1 夜 {a.wikidata_leaves} 葉(本番の site_count が {LEAF_TARGET} 未満の葉を優先)" if a.wikidata_leaves > 0 else "  1 discover   + discover-wikidata 無効")
    L += [f"  2 triage     台帳内の discovered を振り分け", f"  3 fetch      上限 {a.fetch_limit} ホスト（queued: {snap['state'].get('queued', 0)}）",
          f"  4 judge      取得後フィルタ（fetched: {snap['state'].get('fetched', 0)}）",
          f"  5 candidates 今夜の判定対象（未判定の candidate）: {n} 件（candidate 全体 {snap['state'].get('candidate', 0)}、判定上限 {a.judge_limit}）",
          f"  6 llm        haiku -> sonnet / mode={a.judge_mode}", "  7 import     結果を台帳へ", "  8 approve    自動承認ポリシーを適用",
          f"  9 sql        承認済み未出荷を{('最大 %d 件' % a.max_add) if a.max_add else 'すべて'} -> var/nightly/{a.date}/approved-sites.sql", " 10 gate       生成 SQL をメモリ DB に流して整合チェック",
          " 11 prod       " + ("--apply-prod: bookmark 取得 -> 本番投入 -> 整合チェック" if a.apply_prod else "実行しない(--apply-prod なし)"),
          " 12 favicon    " + (f"期限が来たサイトを最大 {a.favicon_limit} 件取得だけ(ローカルの var/favicons/ に置く。SQL は作らない。本番には触れない)" if a.favicon_limit > 0 else "無効(--favicon-limit 0。既定)")]
    remaining = a.budget_month - spent
    L += [f"費用の概算(粗い): ${est:.2f}（1 件 ${EST_USD_PER_CANDIDATE}）", f"予算: 当月 ${spent:.2f} / ${a.budget_month:.2f}（残り ${remaining:.2f}）",
          "予算ガード: " + ("発動する見込み(LLM 段は実行されない)" if est > remaining else "発動しない見込み"),
          f"1 夜の追加上限: {a.max_add if a.max_add else 'なし'}", "ANTHROPIC_API_KEY: " + ("設定あり" if os.environ.get("ANTHROPIC_API_KEY") else "未設定(LLM 段で失敗する)")]
    return "\n".join(L)


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", default=datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d"), help="夜の名前(既定: UTC の今日)。再開は同じ日付で")
    ap.add_argument("--max-add", type=int, default=0, help="1 夜に出荷する追加サイト数の上限(0=上限なし。既定)")
    ap.add_argument("--discover-limit", type=int, default=200, help="discover-cc の 1 夜の上限(0 で無効)")
    ap.add_argument("--wikidata-leaves", type=int, default=3, help="discover-wikidata で 1 夜に取る葉の数(0 で無効)。本番の site_count が 100 未満の葉を優先")
    ap.add_argument("--max-links", type=int, default=300, help="discover-links の 1 シードあたりの発見上限")
    ap.add_argument("--fetch-limit", type=int, default=600)
    ap.add_argument("--favicon-limit", type=int, default=0, help="favicon 段で 1 夜に取るサイト数の上限(0 で無効。既定)。実サイトにアクセスする。SQL を作るだけで本番には触れない(#125)")
    ap.add_argument("--from-stage", choices=STAGE_NAMES, help="この段から先は成功済みでもやり直す(status.json を消さずに途中から再開)")
    ap.add_argument("--current-db", help="gate 段が土台にする本番再現の sqlite(ローカル D1)。無ければ空 DB(マイグレーションのみ)")
    ap.add_argument("--judge-limit", type=int, default=600, help="1 夜に LLM へ渡す候補数の上限")
    ap.add_argument("--judge-mode", choices=["batch", "sync"], default="batch")
    ap.add_argument("--budget-month", type=float, default=100.0, help="月(UTC)の LLM 予算 USD")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--apply-prod", action="store_true", help="本番 D1 へ反映する(これが無ければ wrangler を一切呼ばない)")
    ap.add_argument("--db", default=os.path.join(ROOT, "var", "frontier.sqlite"))
    ap.add_argument("--var-dir", default=os.path.join(ROOT, "var"))
    ap.add_argument("--ledger", default=judge.DEFAULT_LEDGER)
    ap.add_argument("--d1-name", default="mise")
    a = ap.parse_args(argv)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", a.date):
        ap.error("--date は YYYY-MM-DD")
    if a.wikidata_leaves < 0:
        ap.error("--wikidata-leaves は 0 以上")
    if a.favicon_limit < 0:
        ap.error("--favicon-limit は 0 以上")
    if a.max_add < 0 or a.fetch_limit < 1 or a.judge_limit < 1 or a.max_links < 1:
        ap.error("上限は 1 以上(--max-add のみ 0=上限なし)")
    if a.dry_run and a.apply_prod:
        ap.error("--dry-run と --apply-prod は同時に使えない")
    return a


def main(argv=None):
    a = parse_args(argv)
    if a.dry_run:
        print(plan(a))
        return 0
    os.makedirs(os.path.join(a.var_dir, "nightly"), exist_ok=True)
    with open(os.path.join(a.var_dir, "nightly", ".lock"), "w") as lf:
        try:
            filelock.lock(lf, blocking=False)
        except OSError:
            print("別の夜間ジョブが実行中(var/nightly/.lock)", file=sys.stderr)
            return 75
        n = Night(a)
        rc = n.run()
    print(render_report(n.status))
    return rc


if __name__ == "__main__":
    sys.exit(main())
