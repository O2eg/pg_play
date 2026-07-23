from __future__ import annotations

from pg_play.state import append_event, read_events


def test_event_log_is_ordered_and_supports_cursor_pagination(tmp_path) -> None:
    path = tmp_path / "events.jsonl"

    first = append_event(path, run_id="run-1", event_type="created", state="queued")
    second = append_event(
        path,
        run_id="run-1",
        event_type="step_started",
        state="running",
        step="stand",
    )
    append_event(path, run_id="run-1", event_type="step_completed", step="stand")

    assert first["sequence"] == 1
    assert second["sequence"] == 2
    page = read_events(path, after_sequence=1, limit=1)
    assert [event["sequence"] for event in page["events"]] == [2]
    assert page["last_sequence"] == 2
    assert page["has_more"] is True
    tail = read_events(path, after_sequence=page["last_sequence"], limit=10)
    assert [event["sequence"] for event in tail["events"]] == [3]
    assert tail["has_more"] is False


def test_missing_event_log_returns_an_empty_page(tmp_path) -> None:
    assert read_events(tmp_path / "missing.jsonl", after_sequence=7) == {
        "events": [],
        "last_sequence": 7,
        "has_more": False,
    }


def test_torn_final_event_is_ignored_and_repaired_on_the_next_append(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    append_event(path, run_id="run-1", event_type="created")
    with path.open("ab") as stream:
        stream.write(b'{"schema_version":"pg_play/run-event-v1","sequence":2')

    assert [event["sequence"] for event in read_events(path)["events"]] == [1]
    repaired = append_event(path, run_id="run-1", event_type="recovered")

    assert repaired["sequence"] == 2
    assert [event["type"] for event in read_events(path)["events"]] == [
        "created",
        "recovered",
    ]
