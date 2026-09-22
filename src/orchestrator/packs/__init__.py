"""Per-locale front-half assets — the trust boundary's language layer
(05b step 1).

One **pack** = the three language-scoped assets DP-2 names: a safety
table (row shape owned by ``safety.py``), an alias table, and the
front-half prompt templates. ``zh`` holds the pre-05b content verbatim —
the untouched pass of the existing golden suite is the "behavior did not
move" proof.

Two invariants the registry exists to protect:

- **Locale comes from caller context, never from the model.** The only
  consumer is ``LlmFrontHalf``, reading ``envelope.original_input.locale``
  — a value set by ``run_turn``, not by any interpretation. A model-side
  proposal must not choose which safety table guards its own input.
- **A missing pack is conservative, not permissive.** ``safety.evaluate``
  returns the explicit ``s_unsupported_locale`` clarify verdict instead
  of falling through to an allow default. Before 05b, every non-Chinese
  input silently passed a Chinese-only gate and nobody got an error —
  that silent default was the bug.
"""

from __future__ import annotations

from ..contracts import ActionType, RiskLevel, SafetyDecision
from ..safety import SafetyVerdict
from . import en, zh
from .base import LocalePack, PromptPack

__all__ = ["LocalePack", "PromptPack", "PACKS", "UNSUPPORTED_VERDICT", "get_pack"]

#: one entry per authored pack; adding a locale is one module in this
#: package plus one line here.
PACKS: dict[str, LocalePack] = {pack.locale: pack for pack in (zh.PACK, en.PACK)}

#: the row outside every table — no pack means the input is *unchecked*,
#: which is a scope question for the user, not an allow. The wording
#: names the supported locales because a user who writes in an unbacked
#: language cannot be helped by a Chinese-only error.
UNSUPPORTED_VERDICT = SafetyVerdict(
    decision=SafetyDecision.clarify_scope,
    action_type=ActionType.read_only,
    risk=RiskLevel.medium,
    reason="safety_gate: no policy pack for this locale, input unchecked",
    row_id="s_unsupported_locale",
    question=(
        "当前语言尚无对应的安全策略包，请改用中文或英文重新表述。"
        " No safety policy pack exists for this language yet —"
        " please restate in Chinese (zh) or English (en)."
    ),
)


def get_pack(locale: str) -> LocalePack | None:
    return PACKS.get(locale)
