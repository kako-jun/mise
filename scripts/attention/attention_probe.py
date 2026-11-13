"""注目度の積み上げ指標の検証用(#55)。docs/ideas/attention-feasibility.md を参照。

サブコマンド:
  ranks   RELEASE...  Common Crawl のドメイン版 ranks の先頭 MB を Range 取得して var/attention/dom/ に保存
  lookup  DOMAIN...   取得済みの ranks から指定ドメインの順位を引き、var/attention/ranks.json に保存
  wayback DOMAIN...   Wayback CDX から各 URL の初回保存日(年)を取り var/attention/wb/ に保存
  plot                取得済みデータから累計・最高順位のグラフ(PNG)を作る(matplotlib が必要)

生データは var/ 配下(.gitignore 済み)に置き、コミットしない。
実行例: uv run python3 scripts/attention/attention_probe.py ranks cc-main-2026-aug-sep-oct
"""
import glob, json, os, re, ssl, sys, time, zlib, collections
import urllib.error, urllib.request

UA = "MiseResearchBot/0.1 (+https://github.com/kako-jun/mise)"
BASE = "https://data.commoncrawl.org/projects/hyperlinkgraph"
OUT = "var/attention"
RANGE_MB = int(os.environ.get("RANGE_MB", "200"))  # 先頭からこの MB だけ読む(順位順なので上位のみ)
INTERVAL = 2.0          # リクエスト間隔(秒)
MAX_TRIES, BACKOFF_BASE, BACKOFF_CAP = 5, 10.0, 300.0  # 503/504 の指数バックオフ


# 施設内プロキシ配下では Python 3.13 の厳格な証明書検証が通らないことがある。
# その場合のみ MISE_RELAX_TLS_STRICT=1 で X509 strict だけを外す(検証自体は有効のまま)。
CTX = ssl.create_default_context()
if os.environ.get("MISE_RELAX_TLS_STRICT"):
    CTX.verify_flags &= ~ssl.VERIFY_X509_STRICT


def get(url, headers=None, timeout=300):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    return urllib.request.urlopen(req, timeout=timeout, context=CTX)


def get_with_backoff(url, timeout=300):
    """503/504 のとき 10,20,40,...秒(上限300秒)で最大 MAX_TRIES 回まで再試行。"""
    for i in range(MAX_TRIES):
        try:
            with get(url, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code not in (429, 502, 503, 504):
                raise
            err = f"HTTP {e.code}"
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            err = repr(e)
        wait = min(BACKOFF_CAP, BACKOFF_BASE * 2 ** i)
        print(f"  {err}: retry {i + 1}/{MAX_TRIES} in {wait:.0f}s", file=sys.stderr)
        time.sleep(wait)
    return None


def cmd_ranks(releases):
    os.makedirs(f"{OUT}/dom", exist_ok=True)
    for r in releases:
        url = f"{BASE}/{r}/domain/{r}-domain-ranks.txt.gz"
        with get(url, {"Range": f"bytes=0-{RANGE_MB * 10**6 - 1}"}) as resp, open(f"{OUT}/dom/{r}.gz", "wb") as f:
            n = 0
            while chunk := resp.read(1 << 20):
                f.write(chunk); n += len(chunk)
        print(r, n, "bytes")
        time.sleep(INTERVAL)


def cmd_lookup(domains):
    rev = {".".join(reversed(d.split("."))): d for d in domains}
    res = {}
    for f in sorted(glob.glob(f"{OUT}/dom/*.gz")):
        d = zlib.decompressobj(31); buf = b""; found = {}; last = 0
        with open(f, "rb") as fh:
            while ch := fh.read(1 << 20):
                try:
                    buf += d.decompress(ch)          # Range で切れた末尾は例外になる(無視)
                except zlib.error:
                    break
                *lines, buf = buf.split(b"\n")
                for l in lines:
                    if l.startswith(b"#"): continue
                    p = l.split(b"\t"); last = int(p[0])
                    h = p[4].decode()
                    if h in rev:
                        found[rev[h]] = dict(hc_pos=int(p[0]), hc=float(p[1]), pr_pos=int(p[2]), pr=float(p[3]))
        res[os.path.basename(f)[:-3]] = dict(last_pos=last, found=found)
        print(f, "covered up to rank", last, "found", len(found))
    json.dump(res, open(f"{OUT}/ranks.json", "w"), indent=1)


def cmd_wayback(domains, limit=30000):
    os.makedirs(f"{OUT}/wb", exist_ok=True)
    for dom in domains:
        url = (f"https://web.archive.org/cdx/search/cdx?url={dom}&matchType=domain&collapse=urlkey"
               f"&filter=statuscode:200&filter=mimetype:text/html&fl=timestamp&output=txt&limit={limit}")
        body = get_with_backoff(url)
        if body is None:
            print(dom, "FAILED"); continue
        with open(f"{OUT}/wb/{dom}.txt", "wb") as f: f.write(body)
        print(dom, len(body.splitlines()), "rows (limit reached = truncated)" if len(body.splitlines()) >= limit else "rows")
        time.sleep(5)


def cmd_plot():
    import matplotlib; matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    cum = {}
    for f in glob.glob(f"{OUT}/wb/*.txt"):
        ts = [l.strip() for l in open(f) if re.fullmatch(r"\d{14}", l.strip())]
        if len(ts) < 20: continue
        c = collections.Counter(int(t[:4]) for t in ts); run = 0; ys = []; ss = []
        for y in range(min(c), time.gmtime().tm_year + 1):
            run += c.get(y, 0); ys.append(y); ss.append(run)
        cum[os.path.basename(f)[:-4]] = (ys, ss)
    fig, ax = plt.subplots(figsize=(8, 5))
    for d, (ys, ss) in cum.items(): ax.plot(ys, ss, marker="o", ms=3, label=d)
    ax.set_yscale("log"); ax.legend(fontsize=7); ax.grid(alpha=.3)
    ax.set_title("cumulative distinct HTML pages first archived (Wayback)")
    fig.savefig(f"{OUT}/wayback_cum.png", dpi=90)
    print("saved", f"{OUT}/wayback_cum.png")
    # 最高順位(running max)は ranks.json をリリース順に並べて min を取るだけ: best = min(hc_pos so far)


if __name__ == "__main__":
    cmd, args = (sys.argv[1], sys.argv[2:]) if len(sys.argv) > 1 else ("", [])
    {"ranks": cmd_ranks, "lookup": cmd_lookup, "wayback": cmd_wayback}.get(cmd, lambda a: cmd_plot() if cmd == "plot" else print(__doc__))(args) if cmd != "plot" else cmd_plot()
