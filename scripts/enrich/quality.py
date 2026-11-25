"""Quality rules of the public-bound fields (site name, Japanese description, tags, categories). Issue #273.

Pure functions: no I/O (only load_synonyms reads the one csv it is given). The ONE definition used by the judge / deliberation prompts and
validation (judge_stage), the publish gate (publish_stage.gate_reason) and the integrity report (scripts/check-data-integrity.py).

Every check_* returns a list of violation codes (empty = fine); a code starts with the field it belongs to
(name_*, description_*, tag_*, category_*). `which(code)` gives the field. No popularity / size criterion anywhere; nothing here
rewrites a meaning: the repairs (suggest_name, to_polite) only cut / convert the form and return None when unsure.
"""
import csv
import re
import unicodedata

NAME_MAX = 40                # a site's own name, in characters
NAME_SUBTITLE_MAX = 12       # with exactly two clauses the shorter one (the subtitle) may be at most this long ...
NAME_LONG_MAX = 80           # a SINGLE-clause title (a long formal name) up to this length is kept as it is (name_acceptable_long)
NAME_SOFT_MAX = 60           # a derived name (derive_name): as many whole segments as fit in this many characters (the first segment always stays)
NAME_PAIR_MAX = 24           # ... and the longer one at most this long, and neither may read as a sentence
TAG_JA_MAX = 20
TAG_EN_MAX = 40
TAG_COUNT_MAX = 5
DESCRIPTION_JA_MAX = 120

_JA = re.compile(r"[ぁ-ゖァ-ヺー㐀-䶿一-鿿豈-﫿]")


def which(code):
    """'name_too_long' -> 'name'; 'tag_label_ja_not_japanese' -> 'tags'; 'category_non_leaf' -> 'categories'."""
    head = code.split("_", 1)[0]
    return {"tag": "tags", "category": "categories"}.get(head, head)


def has_japanese(s):
    """Kana / kanji present (half-width kana are folded by NFKC first)."""
    return bool(_JA.search(unicodedata.normalize("NFKC", s or "")))


def collapse(s):
    return re.sub(r"\s+", " ", s or "").strip()


# ------------------------------------------------------------------------------------------------------------------ names
# Separators between clauses of a <title>: full-width bar, bar, dashes with spaces around them, slash with spaces (or a slash right
# after a closing bracket, as in 「…（JLPA）/…」), full-width slash. A hyphen or slash inside a word (AC/DC, e-mail) is not a separator.
SEP = re.compile(r"\s*[｜|│┃／]\s*|\s+[–—―\-−:：・]\s+|\s+/\s+|(?<=[）)」』】])/\s*|\s*[–—―]\s*")
# Promotional / sales phrases. Only signals: a promo marker counts as a violation only together with a separator or a long name.
PROMO = ("公式通販", "オンラインショップ", "オンラインストア", "通販サイト", "ネットショップ", "を提案", "をご提案", "をお届け", "新感覚",
         "送料無料", "最安値", "激安", "おすすめ", "お役立ち", "official online store", "online shop", "free shipping")
# Site-type words trimmed off the end (or dropped as a whole clause).
SITE_TYPE = ("公式ホームページ", "オフィシャルホームページ", "公式サイト", "オフィシャルサイト", "公式ウェブサイト", "ホームページ", "公式HP", "公式hp",
             "トップページ", "official website", "official site", "official web site", "official homepage", "homepage", "home page", "top page")
CORP_FORMS = ("一般社団法人", "一般財団法人", "公益社団法人", "公益財団法人", "特定非営利活動法人", "認定特定非営利活動法人", "社会福祉法人", "学校法人",
              "国立大学法人", "公立大学法人", "医療法人", "社団法人", "財団法人", "NPO法人", "株式会社", "有限会社", "合同会社", "(株)", "（株）", "㈱",
              "(有)", "（有）", "㈲")
_SITE_TYPE_ONLY = {"home", "top", "トップ", "ホーム", "index", "welcome", "ようこそ", "メニュー"}


# ---- Titles that are not a site's name: error / default pages, shared-service names, broken brackets (#273)
# Long phrases are shared with the judge's placeholder_title (judge_stage.PLACEHOLDER_TITLE_PARTS); the short words only as a whole clause.
# Short words (error, 404, 403, forbidden, it works, エラー, home, top ...) count only when they are the WHOLE clause; "404 Gallery", "エラー研究所",
# "Forbidden City Tours" are real names. Only the long stock phrases below match as a part of a clause.
GENERIC_PAGE_PHRASES = ("not found", "page not found", "404 not found", "403 forbidden", "index of", "welcome to nginx", "apache2", "default page",
                        "under construction", "untitled document", "ページが見つかりません", "見つかりません", "エラー ページ", "エラーページ")
GENERIC_PAGE_EXACT = frozenset(["home", "top", "トップ", "トップページ", "ホーム", "ホームページ", "ようこそ", "index", "welcome", "無題", "no title", "untitled",
                                "error", "404", "403", "500", "forbidden", "access denied", "it works", "エラー"])
