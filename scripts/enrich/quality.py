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
    return bool(_JA.search(s or ""))


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


def _strip_site_type(s):
    s = s.strip()
    for w in sorted(SITE_TYPE, key=len, reverse=True):
        m = re.search(r"[\s\-–—|｜/・:：]*" + re.escape(w) + r"\s*$", s, re.I)
        if m:
            rest = s[:m.start()].strip(" -–—|｜/・:：")
            if rest:
                return rest
            return ""      # the whole clause was only the site type
    return s


def _strip_corp(s):
    for w in CORP_FORMS:
        if s.startswith(w) and len(s) > len(w):
            return s[len(w):].lstrip(" 　")
        if s.endswith(w) and len(s) > len(w):
            return s[:-len(w)].rstrip(" 　")
    return s


def _brand_tokens(host):
    labels = [x for x in (host or "").lower().split(".") if x and x not in ("www", "com", "net", "org", "jp", "co", "or", "ne", "ac", "go", "ed", "lg", "gr", "info", "biz")]
    toks = []
    for lab in labels:
        for t in re.split(r"[-_]", lab):
            if len(t) >= 3:
                toks.append(t)
    return toks


def _cut_at_word(s):
    """> NAME_MAX: cut at a space (whole words only); None when that leaves too little or the text has no word boundary."""
    if len(s) <= NAME_MAX:
        return s
    words, out = s.split(" "), ""
    if len(words) < 2:
        return None
    for w in words:
        nxt = (out + " " + w).strip()
        if len(nxt) > NAME_MAX:
            break
        out = nxt
    if len(out) < 8 or out.count("(") != out.count(")") or out.count("（") != out.count("）"):
        return None
    return out


def suggest_name(title, host=""):
    """The site's own name out of a <title>, or None (the LLM / a human must supply one). Deterministic; never cuts a word in two."""
    t = collapse(title)
    if not t:
        return None
    if not check_name(t):
        if len(split_clauses(t)) > 1:
            rest = [c for c in (_strip_site_type(c) for c in split_clauses(t)) if c and c.casefold() not in _SITE_TYPE_ONLY]
            return rest[0] if len(rest) == 1 and not check_name(rest[0]) else t
        clean = _strip_site_type(t)
        return clean if clean and not check_name(clean) else None
    cands = []
    for c in split_clauses(t):
        c = _strip_site_type(c)
        if not c or c.casefold() in _SITE_TYPE_ONLY or promo_hit(c) or sentence_like(c):
            continue
        c2 = _strip_corp(c)
        cands.append(c2 if c2 else c)
    if not cands:
        return None
    toks = _brand_tokens(host)
    pick = next((c for c in cands if any(tk in unicodedata.normalize("NFKC", c).casefold() for tk in toks)), cands[0])
    pick = _cut_at_word(pick)
    if pick is None or check_name(pick):
        return None
    return pick


# ---------------------------------------------------------------------------------------------------------- description style
_PLAIN_END = re.compile(
    r"(であった|だった|である|でいる|でいた|ている|ていた|ておる|いる|ある|する|される|された|した|れる|れた|った|んだ|いた|えた|けた|めた|"
    r"できる|ならない|ない|なる|いく|くる|たい|だ)$")
_POLITE_END = re.compile(r"(です|ます|でした|ました|ません|でしょう|ください)$")


def _sentences(text):
    t = collapse(text)
    parts = re.findall(r"[^。！？!?]+[。！？!?]?", t)
    return [p.strip() for p in parts if p.strip()]


def _core(sentence):
    return sentence.rstrip("。！？!? ")


def check_description_style(text):
    """Japanese description: 体言止め (a noun, with or without 「。」) or 「です・ます・でした・ました」. -> ['description_style'] when a sentence
    ends in the 常体 (である/だ/ている/する/れる/った/ない ...). Not a Japanese text -> []."""
    if not isinstance(text, str) or not has_japanese(text):
        return []
    for s in _sentences(text):
        core = _core(s)
        if not core or _POLITE_END.search(core):
            continue
        if _PLAIN_END.search(core):
            return ["description_style"]
    return []


_POLITE_RULES = (  # (regex on the sentence core, replacement); first match wins; order matters
    (r"であった$", "でした"), (r"だった$", "でした"), (r"である$", "です"), 
    (r"ていた$", "ていました"), (r"ている$", "ています"), (r"ではない$", "ではありません"), (r"がない$", "がありません"),
    (r"される$", "されます"), (r"された$", "されました"), (r"します$", "します"), (r"する$", "します"), (r"した$", "しました"),
    (r"れる$", "れます"), (r"れた$", "れました"), (r"できる$", "できます"), (r"ある$", "あります"), (r"いる$", "います"), (r"なる$", "なります"),
)
_DA_OK_PREV = re.compile(r"[^ぁ-ゖ]$|[のめろとこ]$")


def _polite_sentence(core):
    if not core or _POLITE_END.search(core) or not _PLAIN_END.search(core):
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
        return w[:-1]
    return w


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
            if not variants:
                errs.append(f"line {n}: no variants")
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


def check_tags(tags, synonyms=None):
    """-> codes: tag_malformed, tag_count, tag_empty, tag_label_ja_not_japanese (label_ja needs kana/kanji), tag_label_en_not_english
    (label_en empty or Japanese), tag_too_long, tag_duplicate (after normalize_tag_key / the synonym table)."""
    if not isinstance(tags, list):
        return ["tag_malformed"]
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
        if not has_japanese(ja):
            out.append("tag_label_ja_not_japanese")
        if not en or has_japanese(en):
            out.append("tag_label_en_not_english")
        if len(ja) > TAG_JA_MAX or len(en) > TAG_EN_MAX:
            out.append("tag_too_long")
        k = synonyms.key(ja) if synonyms else normalize_tag_key(ja)
        k2 = synonyms.key(en) if synonyms and en else normalize_tag_key(en)
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
PROMPT_NAME_RULE = ("Site name (never output a name field; this is the rule the title is held to): the site's OWN name only, at most 40 characters; "
                    "no slogans, product lists, prefecture lists or text after a separator.")
PROMPT_DESCRIBE_RULES = (
    "STYLE RULES (violations are rejected and you must redo them): dj must end as a noun phrase (体言止め, e.g. 「小さな工房の紹介サイト。」) or in "
    "「です・ます」 form (e.g. 「作品を紹介しています。」); never the plain form (である・だ・している・する・された・った・ない). "
    "Each tag in tg is ONE string \"日本語ラベル|english label\" (e.g. \"動画|video\"): the part before | is a Japanese noun label written in kana/kanji "
    "(at most 20 characters), the part after | is a short lowercase English label; no duplicates or near duplicates (video / videos / 動画), "
    "and no administrative words such as 市・町・県 alone.")
PROMPT_TRIAGE_RULES = ("CATEGORY RULE: give LEAF category ids only (an id that has a deeper, indented child below it is NOT allowed); "
                       "when no leaf fits, give the closest leaf, never a top-level or parent id.")
