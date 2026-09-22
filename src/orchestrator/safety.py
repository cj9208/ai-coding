"""The input safety gate — deterministic pattern table, first match wins
(module map: "input gate: deterministic pattern table -> allow | constrain
| clarify_scope | refuse | handoff").

Position in the architecture: this is harness, not model. The gate runs on
``original_input.text`` *before* any LLM call, so a refused request never
costs a token (and refuse/handoff short-circuit in ``interpret.py``).
The verdict lands on ``FrontHalfOutput`` and the routing table consumes it
via ``assess.confidence_of`` (refuse -> unsafe, handoff -> blocked,
clarify_scope -> ambiguous) — no other module interprets safety.

Same rows-as-data posture as ``policy.py`` (DP-7): every row carries an id
so tests and replay can name what fired.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .contracts import ActionType, RiskLevel, SafetyDecision


@dataclass(frozen=True)
class SafetyRow:
    row_id: str
    pattern: re.Pattern[str]
    decision: SafetyDecision
    action_type: ActionType = ActionType.read_only
    risk: RiskLevel = RiskLevel.low
    reason: str = ""
    question: str = ""
    constraints: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SafetyVerdict:
    decision: SafetyDecision
    action_type: ActionType
    risk: RiskLevel
    reason: str
    row_id: str
    question: str = ""
    constraints: dict[str, Any] = field(default_factory=dict)


#: CH01 order preserved: hard stops first, then scope questions, then the
#: write-action constrain row, finally the allow default.
SAFETY_TABLE: tuple[SafetyRow, ...] = (
    SafetyRow(
        row_id="s1_refuse_destructive",
        pattern=re.compile(
            r"删除所有|清空(数据库|表)|绕过(权限|审批)|越权|破解|泄露.{0,6}(密码|密钥|凭证)"
            r"|(给我|提供|导出).{0,6}(密码|密钥|口令)"
        ),
        decision=SafetyDecision.refuse,
        risk=RiskLevel.high,
        reason="safety_gate: destructive or credential-exfiltrating request",
    ),
    SafetyRow(
        row_id="s2_refuse_privacy",
        pattern=re.compile(
            r"(查|查一下|获取|透露).{0,8}(身份证号|银行卡号|家庭住址|手机号)"
            r"|他人.{0,6}(工资|薪资|绩效)"
        ),
        decision=SafetyDecision.refuse,
        risk=RiskLevel.high,
        reason="safety_gate: personal-data violation",
    ),
    SafetyRow(
        row_id="s3_handoff_expert",
        pattern=re.compile(r"劳动仲裁|起诉公司|(赔偿|纠纷).{0,6}(方案|策略)|法律意见"),
        decision=SafetyDecision.handoff,
        risk=RiskLevel.high,
        reason="safety_gate: legal/expert domain, human required",
    ),
    SafetyRow(
        row_id="s4_clarify_scope",
        pattern=re.compile(r"全公司|所有部门|全体员工|全部订单"),
        decision=SafetyDecision.clarify_scope,
        risk=RiskLevel.medium,
        reason="safety_gate: data scope exceeds default, needs narrowing",
        question="您请求的范围较大（如涉及全公司/所有部门），请说明具体的部门或数据类别。",
    ),
    SafetyRow(
        row_id="s5_constrain_write",
        pattern=re.compile(
            r"群发|发送(通知|邮件|消息)|发起退款|取消(订单|审批)|提交(审批|申请)|修改.{0,6}配置"
        ),
        decision=SafetyDecision.constrain,
        action_type=ActionType.write,
        risk=RiskLevel.medium,
        reason="safety_gate: write action requires confirmation before execution",
        constraints={"requires_confirmation": True},
    ),
    SafetyRow(
        row_id="s0_allow",
        pattern=re.compile(""),  # matches everything: the default row
        decision=SafetyDecision.allow,
        reason="safety_gate: no pattern matched",
    ),
)


def evaluate(text: str) -> SafetyVerdict:
    """First fully matching row wins; the table ends in an allow default, so
    a verdict always exists."""
    for row in SAFETY_TABLE:
        if row.pattern.search(text):
            return SafetyVerdict(
                decision=row.decision,
                action_type=row.action_type,
                risk=row.risk,
                reason=row.reason,
                row_id=row.row_id,
                question=row.question,
                constraints=dict(row.constraints),
            )
    raise AssertionError("safety table is missing its allow default row")
