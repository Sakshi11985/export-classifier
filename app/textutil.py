"""Small, dependency-free text helpers shared by retrieval, entity extraction,
the verifier and the evaluation metrics."""
from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter

STOPWORDS = frozenset("""a an the and or of for with without in on to from by as at is are be been being it its this that
these those into over under than then there their them they not no nor other others such any each per via etc
whether which who whom whose what when where while shall may also thereof therein hereby""".split())

_TOKEN = re.compile(r"[a-z0-9]+")


def normalise(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "")
    t = "".join(ch for ch in t if unicodedata.category(ch) not in ("Cf", "Cc") or ch in "\n\t ")
    return t.lower()


def stem(w: str) -> str:
    if len(w) <= 3:
        return w
    if w.endswith("ies"):
        return w[:-3] + "y"
    if w.endswith(("ches", "shes", "sses", "xes", "zes")):
        return w[:-2]
    if w.endswith("s") and not w.endswith(("ss", "us", "is")):
        return w[:-1]
    return w


def tokenize(text: str, drop_stop: bool = True, do_stem: bool = True) -> list[str]:
    out = []
    for tok in _TOKEN.findall(normalise(text)):
        if drop_stop and tok in STOPWORDS:
            continue
        out.append(stem(tok) if do_stem else tok)
    return out


def squash(text: str) -> str:
    """Whitespace/case/quote-insensitive form used for verbatim-quote checks."""
    t = normalise(text)
    t = t.replace("‘", "'").replace("’", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", re.sub(r"[\"']", "", t)).strip()


class BM25:
    def __init__(self, docs: dict[str, list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.docs = docs
        self.tf = {i: Counter(t) for i, t in docs.items()}
        self.len = {i: len(t) for i, t in docs.items()}
        self.avg = (sum(self.len.values()) / max(1, len(docs))) or 1.0
        df: Counter = Counter()
        for c in self.tf.values():
            df.update(c.keys())
        n = len(docs)
        self.idf = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}

    def score(self, query_tokens: list[str]) -> dict[str, float]:
        out: dict[str, float] = {}
        for i, tf in self.tf.items():
            s = 0.0
            for q in query_tokens:
                f = tf.get(q)
                if not f:
                    continue
                s += self.idf.get(q, 0.0) * (f * (self.k1 + 1)) / (
                    f + self.k1 * (1 - self.b + self.b * self.len[i] / self.avg))
            if s > 0:
                out[i] = s
        return out


def rrf(rankings: list[list[str]], k: int = 60) -> dict[str, float]:
    scores: dict[str, float] = {}
    for r in rankings:
        for pos, i in enumerate(r):
            scores[i] = scores.get(i, 0.0) + 1.0 / (k + pos + 1)
    return scores
