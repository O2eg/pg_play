# pg_maker and pg_play

Install the optional integration from PyPI:

```bash
python -m pip install --upgrade 'pg-play[maker]'
pg-play capabilities --component pg_maker
```

For local pg_maker development, install its checkout into the same environment:

```bash
pg_play/.venv/bin/python -m pip install -e ./pg_maker
```

## Adapter contract

`PgPlayService.maker_*` and the corresponding MCP tools invoke the separate
`pg-maker` CLI with argument vectors. pg_maker owns its build workers, Git trees,
patch series and caches. Preparation, resolution, build and runtime creation return
after starting a durable worker. Use `maker_status` and `maker_events` to observe
it, and `maker_cancel` to cancel it, including after a client disconnect.

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

`maker_plan(lock, input_store?, goal="package", run_db_tests=False)` returns
`pg_play/maker-plan-v1`: copies of the BuildLock and BuildPlan, parameters and an
overall `plan_hash`. `maker_start(plan, plan_hash, operations, operation_id,
cache_store?)` checks that hash, stores the reviewed inputs under
`.pg-play-plans/OPERATION/`, passes the original maker plan hash and returns
`operation_directory`. Reusing an operation ID with a different plan is rejected.
Resume uses the maker build plan hash, or the input hash for preparation/resolution.
The wrapper plan hash and the maker plan hash are distinct.

`maker_artifacts` lists verified artifact references, manifests, verification records
and reports. `maker_export` copies a finished operation to a new directory.
Ordinary run cleanup is available through pg_maker's CLI/Python API; development
sessions have the separate `maker_dev_prune` operation.

The CLI exposes `pg-play maker validate|prepare|resolve|plan|start|status|events|
cancel|resume|artifacts|export|runtime-plan|runtime-start`. After preparation and
resolution:

```bash
pg-play maker plan build.lock.json --goal package > reviewed-maker.json
pg-play maker start reviewed-maker.json --plan-hash SHA256_FROM_REVIEWED_PLAN \
  --operations ./maker-operations --operation-id candidate
pg-play maker status ./maker-operations/candidate
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
prefix. Create a separate stand feature with these diagnostic components. PGXS
uses maker's internal headers and pg_config; headers and PGXS are excluded from
the delivered packages. Extensions for a different PostgreSQL prefix do not
satisfy this contract.

Pinned starting recipes are in [examples/maker](../examples/maker). Copy the five
component recipes into the maker project's `components/` directory and
`my-feature-stand.yaml` into `config/features/`, then select the feature in
`build.yaml`. Previous local validation compiled the recipes against PostgreSQL
18.1 and checked InstallTree/DEB packaging and offline runtime construction with
`goal=package`, without documentation or database tests. Control and SQL files
were included. pg_stat_statements uses its native check target because upstream
does not support installcheck. CREATE EXTENSION and native database suites remain
separate acceptance checks.

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
5. Export results before cleanup. Database build tests require an explicit
   `run_db_tests` selection; a quick successful build does not qualify a DB release.
