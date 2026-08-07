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
    def __init__(self, limit=3, pause_seconds=120):
        self.limit = limit
        self.pause_seconds = max(0, pause_seconds)
        self.failures = 0
        self.paused = False
        self.paused_until = 0.0
        self.alerted = False

    def record_failure(self, now=None):
        if self.paused:
            return False
        self.failures += 1
        if self.failures < self.limit:
            return False
        self.paused = True
        current = time.monotonic() if now is None else float(now)
        self.paused_until = current + self.pause_seconds
        if self.alerted:
            return False
        self.alerted = True
        return True

    def rearm_if_due(self, now=None):
        if not self.paused:
            return False
        current = time.monotonic() if now is None else float(now)
        if current < self.paused_until:
            return False
        self.reset()
        return True

    def reset(self):
        self.failures = 0
        self.paused = False
        self.paused_until = 0.0
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


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def watchdog_runtime_config() -> dict:
    return {
        "daily_start": os.getenv("DAILY_START", "02:50"),
        "daily_end": os.getenv("DAILY_END", "03:50"),
        "poll_seconds": _env_int("POLL_SECONDS", 120),
        "click_cooldown_seconds": _env_int("CLICK_COOLDOWN_SECONDS", 120),
        "max_recovery_failures": _env_int("MAX_RECOVERY_FAILURES", 3),
        "timezone": os.getenv("TZ", "Asia/Shanghai"),
        "diagnostic_retention": "仅保留最新一张",
    }


def status_snapshot(settings=None) -> dict:
    source = settings or SETTINGS
    state = source.snapshot() if source is not None else DEFAULT_SETTINGS.copy()
    state.update(watchdog_runtime_config())
    state["login_event"] = login_event_snapshot()
    return state


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
        self._send_json(200, status_snapshot())

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


def recovery_success_path() -> Path:
    return Path(
        os.getenv(
            "RECOVERY_SUCCESS_PATH",
            "/state/auto-recovery-success.json",
        )
    )


def mark_recovery_success(source: str) -> None:
    target = recovery_success_path()
    temporary = target.with_suffix(target.suffix + ".tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps(
                {
                    "version": 1,
                    "source": source,
                    "created_at": time.time(),
                },
                ensure_ascii=True,
            )
            + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, target)
        LOGGER.info("recovery success marker written: %s", source)
    except OSError as error:
        LOGGER.warning("failed to write recovery success marker: %s", error)
    finally:
        temporary.unlink(missing_ok=True)


def login_state_path() -> Path:
    return Path(os.getenv("LOGIN_STATE_PATH", "/state/login-state.json"))


def mark_login_event(source: str) -> None:
    target = login_state_path()
    temporary = target.with_suffix(target.suffix + ".tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps(
                {
                    "version": 1,
                    "state": "logged_in",
                    "source": source,
                    "created_at": time.time(),
                },
                ensure_ascii=True,
            )
            + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, target)
        LOGGER.info("login event marker written: %s", source)
    except OSError as error:
        LOGGER.warning("failed to write login event marker: %s", error)
    finally:
        temporary.unlink(missing_ok=True)


def login_event_snapshot() -> dict:
    try:
        value = json.loads(login_state_path().read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}


class LoginEventTracker:
    """Debounce login transitions so probes do not repeat success alerts."""

    VALID_STATES = {
        "offline",
        "qr_present",
        "confirmation_present",
        "enter_present",
        "clicking",
        "logged_in",
        "unknown",
    }

    def __init__(self):
        self.state = "unknown"
        self.last_success_at = None
        self.last_success_kind = None

    def observe(self, state: str, now: float) -> bool:
        state = state if state in self.VALID_STATES else "unknown"
        previous = self.state
        self.state = state
        if state == "logged_in" and previous != "logged_in":
            self.last_success_at = float(now)
            self.last_success_kind = "observed"
            return True
        return False

    def _success(self, kind: str, now: float) -> bool:
        if self.state == "logged_in":
            return False
        self.state = "logged_in"
        self.last_success_at = float(now)
        self.last_success_kind = kind
        return True

    def manual_success(self, now: float) -> bool:
        return self._success("manual", now)

    def automatic_success(self, now: float) -> bool:
        return self._success("automatic", now)