_GENERIC_RE = re.compile(r"(?<![a-z0-9])(?:" + "|".join(re.escape(w) for w in GENERIC_PAGE_PHRASES if w.isascii()) + r")(?![a-z0-9])")


def generic_page(clause):
    """A clause that names an error / default / placeholder page: the whole clause is a short stock word (Home, 404, Error, It works!), or it
    holds a long stock phrase (not found HGU, 404 Not Found, Index of /, Welcome to nginx, Apache2 ... Default Page, ページが見つかりません)."""
    f = unicodedata.normalize("NFKC", clause or "").casefold().strip(" .!-_|:/")
    f = re.sub(r"\s+", " ", f)
    return (not f or f in GENERIC_PAGE_EXACT or bool(_GENERIC_RE.search(f))
            or any(w in f for w in GENERIC_PAGE_PHRASES if not w.isascii()))


_PAIRS = {"「": "」", "『": "』", "（": "）", "(": ")", "【": "】", "[": "]", "［": "］", "〈": "〉", "《": "》", "<": ">", "＜": "＞"}


def brackets_balanced(t):
    """False when a bracket / quote is left open or closed without an opening (a broken title: its clauses are not trustworthy)."""
    stack = []
    for ch in t or "":
        if ch in _PAIRS:
            stack.append(_PAIRS[ch])
        elif ch in _PAIRS.values():
            if not stack or stack.pop() != ch:
                return False
    return not stack


# Domains whose pages belong to many different owners (blogs, event / shop / classified services, SNS): the blocklists the discovery uses
# (platforms.txt, platform_pages.txt [host]) plus the services below. A host under one of them says nothing about the site's own name.
SHARED_EXTRA = ("peatix.com", "jmty.jp", "wix.com", "kokucheese.com", "connpass.com", "doorkeeper.jp", "eventbrite.com", "tumblr.com",
                "medium.com", "qiita.com", "zenn.dev", "youtube.com", "ameba.jp", "rakuten.co.jp", "booth.pm", "mercari.com", "linkedin.com",
                "jimdofree.com", "jimdosite.com", "jimdo.com")
SERVICE_ALIASES = ("はてなブログ", "はてなダイアリー", "hatena blog", "ジモティー", "アメブロ", "アメーバブログ", "ameba", "ライブドアブログ", "livedoor blog",
                   "楽天ブログ", "楽天市場", "wix.com", "ピーティックス", "こくチーズ", "twitter", "github pages", "google サイト", "google sites", "ココログ", "cocolog",
                   "jimdo", "アメーバ")
# Short / generic labels of the blocklist domains (main.jp, goo.ne.jp, base.shop, lin.ee, lit.link, line.me, g.page ...) are not service names as a clause.
SHORT_GENERIC = frozenset(["main", "goo", "base", "line", "lin", "lit", "g", "x", "fb", "pages", "stores", "theshop", "themedia"])
_SHARED = {}


def _service_tables():
    if "domains" not in _SHARED:
        import os
        import sys
        d = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pipeline")
        if d not in sys.path:
            sys.path.insert(0, d)
        import hostnames
        doms = set(hostnames.blocklist("platforms.txt")[""]) | set(hostnames.blocklist("platform_pages.txt")["host"]) | set(SHARED_EXTRA)
        fold = lambda s: re.sub(r"[\s.]", "", unicodedata.normalize("NFKC", s).casefold())
        labels = {x.split(".")[0] for x in doms if len(x.split(".")[0]) >= 4 and x.split(".")[0] not in SHORT_GENERIC}
        brands = {fold(x) for x in labels} | {fold(a) for a in SERVICE_ALIASES} | {fold(x) for x in doms if x.split(".")[0] in labels}
        _SHARED["domains"], _SHARED["brands"] = frozenset(doms), frozenset(b for b in brands if b)
    return _SHARED["domains"], _SHARED["brands"]


def shared_service_host(host):
    """The host is (under) a domain shared by many owners (blog / event / shop / SNS service)."""
    h = (host or "").lower().strip(".")
    return any(h == d or h.endswith("." + d) for d in _service_tables()[0])


def service_name_clause(clause):
    """A clause that is only the name of such a service (Peatix, note, はてなブログ, Wix.com, ジモティー, Facebook ...)."""
    c = unicodedata.normalize("NFKC", clause or "").casefold()
    c = re.sub(r"\s*[（(][^）)]*[）)]\s*", " ", c).strip() or c         # Ameba (アメーバ) -> Ameba
    f = re.sub(r"[\s.]", "", c)
    brands = _service_tables()[1]
    return f in brands or re.sub(r"(blog|ブログ|公式|official|com|jp)$", "", f) in brands


def not_a_name_clause(clause, shared):
    return generic_page(clause) or (shared and service_name_clause(clause))


def split_clauses(title):
    return [c.strip() for c in SEP.split(collapse(title)) if c.strip()]


def promo_hit(s):
    t = unicodedata.normalize("NFKC", s).casefold()
    return any(unicodedata.normalize("NFKC", p).casefold() in t for p in PROMO)


def sentence_like(s):
    """A clause that reads as a sentence / slogan, not as a name."""
    return bool(re.search(r"。|、.*、", s)) or s.endswith(("。", "、", "ください", "しよう", "探そう", "ませんか"))


