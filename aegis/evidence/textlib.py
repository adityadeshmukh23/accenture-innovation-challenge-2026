"""Shared text primitives for the evidence checks. No external NLP dependency."""
from __future__ import annotations

import math
import re
from collections import Counter
from typing import Iterable

_WORD = re.compile(r"[A-Za-z][A-Za-z'-]+")
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")

STOP = {
    "the", "a", "an", "of", "to", "in", "for", "on", "and", "or", "is", "are", "was",
    "were", "be", "been", "with", "as", "at", "by", "that", "this", "it", "its", "from",
    "we", "our", "you", "your", "i", "my", "they", "their", "he", "she", "his", "her",
    "will", "would", "can", "could", "should", "has", "have", "had", "do", "does", "did",
    "but", "if", "then", "than", "so", "such", "there", "these", "those", "also", "any",
    "all", "into", "over", "per", "which", "what", "when", "where", "who", "how", "based",
    "provided", "documents", "document", "according",
}

NEGATION_CUES = {
    "not", "no", "never", "cannot", "cant", "without", "excluding", "excluded",
    "denies", "denied", "absent", "none", "neither", "nor", "lacks", "lacking",
    "ineligible", "unavailable", "prohibited", "disallowed", "non-refundable",
}

HEDGE_CUES = {
    "may", "might", "could", "possibly", "perhaps", "likely", "unlikely", "approximately",
    "around", "roughly", "generally", "typically", "appears", "suggests", "estimated",
    "about", "seems", "believe", "probably", "usually",
}

CONFIDENCE_CUES = {
    "definitely", "certainly", "guaranteed", "guarantee", "always", "never", "must",
    "will", "ensures", "assured", "confirms", "confirmed", "precisely", "exactly",
}


def sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT_SPLIT.split(text or "") if s.strip()]


def words(text: str) -> list[str]:
    return _WORD.findall(text or "")


def content_tokens(text: str) -> list[str]:
    return [w.lower() for w in words(text) if w.lower() not in STOP and len(w) > 2]


def proper_nouns(text: str) -> set[str]:
    """Capitalised tokens that are not sentence-initial and not common words."""
    out: set[str] = set()
    for sent in sentences(text):
        toks = words(sent)
        for i, t in enumerate(toks):
            if i == 0:
                continue
            if t[0].isupper() and t.lower() not in STOP and len(t) > 2:
                out.add(t.lower())
    return out


# --------------------------------------------------------------------------- #
# Numeric extraction
# --------------------------------------------------------------------------- #
_NUM = re.compile(
    r"(?P<cur>[$£€])?\s*(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"\s*(?P<suf>%|percent|bps|basis points|million|billion|bn|m\b|k\b)?"
)

_UNIT_WORDS = re.compile(
    r"\b(days?|weeks?|months?|years?|hours?|minutes?|business days?|mg|ml|kg|"
    r"per cent|percent|bps|shares?|units?)\b", re.I
)


def extract_numbers(text: str) -> list[dict]:
    """Pull numbers out with a normalised value and a unit tag.

    The unit tag is what makes comparison meaningful: '4.2%' and '4.2 days' are
    different facts, and a claim is only contradicted by an evidence number of
    the *same* unit.
    """
    out: list[dict] = []
    for m in _NUM.finditer(text or ""):
        raw = m.group("num")
        try:
            val = float(raw.replace(",", ""))
        except ValueError:
            continue
        suf = (m.group("suf") or "").strip().lower()
        cur = m.group("cur") or ""
        unit = ""
        scale = 1.0
        if suf in {"%", "percent", "per cent"}:
            unit = "percent"
        elif suf in {"bps", "basis points"}:
            unit = "percent"
            scale = 0.01
        elif suf in {"million", "m"}:
            scale = 1e6
        elif suf in {"billion", "bn"}:
            scale = 1e9
        elif suf == "k":
            scale = 1e3
        if cur:
            unit = "currency"
        if not unit:
            tail = (text or "")[m.end(): m.end() + 24]
            um = _UNIT_WORDS.search(tail)
            if um:
                unit = um.group(0).lower().rstrip("s").replace("business day", "day")
            else:
                unit = "count"
        out.append({
            "raw": m.group(0).strip(),
            "value": val * scale,
            "unit": unit,
            "start": m.start(),
        })
    return out


# --------------------------------------------------------------------------- #
# IDF-weighted retrieval
# --------------------------------------------------------------------------- #
class IdfIndex:
    """A tiny IDF-weighted vector index over context sentences.

    Built per request. This is the component whose cost scales with document
    size, and it is the reason a large context can genuinely blow the 300ms
    budget rather than a simulated delay pretending to.
    """

    def __init__(self, sents: Iterable[str]):
        self.sents: list[str] = list(sents)
        self.tokens: list[list[str]] = [content_tokens(s) for s in self.sents]
        df: Counter[str] = Counter()
        for toks in self.tokens:
            df.update(set(toks))
        n = max(1, len(self.sents))
        self.idf: dict[str, float] = {t: math.log(1.0 + n / (1.0 + c)) for t, c in df.items()}
        self.vectors: list[dict[str, float]] = []
        self.norms: list[float] = []
        for toks in self.tokens:
            tf = Counter(toks)
            vec = {t: (1.0 + math.log(c)) * self.idf.get(t, 0.0) for t, c in tf.items()}
            self.vectors.append(vec)
            self.norms.append(math.sqrt(sum(v * v for v in vec.values())) or 1.0)
        self.vocab: set[str] = set(self.idf)

    def query_vector(self, text: str) -> tuple[dict[str, float], float]:
        tf = Counter(content_tokens(text))
        vec = {t: (1.0 + math.log(c)) * self.idf.get(t, 0.5) for t, c in tf.items()}
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        return vec, norm

    def best_k(self, text: str, k: int = 3, deadline=None) -> list[tuple[int, float]]:
        """Top-k matching context sentences, best first.

        Used for the verifier's re-answer: a multi-part question ("what is the
        return AND the expense ratio?") is not answerable from one sentence,
        and scoring against a single sentence manufactures false mismatches.
        """
        qv, qn = self.query_vector(text)
        scored: list[tuple[int, float]] = []
        for i, vec in enumerate(self.vectors):
            if deadline is not None and (i & 0x3F) == 0 and deadline():
                break
            if len(qv) < len(vec):
                dot = sum(w * vec.get(t, 0.0) for t, w in qv.items())
            else:
                dot = sum(w * qv.get(t, 0.0) for t, w in vec.items())
            if dot <= 0.0:
                continue
            scored.append((i, dot / (qn * self.norms[i])))
        scored.sort(key=lambda x: -x[1])
        return scored[:k]

    def best(self, text: str, deadline=None) -> tuple[int, float]:
        """Return (index, cosine) of the best-matching context sentence.

        `deadline` is a zero-arg callable returning True when the caller must
        stop; scanning yields partial results rather than blowing the budget.
        """
        qv, qn = self.query_vector(text)
        best_i, best_s = -1, 0.0
        for i, vec in enumerate(self.vectors):
            if deadline is not None and (i & 0x3F) == 0 and deadline():
                break
            if len(qv) < len(vec):
                dot = sum(w * vec.get(t, 0.0) for t, w in qv.items())
            else:
                dot = sum(w * qv.get(t, 0.0) for t, w in vec.items())
            if dot <= 0.0:
                continue
            sim = dot / (qn * self.norms[i])
            if sim > best_s:
                best_i, best_s = i, sim
        return best_i, best_s
