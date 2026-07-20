# Iteration guidance

Before proposing a change, record:

- baseline and candidate artifact hashes;
- PostgreSQL major and configuration artifact hash;
- workload profile set, scale, and observation duration;
- collection mode, content checksum, and completeness ratio;
- the exact metric or report item expected to move.

Prefer changes in this order:

1. Correct unsafe or internally inconsistent settings.
2. Resolve resource exhaustion and hard bottlenecks.
3. Adjust workload-profile-specific planner, memory, WAL, vacuum, or concurrency settings.
4. Change OS/tuned settings only when report evidence points to the host subsystem.

Define an acceptance condition before the rerun. Preserve regressions and unexpected plan changes as findings; do not tune solely for one favorable query.
