import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import standup
from standup import is_birthday, send_birthday


class IsBirthdayTest(unittest.TestCase):
    def test_matches_month_and_day_any_year(self):
        self.assertTrue(is_birthday(date(2026, 12, 27), "12-27"))   # Sunday
        self.assertTrue(is_birthday(date(2030, 12, 27), "12-27"))
        self.assertFalse(is_birthday(date(2026, 12, 26), "12-27"))

    def test_feb_29_falls_back_to_feb_28_in_common_years(self):
        self.assertTrue(is_birthday(date(2027, 2, 28), "02-29"))
        self.assertFalse(is_birthday(date(2028, 2, 28), "02-29"))
        self.assertTrue(is_birthday(date(2028, 2, 29), "02-29"))


class SendBirthdayTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.object(standup, "BIRTHDAY_STATE_PATH", Path(tmp.name) / "last_birthday")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cfg = {"BIRTHDAY": "12-27", "TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}

    @mock.patch.object(standup, "send_telegram")
    def test_sends_once_per_year(self, send):
        send_birthday(self.cfg, date(2026, 12, 27), dry_run=False)
        send_birthday(self.cfg, date(2026, 12, 27), dry_run=False)   # manual or catch-up run
        self.assertEqual(send.call_count, 1)
        send_birthday(self.cfg, date(2027, 12, 27), dry_run=False)
        self.assertEqual(send.call_count, 2)

    @mock.patch.object(standup, "send_telegram", side_effect=RuntimeError("network down"))
    def test_failed_send_is_retried_on_the_next_run(self, send):
        send_birthday(self.cfg, date(2026, 12, 27), dry_run=False)
        send_birthday(self.cfg, date(2026, 12, 27), dry_run=False)
        self.assertEqual(send.call_count, 2)

    @mock.patch.object(standup, "send_telegram")
    def test_custom_message_supports_line_breaks(self, send):
        send_birthday({**self.cfg, "BIRTHDAY_MESSAGE": "Hi\\nthere"}, date(2026, 12, 27), dry_run=False)
        self.assertEqual(send.call_args.args[2], "Hi\nthere")


if __name__ == "__main__":
    unittest.main()