def check_name(name):
    """-> violation codes: name_empty, name_too_long (> NAME_MAX), name_list (3 or more clauses, or a long subtitle / slogan after a
    separator: with two clauses the shorter is <= NAME_SUBTITLE_MAX, the longer <= NAME_PAIR_MAX and neither is a sentence), name_promo (a promo phrase together with a separator or a
    name longer than 20), name_sentence (a sentence)."""
    n = collapse(name if isinstance(name, str) else "")
    if not n:
        return ["name_empty"]
    out = []
    if len(n) > NAME_MAX:
        out.append("name_too_long")
    cl = split_clauses(n)
    if len(cl) >= 3 or (len(cl) == 2 and (min(len(c) for c in cl) > NAME_SUBTITLE_MAX or max(len(c) for c in cl) > NAME_PAIR_MAX
                                          or any(sentence_like(c) for c in cl))):
        out.append("name_list")
    if promo_hit(n) and (len(cl) > 1 or len(n) > 20):
        out.append("name_promo")
    if any(sentence_like(c) for c in cl) and len(cl) == 1 and len(n) > 12:
        out.append("name_sentence")
    return out


_PARTICLES = "のとでにをはがもやへ"
_EN_FUNCTION_WORDS = {"a", "an", "the", "of", "and", "or", "for", "in", "on", "at", "to", "with", "by", "from", "as", "&", "·", "-", "|", "is",
                      "our", "your", "my"}


def is_fragment(s):
    """A cut-off piece: ends in a Japanese particle, an English preposition / article, an open bracket or a separator."""
    t = collapse(s).rstrip(" ,;:、，")
    if not t:
        return True
    if t[-1] in _PARTICLES or t[-1] in "（([「『【":
        return True
    last = re.split(r"\s+", t)[-1].casefold()
    return last in _EN_FUNCTION_WORDS or t.count("(") != t.count(")") or t.count("（") != t.count("）")


def name_acceptable_long(title):
    """A single-clause title (a long formal name) up to NAME_LONG_MAX characters, no promo phrase, not a sentence: kept as the name."""
    t = collapse(title)
    if not t or len(t) > NAME_LONG_MAX or len(split_clauses(t)) != 1 or promo_hit(t) or sentence_like(t) or is_fragment(t):
        return False
    if "·" in t or ": " in t or "：" in t or _POLITE_END.search(t) or _plain(t, True):     # a slogan / a statement, not a name
        return False
    return True


# ---- Anchors: what shows that a clause is the site's NAME (and not a place, a product description or a catch-phrase) (#273)
# A suffix that names the kind of page; with it, the clause in front is the owner's name (the suffix is cut off).
ANCHOR_SUFFIX = SITE_TYPE + ("ウェブサイト", "webサイト", "web site", "website", "公式通販サイト", "公式オンラインショップ", "公式オンラインストア", "公式通販", "オンラインショップ", "オンラインストア", "通販サイト",
                             "公式ブログ", "official online shop", "official online store", "official shop")
# A clause that is a location line, a product / service description or a catch-phrase: never a name candidate.
_PITCH = tuple(re.compile(p) for p in (
    r"(鎮座|所在|所在地|にあります|にあるお店|にある)$", r"(都|道|府|県|市|区|町|村)[^\s、。]{0,8}(にある|にあります|鎮座|に所在)",                                       # 新潟県加茂市鎮座 / 駅前にあります
    r"^[぀-ヿ一-鿿]{1,8}(都|道|府|県)[぀-ヿ一-鿿]{0,8}(市|区|町|村|郡)[぀-ヿ一-鿿]{0,4}$",   # a bare prefecture + city
    r"(を|で|に)(改善|解決|サポート|応援|支援|提供|販売|お届け|実現|創造|目指)(する|します|している|いたします|し)?",   # 〜を改善する 機能性表示食品
    r"なら(ここ|当店|当社|当院|私たち)$", r"なら(お任せ|おまかせ)", r"[A-Za-z0-9一-鿿ァ-ヶ]ならば?[A-Za-z0-9一-鿿ァ-ヶ]{1,6}(へ|におまかせ)?$"))


def pitch_like(clause):
    """A clause that describes a place, a product or a service, or is a catch-phrase (〜ならXX・〜を改善する・〇〇県〇〇市鎮座), not a name."""
    t = collapse(clause)
    return bool(t) and any(p.search(t) for p in _PITCH)


def _strip_anchor_suffix(s):
    """-> (rest, had_suffix). Like _strip_site_type with the longer ANCHOR_SUFFIX list; rest == '' when nothing usable is left (a fragment)."""
    s = s.strip()
    for w in sorted(ANCHOR_SUFFIX, key=len, reverse=True):
        m = re.search(r"[\s\-–—|｜/・:：]*" + re.escape(w) + r"\s*$", s, re.I)
        if m:
            rest = s[:m.start()].strip(" -–—|｜/・:：")
            return ("" if (not rest or is_fragment(rest)) else rest), True
    return s, False


