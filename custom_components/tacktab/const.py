"""Constants for the Tacktab integration."""

from datetime import timedelta

DOMAIN = "tacktab"
DEFAULT_PORT = 7979
SCAN_INTERVAL = timedelta(seconds=10)

# Provider realtime transport is intentionally held out of the Assist/bootstrap
# checkpoint. Its controller is independently developed and enabled later.
REALTIME_ENABLED = False
