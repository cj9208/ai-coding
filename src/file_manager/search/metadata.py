"""元数据搜索后端：SQLite FTS5（词/前缀）+ LIKE 中缀兜底 + 结构化筛选。

命中分两级，先严后宽：
1. 严格：FTS 命中（词或前缀）∪ 每个关键词都作为子串出现
2. 放宽：仅当严格一级 0 命中且关键词多于 1 个时，改成"任一关键词出现"

FTS 表达式的构造（折叠 + 每词括号组 + ASCII 前缀通配）在通用层
``storage.fts``；括号是 load-bearing 的：`fold_cjk` 会把"财报"展开成
`财 财报 报` 三个单元，不加括号时 `A B OR C D` 的 AND/OR 优先级会串味，
把只含单个"报"字的文件也拽进来。
"""

from __future__ import annotations

from sqlalchemy import and_, bindparam, func, or_, select, text
from sqlalchemy.orm import Session, selectinload

from storage import match_expr
from storage import token_expr as shared_token_expr

from ..models import FileMeta
from ..services.files import order_by_for
from .base import SearchBackend, SearchHit, SearchQuery, SearchResult

#: (可读标签, FTS 列名, ORM 列)；标签用于渲染"命中来源"
_FIELDS: tuple[tuple[str, str, object], ...] = (
    ("文件名", "filename", FileMeta.original_filename),
    ("标题", "title", FileMeta.title),
    ("备注", "notes", FileMeta.notes),
    ("标签", "tags", FileMeta.tags),
)

#: LIKE 的转义符；% _ \ 三个字符按字面量处理
_ESCAPE = "\\"


def _tokens(q: str) -> list[str]:
    """按空白切出用户原始词——LIKE 要用原文，CJK 子串天然可搜。"""
    return [t for t in (q or "").split() if t]


def _like_pattern(token: str) -> str:
    escaped = (
        token.replace(_ESCAPE, _ESCAPE + _ESCAPE)
        .replace("%", _ESCAPE + "%")
        .replace("_", _ESCAPE + "_")
    )
    return f"%{escaped}%"


def _ilike(token: str, column):
    """SQLite 的 LIKE 对 ASCII 大小写不敏感，中文按字面匹配。"""
    return column.ilike(_like_pattern(token), escape=_ESCAPE)


def token_expr(token: str) -> str:
    """单个关键词 → 折叠后的 FTS 子表达式（通用层实现，本域启用前缀通配）。

    纯 ASCII 词加前缀通配（`repo*`），折叠出来的 CJK 单元精确匹配。
    """
    return shared_token_expr(token, prefix=True)


def build_match(q: str) -> str:
    """整句 → 词之间 AND 的 MATCH 表达式（严格一级）。"""
    return match_expr(_tokens(q), prefix=True)


def build_match_any(q: str) -> str:
    """整句 → 词之间 OR 的 MATCH 表达式（放宽一级）。"""
    return match_expr(_tokens(q), joiner=" OR ", prefix=True)


def _fts_cond(match: str):
    if not match:
        return None
    return FileMeta.id.in_(
        text("SELECT rowid FROM files_fts WHERE files_fts MATCH :m").bindparams(m=match)
    )


def _like_cond(tokens: list[str], joiner):
    """每个词在四列任一列里作为子串出现；joiner 决定词之间 AND 还是 OR。"""
    if not tokens:
        return None
    per_token = [
        or_(*[_ilike(t, column) for _, _, column in _FIELDS])  # type: ignore[misc]
        for t in tokens
    ]
    return joiner(*per_token) if len(per_token) > 1 else per_token[0]


