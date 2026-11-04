import csv
import json
import math
import tempfile
import unittest
from pathlib import Path

from textlab.core import Recorder, analyze, full_report, linguistic, pause_position, text_delta, validate_settings
from textlab.export import export_csv, export_html, export_json
from textlab.storage import Store
from textlab.runtime import InstanceLock


class Clock:
    def __init__(self):
        self.value = 100_000_000_000

    def __call__(self):
        return self.value

    def advance(self, milliseconds):
        self.value += int(milliseconds * 1e6)


def settings(**kwargs):
    return {"title": "Тестовый лист", "description": "", "mode": "continuous",
            "topics": ["Свободный текст"], "time_limit_s": 0, "target_characters": 0,
            "target_words": 0, "micro_pause_ms": 300, "macro_pause_ms": 2000, **kwargs}


class RecorderTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.rec = Recorder({"session_label": "test", "age": 25}, settings(), self.clock)
        self.rec.resume()

    def key(self, char="a", keycode=65, hold=50):
        self.rec.key_down(keycode, char, char, len(self.rec.texts[self.rec.topic]))
        self.rec.edit(self.rec.texts[self.rec.topic] + char)
        self.clock.advance(hold)
        self.rec.key_up(keycode, char)

    def report(self):
        return analyze(self.rec.snapshot())

    def test_hold_and_negative_flight_for_overlapping_keys(self):
        self.rec.key_down(65, "a", "a", 0)
        self.clock.advance(30)
        self.rec.key_down(66, "b", "b", 1)
        self.clock.advance(70)
        self.rec.key_up(65, "a")
        self.clock.advance(20)
        self.rec.key_up(66, "b")
        m = self.report()
        self.assertEqual(m["hold_samples_ms"], [100.0, 90.0])
        self.assertEqual(m["flight_samples_ms"], [-70.0])

    def test_autorepeat_is_one_hold_but_multiple_key_events(self):
        self.rec.key_down(65, "a", "a", 0)
        self.clock.advance(500)
        self.rec.key_down(65, "a", "a", 1)
        self.clock.advance(50)
        self.rec.key_up(65, "a")
        m = self.report()
        self.assertEqual(m["hold_samples_ms"], [550.0])
        self.assertEqual(m["key_down_count"], 2)
        self.assertEqual(m["flight"]["count"], 0)

    def test_pause_boundaries_and_positions(self):
        self.key("а")
        self.clock.advance(250)  # keydown-to-keydown = exactly 300 ms
        self.key("б")
        self.clock.advance(1950)  # exactly 2000 ms
        self.key(" ")
        self.clock.advance(300)
        self.key(",")
        m = self.report()
        self.assertEqual(m["micro_pause_count"], 2)
        self.assertEqual(m["macro_pause_count"], 1)
        self.assertEqual(m["pause_positions"], {"within_word": 1, "between_words": 1, "before_punctuation": 1})

    def test_pause_excludes_focus_loss_and_cancels_hold(self):
        self.rec.key_down(65, "a", "a", 0)
        self.clock.advance(100)
        self.rec.pause("focus_lost")
        self.clock.advance(10000)
        self.rec.resume()
        self.key("b")
        m = self.report()
        self.assertAlmostEqual(m["active_seconds"], .15)
        self.assertEqual(m["pause_seconds"], 0)
        self.assertEqual(m["unpaired_key_downs"], 1)
        self.assertEqual(m["hold"]["count"], 1)

    def test_modifiers_are_recorded_but_excluded_from_typing_summary(self):
        self.rec.key_down(16, "Shift_L", "", 0)
        self.clock.advance(20)
        self.rec.key_up(16, "Shift_L")
        m = self.report()
        self.assertEqual(m["hold"]["count"], 0)
        self.assertEqual(m["key_pairs"][0]["hold_ms"], 20)

    def test_paste_and_undo_do_not_inflate_speed(self):
        self.rec.edit("abc")
        self.clock.advance(1000)
        self.rec.edit("abc pasted text", "paste")
        self.rec.edit("abc", "undo")
        m = self.report()
        self.assertEqual(m["typed_characters"], 3)
        self.assertEqual(m["pasted_characters"], 12)
        self.assertEqual(m["cpm"], 180)
        self.assertEqual(m["deleted_characters"], 12)

    def test_selection_replacement_and_correction_cycles(self):
        self.rec.edit("hello world")
        self.rec.edit("hi world", selection=[0, 5])
        self.clock.advance(1000)
        self.rec.edit("hi worl")
        self.clock.advance(2100)
        self.rec.edit("hi wor")
        m = self.report()
        self.assertEqual(m["selection_deletions"], 1)
        self.assertEqual(m["correction_operations"], 3)
        self.assertEqual(m["correction_cycles"], 2)

    def test_thematic_breaks_do_not_create_pauses_or_words(self):
        self.rec = Recorder({}, settings(mode="topics", topics=["A", "B"]), self.clock)
        self.rec.resume()
        self.key("а")
        self.rec.switch_topic(1)
        self.clock.advance(5000)
        self.rec.resume()
        self.key("б")
        report = full_report(self.rec.snapshot())
        self.assertEqual(report["overall"]["characters"], 2)
        self.assertEqual(report["overall"]["words"], 2)
        self.assertEqual(report["overall"]["pause_seconds"], 0)
        self.assertAlmostEqual(sum(t["active_seconds"] for t in report["topics"]), report["overall"]["active_seconds"])
        self.assertEqual([t["typed_characters"] for t in report["topics"]], [1, 1])

    def test_limits_and_finished_duration_are_stable(self):
        self.rec.settings["target_characters"] = 3
        self.rec.edit("abc")
        self.assertEqual(self.rec.limit_reached(), "character_target")
        self.clock.advance(400)
        self.rec.finish()
        before = self.rec.snapshot()
        self.clock.advance(10000)
        after = self.rec.snapshot()
        self.assertEqual(before["elapsed_ns"], after["elapsed_ns"])
        self.assertEqual(before["events"], after["events"])

    def test_time_limit_counts_active_time_only(self):
        self.rec.settings["time_limit_s"] = 1
        self.clock.advance(500)
        self.rec.pause()
        self.clock.advance(2000)
        self.assertIsNone(self.rec.limit_reached())
        self.rec.resume()
        self.clock.advance(500)
        self.assertEqual(self.rec.limit_reached(), "time_limit")

    def test_word_limit(self):
        self.rec.settings["target_words"] = 2
        self.rec.edit("Первое второе")
        self.assertEqual(self.rec.limit_reached(), "word_target")

    def test_speed_bins_account_for_partial_last_bin(self):
        self.rec.edit("abcde")
        self.clock.advance(5500)
        self.rec.edit("abcdefghij")
        self.clock.advance(500)
        m = self.report()
        self.assertEqual([b["cpm"] for b in m["speed_series"]], [60, 300])
        self.assertEqual(m["cpm"], 100)

    def test_empty_session_is_finite(self):
        self.rec.finish()
        m = self.report()
        self.assertEqual(m["cpm"], 0)
        self.assertEqual(m["ttr"], 0)
        self.assertIsNone(m["hold"]["mean_ms"])
        json.dumps(m, allow_nan=False)

    def test_events_when_paused_are_ignored(self):
        self.rec.pause()
        count = len(self.rec.events)
        self.rec.key_down(65, "a", "a", 0)
        self.rec.key_up(65, "a")
        self.rec.edit("a")
        self.assertEqual(len(self.rec.events), count)
        self.assertEqual(self.rec.texts, [""])


