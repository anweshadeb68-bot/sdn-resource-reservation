"""Shared constants for the Network Resource Reservation System.

Both members import from this file. Never hard-code these values elsewhere.
Any change must be agreed by both members and bumps INTERFACE_VERSION.
"""

INTERFACE_VERSION = 1

HOSTS = {"h1": "10.0.0.1", "h2": "10.0.0.2", "h3": "10.0.0.3", "h4": "10.0.0.4"}
SERVER_HOST = "h4"
SERVER_PORT = 5000
CONTROLLER_URL = "http://192.168.100.1:8080"
CONTROLLER_TIMEOUT_S = 3

LINKS = {"h1-s1": 100, "h2-s1": 100, "h3-s1": 100, "s1-s2": 10, "s2-h4": 100}  # Mbps
RESERVABLE_SHARE = 0.8
MAX_ACTIVE = 8
MIN_MBPS, MAX_MBPS = 1, 8
MIN_SECONDS, MAX_SECONDS = 10, 600

STATS_INTERVAL_S = 2
RULE_TIMEOUT_MARGIN_S = 5
PRIORITY_BOOKING, PRIORITY_FORWARD, PRIORITY_MISS = 100, 10, 0
