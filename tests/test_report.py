from __future__ import annotations

from pg_play.report import compare_benchmark_summaries


def _summary(tps: list[float]) -> dict[str, object]:
    return {
        "artifact_schema_version": "pg_perf_bench/report-v1",
        "iteration_parameters": ["clients"],
        "iteration_values": [1, 4],
        "benchmark_methodology": {"cache_policy": "warm"},
        "postgresql_compatibility": {"server": {"version_num": 180004}},
        "environment_evidence": {"identity_hash": "sha256:same-environment"},
        "database_configuration_evidence": {"effective_settings_hash": "sha256:config-a"},
        "item_count": 4,
        "benchmark_run_count": len(tps),
        "tps_values": tps,
    }


def test_compare_benchmark_summaries_reports_tps_delta() -> None:
    result = compare_benchmark_summaries(
        _summary([100.0, 200.0]),
        _summary([110.0, 220.0]),
    )

    assert result["comparability"]["comparable"] is True
    assert result["delta"]["mean_tps"] == 15.0
    assert result["delta"]["mean_tps_percent"] == 10.0
    assert result["delta"]["tps_values"] == [10.0, 20.0]


def test_compare_benchmark_summaries_marks_methodology_mismatch() -> None:
    baseline = _summary([100.0])
    candidate = _summary([100.0])
    candidate["benchmark_methodology"] = {"cache_policy": "cold"}

    result = compare_benchmark_summaries(baseline, candidate)

    assert result["comparability"]["comparable"] is False
    assert "benchmark_methodology" in result["comparability"]["mismatches"]


def test_compare_benchmark_summaries_rejects_environment_drift() -> None:
    baseline = _summary([100.0])
    candidate = _summary([110.0])
    candidate["environment_evidence"] = {"identity_hash": "sha256:different-environment"}

    result = compare_benchmark_summaries(baseline, candidate)

    assert result["comparability"]["comparable"] is False
    assert "environment_identity_hash" in result["comparability"]["mismatches"]


def test_compare_benchmark_summaries_allows_configuration_as_the_variable() -> None:
    baseline = _summary([100.0])
    candidate = _summary([110.0])
    candidate["database_configuration_evidence"] = {"effective_settings_hash": "sha256:config-b"}

    result = compare_benchmark_summaries(baseline, candidate)

    assert result["comparability"]["comparable"] is True
    assert result["delta"]["database_configuration_changed"] is True
