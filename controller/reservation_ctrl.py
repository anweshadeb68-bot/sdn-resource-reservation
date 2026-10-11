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
  * Capacity: bookings crossing the slow link may add up to 80 percent of it
    (8 of 10 megabits) in each direction; one more is refused with 409.
  * Safety net: a booking disappears by itself after its duration plus the
    margin from common/config.py, even if the server never sends a DELETE.

Not part of the first deliverable yet: GET /stats/links (answers "not implemented").
If a switch reconnects, bookings are forgotten. Restart the controller and
Mininet together.
"""
import csv
import json
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


class ReservationController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
    _CONTEXTS = {"wsgi": WSGIApplication}

    def __init__(self, *args, **kwargs):
        super(ReservationController, self).__init__(*args, **kwargs)
        self.datapaths = {}      # switch number -> connection to that switch
        self.qos_ready = {}      # switch number -> True once its queues exist
        self.queue_uuids = {}    # (port name, queue number) -> database record id
        self.bookings = {}       # booking id -> details
        self.lock = hub.Semaphore(1)
        kwargs["wsgi"].register(ReservationApi, {APP_NAME: self})
        hub.spawn(self._expiry_loop)
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
            queue = self._free_queue()
            if queue is None:
                return self._error(409, "no free queue: %d bookings are already active"
                                   % config.MAX_ACTIVE)

            src_name, dst_name = NAME_OF[src], NAME_OF[dst]
            egress = None
            if HOST_SWITCH[src_name] != HOST_SWITCH[dst_name]:
                egress = HOST_SWITCH[src_name]      # the switch whose slow-link port is used
            if egress is not None:
                # Only part of the slow link may be booked (80 percent). Each direction
                # of the link has its own capacity, so count bookings per direction.
                limit = int(config.LINKS["s1-s2"] * config.RESERVABLE_SHARE)
                already = sum(b["mbps"] for b in self.bookings.values()
                              if b["egress"] == egress)
                if already + mbps > limit:
                    return self._error(409, "not enough bandwidth: %d of %d Mbps already "
                                            "booked in this direction, %d more requested"
                                       % (already, limit, mbps))
            try:
                if egress is not None:
                    self._set_queue_rate(QOS_PORT_NAME[egress], queue, mbps * 1000000)
            except (RuntimeError, OSError) as error:
                self.logger.error("Queue update failed: %s", error)
                return self._error(500, "queue setup failed: %s" % error)

            now = time.time()
            booking = {"id": booking_id, "src": src, "dst": dst,
                       "src_name": src_name, "dst_name": dst_name,
                       "mbps": mbps, "duration_s": duration, "queue": queue,
                       "started": now,
                       "expires_at": now + duration + config.RULE_TIMEOUT_MARGIN_S,
                       "egress": egress}
            self._install_booking_flows(booking)
            self.bookings[booking_id] = booking
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

    def _install_booking_flows(self, booking):
        for dpid, dp in self.datapaths.items():
            parser = dp.ofproto_parser
            port = self._forward_port(dpid, booking["dst_name"])
            match = parser.OFPMatch(eth_type=ETH_IPV4, ipv4_src=booking["src"],
                                    ipv4_dst=booking["dst"])
            self._add_flow(dp, config.PRIORITY_BOOKING, match,
                           self._output_actions(dp, port, booking["queue"]),
                           cookie=booking["id"],
                           hard_timeout=booking["duration_s"] + config.RULE_TIMEOUT_MARGIN_S)

    def _remove_booking_flows(self, booking_id):
        for dp in self.datapaths.values():
            ofp, parser = dp.ofproto, dp.ofproto_parser
            dp.send_msg(parser.OFPFlowMod(
                datapath=dp, cookie=booking_id, cookie_mask=0xFFFFFFFFFFFFFFFF,
                table_id=ofp.OFPTT_ALL, command=ofp.OFPFC_DELETE,
                out_port=ofp.OFPP_ANY, out_group=ofp.OFPG_ANY, match=parser.OFPMatch()))

    def _release(self, booking, event):
        """Remove a booking's rules, free its queue, forget it. Caller holds the lock."""
        self._remove_booking_flows(booking["id"])
        if booking["egress"] is not None:
            try:
                self._clear_queue_rate(QOS_PORT_NAME[booking["egress"]], booking["queue"])
            except (RuntimeError, OSError) as error:
                self.logger.warning("Could not reset queue %d: %s", booking["queue"], error)
        del self.bookings[booking["id"]]
        self._log_event(event, booking["id"], booking["queue"])
        self.logger.info("Booking %d removed (%s)", booking["id"], event)

    def _forget_all_bookings(self, reason):
        self.logger.warning("Forgetting %d bookings: %s", len(self.bookings), reason)
        for booking_id in list(self.bookings):
            self._remove_booking_flows(booking_id)
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

    def _log_event(self, event, booking_id="", queue="", detail=""):
        try:
            os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
            new_file = not os.path.exists(LOG_FILE)
            with open(LOG_FILE, "a", newline="") as handle:
                writer = csv.writer(handle)
                if new_file:
                    writer.writerow(["time", "event", "booking_id", "queue", "detail"])
                writer.writerow(["%.3f" % time.time(), event, booking_id, queue, detail])
        except OSError as error:
            self.logger.warning("Could not write the event log: %s", error)


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
        return self._reply(501, {"status": "error",
                                 "reason": "link statistics are not implemented yet"})
