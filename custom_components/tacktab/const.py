"""Constants for the Tacktab integration."""

from datetime import timedelta

DOMAIN = "tacktab"
DEFAULT_PORT = 7979
SCAN_INTERVAL = timedelta(seconds=10)

# Commands remain idle until a paired, locally enabled tablet starts a session.
REALTIME_ENABLED = True
