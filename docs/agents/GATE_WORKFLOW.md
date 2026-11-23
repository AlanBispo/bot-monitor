# Single-Pass Agent Workflow

Use this workflow for the complete product delivery described in [the specification](../SPEC.md#33-single-delivery-and-final-review). Run both tasks in the same local checkout.

## 1. Implementation

Start one implementation task with the [Coder instructions](CODER.md). Provide the full scope, the current Git diff, and any findings that still apply. Ask the Coder to implement the scope, preserve existing data and unrelated changes, and report the checks run and any gaps.

## 2. Final review

After implementation, start one separate Finalizer task. Provide the [specification](../SPEC.md), the [Reviewer instructions](REVIEWER.md), the current diff, and the implementation results. Authorize the Finalizer to fix confirmed defects and update the relevant tests. Ask it to run the configured checks and record the outcome in `docs/reviews/REVIEW.md`.

Use the report to determine the status of that delivery. Each report covers the revision and evidence it identifies.
