"""Deterministic lexical query shaping.

M1 shaping is deliberately inside the lexical domain — no vectors, no
rewriting of intent. The only hard part is CJK: this machine's FTS5 has no
CJK tokenizer, so ``storage.fold_cjk`` expands each CJK run into
single chars + bigrams and ``match_expr`` ANDs them. That means one long
CJK run as a single token demands an exact contiguous phrase — too strict
for recall. Rule: runs of <= 3 chars stay one token; longer runs are cut
into 2-char segments, each still folded, giving a phrase-adjacency
requirement per segment instead of across the whole run.

A future LLM keyword-expansion shaper (CH03_03 representation alignment)
plugs in beside this one through the same protocol.
"""

from __future__ import annotations

import re

from .protocols import ShapedQuery

_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
_ASCII_WORD = re.compile(r"[A-Za-z0-9]+")
_MAX_STRICT_RUN = 3


def _split_cjk_run(run: str) -> list[str]:
    if len(run) <= _MAX_STRICT_RUN:
        return [run]
    return [run[i : i + 2] for i in range(0, len(run) - 1, 2)]


class LexicalShaper:
    """Implements :class:`rag.protocols.QueryShaper`."""

    name = "lexical"

    def shape(self, query: str) -> ShapedQuery:
        tokens: list[str] = []
        cursor = 0
        for m in _CJK_RUN.finditer(query):
            tokens.extend(
                w.lower() for w in _ASCII_WORD.findall(query[cursor : m.start()])
            )
            tokens.extend(_split_cjk_run(m.group(0)))
            cursor = m.end()
        tokens.extend(w.lower() for w in _ASCII_WORD.findall(query[cursor:]))
        return ShapedQuery(raw=query, tokens=[t for t in tokens if t])