# A phone / fax number, postal code, URL, opening hours or a long digit run is never part of a name (#273).
_CONTACT = re.compile(r"〒|\d{2,5}-\d{1,4}-\d{3,4}|\d{3}-\d{4}|\d{5,}|https?://|www\.|\d{1,2}[:：]\d{2}|(?:tel|fax|電話)\s*[:：]?\s*\d", re.I)
# "Official Site", "Official Dealer Site", "Dealer Site", "Website", "Homepage" ... (with or without a brand word in front): a kind of page, not a name.
_GEN_SITE = re.compile(r"(?:^|\s)(?:official\s+(?:dealer\s+)?|dealer\s+)(?:web\s*site|site|homepage|home\s*page)$|^(?:web\s*site|website|homepage|home\s*page|official)$")


def contact_like(s):
    return bool(_CONTACT.search(unicodedata.normalize("NFKC", s or "")))


def generic_clause(clause, shared=False):
    """A clause that says nothing about the owner: empty, a page-kind word / suffix only (トップページ・ホーム・TOP・公式サイト・ウェブサイト), an error /
    default page, a service name on a shared host, or a Latin 'Official (Dealer) Site' phrase."""
    c = collapse(clause)
    f = unicodedata.normalize("NFKC", c).casefold().strip(" .!-_|:/")
    if not f or f in _SITE_TYPE_ONLY or generic_page(c) or (shared and service_name_clause(c)) or _GEN_SITE.search(f):
        return True
    rest, had = _strip_anchor_suffix(c)
    return had and not rest


# ---- derive_name: the plain, standard way to get a name out of a <title> (owner direction 2026-11-25, #273 / #167) ----------------------
# NO choosing between segments. Split the title into segments, drop the boilerplate ones, drop repeats, keep the rest IN ORDER as far as
# NAME_SOFT_MAX allows (cut only between segments). Never reorders, rewrites, translates or adds a word. Deterministic, no LLM, no I/O.
_DASHES = "–—―\\-−:：·"
_SEG_SPLIT = re.compile(r"\s*[｜│┃|／/]\s*|\s+[" + _DASHES + r"]\s+")
_D = "0-9０-９"
_DASH_IN_NUM = "-－‐‑−"
# a phone / fax number, a postal code, a URL, opening hours: never a part of a name (removed from inside a segment; a segment with nothing else is dropped)
_NOISE = re.compile(
    r"(?:(?:tel|fax|ＴＥＬ|ＦＡＸ|電話|ＴＥＬ)\s*[:：.]?\s*)?[(（]?0[" + _D + r"]{1,4}[)）]?[" + _DASH_IN_NUM + r"][" + _D + r"]{1,4}[" + _DASH_IN_NUM + r"][" + _D + r"]{3,4}"
    r"|〒?\s*[" + _D + r"]{3}[" + _DASH_IN_NUM + r"][" + _D + r"]{4}"
    r"|(?:営業時間|受付時間|OPEN|Open)?\s*[:：]?\s*[" + _D + r"]{1,2}[:：][" + _D + r"]{2}\s*[-〜~～－–]\s*[" + _D + r"]{1,2}[:：][" + _D + r"]{2}"
    r"|https?://\S+|\bwww\.\S+", re.I)
# 【公式】 [Official] ... : a label decoration; the label goes, the rest stays
_LABEL = re.compile(r"[【\[［]\s*(?:公式サイト|公式ホームページ|公式通販|公式|オフィシャルサイト|オフィシャル|official(?:\s+(?:site|website))?)\s*[】\]］]", re.I)
_BOILER_EXACT = frozenset([
    "トップページ", "トップ", "ホーム", "ホームページ", "top", "home", "top page", "home page", "homepage", "welcome", "ようこそ", "index", "メニュー",
    "公式サイト", "公式ホームページ", "公式ウェブサイト", "公式webサイト", "公式hp", "公式", "オフィシャルサイト", "オフィシャルホームページ", "オフィシャル",
    "ウェブサイト", "webサイト", "website", "web site", "official", "official site", "official website", "official web site", "official homepage",
    "official home page", "dealer site", "official dealer site"])
_ERROR_PAGE = re.compile(
    r"(?:page )?not found|404(?: not found| error)?|403(?: forbidden)?|500(?: internal server error)?|forbidden|error|access denied|it works|"
    r"index of\b.*|welcome to nginx|apache2? .*default page|under construction|untitled(?: document)?|no title|無題|エラー|エラーページ|"
    r"ページが見つかりません|見つかりません|ようこそ.*へ")


def _fold_seg(s):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", s or "")).casefold().strip()


def boilerplate_segment(seg, shared=False):
    """WHOLE-segment test only ('404 Gallery', 'Home Sweet Home Bakery' and 'Jeep Official Dealer Site' are not boilerplate): a page-kind word
    (トップページ・ホーム・TOP・公式サイト・ウェブサイト・Official Site ...), an error / default page, or (on a shared host) a service's own name."""
    f = _fold_seg(seg).strip(" .!-_|:/・")
    return (not f or f in _BOILER_EXACT or bool(_ERROR_PAGE.fullmatch(f)) or (shared and service_name_clause(seg)))