def touch_heartbeat():
    heartbeat = Path(os.getenv("HEARTBEAT_PATH", "/tmp/watchdog-heartbeat"))
    heartbeat.parent.mkdir(parents=True, exist_ok=True)
    heartbeat.touch()


def _send_telegram_notice(message, reply_markup=None):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        LOGGER.warning("telegram alert skipped: credentials are not configured")
        return
    api = os.getenv("TELEGRAM_BOT_API", "http://127.0.0.1:8081").rstrip("/")
    payload = {"chat_id": chat_id, "text": message}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    response = requests.post(
        f"{api}/bot{token}/sendMessage",
        json=payload,
        timeout=10,
    )
    response.raise_for_status()


def send_alert(message):
    _send_telegram_notice(
        message,
        reply_markup={
            "inline_keyboard": [[
                {"text": "查看失败诊断", "callback_data": "ops:diagnostic"}
            ]]
        },
    )


def send_login_success(source: str):
    labels = {"manual": "手动登录", "automatic": "自动恢复"}
    _send_telegram_notice(f"EFB 微信登录成功（{labels.get(source, source)}）")


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
            # ComWeChat has used both the older dark green and the newer lighter
            # green button colors. Geometry checks below keep small icons out.
            if (
                red < 100
                and 150 < green < 235
                and 50 < blue < 180
                and green > red * 1.35
                and green > blue * 1.10
            ):
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
            for attempt in range(3):
                vnc_command("capture", screenshot)
                with Image.open(screenshot) as image:
                    target = select_click_target(image, allow_confirmation=False)
                if target:
                    break
                if attempt < 2:
                    time.sleep(1)

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
    login_tracker = LoginEventTracker()
    login_probe_initialized = False
    trackers = {
        "event": FailureTracker(failure_limit, cooldown_seconds),
        "night": FailureTracker(failure_limit, cooldown_seconds),
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
            logged_in = is_logged_in()
            if logged_in:
                LOGGER.info("wechat is logged in")
                last_attempt = 0.0
                if not login_probe_initialized:
                    login_tracker.state = "logged_in"
                    login_probe_initialized = True
                elif login_tracker.observe("logged_in", time.time()):
                    mark_login_event("manual")
                    send_login_success("manual")
                recovery_source = None
                for tracker in trackers.values():
                    tracker.reset()
                clear_diagnostic()
                continue

            login_probe_initialized = True
            login_tracker.observe("offline", time.time())
            LOGGER.warning("wechat is offline")
            tracker = trackers[recovery_source]
            if (
                recovery_source == "event"
                and rearm_for_new_event(tracker, triggered)
            ):
                LOGGER.info(
                    "new offline event rearmed the full event recovery flow"
                )
            if tracker.rearm_if_due(monotonic_now):
                LOGGER.info(
                    "%s recovery automatically rearmed after timed pause",
                    recovery_source,
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
                    recovered_source = recovery_source
                    if login_tracker.automatic_success(time.time()):
                        mark_login_event("automatic")
                        send_login_success("automatic")
                    for item in trackers.values():
                        item.reset()
                    mark_recovery_success(recovered_source)
                    recovery_source = None
                    clear_diagnostic()
                else:
                    vnc_command("capture", diagnostic_path())
                    LOGGER.info("latest failed-login diagnostic saved")
            if not restored and tracker.record_failure(now=monotonic_now):
                send_alert(
                    f"EFB 微信{RECOVERY_LABELS[recovery_source]}恢复连续失败 3 次，"
                    f"已暂停 {cooldown_seconds} 秒后自动重试。"
                    "请查看 watchdog 最新诊断画面并人工确认登录状态。"
                )
        except Exception as error:
            LOGGER.warning("watchdog check failed: %s", error)


if __name__ == "__main__":
    main()
