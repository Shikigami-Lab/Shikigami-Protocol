import unittest
from datetime import datetime, timedelta
from unittest import mock

from src.context import sleep_window
from src.context.sleep_window import DEFAULT_SLEEP_WINDOW, SleepWindow, compute_sleep_window


def _daily(hours, n_days=30, start=datetime(2026, 8, 1)):
    """One message in each of the given hours, every day."""
    return [
        (start + timedelta(days=d, hours=h, minutes=17)).timestamp()
        for d in range(n_days)
        for h in hours
    ]


LATE_SLEEPER = list(range(10, 24)) + [0, 1, 2]   # awake from 10:00 until 02:59 the next day
OFFICE_WORKER = [7, 8] + list(range(18, 24)) + [0]   # messages 07:00-08:59 and 18:00-00:59


class ComputeSleepWindowTests(unittest.TestCase):
    def test_learns_late_sleeper_window(self):
        self.assertEqual(SleepWindow(3, 10, learned=True), compute_sleep_window(_daily(LATE_SLEEPER)))

    def test_window_can_wrap_midnight(self):
        window = compute_sleep_window(_daily(range(7, 23)))

        self.assertEqual(SleepWindow(23, 7, learned=True), window)
        for hour in (23, 0, 6):
            self.assertTrue(window.contains(hour), hour)
        for hour in (7, 12, 22):
            self.assertFalse(window.contains(hour), hour)

    def test_occasional_late_nights_do_not_break_the_window(self):
        timestamps = _daily(LATE_SLEEPER)
        # Up past 4 a.m. on two nights, 30 messages on one of them: counted by day,
        # that is still only two days awake at that hour.
        binge = datetime(2026, 8, 5, 4, 30).timestamp()
        timestamps += [binge + i for i in range(30)]
        timestamps.append(datetime(2026, 8, 12, 4, 10).timestamp())

        self.assertEqual(SleepWindow(3, 10, learned=True), compute_sleep_window(timestamps))

    def test_longer_daytime_quiet_stretch_is_not_learned_as_sleep(self):
        # Quiet 01:00-07:00 (6h) and 09:00-18:00 (9h): the longer run is office hours.
        window = compute_sleep_window(_daily(OFFICE_WORKER))

        self.assertEqual(SleepWindow(1, 7, learned=True), window)
        for hour in (9, 12, 17):
            self.assertFalse(window.contains(hour), hour)

    def test_falls_back_when_no_quiet_stretch_touches_the_night(self):
        awake = [h for h in range(24) if not 8 <= h < 16]   # quiet only 08:00-15:59
        self.assertEqual(DEFAULT_SLEEP_WINDOW, compute_sleep_window(_daily(awake)))

    def test_falls_back_with_too_few_days(self):
        self.assertEqual(DEFAULT_SLEEP_WINDOW, compute_sleep_window(_daily(LATE_SLEEPER, n_days=6)))

    def test_falls_back_when_there_is_no_quiet_stretch(self):
        self.assertEqual(DEFAULT_SLEEP_WINDOW, compute_sleep_window(_daily(range(24))))

    def test_falls_back_when_quiet_stretch_is_implausibly_long(self):
        # Only active 12:00-15:59 each day: 20 quiet hours means light use, not 20 hours of sleep.
        self.assertEqual(DEFAULT_SLEEP_WINDOW, compute_sleep_window(_daily(range(12, 16))))

    def test_default_window_matches_previous_hardcoded_hours(self):
        for hour in range(24):
            self.assertEqual(hour >= 23 or hour < 6, DEFAULT_SLEEP_WINDOW.contains(hour), hour)


class GetUserSleepWindowTests(unittest.TestCase):
    def setUp(self):
        sleep_window._cache.clear()
        self.addCleanup(sleep_window._cache.clear)

    def test_failed_scan_serves_cached_default_before_retrying(self):
        with mock.patch.object(sleep_window.glob, "glob", side_effect=OSError("unreadable")) as scan, \
                mock.patch.object(sleep_window.time, "time", return_value=1_000_000.0) as clock:
            self.assertEqual(DEFAULT_SLEEP_WINDOW, sleep_window.get_user_sleep_window("root"))

            clock.return_value += sleep_window.FAILURE_CACHE_TTL_SECONDS - 1
            self.assertEqual(DEFAULT_SLEEP_WINDOW, sleep_window.get_user_sleep_window("root"))
            self.assertEqual(1, scan.call_count)

            clock.return_value += 2
            self.assertEqual(DEFAULT_SLEEP_WINDOW, sleep_window.get_user_sleep_window("root"))
            self.assertEqual(2, scan.call_count)


if __name__ == "__main__":
    unittest.main()
