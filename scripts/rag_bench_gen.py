"""Synthetic corpus generator for RAG retrieval benchmarks.

Generates OcrDocument bundles with controlled content and known keywords,
plus a golden query set that targets those keywords. The generator produces
realistic Chinese technical documents across six topic domains, each with
section hierarchy and keyword-bearing paragraphs that the FTS5 index can
match on.

Usage::

    uv run python scripts/rag_bench_gen.py --n-docs 1000 --out data/rag_bench/inbox
    uv run python scripts/rag_bench_gen.py --n-docs 50000 --out data/rag_bench/inbox
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
import time
from pathlib import Path

from ocr_backend.contract import (
    BlockKind,
    ContentFormat,
    OcrBackendInfo,
    OcrBlock,
    OcrDocument,
    OcrPage,
    OcrSource,
)

# -- topic templates ---------------------------------------------------------

TOPICS = [
    {
        "name": "人力资源",
        "sections": [
            "年假管理规定",
            "薪酬福利体系",
            "绩效考核标准",
            "员工培训制度",
            "考勤管理办法",
        ],
        "keywords": [
            "年假审批",
            "薪酬结构",
            "绩效奖金",
            "社保缴纳",
            "培训学时",
            "考勤异常",
            "三级流程",
            "季度考核",
        ],
    },
    {
        "name": "财务报告",
        "sections": [
            "资产负债表分析",
            "现金流量说明",
            "利润表解读",
            "审计意见汇总",
            "预算执行报告",
        ],
        "keywords": [
            "流动资产",
            "固定资产",
            "现金流",
            "资产负债率",
            "审计合规",
            "预算偏差",
            "季度结算",
            "应收账款",
        ],
    },
    {
        "name": "技术规范",
        "sections": [
            "系统架构设计",
            "接口规范说明",
            "数据库设计方案",
            "性能优化指南",
            "安全合规要求",
        ],
        "keywords": [
            "微服务架构",
            "API网关",
            "数据库分片",
            "缓存策略",
            "负载均衡",
            "服务降级",
            "链路追踪",
            "容灾方案",
        ],
    },
    {
        "name": "法律合同",
        "sections": [
            "服务条款细则",
            "保密协议内容",
            "知识产权条款",
            "违约责任说明",
            "争议解决机制",
        ],
        "keywords": [
            "服务等级协议",
            "保密期限",
            "知识产权归属",
            "违约赔偿",
            "不可抗力",
            "仲裁条款",
            "数据保护",
            "合规审计",
        ],
    },
    {
        "name": "产品需求",
        "sections": [
            "用户增长策略",
            "功能迭代计划",
            "市场调研报告",
            "竞品分析总结",
            "用户反馈处理",
        ],
        "keywords": [
            "用户留存",
            "转化率",
            "日活跃用户",
            "功能优先级",
            "竞品对比",
            "需求池",
            "用户画像",
            "增长黑客",
        ],
    },
    {
        "name": "运维手册",
        "sections": [
            "部署流程说明",
            "监控告警配置",
            "故障排查指南",
            "备份恢复方案",
            "容量规划建议",
        ],
        "keywords": [
            "灰度发布",
            "回滚策略",
            "监控指标",
            "告警阈值",
            "故障定位",
            "数据备份",
            "容灾演练",
            "容量评估",
        ],
    },
]

_FILLER = [
    "根据最新的内部调研和行业标准，该领域的最佳实践需要综合考虑多方面因素。",
    "具体实施过程中，需要注意以下几点要求，确保操作规范且可追溯。",
    "各部门应当密切配合，按照既定流程执行，并定期汇报进展情况。",
    "本规定自发布之日起生效，原有相关规定同时废止，如有调整另行通知。",
    "相关数据和分析结果仅供内部参考使用，不得对外披露或用于其他用途。",
    "在实际操作中，需要根据具体情况进行适当调整，但不得低于最低标准。",
    "以上内容由主管部门负责解释和修订，如有异议可通过正式渠道反馈。",
    "该方案已经过充分论证和试点验证，具备全面推广的条件和基础。",
    "所有参与人员应当严格遵守相关规范，违规行为将按照制度处理。",
    "后续将根据实施效果和市场变化，适时对本方案进行优化和完善。",
    "技术选型应当以业务需求为导向，兼顾系统的可扩展性和可维护性。",
    "风险评估结果显示，主要风险集中在执行层面，需要建立有效的监控机制。",
]


def _block(
    id_: int,
    kind: BlockKind,
    content: str,
    order: int | None = None,
) -> OcrBlock:
    y = 60.0 + (order or 0) * 45.0
    return OcrBlock(
        id=id_,
        kind=kind,
        raw_label=str(kind),
        content=content,
        content_format=ContentFormat.text,
        bbox=(40.0, y, 550.0, y + 40.0),
        order=order,
        score=0.95,
    )


def _keyword_paragraph(rng: random.Random, keywords: list[str]) -> str:
    kws = rng.sample(keywords, min(2, len(keywords)))
    templates = [
        f"关于{kws[0]}，需要特别注意{kws[1]}的相关要求。"
        f"在实际操作中，两者需要协同配合，确保整体目标的达成。",
        f"本节重点阐述{kws[0]}的核心要点。"
        f"在{kws[1]}方面，应当遵循既定的标准和流程。",
        f"针对{kws[0]}的管理，已建立了完善的制度体系。"
        f"{kws[1]}作为其中的关键环节，需要重点关注和持续优化。",
        f"{kws[0]}的实施需要跨部门协作。"
        f"特别是在{kws[1]}环节，各部门应当明确职责分工。",
    ]
    return rng.choice(templates)


def _make_doc(
    doc_index: int,
    topic: dict,
    sections: list[str],
    rng: random.Random,
    n_pages: int,
) -> OcrDocument:
    all_blocks: list[OcrBlock] = []
    block_id = 0
    order = 0

    doc_title = f"{topic['name']}文档 第{doc_index + 1}号"
    all_blocks.append(_block(block_id, BlockKind.title, doc_title, order=order))
    block_id += 1
    order += 1

    for sec_title in sections:
        all_blocks.append(_block(block_id, BlockKind.title, sec_title, order=order))
        block_id += 1
        order += 1

        kws = rng.sample(topic["keywords"], min(2, len(topic["keywords"])))
        all_blocks.append(
            _block(
                block_id,
                BlockKind.paragraph,
                _keyword_paragraph(rng, kws),
                order=order,
            )
        )
        block_id += 1
        order += 1

        n_filler = rng.randint(2, 4)
        for _ in range(n_filler):
            all_blocks.append(
                _block(
                    block_id,
                    BlockKind.paragraph,
                    rng.choice(_FILLER),
                    order=order,
                )
            )
            block_id += 1
            order += 1

    blocks_per_page = max(1, len(all_blocks) // n_pages)
    pages: list[OcrPage] = []
    for i in range(n_pages):
        start = i * blocks_per_page
        end = start + blocks_per_page if i < n_pages - 1 else len(all_blocks)
        page_blocks = all_blocks[start:end]
        if page_blocks:
            pages.append(
                OcrPage(
                    page_index=len(pages), width=600, height=800, blocks=page_blocks
                )
            )

    if not pages:
        pages.append(
            OcrPage(page_index=0, width=600, height=800, blocks=all_blocks[:5])
        )

    content = doc_title + " ".join(s for s in sections) + " ".join(topic["keywords"])
    sha = hashlib.sha256(content.encode()).hexdigest()

    return OcrDocument(
        source=OcrSource(
            kind="pdf",
            path=f"synth_{doc_index:06d}.pdf",
            sha256=sha,
            page_count=len(pages),
        ),
        backend=OcrBackendInfo(
            name="synthetic",
            library_version="1.0",
            model="bench-gen",
            pipeline_version="1.0",
            options={},
        ),
        created_at="2026-09-21T10:00:00+08:00",
        pages=pages,
    )


def _golden_queries(
    docs: list[tuple[int, dict, list[str]]],
    n_queries: int = 50,
) -> list[dict]:
    """Build golden queries from the generated docs.

    ``docs`` is a list of ``(index, topic, keywords)`` tuples — the same
    data the generator used, so we know exactly which docs contain which
    keywords.
    """
    queries: list[dict] = []
    by_topic: dict[str, list[tuple[int, list[str]]]] = {}
    for idx, topic, kws in docs:
        by_topic.setdefault(topic["name"], []).append((idx, kws))

    rng = random.Random(42)  # nosec B311

    topic_query_templates = {
        "人力资源": [
            "公司的{kw}是如何规定的",
            "请说明{kw}的具体流程和要求",
            "{kw}需要注意哪些事项",
        ],
        "财务报告": [
            "报告中{kw}的数据是多少",
            "请分析{kw}的变化趋势",
            "{kw}对整体财务状况有什么影响",
        ],
        "技术规范": [
            "系统中{kw}是怎么实现的",
            "请描述{kw}的技术方案",
            "{kw}的设计原则是什么",
        ],
        "法律合同": [
            "合同中关于{kw}的条款是什么",
            "{kw}的具体约定有哪些",
            "请解释{kw}相关的法律规定",
        ],
        "产品需求": [
            "产品的{kw}策略是什么",
            "如何提升{kw}",
            "{kw}的当前状态和改进方向",
        ],
        "运维手册": [
            "{kw}的操作流程是什么",
            "请说明{kw}的配置方法",
            "{kw}遇到问题如何排查",
        ],
    }

    case_id = 0
    for topic_name, topic_docs in by_topic.items():
        templates = topic_query_templates.get(topic_name, ["请查找关于{kw}的信息"])
        for idx, kws in topic_docs:
            if len(queries) >= n_queries:
                break
            kw = rng.choice(kws)
            q_text = rng.choice(templates).format(kw=kw)
            queries.append(
                {
                    "case_id": f"bench_{case_id:04d}",
                    "question": q_text,
                    "expected_substrings": [kw],
                    "rationale": f"targets {kw} in doc {idx}",
                }
            )
            case_id += 1

    queries.append(
        {
            "case_id": f"bench_{case_id:04d}",
            "question": "量子计算在密码学中的应用前景",
            "expected_substrings": [],
            "rationale": "abstention test — no doc covers this",
        }
    )

    return queries[:n_queries]


def generate_corpus(
    n_docs: int,
    out_dir: Path,
    *,
    n_pages: int = 10,
    seed: int = 42,
) -> Path:
    """Generate *n_docs* synthetic bundles into *out_dir*, return golden path."""
    rng = random.Random(seed)  # nosec B311
    out_dir.mkdir(parents=True, exist_ok=True)

    golden_path = out_dir.parent / "golden.jsonl"
    doc_meta: list[tuple[int, dict, list[str]]] = []

    t0 = time.monotonic()
    for i in range(n_docs):
        topic = TOPICS[i % len(TOPICS)]
        n_secs = rng.randint(3, min(5, len(topic["sections"])))
        sections = rng.sample(topic["sections"], n_secs)
        doc = _make_doc(i, topic, sections, rng, n_pages)

        bundle_path = out_dir / f"synth_{i:06d}.ocr.json"
        bundle_path.write_text(doc.model_dump_json(), encoding="utf-8")

        doc_meta.append((i, topic, list(topic["keywords"])))

        if (i + 1) % 5000 == 0:
            elapsed = time.monotonic() - t0
            rate = (i + 1) / elapsed
            eta = (n_docs - i - 1) / rate if rate > 0 else 0
            print(
                f"  generated {i + 1}/{n_docs} docs "
                f"({rate:.0f} docs/s, ETA {eta:.0f}s)",
                file=sys.stderr,
            )

    golden = _golden_queries(doc_meta)
    golden_path.write_text(
        "\n".join(json.dumps(q, ensure_ascii=False) for q in golden) + "\n",
        encoding="utf-8",
    )

    elapsed = time.monotonic() - t0
    print(
        f"generated {n_docs} docs in {elapsed:.1f}s "
        f"({n_docs / elapsed:.0f} docs/s) -> {out_dir}",
        file=sys.stderr,
    )
    print(f"golden queries: {golden_path} ({len(golden)} cases)", file=sys.stderr)
    return golden_path


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate synthetic RAG benchmark corpus")
    ap.add_argument("--n-docs", type=int, default=1000, help="number of documents")
    ap.add_argument(
        "--out",
        type=Path,
        default=Path("data/rag_bench/inbox"),
        help="output inbox directory",
    )
    ap.add_argument("--pages", type=int, default=10, help="pages per document")
    ap.add_argument("--seed", type=int, default=42, help="RNG seed")
    args = ap.parse_args()
    generate_corpus(args.n_docs, args.out, n_pages=args.pages, seed=args.seed)


if __name__ == "__main__":
    main()
