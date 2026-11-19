#!/usr/bin/env python3
"""使用量ガード（Issue #157）。Cloudflare の GraphQL Analytics API から Workers のリクエスト数と D1 の読み取り・書き込み行数を取り、
Workers Paid の込みの枠に対する割合・月末予測・1 日の急増を出す。**読むだけ**（トークンは Account Analytics: Read のみ）。

  uv run python3 scripts/usage_guard.py [--date YYYY-MM-DD] [--env-file PATH] [--var-dir DIR]

- 金額で止まる上限機能は Cloudflare に無い。暴走（攻撃・バグ）に早く気づくための警告で、検知は最大 1 日遅れ。
- トークンとアカウント ID は ~/.config/mise/cloudflare-analytics.env（CLOUDFLARE_ANALYTICS_TOKEN=... / CLOUDFLARE_ACCOUNT_ID=...）から読む。**値は表示・ログ・JSON に出さない**。
  無い・読めないときはエラーにせず「未設定」の警告を出して exit 0（夜間ジョブを止めない）。API の失敗（429・5xx・不正な応答）も警告にして exit 0。
- 出力: 人が読む表（stdout）と var/usage/<date>.json。しきい値超過・予測超過・急増は guards（文字列の一覧）として返し、夜間ジョブの report に載る。
運用の正本は docs/operations/usage-guard.md。
"""
import argparse
import calendar
import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_ENV = os.path.expanduser("~/.config/mise/cloudflare-analytics.env")
GRAPHQL_URL = "https://api.cloudflare.com/client/v4/graphql"
WORKER_SCRIPT = "mise"                                  # wrangler.jsonc の name
D1_DATABASE_ID = "ebcd8092-5f2e-4119-9e15-692c1aae631a"  # wrangler.jsonc の d1_databases[0].database_id（データベース mise。秘密ではない）

# Workers Paid（月 $5）の込みの枠。出典（2026-11-19 に確認）:
#   D1: https://developers.cloudflare.com/d1/platform/pricing/  読み取り月 250 億行（超過 $0.001/百万行）・書き込み月 5,000 万行（超過 $1/百万行）
#   Workers: https://developers.cloudflare.com/workers/platform/pricing/  月 1,000 万リクエスト（超過 $0.30/百万リクエスト）
# 枠の定数はここ 1 か所。料金表が変わったらここだけ直す。
METRICS = {
    "workers_requests": {"label": "Workers リクエスト", "limit": 10_000_000, "overage": "$0.30/百万リクエスト", "spike_floor": 10_000},
    "d1_rows_read": {"label": "D1 読み取り行", "limit": 25_000_000_000, "overage": "$0.001/百万行", "spike_floor": 10_000_000},
    "d1_rows_written": {"label": "D1 書き込み行", "limit": 50_000_000, "overage": "$1/百万行", "spike_floor": 5_000},
}
WARN_PCT, ALERT_PCT = 50.0, 80.0      # 当月累計が枠のこの割合を超えたら警告 / 強い警告
SPIKE_FACTOR = 3.0                    # 直近 1 日（昨日）が、その前 7 日の平均のこの倍以上なら急増
SPIKE_BASE_DAYS = 7
NOT_SET_MSG = "未設定: 使用量ガードは動いていません。手順は docs/operations/usage-guard.md"

QUERY = """
query Usage($acct: String!, $script: String!, $db: String!, $from: Time!, $to: Time!, $fromDate: Date!, $toDate: Date!) {
  viewer { accounts(filter: {accountTag: $acct}) {
    workers: workersInvocationsAdaptive(limit: 10000, filter: {scriptName: $script, datetime_geq: $from, datetime_leq: $to}) {
      sum { requests } dimensions { date }
    }
    d1: d1AnalyticsAdaptiveGroups(limit: 10000, filter: {databaseId: $db, date_geq: $fromDate, date_leq: $toDate}) {
      sum { rowsRead rowsWritten } dimensions { date }
    }
  } }
}
"""


class GuardError(Exception):
    """API の失敗・不正な応答。メッセージにトークンを含めない（redact 済みの文字列だけを入れる）。"""


# ------------------------------------------------------------------ 設定
def read_env_file(path):
    """KEY=VALUE の行を読む（# 始まりと空行は無視。値の前後の引用符は外す）。無い・読めないなら None。"""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return None
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k.strip()] = v
    return out


def load_credentials(path):
    """(token, account_id) か、無ければ None。"""
    env = read_env_file(path)
    if not env:
        return None
    token, acct = env.get("CLOUDFLARE_ANALYTICS_TOKEN", ""), env.get("CLOUDFLARE_ACCOUNT_ID", "")
    return (token, acct) if token and acct else None


