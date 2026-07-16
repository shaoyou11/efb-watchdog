import os
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw

import watchdog


class ScheduleTests(unittest.TestCase):
    def test_default_click_cooldown_matches_poll_interval(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(watchdog.click_cooldown_seconds(), 120)

    def test_daily_window(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(watchdog.schedule_active(datetime(2026, 7, 13, 3, 0)))
            self.assertFalse(watchdog.schedule_active(datetime(2026, 7, 13, 4, 0)))

    def test_one_time_test_window(self):
        environment = {
            "TEST_DATE": "2026-07-12",
            "TEST_START": "22:50",
            "TEST_END": "23:10",
        }
        with patch.dict(os.environ, environment, clear=True):
            self.assertTrue(watchdog.schedule_active(datetime(2026, 7, 12, 23, 0)))
            self.assertFalse(watchdog.schedule_active(datetime(2026, 7, 13, 23, 0)))


class ButtonDetectionTests(unittest.TestCase):
    def test_detects_confirmation_button(self):
        image = Image.new("RGB", (520, 380), "black")
        ImageDraw.Draw(image).rectangle((246, 270, 308, 296), fill=(7, 193, 96))

        self.assertEqual(watchdog.find_confirmation_button(image), (277, 283))

    def test_detects_expected_green_button(self):
        image = Image.new("RGB", (1920, 1080), "black")
        ImageDraw.Draw(image).rectangle((870, 678, 1049, 713), fill=(7, 193, 96))

        self.assertEqual(watchdog.find_enter_button(image), (959, 695))

    def test_ignores_small_green_icon(self):
        image = Image.new("RGB", (1920, 1080), "black")
        ImageDraw.Draw(image).rectangle((20, 20, 50, 50), fill=(7, 193, 96))

        self.assertIsNone(watchdog.find_enter_button(image))


class RecoveryTests(unittest.TestCase):
    def test_pauses_and_alerts_once_after_three_failures(self):
        tracker = watchdog.FailureTracker(limit=3)

        self.assertFalse(tracker.record_failure())
        self.assertFalse(tracker.record_failure())
        self.assertTrue(tracker.record_failure())
        self.assertTrue(tracker.paused)
        self.assertFalse(tracker.record_failure())

    def test_success_resets_failure_state(self):
        tracker = watchdog.FailureTracker(limit=3)
        tracker.record_failure()
        tracker.record_failure()

        tracker.reset()

        self.assertEqual(tracker.failures, 0)
        self.assertFalse(tracker.paused)

    @patch("watchdog.requests.post")
    def test_sends_alert_through_local_bot_api(self, post):
        post.return_value = Mock(raise_for_status=Mock())
        environment = {
            "TELEGRAM_BOT_TOKEN": "secret",
            "TELEGRAM_CHAT_ID": "123",
            "TELEGRAM_BOT_API": "http://127.0.0.1:8081",
        }
        with patch.dict(os.environ, environment, clear=True):
            watchdog.send_alert("恢复失败")

        self.assertEqual(post.call_count, 1)
        self.assertNotIn("secret", post.call_args.kwargs["json"]["text"])

    def test_heartbeat_file_is_updated(self):
        with TemporaryDirectory() as directory:
            target = Path(directory) / "heartbeat"
            with patch.dict(os.environ, {"HEARTBEAT_PATH": str(target)}, clear=True):
                watchdog.touch_heartbeat()

            self.assertTrue(target.exists())


if __name__ == "__main__":
    unittest.main()