# Page-kind phrases hanging on the edge of a segment (S2): removed, the name in front / behind stays. Whole words only (ASCII ones are not
# cut out of a longer word); only at the END or the START of the segment, never from the middle of a name. 公式サイト is removed only when it
# FOLLOWS a name (a segment that is only 公式サイト is boilerplate anyway).
_EDGE_TAIL = re.compile(r"(?:へようこそ|ようこそ|トップページ|(?<![A-Za-z0-9])(?:top\s*ページ|top\s*page|official\s+web\s*site|untitled\s+document)|"
                        r"オフィシャルサイト|公式サイト|無題ドキュメント)\s*$", re.I)
_EDGE_HEAD = re.compile(r"^(?:トップページ|top\s*ページ|top\s*page|無題ドキュメント|untitled\s+document)(?=\s|$)\s*", re.I)   # at the start only as a separate word
_EDGE_DASH_TAIL = re.compile(r"(?:^|\s)[" + _DASHES + r"]+\s*$")
_EDGE_DASH_HEAD = re.compile(r"^\s*[" + _DASHES + r"]+(?:\s|$)")


def _strip_edges(piece):
    """The phrases of _EDGE_TAIL / _EDGE_HEAD off the ends of one segment, repeatedly (a name must remain: '' when nothing does)."""
    prev = None
    while prev != piece:
        prev = piece
        piece = collapse(_EDGE_HEAD.sub("", _EDGE_TAIL.sub("", piece)))
    return piece


def _tidy_sep(raw):
    """The separator as the title wrote it; a bar with a space on one side only ('X ｜Y', left over from a removed 【公式】) -> no spaces."""
    if not raw.strip():
        return " | "
    return raw.strip() if raw[0].isspace() != raw[-1].isspace() else raw


def split_segments(title):
    """-> [(segment, separator in front of it)]: the segments of the title (noise removed, label decorations stripped, edge page-kind phrases
    removed, empty ones dropped); the separator is the text the title itself used there ('' for the first, ' | ' when a joined-up gap has none).
    Full-width / vertical bars and slashes always split (spaces or not); a dash / colon / middle dot only with spaces around it
    (アシザワ・ファインテック, 20th-Century stay whole); a bare dash at either end of the title is dropped."""
    t = collapse(_LABEL.sub("", _NOISE.sub("", collapse(title))))
    t = collapse(_EDGE_DASH_HEAD.sub("", _EDGE_DASH_TAIL.sub("", t)))
    out, pos, sep = [], 0, ""
    for m in list(_SEG_SPLIT.finditer(t)) + [None]:
        piece = _strip_edges(collapse(t[pos:m.start()] if m else t[pos:]))
        if piece:
            out.append((piece, sep if out else ""))
            sep = _tidy_sep(m.group(0)) if m else ""
        elif m and not sep:                      # a gap with nothing in it (a removed phone number ...): the first separator of the run stands
            sep = _tidy_sep(m.group(0))
        pos = m.end() if m else pos
    return out


def _kind(seg):
    """'ja' (kana / kanji), 'en' (Latin letters only), or '' (digits / symbols only)."""
    if has_japanese(seg):
        return "ja"
    return "en" if re.search(r"[A-Za-z]", unicodedata.normalize("NFKC", seg)) else ""


def _cut_long(seg):
    """> NAME_LONG_MAX: cut at the last clause punctuation (、。）」) or space before the limit. None when there is none."""
    head = seg[:NAME_LONG_MAX]
    cut = max(head.rfind(c) for c in "、。）」 ")
    if cut <= 0:
        return None
    out = head[:cut + 1].rstrip(" 、")
    return out if out else None


def derive_name(title, host=""):
    """-> {'name', 'name_ja', 'name_en'} or None (nothing usable: the row is left alone and listed as needs_human).
    Segments (split_segments) -> drop boilerplate (boilerplate_segment) -> drop repeats (NFKC + casefold) -> keep in order, each kept segment
    joined with the separator the title had in front of it, as many as fit in NAME_SOFT_MAX (cut only at a segment boundary; the first
    segment that does not fit ends the list); the first one always stays whole when it is at most NAME_LONG_MAX, else it is cut at the last
    、。）」 or space (none -> None). name_ja / name_en are set only when exactly ONE Japanese and ONE Latin-only segment remain and both read as short names
    (_short_ja_name / _short_en_name: 'brand + tag line' is not a translation pair; each = its segment
    joined); otherwise they are None. Digit-only segments belong to neither."""
    shared = shared_service_host(host)
    seen, keep = set(), []
    for s, sep in split_segments(title):
        k = re.sub(r"\s+", "", _fold_seg(s))
        if boilerplate_segment(s, shared) or k in seen:
            continue
        seen.add(k)
        keep.append((s, sep or " | "))
    if not keep:
        return None
    first = keep[0][0]
    if len(first) > NAME_LONG_MAX:
        first = _cut_long(first)
        if first is None:
            return None
        keep = [(first, "")]
    kept, total = [(first, "")], len(first)
    for s, sep in keep[1:]:
        if total + len(sep) + len(s) > NAME_SOFT_MAX:
            break
        kept.append((s, sep))
        total += len(sep) + len(s)

    def join(items):
        return "".join((sep if i else "") + s for i, (s, sep) in enumerate(items))
    ja = [(s, sep) for s, sep in kept if _kind(s) == "ja"]
    en = [(s, sep) for s, sep in kept if _kind(s) == "en"]
    both = len(ja) == 1 and len(en) == 1 and _short_ja_name(ja[0][0]) and _short_en_name(en[0][0])
    return {"name": join(kept), "name_ja": join(ja) if both else None, "name_en": join(en) if both else None}