def redact(text, secrets):
    for s in secrets:
        if s:
            text = str(text).replace(s, "***")
    return str(text)


# ------------------------------------------------------------------ API
def http_post(url, body, token, timeout=30):
    """(status, text)。テストはここを差し替える。"""
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "User-Agent": "mise-usage-guard"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def fetch_daily(token, acct, start, end, post=http_post, sleep=time.sleep):
    """start..end（date）の日別の値を {metric: {YYYY-MM-DD: n}} で返す。失敗は GuardError（429・5xx は 1 回だけやり直す）。"""
    body = {"query": QUERY, "variables": {
        "acct": acct, "script": WORKER_SCRIPT, "db": D1_DATABASE_ID,
        "from": f"{start.isoformat()}T00:00:00Z", "to": f"{end.isoformat()}T23:59:59Z", "fromDate": start.isoformat(), "toDate": end.isoformat()}}
    secrets = (token, acct)
    for attempt in (0, 1):
        try:
            status, text = post(GRAPHQL_URL, body, token)
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            raise GuardError("通信に失敗: " + redact(type(e).__name__, secrets))
        if status in (429, 500, 502, 503, 504) and attempt == 0:
            sleep(2)
            continue
        break
    if status == 429:
        raise GuardError("API が 429（レート制限）")
    if status >= 500:
        raise GuardError(f"API が {status}（Cloudflare 側の障害）")
    if status in (401, 403):
        raise GuardError(f"API が {status}（トークンが無効・期限切れ・権限不足。Account Analytics: Read を確認）")
    if status != 200:
        raise GuardError(f"API が {status}")
    try:
        data = json.loads(text)
    except ValueError:
        raise GuardError("応答が JSON でない")
    if not isinstance(data, dict):
        raise GuardError("応答の形が想定外")
    if data.get("errors"):
        msgs = "; ".join(str(e.get("message", e)) if isinstance(e, dict) else str(e) for e in data["errors"][:3])
        raise GuardError("GraphQL エラー: " + redact(msgs, secrets)[:200])
    try:
        acc = data["data"]["viewer"]["accounts"][0]
        out = {k: {} for k in METRICS}
        for row in acc["workers"]:
            d = row["dimensions"]["date"]
            out["workers_requests"][d] = out["workers_requests"].get(d, 0) + int(row["sum"]["requests"])
        for row in acc["d1"]:
            d = row["dimensions"]["date"]
            out["d1_rows_read"][d] = out["d1_rows_read"].get(d, 0) + int(row["sum"]["rowsRead"])
            out["d1_rows_written"][d] = out["d1_rows_written"].get(d, 0) + int(row["sum"]["rowsWritten"])
    except (KeyError, IndexError, TypeError, ValueError):
        raise GuardError("応答の形が想定外（アカウント ID の誤り、または API の項目名の変更）")
    return out


# ------------------------------------------------------------------ 判定（純粋関数）
def analyze(daily, today):
    """daily={metric:{date:n}}、today=UTC の今日（date）。今日は途中なので、予測・急増は「昨日までの完了した日」で計算する。"""
    month = today.strftime("%Y-%m")
    dim = calendar.monthrange(today.year, today.month)[1]
    yesterday = today - datetime.timedelta(days=1)
    done_days = today.day - 1                                   # 今月の完了した日数
    out, guards = {}, []
    for key, m in METRICS.items():
        d = daily.get(key, {})
        total = sum(v for k, v in d.items() if k.startswith(month))
        done_total = sum(v for k, v in d.items() if k.startswith(month) and k < today.isoformat())
        pct = total / m["limit"] * 100
        proj = done_total / done_days * dim if done_days > 0 else None
        proj_pct = proj / m["limit"] * 100 if proj is not None else None
        y = d.get(yesterday.isoformat(), 0)
        base = [d.get((yesterday - datetime.timedelta(days=i)).isoformat(), 0) for i in range(1, SPIKE_BASE_DAYS + 1)]
        avg = sum(base) / len(base)
        ratio = y / avg if avg > 0 else None
        spike = (ratio is not None and ratio >= SPIKE_FACTOR and y >= m["spike_floor"]) or (avg == 0 and y >= m["spike_floor"] * SPIKE_FACTOR)
        level = "alert" if pct >= ALERT_PCT else "warn" if pct >= WARN_PCT else "ok"
        out[key] = {"label": m["label"], "limit": m["limit"], "month_total": total, "month_pct": round(pct, 3), "today": d.get(today.isoformat(), 0),
                    "yesterday": y, "avg_prev7": round(avg, 1), "spike_ratio": None if ratio is None else round(ratio, 2), "spike": spike,
                    "projected_month": None if proj is None else round(proj), "projected_pct": None if proj_pct is None else round(proj_pct, 3), "level": level}
        if level != "ok":
            guards.append(f"使用量: {m['label']} が当月の枠の {pct:.1f}%（しきい値 {int(WARN_PCT if level == 'warn' else ALERT_PCT)}%）。超過は上限なしで課金（{m['overage']}）")
        if proj_pct is not None and proj_pct >= 100:
            guards.append(f"使用量: {m['label']} の月末予測が枠の {proj_pct:.0f}%（今月の完了した {done_days} 日のペース）")
        if spike:
            guards.append(f"使用量: {m['label']} が昨日急増（昨日 {y:,}、その前 {SPIKE_BASE_DAYS} 日平均 {avg:,.0f}）")
    return out, guards


