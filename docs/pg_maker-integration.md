# pg_maker and pg_play

See the [build and extension scenario](user-scenarios.md#5-test-a-postgresql-patch-build-or-extension)
for the component flow and intended uses.

Starting with pg-play 0.6.0, the standard installation includes pg-maker:

```bash
python -m pip install --upgrade pg-play
pg-play capabilities --component pg_maker
```

For local pg_maker development, install its checkout into the same environment:

```bash
pg_play/.venv/bin/python -m pip install -e ./pg_maker
```

pg-play requires `pg-maker>=0.6.0,<1`. Commands are capability-checked before
execution, since a version number alone does not identify every supported workflow.
Runtime preflight uses maker's `runtime check` command, included in this minimum
version. pg-maker can also be installed and used independently of pg-play. CI tests against the current maker source checkout,
including the wheel installation smoke test. Publish a required maker change first.

## Adapter contract

`PgPlayService.maker_*` and the corresponding MCP tools invoke the separate
`pg-maker` CLI with argument vectors. pg_maker owns its build workers, Git trees,
patch series and caches. Preparation, resolution, build and runtime creation return
after starting a durable worker. Use `maker_status` and `maker_events` to observe
it, and `maker_cancel` to cancel it, including after a client disconnect.

Both CLIs run on the same machine and share paths and Docker images. This adapter
does not transport operations to a remote builder. Build profiles, PostgreSQL and
ICU versions, extension recipes, SDK compatibility, package names and installation
paths belong to pg_maker. pg_play supplies only the selected profile/configuration
and the consumer's runtime requirements; it does not invent vendor-specific variants.
Use `maker_init(directory, profile?)`, `maker_config_show(manifest)` and
`maker_package_names(manifest)` to obtain maker's effective inputs and native names.

Preparation and resolution accept `config_dir`, `features`, `build_mode`,
`with_doc` and `doc_languages` in Python and MCP. CLI options are
`--config-dir`, `--feature`, `--build-mode`, `--with-doc`/`--without-doc`
and `--doc-language`.

Development operations are `maker_dev_open`, `maker_dev_status`,
`maker_dev_patch`, `maker_dev_explain`, `maker_dev_build`, `maker_dev_exec`,
`maker_dev_pin` and `maker_dev_prune`. The CLI uses matching hyphenated names:

```bash
pg-play maker dev-open ./work --from-run /path/to/operations/RUN
pg-play maker dev-patch ./work commit --scope component:pg-wait-sampling --id fix-build
pg-play maker dev-explain ./work --component pg-wait-sampling
pg-play maker dev-build ./work --component pg-wait-sampling
pg-play maker dev-exec ./work --component pg-wait-sampling -- make -n
pg-play maker dev-prune ./work --keep-last 2 --out ./cleanup.json
pg-play maker dev-prune ./work --keep-last 2 --apply ./cleanup.json
```

Build and debug commands return an operation directory immediately. Observe or
cancel them through the regular maker operation methods. A new development
iteration freezes a new lock; `resume` retains the previous inputs and plan.
Debug commands run inside a private container copy of an iteration's workspace.
Pruning preserves the current and pinned iterations and requires an unchanged
cleanup plan. It does not run a global Docker prune.

`maker_plan(lock, input_store?, goal="package", run_db_tests=None)` returns
`pg_play/maker-plan-v1`: copies of the BuildLock and BuildPlan, parameters and an
overall `plan_hash`. `maker_start(plan, plan_hash, operations, operation_id,
cache_store?, publish_config?)` checks that hash, stores the reviewed inputs under
`.pg-play-plans/OPERATION/`, passes the original maker plan hash and returns
`operation_directory`. Reusing an operation ID with a different plan is rejected.
With no test override, maker's locked policy applies (tests enabled by default).
The wrapper records the effective boolean returned in the BuildPlan. CLI overrides
are mutually exclusive `--run-db-tests` and `--skip-db-tests`. `--goal verified`
and `--goal test-full` require DB tests; an explicit skip is rejected by maker.
`test-full` runs the long suites and does not produce packages for a stand.
Resume uses the maker build/runtime plan hash, or the operation input hash for
preparation, resolution, standalone extensions and publication.
The wrapper plan hash and the maker plan hash are distinct.

`maker_artifacts` lists verified artifact references, manifests, verification records
and reports. `maker_export` copies a finished operation to a new directory;
`include_inputs=True` / `--include-inputs` also exports frozen inputs.
Ordinary run cleanup is available through pg_maker's CLI/Python API; development
sessions have the separate `maker_dev_prune` operation.

The CLI exposes `pg-play maker validate|prepare|resolve|plan|start|status|events|
cancel|resume|artifacts|export|runtime-check|runtime-plan|runtime-start` and the
development/package commands described below. Initialize and inspect editable
inputs first:

```bash
pg-play maker init ./maker-project
pg-play maker config-show ./maker-project/build.yaml
pg-play maker validate ./maker-project/build.yaml
pg-play maker prepare ./maker-project/build.yaml \
  --output ./maker-project/environments.json --input-store ./maker-project/inputs \
  --operations ./maker-operations --operation-id prepare-01
pg-play maker status ./maker-operations/prepare-01
```

Edit the feature and runtime dependency recipe before preparation if a stand
runtime is required. Wait for preparation to succeed before resolving:

```bash
pg-play maker resolve ./maker-project/build.yaml \
  --environments ./maker-project/environments.json \
  --output ./maker-project/build.lock.json --input-store ./maker-project/inputs \
  --operations ./maker-operations --operation-id resolve-01
pg-play maker status ./maker-operations/resolve-01
```

Wait for resolution to succeed before planning. Select the actual variant and
prepared runtime image from the returned artifacts; the names/hashes below are
examples, not values to copy unchanged:

```bash
pg-play maker plan ./maker-project/build.lock.json --goal package > reviewed-maker.json
# Before an experiment, ask maker to verify the selected variant's prerequisites:
pg-play maker runtime-check reviewed-maker.json \
  --variant my-feature.debian13-x64.candidate --base-image sha256:PREPARED_IMAGE_ID
pg-play maker start reviewed-maker.json --plan-hash SHA256_FROM_REVIEWED_PLAN \
  --operations ./maker-operations --operation-id candidate
pg-play maker status ./maker-operations/candidate
# Wait for a successful terminal build before reading/exporting its artifacts.
pg-play maker artifacts ./maker-operations/candidate
```

## Runtime image for pg_stand

The initial profile targets Debian 13 / x86_64 with server, client and libpq DEBs
under a custom `/opt/...` prefix. `maker_runtime_plan(artifacts, base_image,
output)` records hashes of packages, the local runtime repository and entrypoint
files. `base_image` must be a prepared local Debian 13 image identified by
`sha256:...`, containing Python 3, gosu, openssh-server, PgBouncer, iproute2,
procps, lshw, sysstat, passwd, util-linux, locales and certificates. Match PgBouncer
to the installed pg_stand profile. A custom platform recipe can add these packages
to `environments.test.packages`; use the resulting image ID from `maker_prepare`.

Runtime construction is offline and does not run initdb or start PostgreSQL. It
checks libraries with ldd. The package payload must contain pg_stat_statements,
pg_buffercache, pg_wait_sampling, pg_stat_kcache and auto_explain under the same
prefix. Select a maker feature providing these modules. PGXS uses maker's headers
and pg_config; shipping headers and PGXS is controlled by maker's
`packaging.server_dev_files`. Extensions for another prefix do not satisfy this contract.

The generic [stand feature example](../examples/maker/my-feature-stand.yaml) refers
to recipes supplied by `pg-maker init`. Copy only that feature into
`config/features/`, select it in `build.yaml`, and retain
`packaging.standard_contrib: true` for pg_buffercache and auto_explain. Component
versions and compatibility remain in maker's recipes. For an existing custom
distribution, adjust its feature in the maker project instead of replacing its
component list with the generic example.

`maker_runtime_check(plan, variant, base_image)` asks maker to check the selected
candidate, platform, prefix, dependency-export policy and declared modules before
compilation. Experiment planning calls it automatically. This checks declared
inputs; the subsequent runtime plan still verifies the actual packages and evidence.
If a module is missing, change the maker feature and prepare/resolve again.

Runtime handoff needs the pinned dependency repository. Default source and package
tests provide it. To skip source tests while retaining a usable runtime export,
set `spec.validation.run_package_tests: true` in the maker project's
`config/defaults.yaml` **before preparation/resolve**, then explicitly skip DB
tests. Completely untested binary-only builds without documentation fail preflight.
Standalone extension packages can be built through the tools below, but this
runtime adapter currently reads modules from the three database DEBs; it does not
merge separately packaged extensions into their payloads.

The profile reuses the installed pg_stand entrypoint and PgBouncer helper, hashing
their contents. Updates require a new plan. The custom bootstrap uses maker's
binaries, the postgres account, PGDATA/POSTGRES_* and pg_stand's WAL/log mounts.
Configuration stays in PGDATA. The Unix socket directory is fixed at
`/var/run/postgresql`; do not override it in stand parameters.

The result records the immutable image ID, build/runtime plan hashes, artifact
manifest hash, prefix, version and original qualification. Creating an image does
not raise its qualification. Publishing or exporting that local Docker image is a
separate operation.

## Maker phase in an experiment

Add an optional section to the experiment manifest:

```yaml
spec:
  maker:
    plan: reviewed-maker.json
    plan_hash: sha256:... # pg_play/maker-plan-v1 hash
    variant: my-feature-stand.debian13-x64.candidate
    runtime_base_image: sha256:... # prepared image with pg_stand utilities
    timeout_seconds: 7200
```

Execution order is maker build, package verification, runtime image, pg_stand, then
the existing benchmark/workload/diagnostic phases. Planning uses an image reference
derived from the reviewed inputs. Execution checks the original configuration,
substitutes the produced immutable image ID and records the resulting stand plan
in `RUN/maker/stand-plan.json`. Other settings retain their reviewed values and
`pull_policy=never`. The new image ID changes the desired-state hash. The original
stand YAML is not overwritten.

`RUN/maker/active.json` is written before launching a worker so cancellation can
find it after coordinator loss. Resume reuses successful operations and explicitly
resumes failed ones after checking the maker plan hash. A timeout cancels the
current maker operation. `RUN/maker/provenance.json` and `state.maker` retain
qualification, database-test status and the plan, package and image hashes.
A successful build with `db_tests=skipped` remains `unverified`.

For baseline/candidate comparisons without long database suites, create two
features with the same diagnostic components, one with the ChangeSet and one
without it. Use separate manifests and stand project/storage paths, run the
experiments sequentially and release each active stand through teardown. Compare
their reports with the existing report comparison commands. Maker's built-in
baseline branch is included only when database tests are enabled.

Full database/workload execution is a separate acceptance step. Unit/contract
tests and runtime image construction do not establish all topology/SQL behavior.

## Standalone extension packages

The CLI, Python service and MCP expose `extension-init`, `extension-prepare`,
`extension-plan` and `extension-build` (MCP names use `maker_extension_*`).
The native maker project controls SDK dependencies, source tests and clean-container
package tests. For example:

```bash
pg-play maker extension-init ./extensions --major 18 --platform debian-13 \
  --extension pg_wait_sampling --extension pg_stat_kcache --extension pg_repack
pg-play maker extension-prepare ./extensions/extensions.yaml \
  --output ./extensions/lock.json --input-store ./extensions/inputs \
  --operations ./operations --operation-id extension-prepare
# Wait for preparation to succeed using status/events before reading its lock.
pg-play maker extension-plan ./extensions/lock.json
pg-play maker extension-build ./extensions/lock.json \
  --operations ./operations --operation-id extension-build
```

Use `--platform rocky-10` for RPMs. A profile target uses
`--profile PROFILE --packages DIRECTORY_OF_DATABASE_PACKAGES`; maker validates the
actual SDK and dependencies. `--split-bundled` is an explicit native migration
request, never automatically enabled by pg_play. Edit maker's generated recipes
for other extensions. A successful extension build does not qualify the database core.

## Publication and CI/CD

`maker_publish_plan(source, config)` previews verified artifacts and receivers
without network requests. `maker_publish_run(source, config, operations,
operation_id, wait_for_source=False)` starts maker's durable publisher using the
current source/configuration. Preview is read-only; this native interface does not
accept a reviewed publication hash. Maker seals and validates the inputs when run
is invoked. Keep the configuration immutable between preview and execution.

```bash
pg-play maker publish-plan ./operations/extension-build --config publication.yaml
pg-play maker publish-run ./operations/extension-build --config publication.yaml \
  --operations ./operations --operation-id publication
```

Receiver configuration and credential references use maker's publication format.
Maker owns Nexus APT/YUM/Raw uploads, directory delivery, package-test evidence gates,
hash verification, retry/reuse and TLS policy. pg_play does not store receiver
credentials or change TLS settings. A plain directory of DEBs/RPMs is insufficient:
the source must be a successful operation or a verified portable export.

`maker start` and `maker extension-build` accept `--publish-config publication.yaml`.
The returned `operation_directory` then belongs to **publication**;
`source_operation_directory` identifies the independent build. Observe the returned
operation until it succeeds: a successful launcher exit only confirms scheduling.
Cancellation of publication leaves the build running. Resume extensions/publication
with their `result.input_hash` from `maker status`, not a database plan hash.
An already-running build can be followed using `publish-run --wait-for-source`.

The experiment's automatic maker phase only builds its packages and runtime image.
Publish its completed build through the separate publication tools when requested.
No upload is implied by starting a performance experiment.

## Source-editing workflow

1. Resolve freezes a source snapshot in InputStore. `maker_changes_workspace`
   creates a separate editable `source/` tree. Supply `lock` and `feature` to
   include common and preceding ChangeSets; `until=CHANGE_ID/OPERATION_ID` selects
   the point before that operation. Without a lock, the base is raw upstream.
2. Edit that copy, use `maker_changes_export` to create a ChangeSet and diff, and
   check its structure/files with `maker_changes_validate`. ABI, data and WAL
   compatibility remain unknown until separately established.
3. Add the ChangeSet to a feature, resolve again and review the new plan hash.
   Build, run the experiment and compare it with the baseline.
4. Documentation goals are `doc-source`, `translations`, `translated-docs` and
   `docs`. Translation bundles are checked for source identity and completeness;
   model credentials and calls remain outside maker.
5. Export results before cleanup. Database build tests follow the locked policy
   unless explicitly overridden with `--run-db-tests` or `--skip-db-tests`.
   A quick successful build does not qualify a DB release.
