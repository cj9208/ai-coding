"""The Chinese pack — every asset here is the pre-05b content moved
verbatim from ``safety.SAFETY_TABLE``, ``normalize.ALIASES`` and
``interpret.SYSTEM_PROMPT``. Editing rules:

- the safety rows follow CH01 order: hard stops first, then scope
  questions, then the write-action constrain row, finally the allow
  default (first match wins);
- ``ALIASES`` is data-as-code (DP-7 posture): canonical name -> known
  short/alternative names, longest match wins;
- the prompt templates must reproduce the legacy assembled prompt
  byte-for-byte (``test_packs`` pins this).
"""

from __future__ import annotations

import re

from ..contracts import ActionType, RiskLevel, SafetyDecision
from ..safety import SafetyRow
from .base import LocalePack, PromptPack

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

#: canonical entity name -> alias spellings (Chinese nicknames, short
#: names, mixed-language forms). Sample enterprise vocabulary; extend
#: by row.
ALIASES: dict[str, tuple[str, ...]] = {
    "春晖省钱卡": ("春晖卡", "省钱卡", "春晖省钱卡"),
    "费用报销系统": ("报销系统", "报销平台", "费用报销", "报销"),
    "人事服务": ("HR服务", "hr服务", "人事系统"),
    "差旅标准": ("出差标准", "差旅政策"),
}

SYSTEM_PROMPT = (
    "你是企业请求编排系统的前半程解释器，负责把员工请求解析成结构化解释。"
    "规则：不翻译、不改写用户原文；只提出解释，不做任何执行决定。"
    "拿不准时降低 confidence 并填写 ambiguity_flags 与 clarification_question，"
    "不要编造。只输出一个 JSON 对象。"
)

PROMPT = PromptPack(
    system=SYSTEM_PROMPT,
    raw_input="【原始输入】{text}",
    answer="【用户对澄清问题的回答】{answer}",
    normalized_query="【确定性归一化查询】{query}",
    alias_hits="【别名命中】{hits}",
    none_label="无",
    join="、",
    escalation="【说明】这是对上次解释的升级复核，请更审慎地给出解释。",
    instructions=(
        "请输出 JSON，字段："
        "task_type（{task_types} 之一）、target_entity_guess、"
        "requested_attributes（数组）、interpretation_summary（一句话）、"
        "confidence（0~1）、ambiguity_flags（数组）、"
        "alternative_interpretations（数组）、clarification_question"
        "（中文，仅在需要用户补充时）、user_resolvable_ambiguity"
        "（布尔，只填 true/false）、missing_required_constraint"
        "（布尔，只填 true/false，缺失内容写进 clarification_question）、"
        "constraints（对象，可为空）。"
    ),
)

PACK = LocalePack(
    locale="zh",
    safety_table=SAFETY_TABLE,
    aliases=ALIASES,
    prompt=PROMPT,
)
