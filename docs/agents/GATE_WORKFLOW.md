# Single-Pass Agent Workflow

This file is an operational guide. `docs/SPEC.md` remains the normative source of product
requirements.

## 1. Agent Settings

- Coder: `gpt-5.6-luna`, reasoning `high`.
- Finalizer: `gpt-5.6-sol`, reasoning `xhigh`.
- Run the agents sequentially in the same local project directory.
- Invoke the Coder exactly once for the complete implementation.
- After the Coder stops, invoke the Finalizer exactly once to review, correct, validate, and
  deliver the complete result.
- Do not split the work into gates, subgates, repeated Coder tasks, or review/repair loops.

## 2. Optional Version-Control Baseline

This repository may not have Git metadata. A baseline commit is recommended so the Finalizer can
inspect an exact implementation diff.

From `C:\dev\price-monitor`:

```powershell
git init
git status --short
git add .
git status --short
```

Before committing, confirm that `.env`, `.venv`, `data/*.db`, logs, caches, and secrets are not
staged. They are covered by `.gitignore`, but the staged list must still be inspected.

```powershell
git commit -m "chore: baseline before complete implementation"
```

Do not commit the completed implementation until the Finalizer has finished its corrections and
reported the validation results.

## 3. Single Coder Execution

Start one Codex task in the local `price-monitor` project. Select Luna with reasoning High and use
this complete prompt once:

```text
You are the Coder for C:\dev\price-monitor. Complete the whole product in one execution.

Read AGENTS.md, docs/SPEC.md, docs/agents/CODER.md, the existing docs/reviews/REVIEW.md, and all
implementation, test, configuration, and user-documentation files before editing. Treat
docs/SPEC.md as the complete scope; do not divide the work into gates or stop after an intermediate
stage.

Implement every unmet requirement in docs/SPEC.md and fix every applicable existing blocking
finding. Preserve correct existing behavior and unrelated user changes. Do not modify
docs/SPEC.md, docs/agents/REVIEWER.md, or docs/reviews/REVIEW.md. Do not add AI, LLM, embeddings,
CAPTCHA bypasses, or anti-protection behavior to the product.

Use deterministic fixtures or fake transports for automated tests. Do not make the test suite
depend on live internet or real credentials. Fail closed when external data is incomplete or
ambiguous. Keep the design simple, local, secure, and consistent with the specification.

Do not stop after planning or after a partial milestone. Inspect, implement the complete scope,
test it, repair failures caused by the work, and finish as much of the specification as can be
completed in this one execution. Run the full test suite and every configured lint, type,
security, dependency, and secret check. Update ordinary documentation and .env.example when setup
or user behavior changes.

At completion, report changed files, behavior implemented, exact validation commands and results,
known limitations, and any specification requirement that remains incomplete. Do not ask for a
second Coder run.
```

## 4. Single Finalizer Execution

After Luna stops, start one separate Codex task in the same project. Select Sol with reasoning
XHigh and use this prompt once. This agent is authorized to inspect and correct the implementation;
it is not a read-only reviewer.

```text
You are the Finalizer for C:\dev\price-monitor. Review, correct, validate, and deliver the whole
product in this single execution.

Read AGENTS.md, docs/SPEC.md, docs/agents/CODER.md, docs/agents/REVIEWER.md, the existing
docs/reviews/REVIEW.md, all implementation/tests/fixtures/configuration/user documentation, and the
complete Git diff when available. Do not trust the Coder summary as evidence.

Evaluate the entire implementation against every requirement in docs/SPEC.md, including
functional behavior, regression risk, security boundaries, persistence, Telegram authorization,
URL and redirect safety, monetary parsing, availability, shipping and discount uncertainty,
append-only history, duplicate alerts, source-failure isolation, bounded resource use, and the
mandatory end-to-end acceptance scenario.

Unlike a read-only Reviewer, you must directly fix every confirmed defect and incomplete
requirement you find. Add or improve meaningful regression tests for each correction. You may edit
application code, tests, fixtures, configuration examples, and ordinary user documentation. Do
not modify docs/SPEC.md, docs/agents/CODER.md, or docs/agents/REVIEWER.md, and do not weaken a
requirement to make the product appear complete.

Run the complete automated test suite and every configured lint, type, static-security,
dependency-consistency, dependency-audit, and secret check. Repair failures and re-run affected
checks within this same execution. Do not create another agent task or request a repair loop.

Create or replace docs/reviews/REVIEW.md with the final status and the evidence required by
docs/agents/REVIEWER.md. Report APPROVED only if no blocking defect or unmet core requirement
remains; otherwise report CHANGES_REQUIRED and precisely identify anything that could not be
completed in this one execution.

Your final response is the delivery: summarize all corrections, changed areas, exact commands and
results, final review status, known limitations, and unresolved blockers. Do not ask for another
agent pass.
```

## 5. Running This in the Codex Desktop App

1. Open the `price-monitor` project.
2. Create one local task using the saved project directory.
3. Select GPT-5.6 Luna with reasoning High and paste the Coder prompt from section 3.
4. Wait until Luna finishes. Do not start another writing task concurrently.
5. Create one separate local task in the same saved project directory.
6. Select GPT-5.6 Sol with reasoning XHigh and paste the Finalizer prompt from section 4.
7. Treat the Finalizer response and `docs/reviews/REVIEW.md` as the final delivery.

The complete workflow therefore uses exactly two agent executions: one Luna implementation and
one Sol finalization. There are no intermediate gates and no repeated repair cycle.
