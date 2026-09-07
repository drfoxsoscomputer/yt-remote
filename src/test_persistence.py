"""Tests de src/persistence.py: StateStore con escritura atomica y versionado.

Cubre: round-trip, archivo faltante, JSON corrupto, version desconocida,
atomico (no quedan .tmp), truncado a 100 items, debouncing y flush.
"""
import json
import sys
import tempfile
from pathlib import Path

SRC = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC))

from persistence import StateStore, CURRENT_VERSION, MAX_HISTORY  # noqa: E402


def make_store() -> StateStore:
    """Crea un StateStore con un archivo temporal, sin tocar data/."""
    tmp = Path(tempfile.mkdtemp()) / "state.json"
    return StateStore(tmp)


def test_defaults_when_file_missing():
    s = make_store()
    state = s.load()
    assert state["version"] == CURRENT_VERSION
    assert state["volume"] == 100
    assert state["paused"] is False
    assert state["current"] is None
    assert state["playlist"] == []
    assert state["history"] == []
    assert state["radio_artist"] == ""
    print("  OK  test_defaults_when_file_missing")


def test_round_trip_basic():
    s = make_store()
    state = {
        "version": 1,
        "volume": 80,
        "paused": True,
        "current": None,
        "playlist": [],
        "history": [],
        "radio_artist": "GP Band",
    }
    s.save(state)
    loaded = s.load()
    assert loaded["volume"] == 80
    assert loaded["paused"] is True
    assert loaded["radio_artist"] == "GP Band"
    print("  OK  test_round_trip_basic")


def test_round_trip_complex():
    s = make_store()
    state = {
        "version": 1,
        "volume": 65,
        "paused": False,
        "current": {
            "url": "u1",
            "title": "Inexplicable",
            "duration_seconds": 200,
            "thumbnail": "https://x",
            "channel": "GP Band",
        },
        "playlist": [
            {"url": "u2", "title": "A", "duration_seconds": 0},
            {"url": "u3", "title": "B", "duration_seconds": 0},
        ],
        "history": [{"url": "u0", "title": "Z"}],
        "radio_artist": "Mafe Restrepo",
    }
    s.save(state)
    loaded = s.load()
    assert loaded["current"]["title"] == "Inexplicable"
    assert loaded["playlist"][0]["url"] == "u2"
    assert loaded["history"][0]["url"] == "u0"
    assert loaded["radio_artist"] == "Mafe Restrepo"
    print("  OK  test_round_trip_complex")


def test_corrupt_json_returns_defaults():
    s = make_store()
    s.path.write_text("{ esto no es json valido ", encoding="utf-8")
    state = s.load()
    assert state["version"] == CURRENT_VERSION
    assert state["volume"] == 100
    print("  OK  test_corrupt_json_returns_defaults")


def test_truncated_json_returns_defaults():
    s = make_store()
    s.path.write_text('{"version": 1, "volume": 80, "playlist": [', encoding="utf-8")
    state = s.load()
    assert state["volume"] == 100, "estado corrupto deberia caer a defaults"
    print("  OK  test_truncated_json_returns_defaults")


def test_unknown_version_returns_defaults():
    s = make_store()
    s.path.write_text('{"version": 99, "volume": 50}', encoding="utf-8")
    state = s.load()
    assert state["volume"] == 100, "version futura debe caer a defaults"
    print("  OK  test_unknown_version_returns_defaults")


def test_save_creates_parent_dir():
    tmp = Path(tempfile.mkdtemp()) / "subdir" / "state.json"
    s = StateStore(tmp)
    s.save({"version": 1, "volume": 50})
    assert tmp.exists()
    print("  OK  test_save_creates_parent_dir")


def test_atomic_no_leftover_tmp():
    s = make_store()
    s.save({"version": 1, "volume": 70})
    assert s.path.exists()
    assert not s.path.with_suffix(".json.tmp").exists()
    print("  OK  test_atomic_no_leftover_tmp")


def test_history_truncated_to_max():
    s = make_store()
    huge = [{"url": f"u{i}", "title": f"T{i}"} for i in range(500)]
    s.save({"version": 1, "history": huge})
    loaded = s.load()
    assert len(loaded["history"]) == MAX_HISTORY
    assert loaded["history"][-1]["url"] == "u499"
    print("  OK  test_history_truncated_to_max")


def test_save_overwrites_previous():
    s = make_store()
    s.save({"version": 1, "volume": 30, "radio_artist": "A"})
    s.save({"version": 1, "volume": 90, "radio_artist": "B"})
    loaded = s.load()
    assert loaded["volume"] == 90
    assert loaded["radio_artist"] == "B"
    print("  OK  test_save_overwrites_previous")


def test_mark_dirty_and_flush_cycle():
    s = make_store()
    s.load()
    s._current = {"version": 1, "volume": 50}
    s.mark_dirty()
    assert s._dirty is True
    s.flush()
    assert s._dirty is False
    loaded = s.load()
    assert loaded["volume"] == 50
    print("  OK  test_mark_dirty_and_flush_cycle")


def test_mark_dirty_creates_flush_timer():
    """mark_dirty programa un timer cuando hay un loop activo."""
    import asyncio
    s = make_store()
    s.load()
    s._current = {"version": 1, "volume": 60}

    async def run():
        s._loop = asyncio.get_event_loop()
        s.mark_dirty()
        return s._flush_timer

    timer = asyncio.run(run())
    assert timer is not None, "mark_dirty debe programar timer con loop activo"
    timer.cancel()
    print("  OK  test_mark_dirty_creates_flush_timer")


def test_mark_dirty_no_timer_without_loop():
    """Sin loop activo, mark_dirty no rompe y queda dirty=True."""
    s = make_store()
    s.load()
    s._current = {"version": 1, "volume": 60}
    s._loop = None
    s.mark_dirty()
    assert s._dirty is True
    assert s._flush_timer is None
    print("  OK  test_mark_dirty_no_timer_without_loop")


def test_save_unicode_preserved():
    s = make_store()
    s.save({"version": 1, "radio_artist": "Acentos: ñ á é í"})
    loaded = s.load()
    assert loaded["radio_artist"] == "Acentos: ñ á é í"
    print("  OK  test_save_unicode_preserved")


def test_save_partial_state_with_defaults():
    s = make_store()
    s.save({"version": 1, "volume": 42})
    loaded = s.load()
    assert loaded["volume"] == 42
    assert loaded["paused"] is False
    assert loaded["current"] is None
    print("  OK  test_save_partial_state_with_defaults")


def run():
    print("\n=== Tests de persistence ===\n")
    test_defaults_when_file_missing()
    test_round_trip_basic()
    test_round_trip_complex()
    test_corrupt_json_returns_defaults()
    test_truncated_json_returns_defaults()
    test_unknown_version_returns_defaults()
    test_save_creates_parent_dir()
    test_atomic_no_leftover_tmp()
    test_history_truncated_to_max()
    test_save_overwrites_previous()
    test_mark_dirty_and_flush_cycle()
    test_mark_dirty_creates_flush_timer()
    test_mark_dirty_no_timer_without_loop()
    test_save_unicode_preserved()
    test_save_partial_state_with_defaults()
    print("\nPERSISTENCE TESTS OK")


if __name__ == "__main__":
    run()
