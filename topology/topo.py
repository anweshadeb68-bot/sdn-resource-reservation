#!/usr/bin/env python3
"""Mininet topology for the Network Resource Reservation System (Member B).

Builds exactly what the Interface Agreement (version 2) describes:

    h1 --\
    h2 ---- s1 ==== s2 ---- h4        s1 to s2 is the slow link (10 megabits per second)
    h3 --/

  * Links are added in the agreement's order so the switch port numbers match:
        s1 ports 1 to 3 = h1, h2, h3     s1 port 4 = s2
        s2 port 1       = s1             s2 port 2 = h4
  * One extra "management cable" joins h4 to the main machine, outside the
    switches: h4-eth1 at 192.168.100.4 and the main machine at 192.168.100.1.
    The reservation server on h4 reaches the controller through it.
  * The controller runs on the main machine and speaks OpenFlow 1.3 on port 6653.

Run (start the controller first):
    sudo python3 topology/topo.py            opens the Mininet prompt
    sudo python3 topology/topo.py --test     runs a ping between all hosts, then exits

Optional switches for later:
    --mininet-limit      put Mininet's own speed limit on the s1-s2 link (this fights
                         with the queues, so bookings stop working; only for comparison)
    --delay-ms 100       add delay (to suggest Moon distance)
    --delay-link s2-h4   put that delay on the server link instead of s1-s2
"""
import argparse
import os
import subprocess
import sys
from functools import partial
from urllib.parse import urlparse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common import config  # noqa: E402

from mininet.cli import CLI  # noqa: E402
from mininet.link import TCLink  # noqa: E402
from mininet.log import info, setLogLevel  # noqa: E402
from mininet.net import Mininet  # noqa: E402
from mininet.node import OVSKernelSwitch, RemoteController  # noqa: E402

# Values the agreement fixes in words but that are not in common/config.py yet.
# Agree with Member A before moving them into the shared file (and bump the version).
CONTROLLER_ADDRESS = "127.0.0.1"   # the controller runs on the main machine
OPENFLOW_PORT = 6653               # switches connect to the controller here
MGMT_PREFIX_LENGTH = 24
MGMT_HOST_ADDRESS = "192.168.100.4"
MGMT_HOST_INTERFACE = "h4-eth1"
MGMT_MAIN_INTERFACE = "mgmt-h4"

LINK_ORDER = [("h1", "s1"), ("h2", "s1"), ("h3", "s1"), ("s1", "s2"), ("s2", "h4")]

# (switch, neighbour): port number the agreement expects
EXPECTED_PORTS = {
    ("s1", "h1"): 1, ("s1", "h2"): 2, ("s1", "h3"): 3, ("s1", "s2"): 4,
    ("s2", "s1"): 1, ("s2", "h4"): 2,
}


def run(command, check=True):
    result = subprocess.run(command, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError("command failed: %s\n%s" % (" ".join(command), result.stderr.strip()))
    return result


def build_network(args):
    net = Mininet(
        controller=None,
        switch=partial(OVSKernelSwitch, protocols="OpenFlow13"),
        link=TCLink,
        autoSetMacs=True,
    )
    net.addController("c0", controller=RemoteController,
                      ip=CONTROLLER_ADDRESS, port=OPENFLOW_PORT)

    nodes = {}
    for name in sorted(config.HOSTS):
        nodes[name] = net.addHost(name, ip="%s/24" % config.HOSTS[name])
    nodes["s1"] = net.addSwitch("s1", dpid="0000000000000001")
    nodes["s2"] = net.addSwitch("s2", dpid="0000000000000002")

    for first, second in LINK_ORDER:
        key = "%s-%s" % (first, second)
        options = {}
        if not (key == "s1-s2" and not args.mininet_limit):
            options["bw"] = config.LINKS[key]
        if args.delay_ms and key == args.delay_link:
            options["delay"] = "%dms" % args.delay_ms
        net.addLink(nodes[first], nodes[second], **options)
    return net


def check_ports(net):
    all_good = True
    for (switch_name, other_name), expected in EXPECTED_PORTS.items():
        switch = net.get(switch_name)
        local_interface = switch.connectionsTo(net.get(other_name))[0][0]
        actual = switch.ports[local_interface]
        if actual != expected:
            info("PORT MISMATCH: %s towards %s is port %d, agreement says %d\n"
                 % (switch_name, other_name, actual, expected))
            all_good = False
    info("Port numbers match the agreement: %s\n" % ("yes" if all_good else "NO"))
    return all_good


def add_management_link(net):
    """Extra cable from h4 to the main machine, outside the switches."""
    server = net.get(config.SERVER_HOST)
    main_address = urlparse(config.CONTROLLER_URL).hostname

    run(["ip", "link", "del", MGMT_MAIN_INTERFACE], check=False)  # leftover from a crashed run
    run(["ip", "link", "add", MGMT_MAIN_INTERFACE, "type", "veth",
         "peer", "name", MGMT_HOST_INTERFACE])
    run(["ip", "link", "set", MGMT_HOST_INTERFACE, "netns", str(server.pid)])
    run(["ip", "addr", "add", "%s/%d" % (main_address, MGMT_PREFIX_LENGTH),
         "dev", MGMT_MAIN_INTERFACE])
    run(["ip", "link", "set", MGMT_MAIN_INTERFACE, "up"])
    server.cmd("ip addr add %s/%d dev %s"
               % (MGMT_HOST_ADDRESS, MGMT_PREFIX_LENGTH, MGMT_HOST_INTERFACE))
    server.cmd("ip link set %s up" % MGMT_HOST_INTERFACE)

    reply = server.cmd("ping -c 1 -W 2 %s" % main_address)
    working = "1 received" in reply
    info("Management cable: %s can reach %s: %s\n"
         % (config.SERVER_HOST, main_address, "yes" if working else "NO"))
    return working


def main():
    parser = argparse.ArgumentParser(description="Reservation system topology")
    parser.add_argument("--test", action="store_true",
                        help="ping between all hosts, then exit")
    parser.add_argument("--mininet-limit", action="store_true",
                        help="use Mininet's own speed limit on the s1-s2 link instead of "
                             "leaving it to the queues (breaks bookings; comparison only)")
    parser.add_argument("--plain-bottleneck", action="store_true",
                        help="no longer needed, this is now the default (kept so old "
                             "commands still work)")
    parser.add_argument("--delay-ms", type=int, default=0,
                        help="delay in milliseconds (applied at both ends of the link)")
    parser.add_argument("--delay-link", choices=["s1-s2", "s2-h4"], default="s1-s2",
                        help="which link gets the delay")
    args = parser.parse_args()

    if os.geteuid() != 0:
        sys.exit("Run with sudo: sudo python3 topology/topo.py")

    setLogLevel("info")
    net = build_network(args)
    net.start()
    exit_code = 0
    try:
        if not check_ports(net):
            exit_code = 1
        if not add_management_link(net):
            exit_code = 1
        if args.test:
            loss = net.pingAll()
            if loss != 0:
                exit_code = 1
        else:
            CLI(net)
    finally:
        net.stop()
        run(["ip", "link", "del", MGMT_MAIN_INTERFACE], check=False)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
