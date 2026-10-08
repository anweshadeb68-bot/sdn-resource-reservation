"""Booking table for the reservation server (Member A, Day 3).

Keeps every active booking, decides whether a new one fits, and makes sure
two clients can never take the same last Mbps at the same moment.

The server (server.py) calls only these methods:
    table.reserve(src, dst, mbps, seconds, owner_ip) -> (True, id) or (False, reason)
    table.release(booking_id, owner_ip)              -> "released" | "not_found" | "forbidden"
    table.list_active()                              -> list of (id, src, dst, mbps, seconds_left)
    table.status()                                   -> dict for the Moon link (s1-s2)
"""

import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common import config  # noqa: E402

BOTTLENECK = "s1-s2"   # the Moon-Earth link in the storyline


class Booking:
    """One reservation. A plain record: no logic."""

    def __init__(self, booking_id, src, dst, mbps, seconds, owner_ip):
        self.id = booking_id
        self.src = src
        self.dst = dst
        self.mbps = mbps
        self.seconds = seconds
        self.owner_ip = owner_ip
        # time.monotonic() is a clock that only moves forward, even if someone
        # changes the computer's date. Right for measuring "how long until...".
        self.expires_at = time.monotonic() + seconds
        self.links = path_links(src, dst)

    def seconds_left(self):
        return max(0, int(round(self.expires_at - time.monotonic())))

    def is_active(self):
        return time.monotonic() < self.expires_at


def path_links(src, dst):
    """The links a booking from src to dst uses, e.g. h1 -> h4:
    ["h1-s1", "s1-s2", "s2-h4"].

    Each host has exactly one link in config.LINKS (like "h1-s1" or "s2-h4"),
    which also tells us which switch it hangs off."""
    def host_link(host):
        for name in config.LINKS:
            if host in name.split("-"):
                return name
        raise ValueError("no link for host " + host)

    def switch_of(link, host):
        a, b = link.split("-")
        return b if a == host else a

    src_link, dst_link = host_link(src), host_link(dst)
    links = [src_link]
    if switch_of(src_link, src) != switch_of(dst_link, dst):
        links.append(BOTTLENECK)     # different switches: crosses the Moon link
    links.append(dst_link)
    return links


class BookingTable:
    def __init__(self, enforce=None):
        """enforce: a function called with a new Booking BEFORE it is saved.
        It returns True if the network accepted it (Day 4: the controller
        installs the rules). None means "always accept"."""
        self._enforce = enforce
        self._bookings = {}          # id -> Booking
        self._next_id = 1
        self._lock = threading.Lock()

    # ---- helpers -------------------------------------------------------

    def _active(self):
        """All bookings whose time has not run out yet."""
        return [b for b in self._bookings.values() if b.is_active()]

    def reservable(self, link):
        """How many Mbps of this link may be booked in total (80%)."""
        return config.LINKS[link] * config.RESERVABLE_SHARE

    def booked_on(self, link):
        """Total Mbps of active bookings that use this link."""
        # TODO 4 (Day 3): add up b.mbps for every active booking b whose
        #   b.links contains this link, and return the total.
        #   hint: loop over self._active(); "link in b.links" tests membership.
        #   WHY: this is the "how full is this road right now" number. Both the
        #   conflict check and STATUS depend on it.
        total = 0
        for b in self._active():
            if link in b.links:
                total += b.mbps
        return total

    # ---- the four operations -------------------------------------------

    def reserve(self, src, dst, mbps, seconds, owner_ip):
        """Try to book. Returns (True, booking_id) or (False, reason)."""
        # TODO 5 (Day 3): the conflict check -- the heart of the project.
        #   Do ALL of the following inside   with self._lock:
        #     a) if len(self._active()) >= config.MAX_ACTIVE:
        #            return (False, "max_reservations")
        #     b) build the booking:
        #            booking = Booking(self._next_id, src, dst, mbps, seconds, owner_ip)
        #     c) for every link in booking.links: if
        #            self.booked_on(link) + mbps > self.reservable(link)
        #        return (False, "insufficient_capacity")
        #     d) if self._enforce is not None and not self._enforce(booking):
        #            return (False, "controller_error")
        #     e) save it:  self._bookings[booking.id] = booking
        #        then:     self._next_id += 1
        #        and return (True, booking.id)
        #   WHY THE LOCK: between checking (c) and saving (e) there is a gap --
        #   on Day 4 it is a whole network call to the controller. Without the
        #   lock, two clients can both pass the check for the last free Mbps
        #   and BOTH get booked: the link is promised twice ("overbooking").
        #   The lock lets only one thread through this block at a time.
        #   TEST: tests/race_test.py, before and after.

        with self._lock:
            if len(self._active()) >= config.MAX_ACTIVE:
                return (False, "max_reservations")

            booking = Booking(self._next_id, src, dst, mbps, seconds, owner_ip)

            for link in booking.links:
                if self.booked_on(link) + mbps > self.reservable(link):
                    return (False, "insufficient_capacity")

            if self._enforce is not None and not self._enforce(booking):
                return (False, "controller_error")

            self._bookings[booking.id] = booking
            self._next_id += 1
            return (True, booking.id)


    def release(self, booking_id, owner_ip):
        """Cancel a booking early. Only the client that made it may do so."""
        # TODO 6 (Day 3): inside   with self._lock:
        #     - look up the booking:  booking = self._bookings.get(booking_id)
        #     - not there, or not booking.is_active()  -> return "not_found"
        #     - booking.owner_ip != owner_ip            -> return "forbidden"
        #     - otherwise delete it: del self._bookings[booking_id]
        #       and return "released"
        #   WHY: without the owner check, any base module could cancel the
        #   astronaut's booking in the middle of a moonwalk.
        with self._lock:
            booking = self._bookings.get(booking_id)
            if booking is None or not booking.is_active():
                return "not_found"
            if booking.owner_ip != owner_ip:
                return "forbidden"
            del self._bookings[booking_id]
            return "released"

    def list_active(self):
        with self._lock:
            return [(b.id, b.src, b.dst, b.mbps, b.seconds_left())
                    for b in sorted(self._active(), key=lambda b: b.id)]

    def status(self):
        with self._lock:
            reserved = self.booked_on(BOTTLENECK)
            return {
                "capacity": config.LINKS[BOTTLENECK],
                "reserved": reserved,
                "free": self.reservable(BOTTLENECK) - reserved,
                "active": len(self._active()),
            }
