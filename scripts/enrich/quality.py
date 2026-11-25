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
SEP = re.compile(r"\s*[｜|／]\s*|\s+[–—―\-−]\s+|\s+/\s+|(?<=[）)」』】])/\s*|\s*[–—―]\s*")
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


def _strip_site_type(s):
    """Removes a trailing site-type word. '' when the whole clause was only that word OR what is left is a fragment (山田太郎の|ホームページ)."""
    s = s.strip()
    for w in sorted(SITE_TYPE, key=len, reverse=True):
        m = re.search(r"[\s\-–—|｜/・:：]*" + re.escape(w) + r"\s*$", s, re.I)
        if m:
            rest = s[:m.start()].strip(" -–—|｜/・:：")
            return "" if (not rest or is_fragment(rest)) else rest
    return s


def _corp_form(s):
    return next((w for w in CORP_FORMS if s.startswith(w) or s.endswith(w)), None)


def _strip_corp(s):
    for w in CORP_FORMS:
        if s.startswith(w) and len(s) > len(w):
            return s[len(w):].lstrip(" 　")
        if s.endswith(w) and len(s) > len(w):
            return s[:-len(w)].rstrip(" 　")
    return s


GENERIC_LABELS = frozenset("""www web site home info mail store shop cart member members app apps cloud blog news online page pages net com org
    portal service services official top main index html blogs wiki forum bbs""".split())
_SLD = frozenset(["co", "or", "ne", "ac", "go", "ed", "lg", "gr", "ad", "com", "net", "org", "gov", "edu"])


def registrable_label(host):
    """The label of the registrable domain (blog.example.co.jp -> example; shop.foo.jp -> foo). Simple public-suffix rule: a two-letter ccTLD
    with a second-level word (co / or / ne ...) takes the label before them."""
    labels = [x for x in (host or "").lower().strip(".").split(".") if x]
    if len(labels) < 2:
        return labels[0] if labels else ""
    if len(labels[-1]) == 2 and len(labels) >= 3 and labels[-2] in _SLD:
        return labels[-3]
    return labels[-2]


def _brand_tokens(host):
    """Tokens of the registrable domain label only (never a subdomain word such as blog / shop / news), without generic words."""
    return [t for t in re.split(r"[-_]", registrable_label(host)) if len(t) >= 3 and t not in GENERIC_LABELS]


def _has_token(text, tok):
    """Whole-word match (art does not match Smart)."""
    return re.search(r"(?<![a-z0-9])" + re.escape(tok) + r"(?![a-z0-9])", text) is not None


def _shorten(s):
    """> NAME_MAX: only at a clause boundary (a trailing bracket group, or the first sentence stop). Never in the middle of a word / phrase."""
    if len(s) <= NAME_MAX:
        return s
    for pat in (r"\s*[（(][^）)]*[）)]\s*$", r"。.*$"):
        t = re.sub(pat, "", s).strip()
        if t and t != s and len(t) <= NAME_MAX and not is_fragment(t):
            return t
    return None


def name_acceptable_long(title):
    """A single-clause title (a long formal name) up to NAME_LONG_MAX characters, no promo phrase, not a sentence: kept as the name."""
    t = collapse(title)
    if not t or len(t) > NAME_LONG_MAX or len(split_clauses(t)) != 1 or promo_hit(t) or sentence_like(t) or is_fragment(t):
        return False
    if "·" in t or ": " in t or "：" in t or _POLITE_END.search(t) or _plain(t, True):     # a slogan / a statement, not a name
        return False
    return True


def suggest_name(title, host=""):
    """The site's own name out of a <title>, or None (the LLM / a human must supply one). Deterministic. It only picks a whole clause:
    a corporate-form clause (one only), else the clause that matches the host's brand, else the single clause left; with several clauses
    left and no such sign it returns None rather than guess. Never cuts inside a clause; a result that is a fragment is refused."""
    t = collapse(title)
    if not t:
        return None
    if not brackets_balanced(t):      # a bracket left open / closed without an opening: the title is broken, its clauses cannot be trusted
        return None
    shared = shared_service_host(host)
    clauses = split_clauses(t)
    if not check_name(t):
        if len(clauses) > 1:
            rest = [c for c in (_strip_site_type(c) for c in clauses) if c and c.casefold() not in _SITE_TYPE_ONLY and not not_a_name_clause(c, shared)]
            if len(rest) == 1:
                return rest[0] if not check_name(rest[0]) else None
            return t if len(rest) == len(clauses) else None
        clean = _strip_site_type(t)
        return clean if clean and not check_name(clean) and not not_a_name_clause(clean, shared) else None
    cands = []
    for c in clauses:
        c = _strip_site_type(c)
        if not c or c.casefold() in _SITE_TYPE_ONLY or promo_hit(c) or sentence_like(c) or is_fragment(c) or not_a_name_clause(c, shared):
            continue
        if c in CORP_FORMS:        # a corporate form alone is not a name
            continue
        cands.append(c)
    if not cands:
        return None
    toks = [] if shared else _brand_tokens(host)     # a shared service's own label says nothing about this site
    folded = lambda c: unicodedata.normalize("NFKC", c).casefold()
    corp = [c for c in cands if _corp_form(c)]
    brand = [c for c in cands if any(_has_token(folded(c), tk) for tk in toks)]
    if len(brand) == 1:
        pick = brand[0]
    elif len(corp) == 1 and not brand:
        pick = corp[0]
    elif len(cands) == 1:
        pick = cands[0]
    else:
        return None
    # the corporate form is part of the formal name: dropped only when the name would not fit in NAME_MAX otherwise (S9)
    if len(pick) > NAME_MAX and _strip_corp(pick) != pick:
        pick = _strip_corp(pick)
    pick = _shorten(pick)
    if pick is None or check_name(pick) or is_fragment(pick) or pick in CORP_FORMS:
        return None
    return pick


# ---------------------------------------------------------------------------------------------------------- description style
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