_CATCH_MARK = re.compile(r"[、。！？!?「」『』〜~…]|なら|を")


def _short_ja_name(seg):
    """A Japanese segment that reads as a NAME (not a catch-phrase): at most 30 characters, no punctuation / quote / ellipsis, no 〜なら / 〜を,
    and no Latin brand inside (a segment like 'ファンクラブをつくるならBitfan' is the tag line)."""
    return len(seg) <= 30 and not _CATCH_MARK.search(seg) and not re.search(r"[A-Za-z]", unicodedata.normalize("NFKC", seg))


def _short_en_name(seg):
    """A Latin segment that reads as a name: at most 50 characters and not a sentence (a long phrase ending in . ! ?)."""
    return len(seg) <= 50 and not (re.search(r"[.!?]$", seg) and len(seg.split()) >= 5)


def suggest_name(title, host=""):
    """The site's name out of a <title> (derive_name's 'name'), or None."""
    d = derive_name(title, host)
    return d["name"] if d else None


_PLAIN_END = re.compile(
    r"(であった|だった|である|でいる|でいた|ている|ていた|ておる|いる|ある|する|される|された|した|れる|れた|った|んだ|いた|えた|けた|めた|"
    r"できる|ならない|ない|なる|いく|くる|たい|だ)$")
# A terminated (。) sentence that ends in a hiragana dictionary form (う く ぐ す つ ぬ ぶ む る) or a past た / だ is plain (行う・描く・届ける・載せた・集めてきた);
# hiragana nouns that happen to end like that are listed (a noun ending needs no 。-less test: the plain list above covers the common forms).
_POLITE_END = re.compile(r"(です|ます|でした|ました|ません|でしょう|ください)$")
_HIRA_RUN = re.compile(r"[ぁ-ん]+$")


def _dictionary_or_past(core):
    """A sentence ending in a dictionary form (う く ぐ す つ ぬ ぶ む る) or a past た that is a VERB: the trailing hiragana run is an okurigana
    after a kanji / katakana stem (行う・描く・届ける・集めてきた). An all-hiragana word (おやつ・ほたる・みんなのうた) or a run that holds a particle
    followed by more text (世界のおやつ) is a noun and is left alone."""
    m = _HIRA_RUN.search(core)
    if not m or m.group()[-1] not in "うくぐすつぬぶむるた":
        return False
    if not re.search(r"[一-龥ァ-ヶー]$", core[:m.start()]):
        return False
    return not re.search(r"[のがをはにとでも].{2,}$", m.group())


def _plain(core, terminated):
    if _POLITE_END.search(core):
        return False
    return bool(_PLAIN_END.search(core)) or (terminated and _dictionary_or_past(core))


def _sentences(text):
    t = collapse(text)
    parts = re.findall(r"[^。！？!?]+[。！？!?]?", t)
    return [p.strip() for p in parts if p.strip()]


def _core(sentence):
    return sentence.rstrip("。！？!? ")


def _terminated(sentence):
    return sentence.rstrip().endswith(("。", "！", "？", "!", "?"))


def check_description_style(text):
    """Japanese description: 体言止め (a noun, with or without 「。」) or 「です・ます・でした・ました」. -> ['description_style'] when a sentence
    ends in the 常体 (である/だ/ている/する/れる/った/ない ...). Not a Japanese text -> []."""
    if not isinstance(text, str) or not has_japanese(text):
        return []
    for s in _sentences(text):
        core = _core(s)
        if core and _plain(core, _terminated(s)):
            return ["description_style"]
    return []


_POLITE_RULES = (  # (regex on the sentence core, replacement); first match wins; order matters
    (r"であった$", "でした"), (r"だった$", "でした"), (r"である$", "です"), 
    (r"ていた$", "ていました"), (r"ている$", "ています"), (r"ではない$", "ではありません"), (r"がない$", "がありません"),
    (r"される$", "されます"), (r"された$", "されました"), (r"します$", "します"), (r"(?<=[一-龥ァ-ヶー])する$", "します"), (r"した$", "しました"),
    (r"れる$", "れます"), (r"れた$", "れました"), (r"できる$", "できます"), (r"(?<=[一-龥][えけせてねへめべげぜでぺ])る$", "ます"), (r"ある$", "あります"), (r"なる$", "なります"),
)
_DA_OK_PREV = re.compile(r"[^ぁ-ゖ]$|[のめろとこ]$")


