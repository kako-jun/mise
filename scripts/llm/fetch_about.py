#!/usr/bin/env python3
"""Add the top page text and up to 2 About-type pages to judge.py candidates (deterministic, polite).

Per site: robots.txt, the top page, then at most 2 same-host pages whose link text or path looks like
an About / profile / operator / self-introduction page. Same-host redirects only, robots.txt obeyed
(re-checked per URL), noindex skipped, 2 s between every request, body capped at 1 MiB, never parallel.
At most 4 requests per site (plus same-host redirect hops). Reads candidates JSONL, writes JSONL with
body_excerpt filled ("--- page: URL ---" sections). Only run it on a small, explicit list of sites.

Usage: uv run python3 scripts/llm/fetch_about.py var/sites-candidates.jsonl var/sites-about.jsonl
"""
import html.parser
import ipaddress
import json
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.robotparser

UA = "MiseResearchBot/0.1 (+https://github.com/kako-jun/mise)"
MAXBODY = 1024 * 1024
PAGE_CHARS = 3000
WAIT = 2
# Strong words identify an About-type page by themselves; weak words only add to a strong match.
STRONG_WORDS = ["about", "profile", "プロフィール", "運営者", "運営", "このサイトについて", "サイトについて", "自己紹介",
                "管理人", "作者", "著者", "サイト情報", "会社案内", "沿革", "ごあいさつ", "挨拶", "staff", "スタッフ", "author"]
WEAK_WORDS = ["概要", "紹介", "について", "団体"]
SKIP_EXT = re.compile(r"\.(pdf|jpe?g|png|gif|zip|lzh|exe|mp[34]|xml|rss|css|js)$", re.I)


def safe_host(host):
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return host.lower() != "localhost" and "." in host


def fetch(url):
    p = urllib.parse.urlsplit(url)
    if p.scheme not in ("http", "https") or not p.hostname or not safe_host(p.hostname):
        raise ValueError("unsafe url")
    time.sleep(WAIT)
    r = subprocess.run(["curl", "-s", "-S", "--max-time", "15", "--max-filesize", str(MAXBODY), "-A", UA, "-D", "-", url],
                       check=True, capture_output=True)
    raw = r.stdout
    while True:  # skip proxy preamble / 1xx blocks; the real response is the last header block
        head, sep, rest = raw.partition(b"\r\n\r\n")
        if head.startswith(b"HTTP/") and sep and (b"Connection established" in head.split(b"\r\n")[0]):
            raw = rest
            continue
        break
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split()[1])
    hdr = {}
    for l in lines[1:]:
        if ":" in l:
            k, v = l.split(":", 1)
            hdr[k.strip().lower()] = v.strip()
    return status, hdr, rest[:MAXBODY]


def get(url, rules, hops=2):
    for _ in range(hops + 1):
        if not rules.can_fetch(UA, url):
            raise ValueError("disallowed by robots.txt")
        status, h, body = fetch(url)
        if status in (301, 302, 303, 307, 308):
            nxt = urllib.parse.urljoin(url, h.get("location", ""))
            if urllib.parse.urlsplit(nxt).hostname != urllib.parse.urlsplit(url).hostname:
                raise ValueError("cross-host redirect")
            url = nxt
            continue
        if status != 200:
            raise ValueError(f"HTTP {status}")
        m = re.search(r"charset=([\w-]+)", h.get("content-type", ""), re.I)
        enc = m.group(1) if m else None
        if not enc:
            m = re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", body[:4096], re.I)
            enc = m.group(1).decode() if m else "utf-8"
        try:
            return url, body.decode(enc, errors="replace")
        except LookupError:
            return url, body.decode("utf-8", errors="replace")
    raise ValueError("too many redirects")


class Page(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text, self.links, self.skip, self.noindex = [], [], 0, False
        self._href, self._atext = None, []

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag == "meta" and a.get("name", "").lower() in ("robots", "miseresearchbot") and "noindex" in a.get("content", "").lower():
            self.noindex = True
        if tag in ("script", "style", "noscript", "template"):
            self.skip += 1
        if tag == "a" and a.get("href"):
            self._href, self._atext = a["href"], []

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "template") and self.skip:
            self.skip -= 1
        if tag == "a" and self._href is not None:
            self.links.append((self._href, "".join(self._atext).strip()))
            self._href = None

    def handle_data(self, d):
        if not self.skip:
            self.text.append(d)
            if self._href is not None:
                self._atext.append(d)


def parse(body):
    p = Page()
    try:
        p.feed(body)
    except Exception:
        pass
    return p, re.sub(r"\s+", " ", " ".join(p.text)).strip()


def about_links(base_url, links, limit=2):
    host = urllib.parse.urlsplit(base_url).hostname
    scored, seen = [], {base_url.split("#")[0]}
    for href, text in links:
        u = urllib.parse.urljoin(base_url, href).split("#")[0]
        sp = urllib.parse.urlsplit(u)
        if sp.scheme not in ("http", "https") or sp.hostname != host or u in seen or SKIP_EXT.search(sp.path):
            continue
        hay = (text + " " + urllib.parse.unquote(sp.path)).lower()
        strong = sum(1 for w in STRONG_WORDS if w.lower() in hay)
        score = 3 * strong + sum(1 for w in WEAK_WORDS if w.lower() in hay) if strong else 0
        if score:
            seen.add(u)
            scored.append((score, len(text), u))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [u for _, _, u in scored[:limit]]


def enrich(c):
    p = urllib.parse.urlsplit(c["url"])
    base = f"{p.scheme}://{p.netloc}"
    rules = urllib.robotparser.RobotFileParser()
    log = []
    try:
        s, _, b = fetch(base + "/robots.txt")
        rules.parse(b.decode("utf-8", errors="replace").splitlines() if s == 200 else [])
        if s not in (200, 404, 410):
            return c, [f"robots.txt HTTP {s}: skipped"]
    except Exception as e:
        return c, [f"robots.txt: {e}: skipped"]
    sections = []
    try:
        top_url, body = get(c["url"], rules)
    except Exception as e:
        return c, [f"top: {e}"]
    page, text = parse(body)
    if page.noindex:
        return c, ["noindex: skipped"]
    sections.append(f"--- page: {top_url} ---\n{text[:PAGE_CHARS]}")
    for u in about_links(top_url, page.links):
        try:
            au, ab = get(u, rules)
            ap, at = parse(ab)
            if ap.noindex:
                log.append(f"{u}: noindex")
                continue
            sections.append(f"--- page: {au} ---\n{at[:PAGE_CHARS]}")
            log.append(f"about: {au}")
        except Exception as e:
            log.append(f"{u}: {e}")
    out = dict(c)
    out["body_excerpt"] = "\n".join(sections)
    out["features"] = dict(c.get("features") or {}, pages_fetched=len(sections))
    return out, log


def main(argv):
    src, dst = argv
    done = []
    for line in open(src, encoding="utf-8"):
        if not line.strip():
            continue
        c = json.loads(line)
        out, log = enrich(c)
        done.append(out)
        print(f"{c['id']}: {len(out.get('body_excerpt') or '')} chars; {'; '.join(log)}", file=sys.stderr)
    with open(dst, "w", encoding="utf-8") as f:
        for o in done:
            f.write(json.dumps(o, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main(sys.argv[1:])
