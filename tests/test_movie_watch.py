from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import movie_watch as watch


TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 13, 10, 0, tzinfo=TZ)


def session(seq: str, date: str, time: str, *, hall: str = "1号厅") -> dict[str, str]:
    item = {
        "cinema_id": "37534",
        "cinema_name": "MOViE MOViE 影城（前滩太古里店）",
        "movie_name": "奥德赛",
        "date": date,
        "time": time,
        "hall": hall,
        "language": "英语",
        "format": "2D",
        "seq": seq,
    }
    item["fingerprint"] = watch.stable_fingerprint("37534", "奥德赛", item)
    return item


def result(*items: dict[str, str]) -> watch.FetchResult:
    return watch.FetchResult("MOViE MOViE 影城（前滩太古里店）", list(items))


class Recorder:
    def __init__(self, fail: bool = False) -> None:
        self.messages: list[str] = []
        self.fail = fail

    def __call__(self, message: str, stats: dict[str, int]) -> None:
        self.messages.append(message)
        if self.fail:
            raise watch.NotificationError("离线模拟明确失败")


class MovieWatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = self.root / "config.json"
        self.state = self.root / "state.json"
        self.config.write_text(json.dumps({
            "api_url": "https://example.invalid/source",
            "cinema_id": "37534",
            "cinema_name_must_contain": "MOViE MOViE",
            "movie_name": "奥德赛",
            "movie_aliases": [],
            "timeout_seconds": 1,
        }, ensure_ascii=False), encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_with(self, fetch_result: watch.FetchResult, recorder: Recorder, **kwargs) -> int:
        return watch.run_monitor(
            self.config, self.state, current=NOW,
            fetcher=lambda _config, _current: fetch_result,
            notifier=recorder, **kwargs,
        )

    def test_01_first_start_with_sessions_notifies_once(self) -> None:
        recorder = Recorder()
        item = session("A", "2026-08-14", "12:00")
        self.assertEqual(self.run_with(result(item), recorder), 0)
        self.assertEqual(len(recorder.messages), 1)
        state = watch.load_state(self.state)
        self.assertIn(item["fingerprint"], state["successfully_notified_fingerprints"])

    def test_02_empty_then_present_notifies(self) -> None:
        recorder = Recorder()
        self.assertEqual(self.run_with(result(), recorder), 0)
        self.assertEqual(recorder.messages, [])
        self.assertEqual(self.run_with(result(session("A", "2026-08-14", "12:00")), recorder), 0)
        self.assertEqual(len(recorder.messages), 1)

    def test_03_multiple_future_dates_are_monitored(self) -> None:
        recorder = Recorder()
        self.run_with(result(
            session("A", "2026-08-14", "12:00"),
            session("B", "2026-08-20", "19:30"),
        ), recorder)
        self.assertIn("2026-08-14", recorder.messages[0])
        self.assertIn("2026-08-20", recorder.messages[0])

    def test_04_historical_dates_and_ended_today_are_ignored(self) -> None:
        data = {
            "cinemaData": {"nm": "MOViE MOViE 影城（前滩太古里店）"},
            "showData": {"movies": [{"nm": "奥德赛", "shows": [
                {"showDate": "2026-08-12", "plist": [{"seqNo": "OLD", "tm": "20:00"}]},
                {"showDate": "2026-08-13", "plist": [
                    {"seqNo": "ENDED", "tm": "09:00"}, {"seqNo": "FUTURE", "tm": "11:00"}
                ]},
                {"showDate": "2026-08-14", "plist": [{"seqNo": "TOMORROW", "tm": "08:00"}]},
            ]}]},
        }
        extracted = watch.extract_future_sessions(data, json.loads(self.config.read_text(encoding="utf-8")), NOW)
        self.assertEqual([x["seq"] for x in extracted.sessions], ["FUTURE", "TOMORROW"])

    def test_05_only_new_session_is_notified(self) -> None:
        recorder = Recorder()
        first = session("A", "2026-08-14", "12:00")
        second = session("B", "2026-08-14", "14:00")
        self.run_with(result(first), recorder)
        recorder.messages.clear()
        self.run_with(result(first, second), recorder)
        self.assertEqual(len(recorder.messages), 1)
        self.assertNotIn("12:00", recorder.messages[0])
        self.assertIn("14:00", recorder.messages[0])

    def test_06_reappearing_session_is_restored(self) -> None:
        recorder = Recorder()
        item = session("A", "2026-08-14", "12:00")
        self.run_with(result(item), recorder)
        self.run_with(result(), recorder)
        recorder.messages.clear()
        self.run_with(result(item), recorder)
        self.assertEqual(len(recorder.messages), 1)
        self.assertIn("恢复上架", recorder.messages[0])

    def test_07_restart_does_not_repeat_old_session(self) -> None:
        item = session("A", "2026-08-14", "12:00")
        self.run_with(result(item), Recorder())
        after_restart = Recorder()
        self.assertEqual(self.run_with(result(item), after_restart), 0)
        self.assertEqual(after_restart.messages, [])

    def test_08_multiple_changes_are_one_merged_message(self) -> None:
        recorder = Recorder()
        self.run_with(result(
            session("A", "2026-08-14", "12:00"),
            session("B", "2026-08-15", "18:00"),
            session("C", "2026-08-15", "20:00"),
        ), recorder)
        self.assertEqual(len(recorder.messages), 1)
        self.assertIn("12:00", recorder.messages[0])
        self.assertIn("18:00", recorder.messages[0])
        self.assertIn("20:00", recorder.messages[0])

    def test_09_http_418_persistently_halts_source(self) -> None:
        response = Mock(status_code=418, text="blocked")
        with patch("movie_watch.requests.get", return_value=response) as get:
            code = watch.run_monitor(self.config, self.state, current=NOW, notifier=Recorder())
        self.assertEqual(code, 2)
        self.assertEqual(get.call_count, 1)
        state = watch.load_state(self.state)
        self.assertTrue(state["source_halted"])
        self.assertIn("418", state["source_halt_reason"])

    def test_10_halted_state_never_calls_fetcher(self) -> None:
        state = watch.load_state(self.state)
        state.update({"source_halted": True, "source_halt_reason": "offline test"})
        watch.atomic_save_json(self.state, state)
        fetcher = Mock(side_effect=AssertionError("must not fetch"))
        code = watch.run_monitor(self.config, self.state, current=NOW, fetcher=fetcher, notifier=Recorder())
        self.assertEqual(code, 2)
        fetcher.assert_not_called()

    def test_11_cinema_name_mismatch_halts(self) -> None:
        bad_data = {"cinemaData": {"nm": "其他影城"}, "showData": {"movies": []}}
        def bad_fetch(config, current):
            return watch.extract_future_sessions(bad_data, config, current)
        code = watch.run_monitor(self.config, self.state, current=NOW, fetcher=bad_fetch, notifier=Recorder())
        self.assertEqual(code, 2)
        self.assertTrue(watch.load_state(self.state)["source_halted"])

    def test_12_dry_run_neither_sends_nor_changes_state(self) -> None:
        initial = watch.load_state(self.state)
        watch.atomic_save_json(self.state, initial)
        before = self.state.read_bytes()
        recorder = Recorder()
        code = self.run_with(result(session("A", "2026-08-14", "12:00")), recorder, dry_run=True)
        self.assertEqual(code, 0)
        self.assertEqual(recorder.messages, [])
        self.assertEqual(self.state.read_bytes(), before)

    def test_13_same_event_is_attempted_at_most_three_times(self) -> None:
        recorder = Recorder(fail=True)
        item = session("A", "2026-08-14", "12:00")
        for _ in range(3):
            self.assertEqual(self.run_with(result(item), recorder), 1)
        before = self.state.read_bytes()
        later = datetime(2026, 8, 13, 10, 5, tzinfo=TZ)
        self.assertEqual(watch.run_monitor(
            self.config, self.state, current=later,
            fetcher=lambda _config, _current: result(item), notifier=recorder,
        ), 1)
        self.assertEqual(len(recorder.messages), 3)
        self.assertEqual(self.state.read_bytes(), before)
        state = watch.load_state(self.state)
        event_id = next(iter(state["notification_attempts"]))
        self.assertEqual(state["notification_attempts"][event_id], 3)
        self.assertEqual(state["notification_events"][event_id]["status"], "exhausted")

    def test_14_workflow_serializes_concurrent_runs(self) -> None:
        workflow = Path(__file__).parents[1] / ".github" / "workflows" / "movie-watch.yml"
        text = workflow.read_text(encoding="utf-8")
        self.assertIn("group: movie-movie-odyssey-watch", text)
        self.assertIn("cancel-in-progress: false", text)
        self.assertIn("cron: '3/5 * * * *'", text)


if __name__ == "__main__":
    unittest.main()
