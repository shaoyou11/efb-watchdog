FROM python:3.11-slim AS runtime

WORKDIR /app

RUN pip install --no-cache-dir Pillow requests vncdotool

COPY watchdog.py /app/watchdog.py
COPY test_watchdog.py /app/test_watchdog.py

CMD ["python", "/app/watchdog.py"]
