import logging
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime, time as clock_time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from PIL import Image


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
LOGGER = logging.getLogger("wechat-session-watchdog")
OFFLINE_EVENT = threading.Event()
CONTROL_CHANGED = threading.Event()
SETTINGS = None
DEFAULT_SETTINGS = {
    "master_enabled": True,
    "event_enabled": True,
    "night_enabled": True,
}
RECOVERY_LABELS = {
    "event": "全天事件",
    "night": "凌晨自主检测",
}


class WatchdogSettings:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.state = DEFAULT_SETTINGS.copy()
        self._load()

    def _load(self):
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
            for key in DEFAULT_SETTINGS:
                if isinstance(stored.get(key), bool):
                    self.state[key] = stored[key]
        except FileNotFoundError:
            return
        except (OSError, ValueError) as error:
            LOGGER.warning("failed to load watchdog settings: %s", error)

    def snapshot(self):
        with self.lock:
            return self.state.copy()

    def update(self, setting, enabled):
        key = f"{setting}_enabled"
        if key not in DEFAULT_SETTINGS or not isinstance(enabled, bool):
            raise ValueError("invalid watchdog setting")
        with self.lock:
            self.state[key] = enabled
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(self.state, ensure_ascii=True, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self.path)
            return self.state.copy()


class FailureTracker:
    def __init__(self, limit=3):
        self.limit = limit
        self.failures = 0
        self.paused = False
        self.alerted = False

    def record_failure(self):
        if self.paused:
            return False
        self.failures += 1
        if self.failures < self.limit:
            return False
        self.paused = True
        if self.alerted:
            return False
        self.alerted = True
        return True

    def reset(self):
        self.failures = 0
        self.paused = False
        self.alerted = False


def rearm_for_new_event(tracker, triggered):
    if not triggered or not tracker.paused:
        return False
    tracker.reset()
    return True


def rearm_for_new_night_window(tracker, scheduled, was_scheduled):
    if not scheduled or was_scheduled or not tracker.paused:
        return False
    tracker.reset()
    return True


def effective_event_enabled(state):
    return state["master_enabled"] and state["event_enabled"]


def effective_night_enabled(state):
    return state["master_enabled"] and state["night_enabled"]


def parse_clock(value: str) -> clock_time:
    return clock_time.fromisoformat(value)


def in_window(now: datetime, start: clock_time, end: clock_time) -> bool:
    current = now.time().replace(tzinfo=None)
    return start <= current < end


def schedule_active(now: datetime) -> bool:
    if in_window(
        now,
        parse_clock(os.getenv("DAILY_START", "02:50")),
        parse_clock(os.getenv("DAILY_END", "03:50")),
    ):
        return True

    test_date = os.getenv("TEST_DATE")
    return bool(
        test_date
        and now.date().isoformat() == test_date
        and in_window(
            now,
            parse_clock(os.getenv("TEST_START", "22:50")),
            parse_clock(os.getenv("TEST_END", "23:10")),
        )
    )


def is_logged_in() -> bool:
    response = requests.post(
        os.getenv("WECHAT_LOGIN_URL", "http://127.0.0.1:18888/api/?type=0"),
        data="{}",
        timeout=10,
    )
    response.raise_for_status()
    return response.json().get("is_login", 0) == 1


def click_cooldown_seconds() -> int:
    return int(os.getenv("CLICK_COOLDOWN_SECONDS", "120"))


def consume_offline_trigger() -> bool:
    if not OFFLINE_EVENT.is_set():
        return False
    OFFLINE_EVENT.clear()
    LOGGER.info("offline event received from EFB")
    return True


def check_due(
    triggered,
    schedule_is_active,
    recovery_active,
    seconds_since_check,
    poll_seconds,
):
    return triggered or (
        (schedule_is_active or recovery_active)
        and seconds_since_check >= poll_seconds
    )


def select_recovery_source(current_source, triggered, scheduled):
    if triggered:
        return "event"
    if current_source == "event":
        return "event"
    if scheduled:
        return "night"
    return None