def render_table(date, res):
    L = [f"使用量ガード {date}（UTC。当月の込みの枠に対する割合。検知は最大 1 日遅れ）", "",
         f"{'項目':<20}{'当月累計':>16}{'枠':>18}{'割合':>8}{'月末予測':>9}{'昨日':>16}{'7日平均':>16}{'倍率':>7}  判定"]
    for r in res.values():
        proj = "-" if r["projected_pct"] is None else f"{r['projected_pct']:.1f}%"
        ratio = "-" if r["spike_ratio"] is None else f"{r['spike_ratio']:.1f}x"
        flag = ("警告" if r["level"] == "warn" else "強い警告" if r["level"] == "alert" else "ok") + (" 急増" if r["spike"] else "")
        L.append(f"{r['label']:<20}{r['month_total']:>16,}{r['limit']:>18,}{r['month_pct']:>7.2f}%{proj:>9}{r['yesterday']:>16,}{r['avg_prev7']:>16,.0f}{ratio:>7}  {flag}")
    return "\n".join(L)


# ------------------------------------------------------------------ 実行（夜間ジョブからも呼ぶ）
def run(date=None, env_file=DEFAULT_ENV, var_dir=os.path.join(ROOT, "var"), post=http_post, sleep=time.sleep, now=None, out=print):
    """常に例外を投げない。戻り値: {"ok": bool, "configured": bool, "guards": [str], "note": str, "result": dict|None}。トークンの値はどこにも出さない。"""
    today = datetime.date.fromisoformat(date) if date else (now or datetime.datetime.now(datetime.timezone.utc)).date()
    cred = load_credentials(env_file)
    if cred is None:
        out(NOT_SET_MSG)
        return {"ok": True, "configured": False, "guards": [NOT_SET_MSG], "note": "未設定(動いていない)", "result": None}
    token, acct = cred
    start = min(today.replace(day=1), today - datetime.timedelta(days=SPIKE_BASE_DAYS + 1))
    try:
        daily = fetch_daily(token, acct, start, today, post=post, sleep=sleep)
        res, guards = analyze(daily, today)
    except GuardError as e:
        msg = f"使用量ガード: 取得に失敗（{e}）。次回に再試行"
        out(msg)
        return {"ok": False, "configured": True, "guards": [msg], "note": "取得失敗: " + str(e)[:120], "result": None}
    except Exception as e:     # 想定外でも夜間ジョブを止めない
        msg = f"使用量ガード: 想定外のエラー（{type(e).__name__}）"
        out(msg)
        return {"ok": False, "configured": True, "guards": [msg], "note": msg, "result": None}
    out(render_table(today.isoformat(), res))
    for g in guards:
        out("警告: " + g)
    doc = {"schema": 1, "date": today.isoformat(), "metrics": res, "guards": guards, "thresholds": {"warn_pct": WARN_PCT, "alert_pct": ALERT_PCT, "spike_factor": SPIKE_FACTOR}}
    try:
        os.makedirs(os.path.join(var_dir, "usage"), exist_ok=True)
        with open(os.path.join(var_dir, "usage", today.isoformat() + ".json"), "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=1)
            f.write("\n")
    except OSError as e:
        out(f"警告: JSON を書けない（{type(e).__name__}）")
    worst = max((r["month_pct"] for r in res.values()), default=0)
    return {"ok": True, "configured": True, "guards": guards, "note": f"当月の最大 {worst:.2f}%・警告 {len(guards)} 件", "result": res}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", help="今日とみなす UTC の日付 YYYY-MM-DD（既定: UTC の今日）")
    ap.add_argument("--env-file", default=DEFAULT_ENV)
    ap.add_argument("--var-dir", default=os.path.join(ROOT, "var"))
    a = ap.parse_args(argv)
    if a.date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", a.date):
        ap.error("--date は YYYY-MM-DD")
    run(a.date, a.env_file, a.var_dir)
    return 0   # 警告・失敗でも 0（夜間ジョブを止めない）


if __name__ == "__main__":
    sys.exit(main())
