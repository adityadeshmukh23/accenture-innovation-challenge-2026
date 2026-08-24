"""One prepared view of the request's grounding context, built once and shared.

Before this existed, the cheap gating signals and the verifier each tokenised
the whole context independently. On a 5,000-clause contract that made the
"cheap" tier cost 195ms of the 300ms budget -- so the expensive check it was
supposed to be gating could no longer be afforded. The cheap tier has to be
cheap by construction, not by intention.

Two things fix it: derive the context once, and BOUND what the cheap tier
looks at. The cheap pass scans at most `CHEAP_SCAN_LIMIT` sentences and says so
in its output; the verifier, which is the check that is allowed to be
expensive, sees everything.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property

from .textlib import IdfIndex, content_tokens, extract_numbers, proper_nouns, sentences

#: How many context sentences the cheap tier is permitted to scan.
CHEAP_SCAN_LIMIT = 400


@dataclass
class PreparedContext:
    raw: str
    sentences: list[str] = field(default_factory=list)
    truncated_for_cheap: bool = False

    @classmethod
    def build(cls, text: str) -> "PreparedContext":
        sents = sentences(text)
        return cls(raw=text or "", sentences=sents,
                   truncated_for_cheap=len(sents) > CHEAP_SCAN_LIMIT)

    # -- cheap tier: bounded prefix ---------------------------------------- #
    @cached_property
    def cheap_slice(self) -> str:
        if not self.truncated_for_cheap:
            return self.raw
        return " ".join(self.sentences[:CHEAP_SCAN_LIMIT])

    @cached_property
    def cheap_tokens(self) -> set[str]:
        return set(content_tokens(self.cheap_slice))

    @cached_property
    def cheap_propers(self) -> set[str]:
        return proper_nouns(self.cheap_slice)

    @cached_property
    def cheap_numbers(self) -> list[dict]:
        return extract_numbers(self.cheap_slice)

    # -- full tier: everything, for the verifier --------------------------- #
    @cached_property
    def tokens(self) -> set[str]:
        return set(content_tokens(self.raw))

    @cached_property
    def propers(self) -> set[str]:
        return proper_nouns(self.raw)

    @cached_property
    def numbers(self) -> list[dict]:
        return extract_numbers(self.raw)

    _index: IdfIndex | None = field(default=None, repr=False)

    def index_for(self, deadline=None) -> IdfIndex:
        """Build (once) the IDF index. Deadline-aware: a build cut short
        indexes a prefix and reports `partial`."""
        if self._index is None:
            self._index = IdfIndex(self.sentences, deadline=deadline)
        return self._index

    @property
    def index(self) -> IdfIndex:
        return self.index_for(None)

    def __len__(self) -> int:
        return len(self.sentences)