class TriggerHandler(BaseHTTPRequestHandler):
    def _send_json(self, status, content):
        body = json.dumps(content, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path != "/status":
            self.send_error(404)
            return
        self._send_json(200, SETTINGS.snapshot())

    def do_POST(self):
        if self.path == "/offline":
            OFFLINE_EVENT.set()
            self.send_response(204)
            self.end_headers()
            return
        if self.path != "/control":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            state = SETTINGS.update(payload.get("setting"), payload.get("enabled"))
        except (ValueError, OSError, json.JSONDecodeError):
            self._send_json(400, {"error": "invalid request"})
            return
        CONTROL_CHANGED.set()
        self._send_json(200, state)

    def log_message(self, _format, *_args):
        return


def start_trigger_server():
    port = int(os.getenv("TRIGGER_PORT", "18989"))
    server = ThreadingHTTPServer(("127.0.0.1", port), TriggerHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    LOGGER.info("local trigger server started on 127.0.0.1:%s", port)
    return server


def diagnostic_path() -> Path:
    return Path(os.getenv("DIAGNOSTIC_PATH", "/diagnostics/last-login-failure.png"))


def preserve_diagnostic(screenshot: Path) -> None:
    target = diagnostic_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(screenshot, target)
    LOGGER.info("latest failed-login diagnostic saved")


def clear_diagnostic() -> None:
    diagnostic_path().unlink(missing_ok=True)


def touch_heartbeat():
    heartbeat = Path(os.getenv("HEARTBEAT_PATH", "/tmp/watchdog-heartbeat"))
    heartbeat.parent.mkdir(parents=True, exist_ok=True)
    heartbeat.touch()


def send_alert(message):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        LOGGER.warning("telegram alert skipped: credentials are not configured")
        return
    api = os.getenv("TELEGRAM_BOT_API", "http://127.0.0.1:8081").rstrip("/")
    response = requests.post(
        f"{api}/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": message},
        timeout=10,
    )
    response.raise_for_status()


def find_enter_button(image: Image.Image):
    return find_green_button(image, 140, 260, 25, 70, 0.45, 0.80)


def find_confirmation_button(image: Image.Image):
    return find_green_button(image, 45, 110, 20, 50, 0.55, 0.85)


def select_click_target(image: Image.Image, allow_confirmation=True):
    if allow_confirmation:
        confirmation = find_confirmation_button(image)
        if confirmation:
            return "confirmation", confirmation
    enter = find_enter_button(image)
    if enter:
        return "enter", enter
    return None


def find_green_button(image, min_width, max_width, min_height, max_height, min_y, max_y):
    rgb = image.convert("RGB")
    rows = []
    for y in range(rgb.height):
        xs = []
        for x in range(rgb.width):
            red, green, blue = rgb.getpixel((x, y))
            if red < 40 and 160 < green < 220 and 60 < blue < 130:
                xs.append(x)
        if len(xs) >= min_width:
            rows.append((y, min(xs), max(xs)))

    if not rows:
        return None

    groups = []
    current = [rows[0]]
    for row in rows[1:]:
        if row[0] == current[-1][0] + 1:
            current.append(row)
        else:
            groups.append(current)
            current = [row]
    groups.append(current)

    for group in groups:
        left = min(row[1] for row in group)
        right = max(row[2] for row in group)
        top = group[0][0]
        bottom = group[-1][0]
        width = right - left + 1
        height = bottom - top + 1
        center_x = (left + right) // 2
        center_y = (top + bottom) // 2
        if (
            min_width <= width <= max_width
            and min_height <= height <= max_height
            and 0.35 * rgb.width <= center_x <= 0.65 * rgb.width
            and min_y * rgb.height <= center_y <= max_y * rgb.height
        ):
            return center_x, center_y
    return None


def vnc_command(*args):
    command = [
        "vncdo",
        "-s",
        os.getenv("VNC_SERVER", "127.0.0.1::5905"),
        "-p",
        os.environ["VNC_PASSWORD"],
        *map(str, args),
    ]
    subprocess.run(command, check=True, timeout=30)


def capture_and_click(allow_confirmation=True) -> bool:
    screenshot = Path(tempfile.gettempdir()) / "wechat-watchdog.png"
    try:
        vnc_command("capture", screenshot)
        with Image.open(screenshot) as image:
            target = select_click_target(image, allow_confirmation)

        if target and target[0] == "confirmation":
            confirmation = target[1]
            LOGGER.info("confirmation button detected, clicking once")
            vnc_command("move", confirmation[0], confirmation[1], "click", 1)
            time.sleep(3)
            vnc_command("capture", screenshot)
            with Image.open(screenshot) as image:
                target = select_click_target(image, allow_confirmation=False)

        if target and target[0] == "enter":
            enter = target[1]
            LOGGER.info("enter button detected, clicking once")
            vnc_command("move", enter[0], enter[1], "click", 1)
            return True

        preserve_diagnostic(screenshot)
        LOGGER.info("offline detected, actionable button not found")
        return False
    finally:
        screenshot.unlink(missing_ok=True)


def main():
    global SETTINGS
    timezone = ZoneInfo(os.getenv("TZ", "Asia/Shanghai"))
    poll_seconds = int(os.getenv("POLL_SECONDS", "120"))
    cooldown_seconds = click_cooldown_seconds()
    last_check = 0.0
    last_attempt = 0.0
    recovery_source = None
    was_scheduled = False
    failure_limit = int(os.getenv("MAX_RECOVERY_FAILURES", "3"))
    trackers = {
        "event": FailureTracker(failure_limit),
        "night": FailureTracker(failure_limit),
    }

    SETTINGS = WatchdogSettings(os.getenv("STATE_PATH", "/state/settings.json"))
    start_trigger_server()
    LOGGER.info("watchdog started")
    while True:
        touch_heartbeat()
        now = datetime.now(timezone)
        monotonic_now = time.monotonic()
        state = SETTINGS.snapshot()
        event_enabled = effective_event_enabled(state)
        night_enabled = effective_night_enabled(state)
        triggered = consume_offline_trigger() and event_enabled
        scheduled = schedule_active(now) and night_enabled
        if rearm_for_new_night_window(
            trackers["night"],
            scheduled,
            was_scheduled,
        ):
            LOGGER.info("new night window rearmed night recovery")
        was_scheduled = scheduled
        if recovery_source == "event" and not event_enabled:
            recovery_source = None
        if recovery_source == "night" and not night_enabled:
            recovery_source = None
        recovery_source = select_recovery_source(
            recovery_source,
            triggered,
            scheduled,
        )
        if not check_due(
            triggered,
            scheduled,
            recovery_source is not None,
            monotonic_now - last_check,
            poll_seconds,
        ):
            OFFLINE_EVENT.wait(timeout=5)
            continue
        last_check = monotonic_now

        try:
            if is_logged_in():
                LOGGER.info("wechat is logged in")
                last_attempt = 0.0
                recovery_source = None
                for tracker in trackers.values():
                    tracker.reset()
                clear_diagnostic()
                continue

            LOGGER.warning("wechat is offline")
            tracker = trackers[recovery_source]
            if (
                recovery_source == "event"
                and rearm_for_new_event(tracker, triggered)
            ):
                LOGGER.info(
                    "new offline event rearmed the full event recovery flow"
                )
            if tracker.paused:
                LOGGER.warning(
                    "%s recovery clicks paused after repeated failures",
                    recovery_source,
                )
                continue
            if last_attempt and monotonic_now - last_attempt < cooldown_seconds:
                LOGGER.info("click attempt is cooling down")
                continue
            restored = False
            if capture_and_click():
                last_attempt = monotonic_now
                time.sleep(30)
                restored = is_logged_in()
                LOGGER.info("login restored=%s", restored)
                if restored:
                    for item in trackers.values():
                        item.reset()
                    recovery_source = None
                    clear_diagnostic()
                else:
                    vnc_command("capture", diagnostic_path())
                    LOGGER.info("latest failed-login diagnostic saved")
            if (
                not restored
                and tracker.record_failure()
            ):
                send_alert(
                    f"EFB 微信{RECOVERY_LABELS[recovery_source]}恢复连续失败 3 次，"
                    "已暂停本类自动点击。"
                    "请查看 watchdog 最新诊断画面并人工确认登录状态。"
                )
        except Exception as error:
            LOGGER.warning("watchdog check failed: %s", error)


if __name__ == "__main__":
    main()
