from datetime import UTC, datetime, timedelta

import pytest

from friday import watches as wt

TZ = "Asia/Seoul"
NOW = datetime(2026, 11, 17, 3, 0, tzinfo=UTC)  # 12:00 in Seoul


def test_interval_parsing_and_minimum():
    assert wt.parse_every("30m") == "30m" and wt.parse_every("2시간") == "120m" and wt.parse_every("1d") == "1440m"
    assert wt.parse_every("daily 9:00") == "daily 09:00" and wt.parse_every("매일 21:30") == "daily 21:30"
    for bad in ("10m", "5분", "soon", "daily 25:00"):
        with pytest.raises(ValueError):
            wt.parse_every(bad)
    assert wt.next_check("30m", NOW, TZ) == NOW + timedelta(minutes=30)
    assert wt.next_check("daily 09:00", NOW, TZ) == datetime(2026, 11, 18, 0, 0, tzinfo=UTC)  # tomorrow 09:00 KST
    assert wt.next_check("daily 13:00", NOW, TZ) == datetime(2026, 11, 17, 4, 0, tzinfo=UTC)  # later today


def test_notifies_only_when_triggered_with_a_new_fingerprint(store):
    wid = store.add_watch("이캠퍼스 새 공지", "check notices", "30m", NOW)
    verdict = 'Checked.\n{"triggered": %s, "summary": "%s", "fingerprint": "%s"}'
    quiet = wt.record_check(store, wid, verdict % ("false", "최신: 중간고사 안내", "n1"), "", NOW, TZ)
    assert quiet.notify is None and store.watch(wid)["next_check_at"] == "2026-11-17T03:30:00+00:00"
    step = wt.record_check(store, wid, verdict % ("true", "새 공지: 기말 안내", "n2"), "", NOW, TZ)
    assert step.notify == "🔔 이캠퍼스 새 공지\n새 공지: 기말 안내"
    assert wt.record_check(store, wid, verdict % ("true", "새 공지: 기말 안내", "n2"), "", NOW, TZ).notify is None
    assert wt.record_check(store, wid, verdict % ("true", "또 새 공지", "n3"), "", NOW, TZ).notify


def test_three_failures_in_a_row_pause_the_watch_once(store):
    wid = store.add_watch("콘서트 티켓", "check seats", "15m", NOW)
    first = wt.record_check(store, wid, "", "codex exited 1", NOW, TZ)
    assert first.note is None and store.watch(wid)["status"] == "active"
    assert store.watch(wid)["next_check_at"] == "2026-11-17T03:30:00+00:00"  # backed off past the 15m interval
    assert wt.record_check(store, wid, "no json here", "", NOW, TZ).note is None
    third = wt.record_check(store, wid, '{"triggered": false, "error": "site down"}', "", NOW, TZ)
    assert "3 failed checks in a row" in third.note and "site down" in third.note
    assert store.watch(wid)["status"] == "paused"
    assert wt.record_check(store, wid, "", "again", NOW, TZ) == wt.Step()  # paused: no second message