class MetadataSearchBackend(SearchBackend):
    name = "metadata"
    available = True
    description = "搜索文件名、标题、备注、标签（支持部分匹配）"

    def search(self, db: Session, query: SearchQuery) -> SearchResult:
        structural = self._conds(query)
        tokens = _tokens(query.q)
        attempts = self._attempts(tokens)

        chosen = None
        for conds, exprs, relaxed in attempts:
            total = (
                db.scalar(
                    select(func.count())
                    .select_from(FileMeta)
                    .where(*structural, *conds)
                )
                or 0
            )
            if total:
                chosen = (structural + conds, exprs, relaxed, total)
                break
        if chosen is None:  # 各级都 0 命中：停在最宽的一级，total 为 0
            conds, exprs, relaxed = attempts[-1]
            chosen = (structural + conds, exprs, relaxed, 0)
        conds, exprs, relaxed, total = chosen

        page = max(query.page, 1)
        stmt = (
            select(FileMeta)
            .where(*conds)
            .options(selectinload(FileMeta.project))
            .order_by(*order_by_for(query.sort))
            .offset((page - 1) * query.page_size)
            .limit(query.page_size)
        )
        rows = list(db.scalars(stmt).all())
        matched_map = self._matched_fields(db, exprs, tokens, [row.id for row in rows])
        hits = [
            SearchHit(
                id=row.id,
                filename=row.original_filename,
                title=row.title,
                project_id=row.project_id,
                project_name=row.project.name,
                uploader_name=row.uploader_name,
                team_name=row.team_name,
                extension=row.extension,
                size=row.size,
                created_at=row.created_at,
                matched=matched_map.get(row.id, []),
            )
            for row in rows
        ]
        return SearchResult(
            hits=hits,
            total=total,
            page=page,
            page_size=query.page_size,
            mode=self.name,
            relaxed=relaxed,
        )

    @staticmethod
    def _attempts(tokens: list[str]) -> list[tuple[list, list[str], bool]]:
        """候选组合，由严到宽：每项 = (条件列表, 每词的 FTS 子表达式, 是否放宽)。"""
        if not tokens:
            return [([], [], False)]

        exprs = [x for x in (token_expr(t) for t in tokens) if x]

        def level(fts: str, like, relaxed: bool):
            keyword = or_(*[c for c in (_fts_cond(fts), like) if c is not None])
            return [keyword], exprs, relaxed

        attempts = [level(" AND ".join(exprs), _like_cond(tokens, and_), False)]
        if len(tokens) > 1:
            attempts.append(level(" OR ".join(exprs), _like_cond(tokens, or_), True))
        return attempts

    @staticmethod
    def _matched_fields(
        db: Session, exprs: list[str], tokens: list[str], ids: list[int]
    ) -> dict[int, list[str]]:
        """逐列探测当前页文件的命中来源。

        每个词单独作为 `{col}: (…)` 列过滤探测，所以"财报在标题、归档在标签"
        这类跨列的行也能分别归因；某列没被 FTS 命中却被子串命中时标"·模糊"。
        """
        if not ids or (not exprs and not tokens):
            return {}
        found: dict[int, list[str]] = {i: [] for i in ids}
        for label, col_name, column in _FIELDS:
            hit: set[int] = set()
            if exprs:
                probe = " OR ".join(f"{col_name}: ({e})" for e in exprs)
                stmt = text(
                    "SELECT rowid FROM files_fts "
                    "WHERE files_fts MATCH :m AND rowid IN :ids"
                ).bindparams(
                    bindparam("m", value=probe),
                    bindparam("ids", expanding=True, value=tuple(ids)),
                )
                hit = set(db.execute(stmt).scalars())
                for rid in hit:
                    found[rid].append(label)
            if tokens:
                stmt = (
                    select(FileMeta.id)
                    .where(FileMeta.id.in_(ids))
                    .where(or_(*[_ilike(t, column) for t in tokens]))
                )
                for rid in set(db.scalars(stmt).all()) - hit:
                    found[rid].append(f"{label}·模糊")
        return found

    @staticmethod
    def _conds(query: SearchQuery):
        """结构化筛选（不含关键词）。"""
        conds = []
        if query.project_id:
            conds.append(FileMeta.project_id == query.project_id)
        if query.team_id:
            conds.append(FileMeta.team_id == query.team_id)
        if query.uploader_id:
            conds.append(FileMeta.uploader_id == query.uploader_id)
        if query.extension:
            conds.append(FileMeta.extension == query.extension.lstrip(".").lower())
        return conds
