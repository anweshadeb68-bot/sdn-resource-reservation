"""Controller for the Network Resource Reservation System (Member B).

Follows the Interface Agreement (version 2). Run it from the project folder,
with sudo, because it sets up queues with ovs-vsctl:

    sudo ~/ryu-env/bin/ryu-manager controller/reservation_ctrl.py

What it does
  * Talks OpenFlow 1.3 to the two switches (they connect to port 6653).
  * Installs the basic rules when a switch connects:
        priority 0   anything unknown goes to the controller (and is ignored)
        priority 5   address resolution messages are flooded (so hosts find each other)
        priority 10  normal forwarding by destination address, in queue 0
  * Creates 9 queues (0 to 8) on the two ends of the slow link (s1-eth4 and s2-eth1).
    Queue 0 is for everything unbooked. Queues 1 to 8 are one per booking.
  * Web interface on port 8080, used by the reservation server:
        GET    /health
        POST   /reservations          install a booking
        GET    /reservations          list the bookings it holds
        DELETE /reservations/<id>     remove a booking
  * A booking becomes a priority 100 rule for its source and destination pair,
    which puts that traffic into the booking's own queue, and sets the queue's
    guaranteed minimum speed to the booked megabits per second.
    If the same pair is booked twice, the two bookings share one rule and one
    queue (a switch cannot hold two rules with the same match and priority), and
    the queue's minimum speed is the sum of the two ("each booking adds up").
  * Capacity: bookings crossing the slow link may add up to 80 percent of it
    (8 of 10 megabits) in total, both directions added together, which is how the
    server counts too; one more is refused with 409.
  * Safety net: a booking disappears by itself after its duration plus the
    margin from common/config.py, even if the server never sends a DELETE.

  * Monitoring: every STATS_INTERVAL_S seconds it reads the transmitted-byte counters of
    s1 port 4 and s2 port 1 (the two ends of the slow link), turns them into megabits
    per second, answers GET /stats/links with them and appends a row to
    logs/link_usage.csv.

If a switch reconnects, bookings are forgotten. Restart the controller and
Mininet together.
"""
import csv
import json
import math
import os
import re
import subprocess
import sys
import time

PROJECT_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, PROJECT_ROOT)
from common import config  # noqa: E402

from ryu.app.wsgi import ControllerBase, WSGIApplication, route  # noqa: E402
from ryu.base import app_manager  # noqa: E402
from ryu.controller import ofp_event  # noqa: E402
from ryu.controller.handler import (  # noqa: E402
    CONFIG_DISPATCHER, DEAD_DISPATCHER, MAIN_DISPATCHER, set_ev_cls)
from ryu.lib import hub  # noqa: E402
from ryu.ofproto import ofproto_v1_3  # noqa: E402
from webob import Response  # noqa: E402

APP_NAME = "reservation_ctrl_app"

# Facts fixed by the agreement's topology (links added in a fixed order).
SWITCH_NAMES = {1: "s1", 2: "s2"}                       # switch number -> name
HOST_SWITCH = {"h1": 1, "h2": 1, "h3": 1, "h4": 2}      # which switch a host hangs off
HOST_PORT = {"h1": 1, "h2": 2, "h3": 3, "h4": 2}        # its port on that switch
LINK_PORT = {1: 4, 2: 1}                                # port towards the other switch
QOS_PORT_NAME = {1: "s1-eth4", 2: "s2-eth1"}            # the same ports, by interface name
NAME_OF = dict((address, name) for name, address in config.HOSTS.items())

PRIORITY_ARP = 5
ETH_IPV4 = 0x0800
ETH_ARP = 0x0806
BOTTLENECK_BITS = config.LINKS["s1-s2"] * 1000000
LOG_FILE = os.path.join(PROJECT_ROOT, "logs", "controller_events.csv")
USAGE_FILE = os.path.join(PROJECT_ROOT, "logs", "link_usage.csv")
BOTTLENECK_NAME = "s1-s2"


