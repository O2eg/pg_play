---
name: review-postgres-configuration
description: Inspect an existing PostgreSQL server through a minimal read-only pg_diag collection, derive normalized host and database facts, generate a pg_configurator candidate, and compare current settings with recommended values. Use when a user asks to review, tune, size, or recommend PostgreSQL parameters for a named live server and provides or can provide SSH and database access. Do not use this skill to apply settings, restart PostgreSQL, or claim a candidate is proven optimal.
---

# Review PostgreSQL Configuration

Use only `plan_configuration_review`, `collect_configuration_facts`,
`generate_configuration_candidate`, and `compare_configuration_candidate`.
This workflow is read-only on the target.

## Gather inputs

1. Call `plan_configuration_review` with all known values.
2. Ask only for fields returned in `missing_inputs`. An SSH key alone is not a
   PostgreSQL connection; obtain the database endpoint, database, user, and an
   existing passfile when authentication requires one.
3. Pass SSH and database credentials only by local path or environment-variable
   reference. Never request or transmit key contents or passwords.
4. Require the user to choose database duty, storage class, desired replication
   mode, and PITR intent. Do not infer operational intent from current settings.
5. Resolve every plan error, including missing key or known-hosts files, before
   collection.

## Collect and calculate

1. Call `collect_configuration_facts` with a unique `review_id` and dedicated
   output directory. It collects the versioned minimal item set with one
   `pg_diag one-shot` run.
2. Check `facts.collection.usable`, missing and failed item ids, PostgreSQL
   major, CPU cores, RAM bytes, settings, and installed extensions. Stop when
   critical facts are unavailable; do not substitute guesses.
3. Call `generate_configuration_candidate` with the saved facts path and the
   reviewed tuning inputs. Report any explicit resource override separately
   from values derived from the server.
4. Call `compare_configuration_candidate` with the exact facts and candidate
   artifacts. Use its JSON rows as the source of truth and provide the Markdown
   artifact to the user.

## Interpret the result

- Present only parameters whose effective value differs or was not observed.
- Preserve `apply_mode`, current source, pending-restart state, rule, and
  warnings. A reloadable value can still be masked by a session override.
- Call the result a configuration candidate, not an optimum. Recommend a
  controlled `pg_perf_bench` comparison when the user needs performance proof.
- Do not apply the configuration, reload PostgreSQL, restart it, edit remote
  files, or trust a new SSH host key automatically.
