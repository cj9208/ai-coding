"""SessionStore: the only module that writes the DB (01 §6 — subagents never do).

Durability rules encoded here:
- crash-safety = re-read the session and continue; therefore a phase
  transition and the artifacts/budget bumps it produced commit as ONE
  transaction (`transition`).
- finding/capture ids (F*, C*) appear in user-visible citations, so they are
  allocated here and stable once written.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Literal, Sequence

from pydantic import BaseModel
from sqlalchemy import Integer, cast, func, select
from sqlalchemy.orm import Session

from storage import SqliteClient, sha256_hex

from ..contracts.models import Artifact, Finding
from .db import (
    ArtifactRow,
    Base,
    BudgetCounterRow,
    CaptureRow,
    FindingRow,
    GapEventRow,
    ReportRow,
    SessionRow,
    utcnow,
)


class SessionStore:
    def __init__(self, client: SqliteClient):
        self.client = client

    @classmethod
    def open(cls, db: str | Path) -> "SessionStore":
        """One-stop wiring onto the shared storage layer: correctly-configured
        client + idempotent create_all of this project's tables."""
        client = SqliteClient(db)
        client.init_schema(Base.metadata)
        return cls(client)

    # -- sessions -----------------------------------------------------------

    def create_session(self, session_id: str) -> None:
        with self.client.session() as db:
            if db.get(SessionRow, session_id) is None:
                db.add(SessionRow(id=session_id, phase="INTAKE"))
                db.commit()

    def get_phase(self, session_id: str) -> str:
        with self.client.session() as db:
            row = db.get(SessionRow, session_id)
            return row.phase if row else ""

    def get_stop_reason(self, session_id: str) -> str | None:
        with self.client.session() as db:
            row = db.get(SessionRow, session_id)
            return row.stop_reason if row else None

    # -- atomic transition (01 §2: write phase AND its artifact, then commit)

    def transition(
        self,
        session_id: str,
        new_phase: str,
        artifacts: Sequence[BaseModel] = (),
        bumps: dict[str, int] | None = None,
        stop_reason: str | None | Literal[False] = False,
    ) -> None:
        """`stop_reason` sentinel: False = leave unchanged, None = clear."""
        with self.client.session() as db:
            row = db.get(SessionRow, session_id)
            if row is None:
                raise KeyError(f"no such session: {session_id}")
            row.phase = new_phase
            row.updated_at = utcnow()
            if stop_reason is not False:
                row.stop_reason = stop_reason
            for art in artifacts:
                self._insert_artifact(db, session_id, art)
            for name, n in (bumps or {}).items():
                self._bump(db, session_id, name, n)
            db.commit()

    # -- artifacts ----------------------------------------------------------

    def append_artifact(self, session_id: str, artifact: BaseModel) -> int:
        with self.client.session() as db:
            seq = self._insert_artifact(db, session_id, artifact)
            db.commit()
            return seq

    @staticmethod
    def _insert_artifact(db: Session, session_id: str, artifact: BaseModel) -> int:
        kind = type(artifact).__name__
        if isinstance(artifact, Artifact):
            artifact = artifact.model_copy(
                update={
                    "session_id": artifact.session_id or session_id,
                    "created_at": artifact.created_at or utcnow(),
                }
            )
        row = ArtifactRow(
            session_id=session_id,
            kind=kind,
            payload=artifact.model_dump_json(exclude_none=True),
        )
        db.add(row)
        db.flush()
        return row.seq

    def artifacts(self, session_id: str, kind: str) -> list[dict]:
        """Full history for a kind, oldest first (replay story, 05 §5.3)."""
        with self.client.session() as db:
            rows = db.scalars(
                select(ArtifactRow)
                .where(ArtifactRow.session_id == session_id, ArtifactRow.kind == kind)
                .order_by(ArtifactRow.seq)
            ).all()
            return [json.loads(r.payload) for r in rows]

    def latest_artifact(self, session_id: str, kind: str) -> dict | None:
        arts = self.artifacts(session_id, kind)
        return arts[-1] if arts else None

    # -- captures ------------------------------------------------------------

    def next_capture_id(self, session_id: str) -> str:
        return self._next_id(session_id, "C", CaptureRow.id)

    def add_capture(
        self,
        session_id: str,
        *,
        url: str,
        text: str,
        adapter: str,
        title: str = "",
        fetch_status: str = "ok",
    ) -> tuple[str, bool]:
        """Returns (capture_id, deduped). UNIQUE(url, content_hash) doubles as
        budget protection: a re-fetched identical page costs no new slot."""
        content_hash = sha256_hex(text, length=16)
        with self.client.session() as db:
            existing = db.scalars(
                select(CaptureRow).where(
                    CaptureRow.session_id == session_id,
                    CaptureRow.url == url,
                    CaptureRow.content_hash == content_hash,
                )
            ).first()
            if existing:
                return existing.id, True
            cid = self._next_id_in(session_id, "C", db, CaptureRow.id)
            db.add(
                CaptureRow(
                    id=cid,
                    session_id=session_id,
                    url=url,
                    content_hash=content_hash,
                    text=text,
                    adapter=adapter,
                    title=title,
                    fetch_status=fetch_status,
                )
            )
            db.commit()
            return cid, False

    def get_capture(self, session_id: str, capture_id: str) -> CaptureRow | None:
        with self.client.session() as db:
            return db.get(CaptureRow, (capture_id, session_id))

    def captures(self, session_id: str) -> list[CaptureRow]:
        with self.client.session() as db:
            return list(
                db.scalars(
                    select(CaptureRow)
                    .where(CaptureRow.session_id == session_id)
                    .order_by(CaptureRow.id)
                )
            )

    # -- findings -------------------------------------------------------------

    def next_finding_id(self, session_id: str) -> str:
        return self._next_id(session_id, "F", FindingRow.id)

    def allocate_finding_ids(self, session_id: str, n: int) -> list[str]:
        """Batch-allocate stable ids; the collector labels findings at write
        time so report citations never shift under a re-run."""
        with self.client.session() as db:
            first = int(self._next_id_in(session_id, "F", db, FindingRow.id)[1:])
        return [f"F{i}" for i in range(first, first + n)]

    def add_findings(
        self, session_id: str, findings: Iterable[Finding], batch: int = 0
    ) -> None:
        with self.client.session() as db:
            for f in findings:
                db.add(
                    FindingRow(
                        id=f.id,
                        session_id=session_id,
                        capture_id=f.capture_id,
                        claim=f.claim,
                        kind=f.kind,
                        decision_criteria=json.dumps(
                            f.decision_criteria, ensure_ascii=False
                        ),
                        touches_candidates=json.dumps(
                            f.touches_candidates, ensure_ascii=False
                        ),
                        quote=f.quote,
                        confidence=f.confidence,
                        support_key=f.support_key,
                        batch=batch,
                    )
                )
            db.commit()

    def findings(self, session_id: str) -> list[Finding]:
        with self.client.session() as db:
            rows = db.scalars(
                select(FindingRow)
                .where(FindingRow.session_id == session_id)
                .order_by(FindingRow.id)
            ).all()
        return [
            Finding(
                id=r.id,
                session_id=session_id,
                capture_id=r.capture_id,
                claim=r.claim,
                kind=r.kind,
                decision_criteria=json.loads(r.decision_criteria or "[]"),
                touches_candidates=json.loads(r.touches_candidates or "[]"),
                quote=r.quote,
                confidence=r.confidence,
                support_key=r.support_key,
            )
            for r in rows
        ]

    def find_capture(
        self, session_id: str, url: str, content_hash: str
    ) -> CaptureRow | None:
        with self.client.session() as db:
            return db.scalars(
                select(CaptureRow).where(
                    CaptureRow.session_id == session_id,
                    CaptureRow.url == url,
                    CaptureRow.content_hash == content_hash,
                )
            ).first()

    # -- gap events -------------------------------------------------------------

    def gap_event(
        self, session_id: str, kind: str, gap_id: str, status: str, ref: str = ""
    ) -> None:
        with self.client.session() as db:
            db.add(
                GapEventRow(
                    session_id=session_id,
                    kind=kind,
                    gap_id=gap_id,
                    status=status,
                    ref=ref,
                )
            )
            db.commit()

    def open_gaps(self, session_id: str, kind: str) -> list[str]:
        """Gap ids whose latest event is not 'resolved'/'asked'."""
        with self.client.session() as db:
            rows = db.scalars(
                select(GapEventRow)
                .where(GapEventRow.session_id == session_id, GapEventRow.kind == kind)
                .order_by(GapEventRow.id)
            ).all()
        status: dict[str, str] = {}
        for r in rows:
            status[r.gap_id] = r.status
        return [gid for gid, st in status.items() if st == "open"]

    # -- budgets -----------------------------------------------------------------

    def set_budget(self, session_id: str, name: str, max_value: int) -> None:
        with self.client.session() as db:
            row = db.get(BudgetCounterRow, (session_id, name))
            if row is None:
                db.add(
                    BudgetCounterRow(
                        session_id=session_id, name=name, used=0, max=max_value
                    )
                )
            else:
                row.max = max_value
            db.commit()

    def budget_used(self, session_id: str, name: str) -> int:
        with self.client.session() as db:
            row = db.get(BudgetCounterRow, (session_id, name))
            return row.used if row else 0

    def bump_budget(self, session_id: str, name: str, n: int = 1) -> int:
        with self.client.session() as db:
            used = self._bump(db, session_id, name, n)
            db.commit()
            return used

    @staticmethod
    def _bump(db: Session, session_id: str, name: str, n: int) -> int:
        row = db.get(BudgetCounterRow, (session_id, name))
        if row is None:
            row = BudgetCounterRow(session_id=session_id, name=name, used=0, max=0)
            db.add(row)
            db.flush()
        row.used += n
        return row.used

    # -- reports -------------------------------------------------------------------

    def save_report(self, session_id: str, path: str) -> None:
        with self.client.session() as db:
            row = db.get(ReportRow, session_id)
            if row is None:
                db.add(ReportRow(session_id=session_id, path=path))
            else:
                row.path = path
                row.rendered_at = utcnow()
            db.commit()

    def get_report_path(self, session_id: str) -> str | None:
        with self.client.session() as db:
            row = db.get(ReportRow, session_id)
            return row.path if row else None

    # -- id allocation ---------------------------------------------------------------

    def _next_id(self, session_id: str, prefix: str, column) -> str:
        with self.client.session() as db:
            return self._next_id_in(session_id, prefix, db, column)

    @staticmethod
    def _next_id_in(session_id: str, prefix: str, db: Session, column) -> str:
        # CAST before max: lexicographic max would pick "C9" over "C10" and reuse ids.
        numeric = cast(func.substr(column, len(prefix) + 1), Integer)
        stmt = select(func.max(numeric)).where(column.like(f"{prefix}%"))
        if column.table.name == "findings":
            stmt = stmt.where(FindingRow.session_id == session_id)
        elif column.table.name == "captures":
            stmt = stmt.where(CaptureRow.session_id == session_id)
        n = db.scalar(stmt)
        if n is None:
            return f"{prefix}1"
        return f"{prefix}{int(n) + 1}"