class ReservationController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
    _CONTEXTS = {"wsgi": WSGIApplication}

    def __init__(self, *args, **kwargs):
        super(ReservationController, self).__init__(*args, **kwargs)
        self.datapaths = {}      # switch number -> connection to that switch
        self.qos_ready = {}      # switch number -> True once its queues exist
        self.queue_uuids = {}    # (port name, queue number) -> database record id
        self.bookings = {}       # booking id -> details
        self.port_counter = {}   # switch number -> (time, bytes sent) at the last reading
        self.port_rate = {}      # switch number -> megabits per second leaving that end
        self.port_rate_time = {}  # switch number -> when that rate was measured
        self.lock = hub.Semaphore(1)
        kwargs["wsgi"].register(ReservationApi, {APP_NAME: self})
        hub.spawn(self._expiry_loop)
        hub.spawn(self._stats_loop)
        if os.geteuid() != 0:
            self.logger.warning("Not running as root: queue setup will fail. Start with sudo.")
        self.logger.info("Controller started. Waiting for switches s1 and s2.")

    # ------------------------------------------------------------------
    # Switch connection and basic rules
    # ------------------------------------------------------------------
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def _switch_features(self, ev):
        dp = ev.msg.datapath
        if dp.id not in SWITCH_NAMES:
            self.logger.warning("Ignoring switch with datapath id %s (expected 1 or 2)", dp.id)
            return
        self.datapaths[dp.id] = dp
        self.qos_ready[dp.id] = False
        self._forget_counter(dp.id)
        if self.bookings:
            self._forget_all_bookings("switch %s reconnected" % SWITCH_NAMES[dp.id])
        self._install_base_rules(dp)
        self._log_event("switch_connected", detail=SWITCH_NAMES[dp.id])
        hub.spawn(self._setup_qos, dp.id)

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def _state_change(self, ev):
        dp = ev.datapath
        if ev.state == DEAD_DISPATCHER and self.datapaths.get(dp.id) is dp:
            del self.datapaths[dp.id]
            self.qos_ready[dp.id] = False
            self._forget_counter(dp.id)
            self.logger.warning("Switch %s disconnected", SWITCH_NAMES.get(dp.id, dp.id))

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in(self, ev):
        # Anything that reaches the table-miss rule is ignored (dropped).
        self.logger.debug("Ignoring unmatched packet from switch %s", ev.msg.datapath.id)

    def _add_flow(self, dp, priority, match, actions, cookie=0, hard_timeout=0):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        instructions = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        dp.send_msg(parser.OFPFlowMod(datapath=dp, cookie=cookie, priority=priority,
                                      hard_timeout=hard_timeout, match=match,
                                      instructions=instructions))

    def _forward_port(self, dpid, host_name):
        if HOST_SWITCH[host_name] == dpid:
            return HOST_PORT[host_name]
        return LINK_PORT[dpid]

    def _output_actions(self, dp, port, queue):
        """Put the packet in a queue only when it leaves through the slow link."""
        parser = dp.ofproto_parser
        actions = []
        if port == LINK_PORT[dp.id]:
            actions.append(parser.OFPActionSetQueue(queue))
        actions.append(parser.OFPActionOutput(port))
        return actions

    def _install_base_rules(self, dp):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        # Clean slate: remove whatever the switch held before.
        dp.send_msg(parser.OFPFlowMod(datapath=dp, table_id=ofp.OFPTT_ALL,
                                      command=ofp.OFPFC_DELETE, out_port=ofp.OFPP_ANY,
                                      out_group=ofp.OFPG_ANY, match=parser.OFPMatch()))
        # Priority 0: unknown traffic goes to the controller.
        self._add_flow(dp, config.PRIORITY_MISS, parser.OFPMatch(),
                       [parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)])
        # Priority 5: address resolution is flooded (the topology has no loops).
        self._add_flow(dp, PRIORITY_ARP, parser.OFPMatch(eth_type=ETH_ARP),
                       [parser.OFPActionOutput(ofp.OFPP_FLOOD)])
        # Priority 10: normal forwarding by destination address, in queue 0.
        for name, address in config.HOSTS.items():
            port = self._forward_port(dp.id, name)
            match = parser.OFPMatch(eth_type=ETH_IPV4, ipv4_dst=address)
            self._add_flow(dp, config.PRIORITY_FORWARD, match,
                           self._output_actions(dp, port, 0))
        self.logger.info("Basic rules installed on %s", SWITCH_NAMES[dp.id])

    # ------------------------------------------------------------------
    # Queues (Open vSwitch, linux-htb)
    # ------------------------------------------------------------------
    def _ovs(self, *args):
        result = subprocess.run(["ovs-vsctl", "--timeout=5"] + list(args),
                                capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError("ovs-vsctl %s failed: %s"
                               % (" ".join(args), result.stderr.strip()))
        return result.stdout.strip()

    def _remove_old_queues(self, port_name):
        """Delete queue records left behind by earlier runs on this port.

        The QoS and Queue records live in the Open vSwitch database and are NOT
        removed when Mininet deletes the switches, so they pile up. We tag ours
        with the port name and remove the tagged ones before building new ones.
        """
        try:
            self._ovs("--if-exists", "clear", "Port", port_name, "qos")
            for table in ("QoS", "Queue"):
                found = self._ovs("--bare", "--columns=_uuid", "find", table,
                                  "external_ids:cn_port=%s" % port_name)
                for uuid in found.split():
                    self._ovs("--if-exists", "destroy", table, uuid)
        except (RuntimeError, OSError) as error:
            self.logger.warning("Could not remove old queue records on %s: %s", port_name, error)

    def _setup_qos(self, dpid):
        port_name = QOS_PORT_NAME[dpid]
        queue_numbers = range(0, config.MAX_ACTIVE + 1)
        for key in [k for k in self.queue_uuids if k[0] == port_name]:
            del self.queue_uuids[key]
        self._remove_old_queues(port_name)
        args = ["--", "set", "Port", port_name, "qos=@qos",
                "--", "--id=@qos", "create", "QoS", "type=linux-htb",
                "other-config:max-rate=%d" % BOTTLENECK_BITS,
                "external_ids:cn_port=%s" % port_name]
        args += ["queues:%d=@q%d" % (n, n) for n in queue_numbers]
        for n in queue_numbers:
            args += ["--", "--id=@q%d" % n, "create", "Queue",
                     "other-config:max-rate=%d" % BOTTLENECK_BITS,
                     "external_ids:cn_port=%s" % port_name,
                     "external_ids:cn_queue=%d" % n]
        for attempt in range(3):
            try:
                self._ovs(*args)
                self.qos_ready[dpid] = True
                self.logger.info("Queues 0 to %d created on %s", config.MAX_ACTIVE, port_name)
                self._log_event("queues_created", detail=port_name)
                return
            except (RuntimeError, OSError) as error:
                self.logger.error("Queue setup on %s failed (attempt %d): %s",
                                  port_name, attempt + 1, error)
                hub.sleep(1)
        self._log_event("queues_failed", detail=port_name)

    def _queue_uuid(self, port_name, number):
        """Find the queue record that is really attached to this port right now.

        We follow the live links, Port -> QoS -> queues, instead of searching
        the whole Queue table, because old records from earlier runs can still
        be in the table and carry the same labels.
        """
        key = (port_name, number)
        if key not in self.queue_uuids:
            qos = self._ovs("get", "Port", port_name, "qos")
            if not qos or qos == "[]":
                raise RuntimeError("no QoS attached to %s" % port_name)
            queues = self._ovs("get", "QoS", qos, "queues")
            for queue_number, uuid in re.findall(r"(\d+)=([^,\s}]+)", queues):
                self.queue_uuids[(port_name, int(queue_number))] = uuid
            if key not in self.queue_uuids:
                raise RuntimeError("queue %d on %s not found" % (number, port_name))
        return self.queue_uuids[key]

    def _set_queue_rate(self, port_name, number, bits_per_second):
        uuid = self._queue_uuid(port_name, number)
        self._ovs("set", "Queue", uuid, "other-config:min-rate=%d" % bits_per_second)

    def _clear_queue_rate(self, port_name, number):
        uuid = self._queue_uuid(port_name, number)
        self._ovs("remove", "Queue", uuid, "other-config", "min-rate")

    # ------------------------------------------------------------------
    # Bookings
    # ------------------------------------------------------------------
    def _ready(self):
        return all(dpid in self.datapaths and self.qos_ready.get(dpid)
                   for dpid in SWITCH_NAMES)

    def _free_queue(self):
        used = set(b["queue"] for b in self.bookings.values())
        for number in range(1, config.MAX_ACTIVE + 1):
            if number not in used:
                return number
        return None

    @staticmethod
    def _error(code, reason):
        return code, {"status": "error", "reason": reason}

    @staticmethod
    def _whole_number(value):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("not a whole number")
        return value

    def create_booking(self, payload):
        try:
            booking_id = self._whole_number(payload["id"])
            src = payload["src"]
            dst = payload["dst"]
            mbps = self._whole_number(payload["mbps"])
            duration = self._whole_number(payload["duration_s"])
        except (KeyError, TypeError, ValueError):
            return self._error(400, "body must contain whole numbers id, mbps, duration_s "
                                    "and text src, dst")
        if booking_id < 1:
            return self._error(400, "id must be 1 or more")
        if not (isinstance(src, str) and isinstance(dst, str)
                and src in NAME_OF and dst in NAME_OF):
            return self._error(400, "src and dst must be host addresses from the topology")
        if src == dst:
            return self._error(400, "src and dst must differ")
        if not config.MIN_MBPS <= mbps <= config.MAX_MBPS:
            return self._error(400, "mbps must be %d to %d" % (config.MIN_MBPS, config.MAX_MBPS))
        if not config.MIN_SECONDS <= duration <= config.MAX_SECONDS:
            return self._error(400, "duration_s must be %d to %d"
                               % (config.MIN_SECONDS, config.MAX_SECONDS))

        with self.lock:
            if not self._ready():
                return self._error(503, "switches or queues are not ready yet")
            if booking_id in self.bookings:
                return self._error(409, "booking id already in use")
            if len(self.bookings) >= config.MAX_ACTIVE:
                return self._error(409, "no free queue: %d bookings are already active"
                                   % config.MAX_ACTIVE)
            same_pair = [b for b in self.bookings.values()
                         if b["src"] == src and b["dst"] == dst]
            queue = same_pair[0]["queue"] if same_pair else self._free_queue()
            if queue is None:
                return self._error(409, "no free queue: %d bookings are already active"
                                   % config.MAX_ACTIVE)

            src_name, dst_name = NAME_OF[src], NAME_OF[dst]
            egress = None
            if HOST_SWITCH[src_name] != HOST_SWITCH[dst_name]:
                egress = HOST_SWITCH[src_name]      # the switch whose slow-link port is used
            if egress is not None:
                # Only part of the slow link may be booked (80 percent). Counted exactly
                # like the server does: every booking that crosses the link, in either
                # direction, shares the same pool.
                limit = int(config.LINKS["s1-s2"] * config.RESERVABLE_SHARE)
                already = sum(b["mbps"] for b in self.bookings.values()
                              if b["egress"] is not None)
                if already + mbps > limit:
                    return self._error(409, "not enough bandwidth: %d of %d Mbps already "
                                            "booked on the slow link, %d more requested"
                                       % (already, limit, mbps))
            now = time.time()
            booking = {"id": booking_id, "src": src, "dst": dst,
                       "src_name": src_name, "dst_name": dst_name,
                       "mbps": mbps, "duration_s": duration, "queue": queue,
                       "started": now,
                       "expires_at": now + duration + config.RULE_TIMEOUT_MARGIN_S,
                       "egress": egress}
            self.bookings[booking_id] = booking
            try:
                self._sync_pair(booking, now)
            except (RuntimeError, OSError) as error:
                self.logger.error("Queue update failed: %s", error)
                del self.bookings[booking_id]
                try:
                    self._sync_pair(booking, now)      # put the pair back as it was
                except (RuntimeError, OSError) as again:
                    self.logger.warning("Could not restore the pair after a failure: %s", again)
                return self._error(500, "queue setup failed: %s" % error)
            self._log_event("booking_installed", booking_id, queue,
                            "%s>%s %d Mbps %d s" % (src_name, dst_name, mbps, duration))
            self.logger.info("Booking %d installed: %s to %s, %d Mbps, queue %d",
                             booking_id, src_name, dst_name, mbps, queue)
            return 200, {"status": "installed", "id": booking_id}

    def delete_booking(self, booking_id):
        with self.lock:
            booking = self.bookings.get(booking_id)
            if booking is None:
                return self._error(404, "unknown booking id")
            self._release(booking, "booking_removed")
            return 200, {"status": "removed", "id": booking_id}

    def list_bookings(self):
        now = time.time()
        entries = []
        for booking in sorted(self.bookings.values(), key=lambda b: b["id"]):
            left = int(round(booking["started"] + booking["duration_s"] - now))
            entries.append({"id": booking["id"], "src": booking["src"],
                            "dst": booking["dst"], "mbps": booking["mbps"],
                            "duration_s": booking["duration_s"], "queue": booking["queue"],
                            "seconds_left": max(0, left)})
        return {"reservations": entries}

    def _sync_pair(self, ref, now):
        """Make the switches match the bookings that exist for this source/destination pair.

        All bookings of one pair share one rule and one queue, because a switch cannot
        hold two rules with the same match and priority (the second would replace the
        first). The queue's minimum speed is the sum of the pair's bookings and the rule
        lasts until the last of them ends. With no bookings left, the rule is removed
        and the queue is reset. `ref` is any booking of the pair (it may already be gone
        from self.bookings). Caller holds the lock.
        """
        members = [b for b in self.bookings.values()
                   if b["src"] == ref["src"] and b["dst"] == ref["dst"]]
        port_name = QOS_PORT_NAME[ref["egress"]] if ref["egress"] is not None else None
        if members:
            if port_name is not None:
                total = sum(b["mbps"] for b in members)
                self._set_queue_rate(port_name, ref["queue"], total * 1000000)
            longest = max(members, key=lambda b: b["expires_at"])
            seconds = max(1, int(math.ceil(longest["expires_at"] - now)))
            self._install_pair_flows(ref, longest["id"], seconds)
        else:
            self._remove_pair_flows(ref)
            if port_name is not None:
                self._clear_queue_rate(port_name, ref["queue"])

    def _install_pair_flows(self, ref, cookie, hard_timeout):
        for dpid, dp in self.datapaths.items():
            parser = dp.ofproto_parser
            port = self._forward_port(dpid, ref["dst_name"])
            match = parser.OFPMatch(eth_type=ETH_IPV4, ipv4_src=ref["src"],
                                    ipv4_dst=ref["dst"])
            self._add_flow(dp, config.PRIORITY_BOOKING, match,
                           self._output_actions(dp, port, ref["queue"]),
                           cookie=cookie, hard_timeout=hard_timeout)

    def _remove_pair_flows(self, ref):
        for dp in self.datapaths.values():
            ofp, parser = dp.ofproto, dp.ofproto_parser
            match = parser.OFPMatch(eth_type=ETH_IPV4, ipv4_src=ref["src"],
                                    ipv4_dst=ref["dst"])
            dp.send_msg(parser.OFPFlowMod(
                datapath=dp, table_id=ofp.OFPTT_ALL, command=ofp.OFPFC_DELETE_STRICT,
                priority=config.PRIORITY_BOOKING, out_port=ofp.OFPP_ANY,
                out_group=ofp.OFPG_ANY, match=match))

    def _release(self, booking, event):
        """Remove a booking, update its pair's rule and queue, free the queue if unused.

        Caller holds the lock."""
        del self.bookings[booking["id"]]
        try:
            self._sync_pair(booking, time.time())
        except (RuntimeError, OSError) as error:
            self.logger.warning("Could not update queue %d: %s", booking["queue"], error)
        self._log_event(event, booking["id"], booking["queue"])
        self.logger.info("Booking %d removed (%s)", booking["id"], event)

    def _forget_all_bookings(self, reason):
        self.logger.warning("Forgetting %d bookings: %s", len(self.bookings), reason)
        pairs = dict(((b["src"], b["dst"]), b) for b in self.bookings.values())
        for booking in pairs.values():
            self._remove_pair_flows(booking)
        self.bookings.clear()
        self._log_event("bookings_dropped", detail=reason)

    def _expire_due_bookings(self, now):
        with self.lock:
            for booking in [b for b in self.bookings.values() if now >= b["expires_at"]]:
                self._release(booking, "booking_expired")

    def _expiry_loop(self):
        while True:
            hub.sleep(1)
            self._expire_due_bookings(time.time())

    # ------------------------------------------------------------------
    # Monitoring: how much traffic is really on the slow link
    # ------------------------------------------------------------------
    def _forget_counter(self, dpid):
        self.port_counter.pop(dpid, None)
        self.port_rate.pop(dpid, None)
        self.port_rate_time.pop(dpid, None)

    def _stats_loop(self):
        """Ask both switches for their counters, wait, then write one usage row."""
        while True:
            self._request_port_stats()
            hub.sleep(config.STATS_INTERVAL_S)
            self._write_usage_row()

    def _request_port_stats(self):
        for dpid, dp in list(self.datapaths.items()):
            try:
                parser = dp.ofproto_parser
                dp.send_msg(parser.OFPPortStatsRequest(dp, 0, LINK_PORT[dpid]))
            except Exception as error:      # the loop must survive a bad connection
                self.logger.warning("Could not ask %s for port statistics: %s",
                                    SWITCH_NAMES[dpid], error)

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def _port_stats_reply(self, ev):
        dpid = ev.msg.datapath.id
        if dpid not in SWITCH_NAMES:
            return
        for stat in ev.msg.body:
            if stat.port_no == LINK_PORT[dpid]:
                self._record_counter(dpid, stat.tx_bytes, time.time())

    def _record_counter(self, dpid, sent_bytes, now):
        """Turn two readings of a port's sent-bytes counter into megabits per second."""
        previous = self.port_counter.get(dpid)
        self.port_counter[dpid] = (now, sent_bytes)
        if previous is None:
            return                                   # the first reading has nothing to compare with
        seconds = now - previous[0]
        moved = sent_bytes - previous[1]
        if seconds <= 0 or moved < 0:                # the counter restarted: skip this interval
            self.port_rate.pop(dpid, None)
            return
        self.port_rate[dpid] = moved * 8 / seconds / 1000000.0
        self.port_rate_time[dpid] = now

    def link_usage(self):
        """Latest figures for the slow link, or None if there is no fresh measurement."""
        now = time.time()
        for dpid in SWITCH_NAMES:
            if dpid not in self.port_rate:
                return None
            if now - self.port_rate_time.get(dpid, 0) > 3 * config.STATS_INTERVAL_S:
                return None
        s1_to_s2 = self.port_rate[1]
        s2_to_s1 = self.port_rate[2]
        reserved = sum(b["mbps"] for b in list(self.bookings.values())
                       if b["egress"] is not None)
        return {"link": BOTTLENECK_NAME,
                "capacity_mbps": config.LINKS["s1-s2"],
                "reserved_mbps": reserved,
                "used_mbps": round(s1_to_s2 + s2_to_s1, 2),
                "used_mbps_s1_to_s2": round(s1_to_s2, 2),
                "used_mbps_s2_to_s1": round(s2_to_s1, 2)}

    def link_statistics(self):
        usage = self.link_usage()
        if usage is None:
            return self._error(503, "no fresh measurement yet (needs both switches "
                                    "connected and two readings)")
        return 200, {"timestamp": time.time(), "links": [usage]}

    def _write_usage_row(self):
        usage = self.link_usage()
        if usage is None:
            return
        self._append_row(USAGE_FILE,
                         ["time", "link", "capacity_mbps", "reserved_mbps", "used_mbps"],
                         ["%.3f" % time.time(), usage["link"], usage["capacity_mbps"],
                          usage["reserved_mbps"], usage["used_mbps"]])

    def _append_row(self, path, header, row):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            new_file = not os.path.exists(path)
            with open(path, "a", newline="") as handle:
                writer = csv.writer(handle)
                if new_file:
                    writer.writerow(header)
                writer.writerow(row)
        except OSError as error:
            self.logger.warning("Could not write %s: %s", os.path.basename(path), error)

    def _log_event(self, event, booking_id="", queue="", detail=""):
        self._append_row(LOG_FILE, ["time", "event", "booking_id", "queue", "detail"],
                         ["%.3f" % time.time(), event, booking_id, queue, detail])


class ReservationApi(ControllerBase):
    """Web interface used by the reservation server (agreement: port 8080)."""

    def __init__(self, req, link, data, **kwargs):
        super(ReservationApi, self).__init__(req, link, data, **kwargs)
        self.app = data[APP_NAME]

    @staticmethod
    def _reply(code, body):
        return Response(status=code, content_type="application/json",
                        body=json.dumps(body).encode("utf-8"))

    @route("health", "/health", methods=["GET"])
    def health(self, req, **kwargs):
        return self._reply(200, {"status": "ok"})

    @route("create", "/reservations", methods=["POST"])
    def create(self, req, **kwargs):
        try:
            payload = json.loads(req.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return self._reply(400, {"status": "error", "reason": "body is not valid JSON"})
        if not isinstance(payload, dict):
            return self._reply(400, {"status": "error", "reason": "body must be a JSON object"})
        code, body = self.app.create_booking(payload)
        return self._reply(code, body)

    @route("list", "/reservations", methods=["GET"])
    def listing(self, req, **kwargs):
        return self._reply(200, self.app.list_bookings())

    @route("remove", "/reservations/{booking_id}", methods=["DELETE"])
    def remove(self, req, **kwargs):
        try:
            booking_id = int(kwargs["booking_id"])
        except (KeyError, ValueError):
            return self._reply(400, {"status": "error", "reason": "booking id must be a number"})
        code, body = self.app.delete_booking(booking_id)
        return self._reply(code, body)

    @route("stats", "/stats/links", methods=["GET"])
    def stats(self, req, **kwargs):
        code, body = self.app.link_statistics()
        return self._reply(code, body)
