"""Deterministic entity extraction (F3): identify the article(s) and material(s) in a request.

This is used (a) by the offline reasoning policy, (b) by the verifier as an independent second
opinion on what the article is, and (c) to normalise what an LLM extracted. The lexicons contain
only common-language words (materials, filler words). No tariff knowledge lives here: which heading
names an article is always decided from the indexed corpus text."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .textutil import STOPWORDS, normalise, stem

# longest first so "stainless steel" wins over "steel"
MATERIAL_PATTERNS: list[tuple[str, str]] = [
    ("stainless steel", r"stainless[\s-]+steel"),
    ("cast iron", r"cast[\s-]+iron"),
    ("galvanised iron", r"galvani[sz]ed[\s-]+(?:iron|steel)"),
    ("hard rubber", r"hard[\s-]+rubber"),
    ("carbon fibre", r"carbon[\s-]+fib(?:re|er)"),
    ("steel", r"steel"), ("iron", r"iron"),
    ("plastic", r"plastics?|polypropylene|polyethylene|polycarbonate|pvc|abs|melamine|acrylic|resin"),
    ("rubber", r"rubber"), ("aluminium", r"alumini?um"), ("copper", r"copper"), ("brass", r"brass"),
    ("bronze", r"bronze"), ("zinc", r"zinc"), ("nickel", r"nickel"), ("titanium", r"titanium"),
    ("glass", r"glass"), ("ceramic", r"ceramic|porcelain"), ("wood", r"wood(?:en)?|bamboo"),
    ("paper", r"paper|cardboard"), ("cotton", r"cotton"), ("wool", r"(?<!steel )(?<!iron )wool(?:len)?"),
    ("silk", r"silk"), ("polyester", r"polyester"), ("nylon", r"nylon"), ("leather", r"leather"),
    ("silicone", r"silicone"), ("gold", r"gold"), ("silver", r"silver"), ("ivory", r"ivory"),
    ("bone", r"bone"), ("horn", r"horn"), ("wax", r"wax"), ("fabric", r"fabric|textile|cloth"),
]
_MAT_RE = [(name, re.compile(r"\b(?:" + pat + r")\b")) for name, pat in MATERIAL_PATTERNS]

IRON_STEEL = {"steel", "stainless steel", "iron", "cast iron", "galvanised iron"}

FILLER = set("""moulded molded outer inner body small large big new used portable complete finished unfinished
premium quality high grade standard type model brand made make piece pieces pcs pc pack packs unit units item
items product products article articles set sets kit kits material materials stated state unknown unspecified
not no none na n/a per each nos number qty quantity litre litres liter liters ml l kg g gram grams cm mm m inch
inches size sizes colour color colours colors capacity volume weight""".split())

SET_MARKERS = re.compile(
    r"\b(set|kit|combo|combination|assortment|gift\s+pack|gift\s+set|hamper|bundle|comprising|consisting\s+of|"
    r"containing|includes?|including|put\s+up\s+for\s+retail)\b")
_CUT = re.compile(r"\b(with|having|made\s+of|made\s+from|made\s+out\s+of|featuring|fitted\s+with|"
                  r"complete\s+with|for\s+use|used\s+for|for)\b")


@dataclass
class ArticleNP:
    text: str
    tokens: list[str]
    head: str

    def as_dict(self) -> dict:
        return {"text": self.text, "tokens": self.tokens, "head": self.head}


@dataclass
class Entities:
    articles: list[ArticleNP] = field(default_factory=list)
    materials: list[str] = field(default_factory=list)
    is_set: bool = False
    missing: list[str] = field(default_factory=list)

    @property
    def primary(self) -> ArticleNP | None:
        return self.articles[0] if self.articles else None

    def as_dict(self) -> dict:
        return {"articles": [a.as_dict() for a in self.articles], "materials": self.materials,
                "is_set": self.is_set, "missing_info": self.missing}


def find_materials(text: str) -> list[str]:
    t = normalise(text)
    found: list[str] = []
    consumed: list[tuple[int, int]] = []
    for name, rx in _MAT_RE:
        for m in rx.finditer(t):
            if any(a <= m.start() < b for a, b in consumed):
                continue
            consumed.append((m.start(), m.end()))
            if name not in found:
                found.append(name)
    return found


def strip_materials(text: str) -> str:
    t = normalise(text)
    spans: list[tuple[int, int]] = []
    for _, rx in _MAT_RE:  # spans are taken on the original text so look-behinds stay valid
        for m in rx.finditer(t):
            if not any(a <= m.start() < b for a, b in spans):
                spans.append((m.start(), m.end()))
    for a, b in sorted(spans, reverse=True):
        t = t[:a] + " " + t[b:]
    return t


def _np_from(clause: str) -> ArticleNP | None:
    t = strip_materials(clause)
    toks = []
    for raw in re.findall(r"[a-z]+", t):
        if raw in STOPWORDS or raw in FILLER or len(raw) < 2:
            continue
        toks.append(stem(raw))
    if not toks:
        return None
    return ArticleNP(" ".join(toks), toks, toks[-1])


def extract_entities(description: str, fields: dict | None = None) -> Entities:
    fields = fields or {}
    desc = normalise(description)
    ent = Entities()
    declared = [m for m in (fields.get("materials") or [])]
    mats = find_materials(desc)
    for d in declared:
        for m in find_materials(str(d)) or [normalise(str(d)).strip()]:
            if m and m not in mats:
                mats.append(m)
    ent.materials = mats
    main_clause = re.split(r"[;,\n]| - ", desc)[0]
    is_set = bool(fields.get("is_set_for_retail")) or bool(SET_MARKERS.search(main_clause))
    comps: list[str] = []
    if is_set:
        stripped = SET_MARKERS.sub(" ", main_clause)
        parts = [p for p in re.split(r"\band\b|&|\+|\bplus\b", stripped) if p.strip()]
        comps = parts
        if len(parts) < 2:
            is_set = False
    if not is_set:
        comps = [_CUT.split(main_clause)[0]]
    for c in comps:
        np_ = _np_from(_CUT.split(c)[0] if is_set else c)
        if np_ and all(np_.text != a.text for a in ent.articles):
            ent.articles.append(np_)
    ent.is_set = is_set and len(ent.articles) >= 2
    if not ent.articles:
        ent.missing.append("article not identifiable from the description")
    if not mats:
        ent.missing.append("material not stated")
    return ent


def article_tokens(article: str) -> list[str]:
    """Content tokens of an article name given to a tool. Materials are NOT stripped here (the caller was
    told to pass the article's own name; 'steel wool' is an article, 'wool' alone is a material)."""
    out = []
    for raw in re.findall(r"[a-z]+", normalise(article)):
        if raw in STOPWORDS or raw in FILLER or len(raw) < 2:
            continue
        out.append(stem(raw))
    return out