class HelpersTest(unittest.TestCase):
    def test_russian_words_and_ttr(self):
        m = linguistic("Привет, мир! Привет снова.")
        self.assertEqual(m["words"], 4)
        self.assertEqual(m["sentences"], 2)
        self.assertEqual(m["ttr"], .75)
        self.assertEqual(m["mean_sentence_length"], 2)

    def test_unicode_and_hyphenated_words(self):
        m = linguistic("Кто-то 😊 идёт по-русски")
        self.assertEqual(m["words"], 3)
        self.assertEqual(m["characters"], 23)

    def test_delta_round_trip(self):
        for before, after in [("abc", "abXc"), ("aaaa", "aaa"), ("", "😊Ё"), ("abc", ""), ("cat dog", "cat fox"), ("same", "same")]:
            offset, removed, inserted = text_delta(before, after)
            self.assertEqual(before[:offset] + inserted + before[offset+len(removed):], after)

    def test_settings_validation(self):
        for changes in [{"time_limit_s": -1}, {"time_limit_s": math.nan}, {"topics": []}, {"micro_pause_ms": 2000}, {"macro_pause_ms": math.inf}, {"title": " "}]:
            with self.assertRaises(ValueError):
                validate_settings(settings(**changes))


class PersistenceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.clock = Clock()
        self.rec = Recorder({"session_label": "Тест", "notes": "<script>alert(1)</script>"}, settings(), self.clock)
        self.rec.resume()
        self.rec.edit("=HYPERLINK(\"x\")", "paste")
        self.clock.advance(1000)

    def tearDown(self):
        self.temp.cleanup()

    def test_incremental_storage_and_duplicate_batches(self):
        store = Store(self.root / "test.db")
        try:
            store.enqueue(self.rec.snapshot(False), self.rec.events[:])
            store.flush()
            self.rec.edit("Second")
            self.rec.finish()
            store.enqueue(self.rec.snapshot(False), self.rec.events[:])
            store.flush()
            loaded = store.load(self.rec.session_id)
            self.assertEqual(loaded, self.rec.snapshot())
            self.assertTrue(store.has_label("Тест"))
            self.assertEqual(len(store.list_sessions()), 1)
        finally:
            store.close()

    def test_recovery_marks_unfinished_session(self):
        path = self.root / "recover.db"
        store = Store(path)
        store.enqueue(self.rec.snapshot(False), self.rec.events[:])
        store.close()
        store = Store(path)
        try:
            recovered = store.load(self.rec.session_id)
            self.assertEqual(recovered["status"], "interrupted")
            self.assertEqual(recovered["texts"], self.rec.texts)
            self.assertEqual(recovered["events"], self.rec.events)
        finally:
            store.close()

    def test_protocol_round_trip(self):
        store = Store(self.root / "protocol.db")
        try:
            store.save_protocol(settings())
            self.assertEqual(store.protocols()["Тестовый лист"], settings())
        finally:
            store.close()

    def test_same_data_directory_is_exclusive_and_reusable(self):
        first = InstanceLock(self.root)
        try:
            with self.assertRaises(OSError):
                InstanceLock(self.root)
        finally:
            first.close()
        second = InstanceLock(self.root)
        second.close()

    def test_all_exports_preserve_unicode_and_escape_html(self):
        self.rec.finish()
        snapshot = self.rec.snapshot()
        export_json(snapshot, self.root / "data.json")
        export_csv(snapshot, self.root / "events.csv")
        export_html(snapshot, self.root / "report.html")
        loaded = json.loads((self.root / "data.json").read_text("utf-8"))
        self.assertEqual(loaded["events"], snapshot["events"])
        self.assertEqual(loaded["profile"]["session_label"], "Тест")
        html = (self.root / "report.html").read_text("utf-8")
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertEqual(html.count("<svg "), 2)
        with (self.root / "events.csv").open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle, delimiter=";"))
        edit = next(row for row in rows if row["type"] == "edit")
        self.assertTrue(edit["inserted"].startswith("'="))
        self.assertEqual(json.loads(edit["payload_json"])["inserted"], snapshot["texts"][0])


if __name__ == "__main__":
    unittest.main()
