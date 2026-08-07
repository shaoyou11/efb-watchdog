import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw

import watchdog


class ScheduleTests(unittest.TestCase):
    def test_efb_event_is_processed_outside_daily_window(self):
        self.assertTrue(watchdog.check_due(True, False, False, 0, 120))

    def test_event_recovery_retries_outside_daily_window(self):
        self.assertTrue(watchdog.check_due(False, False, True, 120, 120))

    def test_periodic_poll_is_blocked_outside_daily_window(self):
        self.assertFalse(watchdog.check_due(False, False, False, 120, 120))

    def test_event_recovery_supersedes_night_recovery(self):
        self.assertEqual(
            watchdog.select_recovery_source("night", triggered=True, scheduled=True),
            "event",
        )

    def test_night_recovery_stops_when_daily_window_ends(self):
        self.assertIsNone(
            watchdog.select_recovery_source("night", triggered=False, scheduled=False)
        )

    def test_event_recovery_continues_outside_daily_window(self):
        self.assertEqual(
            watchdog.select_recovery_source("event", triggered=False, scheduled=False),
            "event",
        )

    def test_consumes_offline_trigger_once(self):
        watchdog.OFFLINE_EVENT.set()

        self.assertTrue(watchdog.consume_offline_trigger())
        self.assertFalse(watchdog.consume_offline_trigger())

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


class SettingsTests(unittest.TestCase):
    def test_defaults_enable_all_switches(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = watchdog.WatchdogSettings(os.path.join(directory, "settings.json"))

        self.assertEqual(
            settings.snapshot(),
            {"master_enabled": True, "event_enabled": True, "night_enabled": True},
        )

    def test_switches_are_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "settings.json")
            settings = watchdog.WatchdogSettings(path)
            settings.update("event", False)

            restored = watchdog.WatchdogSettings(path)

        self.assertFalse(restored.snapshot()["event_enabled"])

    def test_master_switch_blocks_all_checks(self):
        state = {"master_enabled": False, "event_enabled": True, "night_enabled": True}

        self.assertFalse(watchdog.effective_event_enabled(state))
        self.assertFalse(watchdog.effective_night_enabled(state))

    def test_independent_switches_apply_when_master_is_on(self):
        state = {"master_enabled": True, "event_enabled": False, "night_enabled": True}

        self.assertFalse(watchdog.effective_event_enabled(state))
        self.assertTrue(watchdog.effective_night_enabled(state))

    def test_status_snapshot_includes_recovery_runtime_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = watchdog.WatchdogSettings(os.path.join(directory, "settings.json"))
            environment = {
                "DAILY_START": "02:50",
                "DAILY_END": "03:50",
                "POLL_SECONDS": "120",
                "CLICK_COOLDOWN_SECONDS": "120",
                "MAX_RECOVERY_FAILURES": "3",
                "TZ": "Asia/Shanghai",
            }
            with patch.dict(os.environ, environment, clear=True):
                state = watchdog.status_snapshot(settings)

        self.assertTrue(state["master_enabled"])
        self.assertEqual(state["daily_start"], "02:50")
        self.assertEqual(state["daily_end"], "03:50")
        self.assertEqual(state["poll_seconds"], 120)
        self.assertEqual(state["click_cooldown_seconds"], 120)
        self.assertEqual(state["max_recovery_failures"], 3)
        self.assertEqual(state["timezone"], "Asia/Shanghai")


class ButtonDetectionTests(unittest.TestCase):
    def test_detects_confirmation_button(self):
        image = Image.new("RGB", (520, 380), "black")
        ImageDraw.Draw(image).rectangle((246, 270, 308, 296), fill=(7, 193, 96))

        self.assertEqual(watchdog.find_confirmation_button(image), (277, 283))

    def test_detects_expected_green_button(self):
        image = Image.new("RGB", (1920, 1080), "black")
        ImageDraw.Draw(image).rectangle((870, 678, 1049, 713), fill=(7, 193, 96))

        self.assertEqual(watchdog.find_enter_button(image), (959, 695))

    def test_detects_current_wechat_green_button_color(self):
        image = Image.new("RGB", (1920, 1200), "black")
        ImageDraw.Draw(image).rectangle((870, 678, 1049, 713), fill=(56, 205, 127))

        self.assertEqual(watchdog.find_enter_button(image), (959, 695))

    def test_ignores_small_green_icon(self):
        image = Image.new("RGB", (1920, 1080), "black")
        ImageDraw.Draw(image).rectangle((20, 20, 50, 50), fill=(7, 193, 96))

        self.assertIsNone(watchdog.find_enter_button(image))


