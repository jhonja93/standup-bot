"""Calendar rules for US + Ecuador. Run with: python3 -m unittest discover tests

These dates were checked against the official rules. If a `holidays` upgrade changes how
moved holidays are named, the "moved" cases fail instead of the bot silently skipping workdays.
"""

import unittest
from datetime import date, datetime

import holidays  # noqa: F401  (fail loudly if the package is missing)

from standup import Calendar, previous_workday_start


class CalendarTest(unittest.TestCase):
    def setUp(self):
        self.calendar = Calendar(["US", "EC"], extra_off=["2026-12-24"], extra_work=["2026-11-11"])

    def assertOff(self, day):
        self.assertIsNotNone(self.calendar.day_off(date.fromisoformat(day)), f"{day} should be a day off")

    def assertWorkday(self, day):
        reason = self.calendar.day_off(date.fromisoformat(day))
        self.assertIsNone(reason, f"{day} should be a workday, got: {reason}")

    def test_ecuador_moved_holiday_original_date_is_a_workday(self):
        self.assertOff("2027-08-09")      # Quito, Tue Aug 10 moved to Monday
        self.assertWorkday("2027-08-10")
        self.assertOff("2025-05-02")      # Labor Day, Thu May 1 moved to Friday
        self.assertWorkday("2025-05-01")
        self.assertOff("2026-05-25")      # Pichincha, Sun May 24 moved to Monday

    def test_ecuador_fixed_holidays(self):
        self.assertOff("2026-02-16")      # Carnival Monday
        self.assertOff("2026-02-17")      # Carnival Tuesday, never moved
        self.assertOff("2026-04-03")      # Good Friday
        self.assertOff("2026-11-02")      # All Souls' Day
        self.assertOff("2026-11-03")      # Independence of Cuenca

    def test_us_federal_holidays_with_observed_rule(self):
        self.assertOff("2026-11-26")      # Thanksgiving
        self.assertOff("2026-10-12")      # Columbus Day
        self.assertOff("2026-07-03")      # Jul 4 is a Saturday -> Friday (confirmed by the user)
        self.assertOff("2027-07-05")      # Jul 4 is a Sunday -> Monday

    def test_overrides_and_weekends(self):
        self.assertOff("2026-12-24")      # EXTRA_DAYS_OFF
        self.assertWorkday("2026-11-11")  # Veterans Day, but in EXTRA_WORKDAYS
        self.assertOff("2026-10-17")      # Saturday
        self.assertWorkday("2026-10-15")

    def test_first_run_window_skips_days_off(self):
        start = previous_workday_start(datetime(2026, 10, 13, 5), self.calendar)
        self.assertEqual(start.date(), date(2026, 10, 8))  # skips Fri 9 (EC) and Mon 12 (US)


if __name__ == "__main__":
    unittest.main()
