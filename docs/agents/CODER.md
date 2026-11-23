# Coder

Implement the requested scope against the [product specification](../SPEC.md). Keep its requirements
intact. The files under `docs/history/`, when present, are archival. Use the [review instructions](REVIEWER.md)
for independent approval.

## Before editing

- Read the request, specification, affected code and tests. If fixing a review finding, read the
  relevant report and confirm that the finding applies to the current revision.
- Respect explicit user instructions first, then the specification and this guide. Report conflicts
  instead of silently redefining requirements. Changes to the specification, reviewer guide or
  review report require an explicit documentation request.
- Identify effects on persisted data, external integrations, authorization and failure handling.
  Keep the change local and reuse existing code when it meets the requirement.

## During implementation

- Keep discovery separate from periodic monitoring of confirmed listings. Preserve listing identity
  and append-only history. Use money-safe arithmetic with explicit rounding, confirmed discounts
  and known shipping for target alerts. Preserve duplicate-alert prevention.
- Keep store integrations isolated. Prefer the simplest reliable collection method. Treat invalid,
  ambiguous and unavailable data explicitly. Preserve the last valid observation on failure and
  let other stores continue. Bound requests, concurrency and retries.
- Keep Telegram access limited to the authorized chat. Validate untrusted inputs and URLs, including
  redirects. Use parameterized SQL and keep writes consistent on failure. Keep credentials out of
  version control, fixtures, errors and logs. Respect store access controls.
- Keep local execution, SQLite and the existing architecture unless a product requirement justifies
  changing them. Justify new dependencies and update dependency metadata when needed.

## Before delivery

- Test changed behavior, including meaningful invalid inputs and external failures. Prefer fixtures
  or fake transports to live stores. Run the full relevant suite and the configured lint,
  security and dependency checks.
- Fix regressions caused by the change. Update affected setup instructions and examples. Report the
  changes, checks actually run, unresolved requirements and limitations. Leave the approval verdict
  to the reviewer.
