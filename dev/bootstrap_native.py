"""Apply local-only bootstrap state after instance registration."""

import os


os.environ.setdefault("DJANGO_SETTINGS_MODULE", "plane.settings.local")

import django  # noqa: E402


django.setup()

from plane.license.models import Instance  # noqa: E402


instance = Instance.objects.first()
if instance is None:
    raise SystemExit("No registered instance found; telemetry cannot be disabled.")

if instance.is_telemetry_enabled:
    instance.is_telemetry_enabled = False
    instance.save(update_fields=["is_telemetry_enabled"])

print("Local instance telemetry is disabled.")