def _polite_sentence(core):
    if not core or not _plain(core, True):
        return core
    # 体言止め or a verb we do not convert for sure: None
    if core.endswith("だ") and not core.endswith(("だった", "んだ")):
        prev = core[:-1]
        if prev and _DA_OK_PREV.search(prev) and not prev.endswith(("ただ", "おだ")):
            return prev + "です"
        return None
    for pat, rep in _POLITE_RULES:
        m = re.search(pat, core)
        if m:
            if rep == "します" and pat == r"します$":
                return core
            head = core[:m.start()]
            if not head:
                return None
            return head + rep
    return None


def to_polite(text):
    """常体 -> 「です・ます」, deterministic and conservative. Returns the text unchanged when it already passes, None when any plain ending
    is not covered with certainty (the caller then rejects / asks the LLM). Only the sentence endings change; no word is added or removed."""
    if not isinstance(text, str):
        return None
    if not check_description_style(text):
        return text
    out = []
    for s in _sentences(text):
        m = re.match(r"^(.*?)([。！？!?]?)$", s, re.S)
        core, stop = m.group(1), m.group(2)
        p = _polite_sentence(core)
        if p is None:
            return None
        out.append(p + stop)
    res = "".join(out)
    return res if res and not check_description_style(res) else None


# ------------------------------------------------------------------------------------------------------------------- tags
def _stem_word(w):
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 4 and re.search(r"(ss|x|ch|sh|z)es$", w):
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
        w = w[:-1]
    return w[:-2] + "y" if len(w) > 3 and w.endswith("ie") else w


def normalize_tag_key(label):
    """Comparison key of a tag label: NFKC (full/half width), lower case, hyphen / underscore / space / middle dot removed between words,
    English plural -> singular (simple stemming). Japanese text keeps its characters. Does NOT apply the synonym table (see TagSynonyms)."""
    s = unicodedata.normalize("NFKC", label or "").casefold().strip()
    if not s:
        return ""
    words = re.split(r"[\s\-_・/]+", s)
    return "".join(_stem_word(w) if re.fullmatch(r"[a-z0-9]+", w) else w for w in words if w)


class TagSynonyms:
    """data/tag-synonyms.csv: canonical_ja, canonical_en, variants (separated by '|'). key(label) -> the canonical normalized key."""

    def __init__(self, rows):
        self.rows, self.map = rows, {}
        for ja, en, variants in rows:
            canon = normalize_tag_key(ja)
            for v in [ja, en] + variants:
                self.map[normalize_tag_key(v)] = canon

    def key(self, label):
        k = normalize_tag_key(label)
        return self.map.get(k, k)

    def canonical(self, label):
        """-> (ja, en) of the canonical entry for a label, or None."""
        k = self.key(label)
        for ja, en, _v in self.rows:
            if normalize_tag_key(ja) == k:
                return ja, en
        return None


def load_synonyms(path):
    """Reads and VALIDATES the csv. Raises ValueError listing every invalid row (the test runs it on the real file)."""
    errs, rows, seen = [], [], {}
    with open(path, encoding="utf-8", newline="") as f:
        for n, r in enumerate(csv.DictReader(f), 2):
            ja, en = (r.get("canonical_ja") or "").strip(), (r.get("canonical_en") or "").strip()
            variants = [v.strip() for v in (r.get("variants") or "").split("|") if v.strip()]
            if not has_japanese(ja):
                errs.append(f"line {n}: canonical_ja {ja!r} has no Japanese")
            if not en or has_japanese(en):
                errs.append(f"line {n}: canonical_en {en!r} must be a non-Japanese label")
            for v in [ja, en] + variants:
                k = normalize_tag_key(v)
                if not k:
                    errs.append(f"line {n}: empty variant")
                elif seen.get(k, ja) != ja:
                    errs.append(f"line {n}: {v!r} already belongs to {seen[k]!r}")
                else:
                    seen[k] = ja
            rows.append((ja, en, variants))
    if errs:
        raise ValueError("; ".join(errs))
    return TagSynonyms(rows)


_DEFAULT = {}
DATA = __import__("os").path.join(__import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__)))), "data")


def default_synonyms():
    """data/tag-synonyms.csv, loaded once (the judge and the publish gate must use the same table)."""
    if "syn" not in _DEFAULT:
        _DEFAULT["syn"] = load_synonyms(__import__("os").path.join(DATA, "tag-synonyms.csv"))
    return _DEFAULT["syn"]


def load_proper_nouns(path):
    """data/tag-proper-nouns.csv (column term): Latin-script proper nouns that may stand as label_ja (FPGA, iOS, Linux). Validated."""
    errs, out = [], set()
    with open(path, encoding="utf-8", newline="") as f:
        for n, r in enumerate(csv.DictReader(f), 2):
            t = (r.get("term") or "").strip()
            k = unicodedata.normalize("NFKC", t).casefold()
            if not t or has_japanese(t) or not re.search(r"[A-Za-z]", t):
                errs.append(f"line {n}: {t!r} must be a Latin-script term")
            elif k in out:
                errs.append(f"line {n}: duplicate {t!r}")
            out.add(k)
    if errs:
        raise ValueError("; ".join(errs))
    return frozenset(out)


def proper_nouns():
    if "pn" not in _DEFAULT:
        _DEFAULT["pn"] = load_proper_nouns(__import__("os").path.join(DATA, "tag-proper-nouns.csv"))
    return _DEFAULT["pn"]


