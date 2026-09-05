from __future__ import annotations

import re

TAG_MODEL = "model"
TAG_PRICING = "pricing"
TAG_FEATURE = "feature"
TAG_RESEARCH = "research"
TAG_SECURITY = "security"
TAG_COMPANY = "company"
TAG_ECOSYSTEM = "ecosystem"
TAG_STATUS = "status"

ALL_TAGS = (
    TAG_MODEL,
    TAG_PRICING,
    TAG_FEATURE,
    TAG_RESEARCH,
    TAG_SECURITY,
    TAG_COMPANY,
    TAG_ECOSYSTEM,
    TAG_STATUS,
)

# Order drives digest section ordering (high-signal first).
SECTION_ORDER = (
    (TAG_MODEL, "New models & capabilities"),
    (TAG_PRICING, "Pricing & offers"),
    (TAG_FEATURE, "Features & platform"),
    (TAG_RESEARCH, "Research & science"),
    (TAG_SECURITY, "Security & safety"),
    (TAG_COMPANY, "Company news"),
    (TAG_ECOSYSTEM, "Customers & partnerships"),
    (TAG_STATUS, "Status / availability"),
)
SECTION_LABEL = dict(SECTION_ORDER)

# Strong identifiers (versioned model names) signal a model release with a launch verb.
_STRONG_MODEL_RE = re.compile(
    r"\b(gpt[- ]\d|claude[- ]\d|codex[- ]\d|\w+[- ]?(opus|sonnet|haiku)[- ]\d|"
    r"o[134](-\w+)?|sora[- ]\d|whisper|gpt-oss|gpt-image|gpt-realtime|gpt-audio|"
    r"dall[ -]e[- ]\d|embedding[- ]\d)\b",
    re.IGNORECASE,
)
# Generic family tokens only count when no product-context word is present.
_WEAK_MODEL_RE = re.compile(
    r"\b(model(s)?|gpt|claude|codex|opus|sonnet|haiku|sora|grok|gemini|qwen|llama|"
    r"deepseek|mistral)\b",
    re.IGNORECASE,
)
_MODEL_NOISE_RE = re.compile(
    r"\b(store|platform|api|sdk|apps?|devday|program|marketplace|spec)\b",
    re.IGNORECASE,
)
_LAUNCH_RE = re.compile(
    r"\b(introducing|introduces|announcing|announce(s|d)?|launch(es|ing|ed)?|"
    r"releas(e|es|ed|ing)|now available|model card|open[- ]source|preview|"
    r"general(ly)? available|ga\b)\b",
    re.IGNORECASE,
)
_PRICING_RE = re.compile(
    r"\b(price|pricing|prices|per month|per seat|subscription|credit(s)?|"
    r"discount|free tier|plans?|cost(s)?)\b|\$\s?\d",
    re.IGNORECASE,
)
_SECURITY_RE = re.compile(
    r"\b(security|cyber|safeguard(s)?|prompt injection|preparedness|abuse|"
    r"misuse|defensive|governance|watermark|align(ment|ed)?|misalign(ment)?|"
    r"safety|red team|responsible)\b",
    re.IGNORECASE,
)
_RESEARCH_RE = re.compile(
    r"\b(research|paper|arxiv|benchmark|scientific|scientists?|stud(y|ies)|"
    r"academi|grant(s)?)\b",
    re.IGNORECASE,
)
_ECOSYSTEM_RE = re.compile(r"\b(partner(ship)?s?)\b", re.IGNORECASE)
_POWERED_RE = re.compile(r"powered by", re.IGNORECASE)
_COMPANY_RE = re.compile(
    r"\b(fund(ing|s|ed)?|invest(ing|s|ment)?|billion|hiring?|executive|appoint(s|ed)?|"
    r"team(s)?|event|conference|milestone|anniversary|one year)\b",
    re.IGNORECASE,
)
_FEATURE_RE = re.compile(
    r"\b(introducing|introduces|announcing|announce(s|d)?|launch(es|ing|ed)?|"
    r"releas(e|es|ed|ing)|update(s|d)?|now available|preview|beta|roll(ing)? out|"
    r"expands?|adds?|improve(s|ments|d)?|new features|api|sdk|apps?|platform)\b",
    re.IGNORECASE,
)


def tag_item(source, title: str) -> str:
    """Classify an item into one primary thing-type tag from its title."""
    if source.kind == "html_snapshot":
        return TAG_PRICING
    if source.signal == "status":
        return TAG_STATUS
    text = title

    if _PRICING_RE.search(text):
        return TAG_PRICING
    if _POWERED_RE.search(text):
        return TAG_ECOSYSTEM
    if _LAUNCH_RE.search(text) and _STRONG_MODEL_RE.search(text):
        return TAG_MODEL
    if (
        _LAUNCH_RE.search(text)
        and _WEAK_MODEL_RE.search(text)
        and not _MODEL_NOISE_RE.search(text)
    ):
        return TAG_MODEL
    if _SECURITY_RE.search(text):
        return TAG_SECURITY
    if _RESEARCH_RE.search(text):
        return TAG_RESEARCH
    lowered = title.lower()
    if lowered.startswith("how ") or _ECOSYSTEM_RE.search(text):
        return TAG_ECOSYSTEM
    if _COMPANY_RE.search(text):
        return TAG_COMPANY
    if _FEATURE_RE.search(text):
        return TAG_FEATURE

    fallback = {
        "research": TAG_RESEARCH,
        "product": TAG_FEATURE,
        "deal": TAG_PRICING,
        "status": TAG_STATUS,
    }
    return fallback.get(source.signal, TAG_FEATURE)
