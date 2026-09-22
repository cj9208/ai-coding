"""Enterprise request-orchestration runtime.

The harness around a probabilistic engine: LLMs propose, but decision
tables, a budget-bounded state machine, and seven persisted runtime objects
decide. ``src/rag`` is the first capability behind it. The full contract and
its design positions (DP-1..DP-10) live in ``docs/orchestrator-design.md``.
"""
