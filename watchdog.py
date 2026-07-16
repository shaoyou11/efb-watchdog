import logging
import os
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, time as clock_time
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from PIL import Image


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
LOGGER = logging.getLogger("wechat-session-watchdog")


class FailureTracker:
    def __init__(self, limit: int = 3):
        self.limit = limit
        self.failures = 0
        self.paused = False
        self.alerted = False

    def record_failure(self) -> bool:
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

    def reset(self) -> None:
        self.failures = 0
        self.paused = False
        self.alerted = False


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


def diagnostic_path() -> Path:
    return Path(os.getenv("DIAGNOSTIC_PATH", "/diagnostics/last-login-failure.png"))


def preserve_diagnostic(screenshot: Path) -> None:
    target = diagnostic_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(screenshot, target)
    LOGGER.info("latest failed-login diagnostic saved")


def clear_diagnostic() -> None:
    diagnostic_path().unlink(missing_ok=True)


def touch_heartbeat() -> None:
    heartbeat = Path(os.getenv("HEARTBEAT_PATH", "/tmp/watchdog-heartbeat"))
    heartbeat.parent.mkdir(parents=True, exist_ok=True)
    heartbeat.touch()


def send_alert(message: str) -> None:
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


def capture_and_click() -> bool:
    screenshot = Path(tempfile.gettempdir()) / "wechat-watchdog.png"
    try:
        vnc_command("capture", screenshot)
        with Image.open(screenshot) as image:
            confirmation = find_confirmation_button(image)
            enter = find_enter_button(image)

        if confirmation:
            LOGGER.info("confirmation button detected, clicking once")
            vnc_command("move", confirmation[0], confirmation[1], "click", 1)
            time.sleep(3)
            vnc_command("capture", screenshot)
            with Image.open(screenshot) as image:
                enter = find_enter_button(image)

        if enter:
            LOGGER.info("enter button detected, clicking once")
            vnc_command("move", enter[0], enter[1], "click", 1)
            return True

        preserve_diagnostic(screenshot)
        LOGGER.info("offline detected, actionable button not found")
        return False
    finally:
        screenshot.unlink(missing_ok=True)


def main():
    timezone = ZoneInfo(os.getenv("TZ", "Asia/Shanghai"))
    poll_seconds = int(os.getenv("POLL_SECONDS", "120"))
    cooldown_seconds = click_cooldown_seconds()
    last_check = 0.0
    last_attempt = 0.0
    tracker = FailureTracker(int(os.getenv("MAX_RECOVERY_FAILURES", "3")))

    LOGGER.info("watchdog started")
    while True:
        touch_heartbeat()
        now = datetime.now(timezone)
        if not schedule_active(now):
            time.sleep(20)
            continue

        monotonic_now = time.monotonic()
        if monotonic_now - last_check < poll_seconds:
            time.sleep(5)
            continue
        last_check = monotonic_now

        try:
            if is_logged_in():
                LOGGER.info("wechat is logged in")
                last_attempt = 0.0
                tracker.reset()
                clear_diagnostic()
                continue

            LOGGER.warning("wechat is offline")
            if tracker.paused:
                LOGGER.warning("automatic clicks paused after repeated failures")
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
                    tracker.reset()
                    clear_diagnostic()
                else:
                    vnc_command("capture", diagnostic_path())
                    LOGGER.info("latest failed-login diagnostic saved")
            if not restored and tracker.record_failure():
                send_alert(
                    "EFB 微信自动恢复连续失败 3 次，已暂停自动点击。"
                    "请查看 watchdog 最新诊断画面并人工确认登录状态。"
                )
        except Exception as error:
            LOGGER.warning("watchdog check failed: %s", error)


if __name__ == "__main__":
    main()
