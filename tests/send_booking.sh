#!/usr/bin/env bash
# Hand-made bookings for Member B's testing. It stands in for the reservation server.
# Host names (h1 to h4) are turned into addresses using common/config.py.
#
#   tests/send_booking.sh health
#   tests/send_booking.sh post <id> <source host> <destination host> <mbps> <seconds>
#   tests/send_booking.sh list
#   tests/send_booking.sh delete <id>
#   tests/send_booking.sh flows      show the rules on both switches
#   tests/send_booking.sh queues     show the queues on both ends of the slow link
#   tests/send_booking.sh stats      measured traffic on the slow link (GET /stats/links)
#
# Example:  tests/send_booking.sh post 1 h1 h4 5 60
#
# The controller is on this machine, so the default address is 127.0.0.1.
# Override with:  CONTROLLER_URL=http://other:8080 tests/send_booking.sh health

set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
URL="${CONTROLLER_URL:-http://127.0.0.1:8080}"

address_of() {
    python3 -I -c 'import sys; sys.path.insert(0, sys.argv[1]); from common import config; print(config.HOSTS[sys.argv[2]])' "$ROOT" "$1" 2>/dev/null \
        || { echo "Unknown host name: $1 (use h1, h2, h3 or h4)" >&2; return 1; }
}

usage() {
    sed -n '2,16p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 1
}

case "${1:-}" in
    health)
        curl -sS -w '\nHTTP status: %{http_code}\n' "$URL/health"
        ;;
    list)
        curl -sS -w '\nHTTP status: %{http_code}\n' "$URL/reservations"
        ;;
    post)
        [ $# -eq 6 ] || usage
        src="$(address_of "$3")" || exit 1
        dst="$(address_of "$4")" || exit 1
        curl -sS -X POST "$URL/reservations" \
            -H 'Content-Type: application/json' \
            -d "{\"id\": $2, \"src\": \"$src\", \"dst\": \"$dst\", \"mbps\": $5, \"duration_s\": $6}" \
            -w '\nHTTP status: %{http_code}\n'
        ;;
    stats)
        curl -sS -w '\nHTTP status: %{http_code}\n' "$URL/stats/links"
        ;;
    delete)
        [ $# -eq 2 ] || usage
        curl -sS -X DELETE "$URL/reservations/$2" -w '\nHTTP status: %{http_code}\n'
        ;;
    flows)
        for switch in s1 s2; do
            echo "== rules on $switch =="
            sudo ovs-ofctl -O OpenFlow13 dump-flows "$switch"
        done
        ;;
    queues)
        for port in s1-eth4 s2-eth1; do
            echo "== queues on $port =="
            sudo ovs-appctl qos/show "$port"
            sudo tc class show dev "$port"
        done
        ;;
    *)
        usage
        ;;
esac
