import datetime as dt
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import checkin  # noqa: E402


class AutoScheduleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = {
            "checkin_time": "08:50",
            "checkout_time": "18:10",
            "auto_checkin_deadline": "08:59",
            "auto_window_minutes": 30,
        }

    def tpe(self, hour: int, minute: int) -> dt.datetime:
        return dt.datetime(2026, 6, 1, hour, minute, tzinfo=checkin.TZ_TAIPEI)

    def test_checkin_window_ends_before_nine(self) -> None:
        self.assertEqual(
            checkin.auto_label_for_now(self.config, {}, self.tpe(8, 50)),
            ("in", ""),
        )
        self.assertEqual(
            checkin.auto_label_for_now(self.config, {}, self.tpe(8, 59)),
            ("in", ""),
        )
        self.assertEqual(
            checkin.auto_label_for_now(self.config, {}, self.tpe(9, 0)),
            ("", ""),
        )

    def test_checkin_time_after_deadline_moves_window_before_deadline(self) -> None:
        config = dict(self.config, checkin_time="09:00")
        start, end = checkin.auto_windows(config, self.tpe(8, 0).date())["in"]
        self.assertEqual(start, self.tpe(8, 29))
        self.assertEqual(end, self.tpe(8, 59))
        self.assertEqual(checkin.auto_label_for_now(config, {}, self.tpe(8, 45)), ("in", ""))
        self.assertEqual(checkin.auto_label_for_now(config, {}, self.tpe(9, 0)), ("", ""))

    def test_checkout_window_uses_configured_minutes(self) -> None:
        self.assertEqual(
            checkin.auto_label_for_now(self.config, {}, self.tpe(18, 10)),
            ("out", ""),
        )
        self.assertEqual(
            checkin.auto_label_for_now(self.config, {}, self.tpe(18, 40)),
            ("out", ""),
        )
        self.assertEqual(
            checkin.auto_label_for_now(self.config, {}, self.tpe(18, 41)),
            ("", ""),
        )

    def test_done_state_prevents_duplicate_auto_punch(self) -> None:
        state = {}
        today = self.tpe(8, 50).date()
        checkin.mark_auto_done(state, today, "in", "success", self.tpe(8, 51))

        label, reason = checkin.auto_label_for_now(self.config, state, self.tpe(8, 55))

        self.assertEqual(label, "")
        self.assertIn("今天已由自動模式處理過", reason)


if __name__ == "__main__":
    unittest.main()