class RecoveryTests(unittest.TestCase):
    def test_manual_and_automatic_success_events_are_not_duplicated(self):
        tracker = watchdog.LoginEventTracker()

        self.assertTrue(tracker.observe("logged_in", 100.0))
        self.assertFalse(tracker.observe("logged_in", 101.0))
        self.assertFalse(tracker.manual_success(102.0))
        tracker.observe("offline", 103.0)
        self.assertTrue(tracker.manual_success(104.0))
        self.assertFalse(tracker.manual_success(105.0))

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

    def test_new_event_rearms_paused_event_recovery(self):
        tracker = watchdog.FailureTracker(limit=1)
        tracker.record_failure()

        self.assertTrue(watchdog.rearm_for_new_event(tracker, triggered=True))
        self.assertEqual(tracker.failures, 0)
        self.assertFalse(tracker.paused)

    def test_periodic_retry_does_not_rearm_paused_event_recovery(self):
        tracker = watchdog.FailureTracker(limit=1)
        tracker.record_failure()

        self.assertFalse(watchdog.rearm_for_new_event(tracker, triggered=False))
        self.assertTrue(tracker.paused)

    def test_failed_recovery_rearms_after_timed_pause(self):
        tracker = watchdog.FailureTracker(limit=1, pause_seconds=120)

        self.assertTrue(tracker.record_failure(now=100.0))
        self.assertFalse(tracker.rearm_if_due(now=219.9))
        self.assertTrue(tracker.paused)
        self.assertTrue(tracker.rearm_if_due(now=220.0))
        self.assertFalse(tracker.paused)
        self.assertEqual(tracker.failures, 0)

    def test_new_night_window_rearms_paused_night_recovery(self):
        tracker = watchdog.FailureTracker(limit=1)
        tracker.record_failure()

        self.assertTrue(
            watchdog.rearm_for_new_night_window(
                tracker,
                scheduled=True,
                was_scheduled=False,
            )
        )
        self.assertFalse(tracker.paused)

    def test_same_night_window_does_not_rearm_paused_recovery(self):
        tracker = watchdog.FailureTracker(limit=1)
        tracker.record_failure()

        self.assertFalse(
            watchdog.rearm_for_new_night_window(
                tracker,
                scheduled=True,
                was_scheduled=True,
            )
        )
        self.assertTrue(tracker.paused)

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
        self.assertEqual(
            post.call_args.kwargs["json"]["reply_markup"]["inline_keyboard"][0][0]["callback_data"],
            "ops:diagnostic",
        )

    def test_heartbeat_file_is_updated(self):
        with TemporaryDirectory() as directory:
            target = Path(directory) / "heartbeat"
            with patch.dict(os.environ, {"HEARTBEAT_PATH": str(target)}, clear=True):
                watchdog.touch_heartbeat()
            self.assertTrue(target.exists())

    def test_recovery_success_marker_is_written(self):
        with TemporaryDirectory() as directory:
            target = Path(directory) / "recovery.json"
            with patch.dict(
                os.environ,
                {"RECOVERY_SUCCESS_PATH": str(target)},
                clear=True,
            ):
                watchdog.mark_recovery_success("event")

            payload = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(payload["version"], 1)
            self.assertEqual(payload["source"], "event")
            self.assertIsInstance(payload["created_at"], float)
            self.assertFalse(target.with_suffix(target.suffix + ".tmp").exists())

    @patch("watchdog.requests.post")
    def test_login_success_notification_is_separate_and_deduplicated_by_tracker(self, post):
        post.return_value = Mock(raise_for_status=Mock())
        environment = {
            "TELEGRAM_BOT_TOKEN": "token-for-test",
            "TELEGRAM_CHAT_ID": "123",
            "TELEGRAM_BOT_API": "http://127.0.0.1:8081",
        }
        with patch.dict(os.environ, environment, clear=True):
            watchdog.send_login_success("manual")
        self.assertEqual(post.call_count, 1)
        self.assertIn("登录成功", post.call_args.kwargs["json"]["text"])
        self.assertNotIn("diagnostic", post.call_args.kwargs["json"])

    def test_login_state_marker_is_persisted(self):
        with TemporaryDirectory() as directory:
            target = Path(directory) / "login-state.json"
            with patch.dict(os.environ, {"LOGIN_STATE_PATH": str(target)}, clear=True):
                watchdog.mark_login_event("manual")
            payload = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(payload["source"], "manual")
        self.assertEqual(payload["state"], "logged_in")


if __name__ == "__main__":
    unittest.main()
