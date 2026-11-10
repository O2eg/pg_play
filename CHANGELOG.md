# Changelog

## 0.6.0 — 2026-11-11

- Require Python 3.11+ and install all eight component distributions by default,
  including pg-maker 0.6.0+ and pg-logical-repl 0.2.0+. Remove the maker/logical extras.
- Add logical replication integration with typed CLI, Python and MCP operations.
- Verify combined wheel installation and independent installation of each component;
  require pg-stand 0.3.0+ for multiple stands and pg-diag 0.19.1+ for diagnostics.
- Include the public Markdown guides and example configurations in wheels and
  source distributions, with archive content and documentation link checks.
- Add reviewed migration plans, detached controllers, durable events and reports,
  explicit interruption recovery, and separate controller/migration states.
- Add managed DDL experiments, converter compatibility checks, pg_workload scheduler reuse,
  source/target diagnostics and independent exact comparison before completion.
- Preserve native plan hashes and recovery rules. Controller cancellation leaves
  native replication intact; pause, abort and cleanup require separate plans.
- Record host boot identity in subprocess records to reject stale PIDs after reboot.
- Reconcile lost stage results using native completion events, resume repaired
  blocked controllers, and retain cleanup errors independently of worker status.
- Stop only a recorded workload process, persist reports before publishing completion,
  and keep independent sequence/materialized-view checks active under Python optimization.

## 0.5.0 — 2026-10-18

- Add the optional `pg-play[maker]` integration with pg-maker 0.1.0 or newer.
- Expose configuration validation, environment preparation, input resolution,
  reviewed build plans, durable operations, artifact inspection and export through
  CLI, Python and MCP.
- Add Git development sessions, patch series management, selected component
  rebuilds, rebuild explanations, container debugging and iteration retention.
- Bind maker-built packages and immutable runtime images to pg_stand experiments,
  preserving build provenance, cancellation and recovery behavior.
- Package the runtime entrypoint, component recipe examples and integration guide.
- Check the optional maker dependency and its CLI contract during release validation.

Database build tests remain opt-in. An unverified maker build retains that status
when exported or used to create a runtime image.
