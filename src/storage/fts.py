"""FTS5 full-text search over SQLite, with the CJK folding this machine needs.

Verified on this environment: the stock ``unicode61`` tokenizer drops CJK
runs entirely, and the ``trigram`` / ``editdist3`` extensions are unavailable.
The workaround (proven in file_manager, required again by research_agent's
findings_fts) is symmetric folding: on write, expand each CJK run into
single chars + a 2-char sliding window; on query, fold the keywords the same
way and match adjacent windows — so "财报" hits the bigram, not scattered
single chars.

Both ends of that contract live here as one module on purpose: write-side
``FtsTable.upsert`` and query-side ``match_expr`` call the same ``fold_cjk``,
so they can never drift apart the way a copy-paste between projects would.
"""

from __future__ import annotations

import re
from typing import Iterable, Mapping, Sequence

from sqlalchemy import text
from sqlalchemy.orm import Session

# 常见 CJK 区块：汉字扩展A、汉字、兼容表意、平假名、片假名、谚文音节
_CJK_RE = re.compile(
    "[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff" "\u3040-\u30ff\uac00-\ud7a3]+"
)


def fold_cjk(text_value: str | None) -> str:
    """把 CJK 连续段折叠成 单字+双字滑窗，空格分隔；其余字符原样保留。"""

    def fold_run(run: str) -> str:
        units = list(run) + [run[i : i + 2] for i in range(len(run) - 1)]
        return " " + " ".join(units) + " "

    return _CJK_RE.sub(lambda m: fold_run(m.group(0)), text_value or "")


def _quote_phrase(unit: str) -> str:
    return '"' + unit.replace('"', '""') + '"'


def token_expr(token: str, *, prefix: bool = False) -> str:
    """One folded keyword as one parenthesised group.

    The parentheses are load-bearing: unparenthesised ``A B AND C D`` lets
    FTS5's AND/OR precedence bleed across folded units, matching documents
    that contain only a single stray char.

    ``prefix=True`` appends FTS5's ``*`` wildcard to folded ASCII units
    (``repo*``) while CJK units stay exact — the graded behaviour proven in
    file_manager's metadata search. A unit that is not word-like (punctuation
    survives folding: ``a.pdf，``) becomes a quoted phrase — unquoted it
    makes FTS5 raise ``syntax error near "."`` and the whole query dies,
    which a caller cannot tell apart from "no results"."""
    units: list[str] = []
    for t in fold_cjk(token).split():
        if not t:
            continue
        if not t.isalnum():
            units.append(_quote_phrase(t))
        elif prefix and t.isascii():
            units.append(t + "*")
        else:
            units.append(t)
    return f"({' AND '.join(units)})" if units else ""


def match_expr(
    tokens: Iterable[str], *, joiner: str = " AND ", prefix: bool = False
) -> str:
    """FTS5 MATCH expression requiring every token (or any, with
    ``joiner=' OR '``) — the graded-relaxation knob file_manager's search
    uses."""
    return joiner.join(x for x in (token_expr(t, prefix=prefix) for t in tokens) if x)


class FtsTable:
    """A contentless-ish FTS5 virtual table addressed by external rowid.

    Sync is explicit (no SQLite triggers): the owning service calls
    ``upsert``/``delete`` inside its own transaction, which keeps the
    folding logic in one testable place and lets a future non-SQLite index
    slot in behind the same calls.
    """

    def __init__(self, name: str, columns: Sequence[str]):
        self.name = name
        self.columns = tuple(columns)

    def create(self, db: Session) -> None:
        cols = ", ".join(self.columns)
        db.execute(
            text(f"CREATE VIRTUAL TABLE IF NOT EXISTS {self.name} USING fts5({cols})")
        )

    def upsert(self, db: Session, rowid: int, values: Mapping[str, str | None]) -> None:
        """Insert or replace the indexed row for ``rowid``; every value is
        folded on the way in."""
        missing = set(values) - set(self.columns)
        if missing:
            raise KeyError(f"columns not in {self.name}: {sorted(missing)}")
        folded = {c: fold_cjk(values.get(c)) for c in self.columns}
        bound = db.execute(
            text(f"SELECT 1 FROM {self.name} WHERE rowid = :rowid"),  # nosec B608
            {"rowid": rowid},
        ).first()
        if bound is None:
            col_list = ", ".join(["rowid", *self.columns])
            placeholders = ", ".join([":rowid", *[f":{c}" for c in self.columns]])
            db.execute(
                text(
                    f"INSERT INTO {self.name} ({col_list}) "  # nosec B608
                    f"VALUES ({placeholders})"
                ),
                {"rowid": rowid, **folded},
            )
        else:
            assignments = ", ".join(f"{c} = :{c}" for c in self.columns)
            db.execute(
                text(
                    f"UPDATE {self.name} SET {assignments} "  # nosec B608
                    f"WHERE rowid = :rowid"
                ),
                {"rowid": rowid, **folded},
            )

    def delete(self, db: Session, rowid: int) -> None:
        db.execute(
            text(f"DELETE FROM {self.name} WHERE rowid = :rowid"),  # nosec B608
            {"rowid": rowid},
        )

    def rowids_for(self, db: Session, match: str) -> list[int]:
        """Rowids matching an expression (build it with ``match_expr``)."""
        if not match:
            return []
        rows = db.execute(
            text(
                f"SELECT rowid FROM {self.name} "  # nosec B608
                f"WHERE {self.name} MATCH :m"
            ),
            {"m": match},
        ).fetchall()
        return [r[0] for r in rows]
