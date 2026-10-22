"""Return success only when a Celery worker replies to inspect ping."""

import os


os.environ.setdefault("DJANGO_SETTINGS_MODULE", "plane.settings.local")

from plane.celery import app  # noqa: E402


replies = app.control.ping(timeout=1)
if not replies:
    raise SystemExit(1)

print("CELERY_PONG")
