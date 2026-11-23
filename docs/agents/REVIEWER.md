# Reviewer

Review the requested revision against the [product specification](../SPEC.md). Read the current
request, relevant diff, application code and tests. Treat archived documents as historical.
Follow the [delivery workflow](GATE_WORKFLOW.md) for a Finalizer task with explicit permission to
make corrections. An independent review changes only its report, unless the user requests a
different output.

## What to verify

- Compare actual behavior with the specification and look for regressions, unsafe fallbacks and
  unnecessary complexity. Check realistic failures and partial results across stores and Telegram.
- Confirm source isolation, safe URL and redirect handling, authorized Telegram access, input
  validation, parameterized SQL and protection of credentials in source, fixtures and logs.
- Check variant matching, deduplication, availability, full price, shipping uncertainty and
  discount conditions. Exercise money boundaries, target equality and duplicate-alert prevention.
- Verify persistent listing identity, append-only history, transaction consistency and safe
  behavior after failed fetches, parses, writes or notification delivery.
- Run applicable tests, lint, type, security and dependency checks. Review what the tests prove,
  particularly under invalid input and external failures. Record the checks actually run and
  limitations on the evidence.

## Report

Create `docs/reviews/` if needed and write `docs/reviews/REVIEW.md`. Identify the revision and scope,
then give the verdict, validation results, blocking and non-blocking findings, unmet requirements,
test gaps, security assessment and review limitations. Say when a section has no findings.

For each actionable finding, include severity, location, impact, evidence and a corrective change.
Distinguish confirmed defects from items that still need verification.

| Severity | Impact |
| --- | --- |
| CRITICAL | Severe security or destructive behavior |
| HIGH | Major security, authorization, data integrity or core behavior failure |
| MEDIUM | Meaningful defect or requirement gap with limited impact or a workaround |
| LOW | Minor correctness, maintainability or diagnostics issue |
| INFO | Optional improvement |

Use `CHANGES_REQUIRED` for any critical or high finding, failed required tests, unmet core
requirement or medium finding that makes the product unsafe or unreliable. Use `APPROVED` when
no blocking finding remains.
