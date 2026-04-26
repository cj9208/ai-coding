import asyncio
import logging

from .chunker import chunk_document
from .extractor import create_extractor
from .llm_client import LLMClient
from .models import Chunk, SummaryStyle

logger = logging.getLogger(__name__)

MAP_PROMPTS = {
    SummaryStyle.CONCISE: (
        "Summarize the following text concisely in 2-3 paragraphs, "
        "preserving key facts, figures, and conclusions."
    ),
    SummaryStyle.DETAILED: (
        "Provide a thorough and detailed summary of the following text, "
        "covering all key points and important details."
    ),
    SummaryStyle.BULLETS: (
        "Summarize the following text as a list of bullet points, "
        "capturing all key information. Use '- ' for each bullet."
    ),
    SummaryStyle.EXECUTIVE: (
        "Write an executive summary of the following text with key findings "
        "and recommendations. Be concise and actionable."
    ),
}

REDUCE_PROMPT = (
    "Combine the following summaries into a single coherent summary. "
    "Remove any redundancy and preserve the most important information."
)


async def summarize(pdf_path: str, config) -> str:
    extractor = create_extractor(config.extractor_backend)
    logger.info(
        "Extracting text from %s using %s backend...",
        pdf_path,
        config.extractor_backend,
    )
    doc = await extractor.extract(pdf_path)
    logger.info(
        "Extracted %d pages, %d characters", doc.total_pages, len(doc.total_text)
    )

    logger.info("Chunking document...")
    chunks = chunk_document(doc, config.chunk_size, config.chunk_overlap, config.model)
    logger.info("Split into %d chunks", len(chunks))

    total_tokens = sum(c.token_count for c in chunks)
    if total_tokens > 1_000_000:
        logger.warning(
            "Document is very large (~%d tokens). Summarization may take a long time "
            "and incur significant API costs.",
            total_tokens,
        )

    llm = LLMClient(config)
    map_prompt = MAP_PROMPTS.get(
        config.summary_style, MAP_PROMPTS[SummaryStyle.CONCISE]
    )

    sem = asyncio.Semaphore(config.max_concurrency)

    async def summarize_one(chunk: Chunk, idx: int) -> str:
        async with sem:
            logger.info(
                "Summarizing chunk %d/%d (%d tokens, pages %d-%d)...",
                idx + 1,
                len(chunks),
                chunk.token_count,
                chunk.start_page,
                chunk.end_page,
            )
            return await llm.chat(chunk.text, system_prompt=map_prompt)

    chunk_summaries = await asyncio.gather(
        *[summarize_one(c, i) for i, c in enumerate(chunks)]
    )
    logger.info("Got %d chunk summaries", len(chunk_summaries))

    final = await _reduce_summaries(chunk_summaries, llm, config)
    return final


async def _reduce_summaries(summaries: list[str], llm: LLMClient, config) -> str:
    current = summaries[:]

    while len(current) > 1:
        logger.info("Reducing %d summaries...", len(current))
        pairs = []
        for i in range(0, len(current), 2):
            if i + 1 < len(current):
                pairs.append(current[i] + "\n---\n" + current[i + 1])
            else:
                pairs.append(current[i])

        sem = asyncio.Semaphore(config.max_concurrency)

        async def reduce_one(pair: str) -> str:
            async with sem:
                return await llm.chat(pair, system_prompt=REDUCE_PROMPT)

        current = await asyncio.gather(*[reduce_one(p) for p in pairs])

    return current[0]
