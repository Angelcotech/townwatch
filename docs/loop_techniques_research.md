# Loop-based (iterative) AI techniques — research + adoption plan

Status: research complete, implementation deferred to a dedicated sweep session.
Source: deep-research pass (June 2026) — 22 sources, 25 claims 3-vote verified, 0
refuted. Question: should we use iterative/loop-based workflows instead of
single-shot prompts, for both our dev workflow and the product's LLM pipelines?

## The governing principle

Iterative loops beat single-shot prompting **only when grounded in a real
external signal** (tests, execution output, compiler/linter, schema validation,
search). Ungrounded "just reflect again" loops are inconsistent and frequently
*degrade* accuracy — this is benchmarked and replicated, not anecdotal. So: if a
loop has no machine-readable check to close against, don't add the loop.

Evidence (grounded helps): ReAct +34% ALFWorld / +10% WebShop
([2210.03629](https://arxiv.org/abs/2210.03629)); Reflexion 91% vs 80% pass@1
HumanEval, driven by executing self-generated tests
([2303.11366](https://arxiv.org/abs/2303.11366)); self-debugging up to +12 pts
with test feedback ([2304.05128](https://arxiv.org/abs/2304.05128)).
Evidence (ungrounded hurts): intrinsic self-correction can lose net accuracy
([2310.01798](https://arxiv.org/abs/2310.01798), CorrectBench 2025).
Tool alignment: Claude Code / Agent SDK run gather→act→verify→repeat and rank
feedback **rules-based (lint/types) > tests/execution > LLM-as-judge** ("not very
robust", "heavy latency")
([building-agents-with-the-claude-agent-sdk](https://www.anthropic.com/engineering/building-agents-with-the-claude-agent-sdk),
[Claude Code best practices](https://code.claude.com/docs/en/best-practices)).

## What we already do right (keep)

The ETL's escalating **recovery ladder** (text-layer → OCR → vision) and
**chunk-and-merge per-window retries** (incl. the new packet windowing) are
textbook grounded fallback + large-input loops. No change needed.

## Gaps to implement — priority order (the sweep)

1. **Schema-validation retry loops on structured extraction** (highest-leverage,
   lowest-risk). On invalid JSON / failed Pydantic, re-prompt with the validation
   error and retry (bounded). Rules-based grounding = the SDK's "best" feedback.
   Targets `etl/townwatch_etl/extractors/`.
2. **Test-driven "loop until green" in dev.** Needs a runnable signal: add a test
   suite to the ETL (almost none today) and lean on TypeScript + lint on the web
   side. This is what lets the agent loop close autonomously instead of making
   the human the verification loop.
3. **LLM-as-judge — only for genuinely fuzzy outputs** (proposal summaries,
   comment-digest moderation). Accept the latency cost; prefer a cheaper
   rules-based check (entity/fact presence) as a first line where possible.

Avoid: ungrounded self-reflection loops with no external signal.

## Guardrails for any unattended loop (non-negotiable)

Bounded retries, a budget/iteration cap, idempotency, and recorded failures —
never silent infinite retry. (Directly addresses the prior 2× batch-resubmit
incident.) Mirrors the SDK's `max_turns` / `max_budget_usd` / Stop-hook caps; our
equivalents are the fund-gate, idempotency stamps (e.g. `packet_segmented_at`),
and `pipeline_failure` records.

## Open questions to resolve during the sweep

- At what invalid-output rate does a schema-retry loop pay for itself on our
  doc/model mix? (measure cost/latency overhead vs single-shot)
- For LLM-as-judge on summaries: which judge tier + rubric agree acceptably with
  human review — or is a rules-based fact-presence check the better first line?
- Do Reflexion/self-debug gains hold on Opus-4.8-class models, or has single-shot
  capability shrunk the loop's marginal benefit on easy tasks?
- Concrete idempotency / bounded-retry / failure-recording standard for all
  unattended extraction loops.

## Caveat

Vendor-doc specifics (SDK cap names, Stop-hook 8-block number) are current as of
June 2026 — re-verify against live docs before relying on exact behavior. Benchmark
numbers are GPT-3.5/4-era; the grounded-vs-ungrounded *direction* is robust, the
precise effect sizes on current models are not independently re-measured here.
