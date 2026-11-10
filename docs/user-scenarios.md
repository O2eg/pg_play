# User scenarios

pg-play coordinates eight independent components. Choose a workflow by the
question you need to answer; installing all components does not mean every run
uses all of them. The CLI, Python service and MCP tools share orchestration
logic. The [README](../README.md) lists installation requirements and interfaces.

In these diagrams, arrows show control or artifact flow. A replication arrow
specifically represents native PostgreSQL data transfer. Every mutating workflow
needs its own reviewed inputs; these scenarios are not one automatic pipeline.

## 1. Reproduce a workload or database incident

Use this to reproduce lock contention, vacuum pressure, slow queries or an
application-like mix on a disposable stand. pg-workload creates the selected
datasets and schedules activity; pg-diag records what PostgreSQL did during the
observation window.

```text
experiment.yaml -> pg-play validate / plan -> reviewed start
                            |
                 pg-configurator candidate
                            |
                         pg-stand
                            |
                         PostgreSQL <--- pg-workload profiles
                            |
                         pg-diag
                            |
                   JSON + HTML + run events
```

Initialize stand/workload projects, adapt the [manifest](../README.md#experiment-manifest),
then use `validate`, `plan`, `start`, `status` and `events`. Review the stand
action and database recreation flags first. Use a new run ID for each changed
experiment. For OS evidence from the container use `collection_mode: remote`;
`remote-db-only` collects database evidence only.

The result is a reproducible setup and an inspectable observation window, not a
guarantee that a particular incident occurred. Check report completeness and
the relevant sessions, waits or query items. The existing
[site demo lab](https://github.com/O2eg/pg_play/blob/main/labs/site-demo-report/README.md) adds explicit incident staging
to this pattern and has its own preparation requirements.

## 2. Compare configuration or capacity

Use this to evaluate a pg-configurator candidate, connection limit, CPU/RAM
allocation, storage layout or OS tuning change. Keep a dedicated disposable
benchmark database and define the metric and intended variable before running.

```text
baseline inputs  -> pg-play -> pg-stand A -> pg-perf-bench -> baseline.json
candidate inputs -> pg-play -> pg-stand B -> pg-perf-bench -> candidate.json
                                                             |
                   inspect -> compare / selected JOIN task <--+
                                      |
                      TPS curve + latency + OS evidence
```

The two stands can be used sequentially. The optional benchmark phase runs
before the pg-workload/pg-diag phase in a standard experiment, so those phases
do not create a simultaneous benchmark under background workload. Benchmark OS
sampling runs on the runner host; it does not automatically measure a remote
database host's OS. Keep client placement constant and collect remote evidence
separately when needed.

Use `inspect-benchmark-report` for each artifact. `compare-benchmark-reports`
returns summary deltas and comparability findings; read both. For an intentional
change, use `benchmark-join-tasks` and select the appropriate task, for example
`optimize-db-config`, `scale-cpu` or `compare-storage`, then
`join-benchmark-reports` with an explicit report list and baseline first.
[JOIN guidance](https://github.com/O2eg/pg_play/blob/main/src/pg_play/skills/analyze-postgres-experiment/references/benchmark-joins.md)
describes the controlled dimensions. Repeat measurements before interpreting
small differences; a single mean TPS increase is not proof of improvement.

## 3. Diagnose or review an existing server

Use this when a PostgreSQL server already exists and recreating it is unsuitable.
Neither workflow requires pg-stand or pg-workload. Both use local references to
database credentials and verified SSH access; they do not deploy configuration.

```text
existing PostgreSQL + SSH host
             |
      pg-play reviewed collection
             |
           pg-diag
             +--> bounded incident report -> inspect findings
             |
             +--> configuration facts -> pg-configurator candidate
                                                 |
                                     current/candidate difference
```

For incidents, use the MCP or Python operations `plan_live_diagnostics`,
`start_live_diagnostics`, then status/events and report inspection. Choose
`performance`, `locks`, `io` or `cpu`; the capture lasts 30–900 seconds.
For tuning review, use `plan_configuration_review`, `collect_configuration_facts`,
`generate_configuration_candidate` and `compare_configuration_candidate`.
Supply the intended duty, storage class, replication and PITR policy explicitly.

The output is a diagnostic window or a JSON/Markdown configuration difference.
Collection adds diagnostic queries and sampling load. A candidate is a proposal;
use a controlled lab comparison before treating it as performance evidence.
See [live diagnostics](../README.md#live-server-incident-diagnostics),
[configuration review](../README.md#existing-server-configuration-review) and
[agent setup](agent-client-setup.md).

## 4. Rehearse and execute a reviewed deployment packet

Use this for schema/data migrations, maintenance or export packets managed by
pg-converter. Rehearse against a dedicated pg-stand first; for an existing target,
plan the exact aliases again because a laboratory plan does not authorize another
endpoint.

```text
trusted SQL/Python packet + protected target configuration
                           |
              pg-play plan-converter-run
                           |
                reviewed sources + aliases
                           |
              detached pg-converter worker
                           |
          database A -> database B -> compact result/events
```

Database processing is sequential. Plans bind source hashes, connection
identities and protected input-file hashes. Start with `start-converter-run` and
observe its status/events. A nominally read-only packet may still initialize or
update tracker state, so review it as a mutation. Python packets are trusted code,
not sandboxed input.

The result records packet outcomes, not application correctness. Add application
checks or collect pg-diag reports in separate windows. If execution is partial,
failed or interrupted, inspect target state before making a new plan; there is
no automatic packet resume. See the [converter runbook](pg_converter-integration.md).

## 5. Test a PostgreSQL patch, build or extension

Use the maker extra when the variable is PostgreSQL source, build options or
extension code. pg-maker owns source snapshots, ChangeSets, build workers and
test evidence; pg-play can use its packages to construct a stand runtime image.

```text
source + recipes + ChangeSet -> pg-maker prepare / resolve / plan
                                                |
                                      reviewed build + tests
                                                |
                                  packages + qualification evidence
                                                |
                               runtime image (immutable Docker ID)
                                                |
pg-configurator ----------------------------> pg-stand
                                                |
                               pg-perf-bench / pg-workload + pg-diag
                                                |
                                  baseline/candidate evidence
```

Check runtime prerequisites with `maker runtime-check`, then add the reviewed
maker plan under `spec.maker`. The experiment builds, verifies packages and
creates the image before starting the stand. Use separate baseline/candidate
manifests and projects; select a benchmark comparison/JOIN whose controls match
the intended build change. A rejected comparison must not be bypassed.

The current runtime adapter targets prepared Debian 13/x86_64 images and a
specific package/diagnostic-extension contract. Standalone extension builds are
also available, but their separate DEBs are not automatically merged into that
runtime payload. A build with skipped DB tests remains unverified.

For delivery, preview with `maker publish-plan` and explicitly start
`maker publish-run`. Publication is separate from experiment execution and has
its own worker. That native publication interface does not accept a reviewed
plan hash; preserve its source/configuration between preview and execution.
See [maker integration](pg_maker-integration.md) for development, qualification,
extension packaging and receiver configuration.

## 6. Rehearse logical copy, DDL delivery and cutover

Use the logical extra for a same-major PostgreSQL 15–18 migration rehearsal with
initial copy, ongoing DML, supported DDL, drift checks and explicit recovery.
This is a restricted alpha laboratory workflow, not a cross-major upgrade tool.

```text
pg-stand source                         pg-stand target (empty DB)
       |                                          ^
pg-workload -> source DB == native logical DML === target DB
                   |                              ^
reviewed DDL ------>+--- pg-logical-repl DDL ------+
                   |                              |
                   +----------- pg-diag -----------+
                                  |
               pg-play controller + independent exact comparison
                                  |
                 remain replicating / planned abort / cutover
```

Provision both endpoints first, populate the source and install workloads before
prepare, then configure source-admin, source-replication and target access. The
subscriber must be able to reach the replication endpoint; its address may differ
from the CLI's source address. Existing role attributes and password verifiers
must match. A fresh target database does not imply compatible cluster-wide roles.

Use a separate `pg_play/logical-experiment-v1` scenario and
`logical plan --operation experiment`, followed by `logical start`. Keep diagnostics
and the independent oracle enabled for an acceptance rehearsal. The worker checks
readiness, executes reviewed DDL, observes activity, stops its workload and compares
data/catalogs/roles/ACLs. External writers must also stop before exact comparison.
Cutover requires a real external write fence; pg-play does not redirect clients.

Current pg-converter packets advertise tracker initialization DDL and are rejected
inside this live migration scenario. Run them before prepare or select supported
SQL files via `ddl_files`; do not assume any arbitrary packet or compound SQL can
be delivered through the DDL queue.

Stopping the controller leaves native replication active. Recover `BLOCKED` using
a separate reviewed native operation, then explicitly resume the controller.
Abort/detach can leave source cleanup pending. See
[logical integration](pg_logical_repl-integration.md) for plans, evidence and limits.

## 7. Use independent stands

Use separate projects for source/target migration labs, baseline/candidate builds,
or independent team experiments. pg-play requires pg-stand 0.3.0+, which supports
multi-project discovery and isolation. When testing unpublished releases, install
the matching component checkouts through the [development workflow](../README.md#development).

```text
                      one Docker Engine
                              |
          +-------------------+-------------------+
          |                                       |
project A / credentials A                project B / credentials B
stand A / ports A / storage A             stand B / ports B / storage B
          |                                       |
workload root A + artifacts A             workload root B + artifacts B
          +-------------------+-------------------+
                              |
                   explicit comparison or migration
```

Each experiment selects its project through `spec.stand.project`. Use unique
stand names, container/network names, all published ports and non-overlapping
storage. Keep workload roots and artifact directories separate. A copied YAML
with unchanged names/ports is not an independent stand. One project directory
still owns only one active profile at a time because credentials are shared.

For migration endpoints, pg-stand's `single-logical.yaml` enables logical decoding
without creating a managed subscription; use distinct source/target configurations.
If sharing an external Docker network, create it separately and configure reachable
replication addresses. PostgreSQL credentials/roles still require deliberate setup.
For performance comparisons, run measurements sequentially on a shared host unless
resource contention is the intended variable.

Use pg-stand `list` for daemon discovery and the owning project for lifecycle
actions. Preserve each project's path and credentials for recovery. The
[pg-stand repository](https://github.com/O2eg/pg_stand) supplies lifecycle guidance;
the 0.3.0 source tree also contains `docs/multiple-stands.md` and `docs/recovery.md`.

## Keep the workflow boundaries visible

No single experiment manifest runs all eight components automatically. A complete
rehearsal can compose a maker build, stand experiment, converter preparation and
logical migration as separately reviewed stages. Preserve each stage's plan,
versions, status and artifacts. Launcher success, complete diagnostics, successful
database tests and successful migration verification are different results.
