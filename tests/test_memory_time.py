from datetime import datetime
import unittest

from src.utils.memory_time import has_floating_date, read_time_notice, source_time, write_time_context


class MemoryTimeTests(unittest.TestCase):
    def test_common_floating_event_dates_are_detected(self):
        for text in ('下个礼拜二面试', '下一个星期二面试', '下周二见', '本周五', '明天吃饭',
                     '下个月搬家', '今年毕业', 'next Tuesday', 'tomorrow', 'last week'):
            with self.subTest(text=text):
                self.assertTrue(has_floating_date(text))
        for text in ('2026-11-10（周二）面试', '2026年11月10日面试', '最近准备面试', '每周二跑步'):
            self.assertFalse(has_floating_date(text))

    def test_source_date_is_fixed_and_invalid_time_is_not_replaced_by_today(self):
        stamp = datetime(2026, 11, 3, 12).timestamp()
        self.assertEqual(datetime.fromisoformat(source_time(stamp)).timestamp(), stamp)
        for value in (None, True, '2026-11-03', float('nan'), float('inf')):
            self.assertEqual(source_time(value), 'unknown')
        for locale in ('zh', 'en'):
            text = write_time_context([{'timestamp': stamp}, {}], locale)
            self.assertIn('2026-11-03', text)
            self.assertIn('2: unknown', text)
            self.assertIn('YYYY-MM-DD', text)
            self.assertTrue(read_time_notice(locale))
