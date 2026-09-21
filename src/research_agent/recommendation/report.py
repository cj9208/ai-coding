"""Report renderer (04 §4): pure code over artifacts, stable skeleton.

The report is re-readable without re-running anything: every claim keeps its
finding ids, and the sources section lets a human walk F-ids back to URLs.
"""

from __future__ import annotations

from ..contracts.models import EvidencePack, PreferenceProfile, RecommendationResult
from ..orchestrator.view import SessionView

_STARS = {"high": "★★★★☆", "medium": "★★★☆☆", "low": "★★☆☆☆"}


def render_markdown(
    view: SessionView,
    pack: EvidencePack,
    profile: PreferenceProfile | None,
    result: RecommendationResult,
) -> str:
    brief = view.require_brief()
    top = result.picks[0] if result.picks else None
    lines: list[str] = []

    # TL;DR — three lines, the whole point of the run
    lines.append(f"# Recommendation: {brief.topic}")
    if result.abstained:
        lines.append(
            f"\n> **研究暂不支持有把握的推荐。** {result.missing_for_confidence}"
        )
    elif top:
        tied = [p for p in result.picks if p.tie_group]
        head = " / ".join(p.candidate for p in (tied or [top]))
        lines.append(
            f"\n> **首选：{head}**（{_STARS[top.confidence]}，"
            f"整体置信度 {result.overall_confidence}"
            f"{'，研究因预算提前停止' if result.stop_reason else ''}）"
            f"\n> {top.headline_reason}"
        )
    lines.append("")

    # Your situation — profile + assumptions, checkable in 20s
    lines.append("## 你的情况")
    if profile:
        cons = "; ".join(f"{c.key}: {c.value}" for c in profile.hard_constraints)
        crits = "、".join(f"{c.key}({c.weight:.0%})" for c in profile.criteria)
        if cons:
            lines.append(f"- 硬性约束：{cons}")
        if crits:
            lines.append(f"- 评分维度：{crits}")
        if profile.taste_notes:
            lines.append(f"- 口味备注：{'；'.join(profile.taste_notes)}")
    if result.assumptions:
        lines.append("- 采用的默认假设：")
        lines += [f"  - {a.text}" for a in result.assumptions]
    if pack.coverage:
        cov = "、".join(f"{k}:{v}" for k, v in pack.coverage.items())
        lines.append(f"- 研究覆盖面：{cov}")
    lines.append("")

    # Ranked picks
    if result.picks:
        lines.append("## 排名推荐")
        for p in result.picks:
            head = (
                "#1="
                if p.tie_group and p.rank is None
                else f"#{p.rank} " if p.rank else "#1 "
            )
            lines.append(f"\n### {head}{p.candidate}  {_STARS[p.confidence]}")
            if p.headline_reason:
                lines.append(p.headline_reason)
            if p.strengths:
                lines.append("- 优势：")
                lines += [
                    (
                        f"  - {s.text} `[{', '.join(s.evidence)}]`"
                        if s.evidence
                        else f"  - {s.text}"
                    )
                    for s in p.strengths
                ]
            if p.watch_outs:
                lines.append("- 注意事项：")
                lines += [
                    (
                        f"  - {s.text} `[{', '.join(s.evidence)}]`"
                        if s.evidence
                        else f"  - {s.text}"
                    )
                    for s in p.watch_outs
                ]
            if p.constraint_fit:
                lines.append(f"- 约束匹配：{p.constraint_fit}")
            if p.evidence:
                lines.append(f"- 证据：{', '.join(f'`{e}`' for e in p.evidence[:8])}")
        tie = [p for p in result.picks if p.tie_group]
        if len(tie) > 1:
            twins = " 与 ".join(p.candidate for p in tie)
            lines.append("\n## 接近并列")
            lines.append(f"{twins} 实为刀锋选择，" "见下方敏感性说明。")
    lines.append("")

    # Also considered, and why not — kills the black-box objection
    if result.rejected_notable:
        lines.append("## 也考虑过，但没选")
        for r in result.rejected_notable:
            cites = f" `[{', '.join(r.evidence[:4])}]`" if r.evidence else ""
            lines.append(f"- **{r.candidate}** — {r.reason}{cites}")
        lines.append("")

    # Sensitivity
    if result.sensitivity:
        lines.append("## 什么情况会改变这个排名")
        for n in result.sensitivity:
            lines.append(f"- {n.trigger} → {n.effect}")
        lines.append("")

    # Sources — every F-id walks back to a URL with a fetch date
    if view.capture_sources:
        lines.append("## 来源")
        findings_by_capture: dict[str, list[str]] = {}
        for f in pack.findings:
            findings_by_capture.setdefault(f.capture_id, []).append(f.id)
        for cid, (url, title) in sorted(view.capture_sources.items()):
            used = findings_by_capture.get(cid, [])
            tag = f"（{', '.join(used)}）" if used else "（未产出引用）"
            lines.append(f"- `{cid}` [{title or url}]({url}) {tag}")
        lines.append("")

    if pack.source_failures:
        lines.append("## 采集失败记录")
        for fail in pack.source_failures[:10]:
            lines.append(
                f"- {fail.query_id or '-'} / {fail.adapter} / "
                f"{(fail.url or '-')[:80]} — {fail.error[:120]}"
            )
        lines.append("")

    lines.append(
        f"— session `{view.session_id}`, schema v1, "
        f"stop_reason={result.stop_reason or 'none'}"
    )
    return "\n".join(lines)
