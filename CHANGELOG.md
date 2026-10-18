# Changelog

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
