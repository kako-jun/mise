"""注目度の積み上げ指標の検証用(#55)。docs/ideas/attention-feasibility.md を参照。

サブコマンド:
  ranks   RELEASE...  Common Crawl のドメイン版 ranks の先頭 MB を Range 取得して var/attention/dom/ に保存
  lookup  DOMAIN...   取得済みの ranks から指定ドメインの順位を引き、var/attention/ranks.json に保存
  wayback DOMAIN...   Wayback CDX から各 URL の初回保存日(年)を取り var/attention/wb/ に保存
  plot [SITE...] [--vs COMPARE...]
                      var/attention/ に rank_runmax.png(順位の生値と到達した最高順位、掲載サイト群と比較サイト群の2x2)と
                      wayback_cum.png(累計ページ数と正規化)を作る。matplotlib が必要。
                      SITE/COMPARE は lookup 済みのドメイン(省略時は全て1群)。Wayback は wb/ の取得済み全て

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
        with get(url, {"Range": f"bytes=0-{RANGE_MB * 10**6 - 1}"}) as resp:
            if resp.status != 206:   # Range が無視されると全体(数GB)が来るので読まずに中止
                sys.exit(f"{r}: expected HTTP 206 but got {resp.status}; aborting without reading the body")
            f = open(f"{OUT}/dom/{r}.gz", "wb")
            n = 0
            while chunk := resp.read(1 << 20):
                f.write(chunk); n += len(chunk)
            f.close()
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


def release_key(name):
    """cc-main-2024-feb-apr-may -> (2024, 5)。2023-24-sep-nov-feb のように年が2つなら後ろの年。末尾の月で並べる。"""
    mons = "jan feb mar apr may jun jul aug sep oct nov dec".split()
    t = name.replace("cc-main-", "").split("-")
    nums = [x for x in t if x.isdigit()]
    year = int(nums[0]) if len(nums) == 1 else 2000 + int(nums[1])
    return year + (mons.index(t[-1]) + 0.5) / 12


def cmd_plot(args):
    import matplotlib; matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    sites, cmp = (args[:args.index("--vs")], args[args.index("--vs") + 1:]) if "--vs" in args else (args, [])
    data = json.load(open(f"{OUT}/ranks.json"))
    rels = sorted(data, key=release_key)
    xs = [release_key(r) for r in rels]

    def series(d):
        return [data[r]["found"].get(d, {}).get("hc_pos") for r in rels]

    def runmax(v):
        out, best = [], None
        for x in v:
            if x is not None and (best is None or x < best): best = x
            out.append(best)
        return out

    def draw(a, doms, fn, title):
        for d in doms:
            pts = [(x, y) for x, y in zip(xs, fn(series(d))) if y]
            if pts: a.plot(*zip(*pts), marker="o", ms=3, label=d)
        a.set_yscale("log"); a.invert_yaxis(); a.set_title(title); a.grid(alpha=.3); a.set_ylabel("rank (log, inverted)")

    groups = [("Comparison sites", cmp), ("Listed sites", sites)] if cmp else [("Sites", sites)]
    fig, ax = plt.subplots(len(groups), 2, figsize=(14, 4.5 * len(groups)), squeeze=False)
    for row, (name, doms) in zip(ax, groups):
        draw(row[0], doms, lambda v: v, f"{name}: harmonic centrality rank per release (raw, up=better)")
        draw(row[1], doms, runmax, f"{name}: best rank reached so far (never goes down)")
        row[1].legend(fontsize=7, ncol=2)
    fig.tight_layout(); fig.savefig(f"{OUT}/rank_runmax.png", dpi=90)
    print("saved", f"{OUT}/rank_runmax.png")

    cum = {}
    for f in glob.glob(f"{OUT}/wb/*.txt"):
        ts = [l.strip() for l in open(f) if re.fullmatch(r"\d{14}", l.strip())]
        if len(ts) < 20: continue
        c = collections.Counter(int(t[:4]) for t in ts); run = 0; ys = []; ss = []
        for y in range(min(c), time.gmtime().tm_year + 1):
            run += c.get(y, 0); ys.append(y); ss.append(run)
        cum[os.path.basename(f)[:-4]] = (ys, ss, len(ts))
    fig, ax = plt.subplots(1, 2, figsize=(14, 5))
    for d, (ys, ss, n) in cum.items():
        ax[0].plot(ys, ss, marker="o", ms=3, label=f"{d} ({n})")
        ax[1].plot(ys, [v / ss[-1] for v in ss], label=d)
    ax[0].set_yscale("log"); ax[0].legend(fontsize=7); ax[0].grid(alpha=.3)
    ax[0].set_title("Wayback: cumulative distinct HTML pages first archived by year")
    ax[1].set_title("same, normalized to final"); ax[1].grid(alpha=.3)
    fig.tight_layout(); fig.savefig(f"{OUT}/wayback_cum.png", dpi=90)
    print("saved", f"{OUT}/wayback_cum.png")


if __name__ == "__main__":
    cmd, args = (sys.argv[1], sys.argv[2:]) if len(sys.argv) > 1 else ("", [])
    cmds = {"ranks": cmd_ranks, "lookup": cmd_lookup, "wayback": cmd_wayback, "plot": cmd_plot}
    if cmd in cmds:
        cmds[cmd](args)
    else:
        print(__doc__)
