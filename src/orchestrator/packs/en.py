"""The English pack — first tranche of 05b's slowest workstream.

Rows mirror the zh table's categories in the same CH01 order (hard
stops, scope questions, write constrain, allow default); the English
patterns use IGNORECASE because capitalization is the only signal an
English input carries that Chinese does not. Alias authoring has not
started (the sample enterprise vocabulary is Chinese-named), so
``ALIASES`` is empty — normalization then degrades to NFC + whitespace
collapse, which is honest for English. Content review continues; the
adversarial golden class (05b step 4) tests against this seam.
"""

from __future__ import annotations

import re

from ..contracts import ActionType, RiskLevel, SafetyDecision
from ..safety import SafetyRow
from .base import LocalePack, PromptPack

_IC = re.IGNORECASE

SAFETY_TABLE: tuple[SafetyRow, ...] = (
    SafetyRow(
        row_id="s1_refuse_destructive",
        pattern=re.compile(
            r"delete (all|everything)|wipe (the )?(database|table)"
            r"|bypass (the )?(permission|approval)|hack into|crack .{0,12}(password|key)"
            r"|leak .{0,12}(password|secret|credential)"
            r"|(give|provide|send|export) me .{0,12}(password|secret|api key)",
            _IC,
        ),
        decision=SafetyDecision.refuse,
        risk=RiskLevel.high,
        reason="safety_gate: destructive or credential-exfiltrating request",
    ),
    SafetyRow(
        row_id="s2_refuse_privacy",
        pattern=re.compile(
            r"(look ?up|get|reveal|fetch|find) .{0,20}"
            r"(id (card|number)|social security|bank card|home address|phone number)"
            r"|other (employee|people|staff)'?s? (salary|pay|performance)",
            _IC,
        ),
        decision=SafetyDecision.refuse,
        risk=RiskLevel.high,
        reason="safety_gate: personal-data violation",
    ),
    SafetyRow(
        row_id="s3_handoff_expert",
        pattern=re.compile(
            r"labor (dispute|arbitration)|sue (the |my )?company"
            r"|legal (advice|opinion)|(compensation|dispute) (plan|strategy)",
            _IC,
        ),
        decision=SafetyDecision.handoff,
        risk=RiskLevel.high,
        reason="safety_gate: legal/expert domain, human required",
    ),
    SafetyRow(
        row_id="s4_clarify_scope",
        pattern=re.compile(
            r"(whole|entire|all) (the )?(company|departments?|employees?|staff|orders?)",
            _IC,
        ),
        decision=SafetyDecision.clarify_scope,
        risk=RiskLevel.medium,
        reason="safety_gate: data scope exceeds default, needs narrowing",
        question=(
            "That request spans the whole organization —"
            " please name the specific department or data category."
        ),
    ),
    SafetyRow(
        row_id="s5_constrain_write",
        pattern=re.compile(
            r"(send out|blast|broadcast|mass-send|send) (a |the )?"
            r"(notice|notification|email|message)"
            r"|issue (a |the )?refund|cancel (the |my )?(order|approval)"
            r"|submit (an |the )?(approval|request)"
            r"|change .{0,16}(config|configuration|settings)",
            _IC,
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

ALIASES: dict[str, tuple[str, ...]] = {}

PROMPT = PromptPack(
    system=(
        "You are the front-half interpreter of an enterprise request"
        " orchestration harness. Rules: never translate or rewrite the"
        " user's original text; only propose an interpretation — the"
        " harness decides. When unsure, lower confidence and fill"
        " ambiguity_flags and clarification_question instead of inventing."
        " Output exactly one JSON object."
    ),
    raw_input="[ORIGINAL INPUT] {text}",
    answer="[USER'S ANSWER TO THE CLARIFYING QUESTION] {answer}",
    normalized_query="[DETERMINISTICALLY NORMALIZED QUERY] {query}",
    alias_hits="[ALIAS HITS] {hits}",
    none_label="none",
    join=", ",
    escalation=(
        "[NOTE] This is an escalation review of a previous interpretation;"
        " be more cautious."
    ),
    instructions=(
        "Output JSON with fields: task_type (one of {task_types}),"
        " target_entity_guess, requested_attributes (array),"
        " interpretation_summary (one sentence), confidence (0~1),"
        " ambiguity_flags (array), alternative_interpretations (array),"
        " clarification_question (in English, only when the user must"
        " supply something), user_resolvable_ambiguity (boolean,"
        " true/false only), missing_required_constraint (boolean,"
        " true/false only — describe the gap in clarification_question),"
        " constraints (object, may be empty)."
    ),
)

PACK = LocalePack(
    locale="en",
    safety_table=SAFETY_TABLE,
    aliases=ALIASES,
    prompt=PROMPT,
)