ADMIN_ONLY = frozenset(["市", "町", "村", "県", "区", "都", "府", "道", "郡", "市町村", "都道府県"])


def parse_tag(t):
    """A tag as stored -> (label_ja, label_en). Accepts 'ja|en', a dict (label_ja/label_en or ja/en) or a plain string (both labels the
    same, which check_tags then rejects when it is not Japanese). None for anything else."""
    if isinstance(t, dict):
        ja, en = t.get("label_ja", t.get("ja")), t.get("label_en", t.get("en"))
    elif isinstance(t, str):
        ja, _sep, en = t.partition("|")
        en = en if _sep else ja
    else:
        return None
    if not isinstance(ja, str) or not isinstance(en, str):
        return None
    return collapse(ja), collapse(en)


def check_tags(tags, synonyms=None, proper=None):
    """-> codes: tag_malformed, tag_count, tag_empty, tag_label_ja_not_japanese (label_ja needs kana/kanji), tag_label_en_not_english
    (label_en empty or Japanese), tag_too_long, tag_duplicate (after normalize_tag_key / the synonym table)."""
    if not isinstance(tags, list):
        return ["tag_malformed"]
    synonyms = synonyms or default_synonyms()
    proper = proper_nouns() if proper is None else proper
    out, keys = [], set()
    if len(tags) > TAG_COUNT_MAX:
        out.append("tag_count")
    for t in tags:
        p = parse_tag(t)
        if p is None:
            out.append("tag_malformed")
            continue
        ja, en = p
        if not ja:
            out.append("tag_empty")
            continue
        if ja in ADMIN_ONLY:
            out.append("tag_admin_only")
        if not has_japanese(ja) and unicodedata.normalize("NFKC", ja).casefold() not in proper:
            out.append("tag_label_ja_not_japanese")
        if not en or has_japanese(en):
            out.append("tag_label_en_not_english")
        if len(ja) > TAG_JA_MAX or len(en) > TAG_EN_MAX:
            out.append("tag_too_long")
        k, k2 = synonyms.key(ja), (synonyms.key(en) if en else "")
        if k in keys or (k2 and k2 in keys):
            out.append("tag_duplicate")
        keys.update({k, k2} - {""})
    return list(dict.fromkeys(out))


# ------------------------------------------------------------------------------------------------------------------ categories
def leaf_ids_of(rows):
    """rows = [(id, parent_id)] of the category tree -> the set of ids that have no child."""
    ids = {i for i, _p in rows}
    return ids - {p for _i, p in rows if p}


def check_categories(ids, leaf_ids):
    """Only leaf categories, and at least one. -> category_none, category_non_leaf (also an unknown id)."""
    if not isinstance(ids, list) or not ids:
        return ["category_none"]
    return ["category_non_leaf"] if any(i not in leaf_ids for i in ids) else []


# ------------------------------------------------------------------------------------------------------------------- prompts
PROMPT_DESCRIBE_RULES = (
    "STYLE RULES (violations are rejected and you must redo them): dj must end as a noun phrase (体言止め, e.g. 「小さな工房の紹介サイト。」) or in "
    "「です・ます」 form (e.g. 「作品を紹介しています。」); never the plain form (である・だ・している・する・行う・描く・された・った・ない). "
    "Each tag in tg is ONE string \"日本語ラベル|english label\" (e.g. \"動画|video\"): the part before | is a Japanese noun label written in kana/kanji "
    "(at most 20 characters; a Latin-script proper noun such as FPGA or Linux may stand there as it is), the part after | is a short lowercase "
    "English label; no duplicates or near duplicates (video / videos / 動画), and no administrative word such as 市・町・県 alone. "
    "sn = the site's OWN name, at most 40 characters, copied verbatim from the title or the text: no slogan, no list of services or places, "
    "nothing after a separator, no company-form noise unless it is part of the formal name; null when the text does not show it.")
PROMPT_TRIAGE_RULES = ("CATEGORY RULE (overrides any earlier remark about parent or fallback categories such as business or society): give LEAF "
                       "category ids only (an id that has a deeper, indented child below it is NOT allowed); when no leaf fits, give the "
                       "closest leaf, never a top-level or parent id.")


def _fold(s):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", s or "")).casefold()


def source_values(rendered):
    """The title / meta description / body values of a rendered <candidate> block, without its labels (url:, title:, lang:, features:)."""
    out = []
    for line in rendered.splitlines():
        if line.startswith(("<candidate", "</candidate", "url: ", "lang: ", "features: ", "body_excerpt:")):
            continue
        for lab in ("title: ", "meta_description: "):
            if line.startswith(lab):
                line = line[len(lab):]
        if line.strip() and line.strip() != "(none)":
            out.append(line)
    return "\n".join(out)


def check_site_name(name, source):
    """The LLM's `sn` (None is allowed = not shown): check_name rules, and it must be copied from the source text (nothing invented)."""
    if name is None:
        return []
    errs = check_name(name)
    if not errs and _fold(name) not in _fold(source_values(source)):
        errs = ["name_not_in_source"]
    return errs
