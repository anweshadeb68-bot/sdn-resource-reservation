#!/usr/bin/env bash
# S11 helper: wait for the temporary D2 congestion rule and capture it the moment it appears.
#
# The priority-20 rule only exists while the controller is limiting a flood, so it is
# easy to miss. Start this FIRST, in a normal Ubuntu terminal, then (in Mininet) make a
# booking and start h3's UDP flood. It checks s1 twice a second, and as soon as the rule
# is there it prints the rule plus the controller's /stats/links reply, saves both to a
# file and stops. Take the screenshot of this terminal straight away.
#
#   bash experiments/watch_policy.sh                  # saves experiments/evidence/S11_priority20_rule_active.txt
#   bash experiments/watch_policy.sh my_file.txt      # or choose the file
#
# It only reads the switch and the controller; it changes nothing.

OUT="${1:-experiments/evidence/S11_priority20_rule_active.txt}"
URL="${CONTROLLER_URL:-http://127.0.0.1:8080}"
mkdir -p "$(dirname "$OUT")"
echo "Watching s1 for the priority-20 congestion rule (Ctrl+C to stop)..."
while true; do
    RULES="$(sudo ovs-ofctl -O OpenFlow13 dump-flows s1 2>/dev/null | grep 'priority=20')"
    if [ -n "$RULES" ]; then
        {
            echo "Captured $(date '+%Y-%m-%d %H:%M:%S')"
            echo
            echo "\$ sudo ovs-ofctl -O OpenFlow13 dump-flows s1 | grep priority=20"
            echo "$RULES"
            echo
            echo "\$ curl $URL/stats/links"
            curl -sS "$URL/stats/links" | python3 -m json.tool 2>/dev/null || curl -sS "$URL/stats/links"
        } | tee "$OUT"
        echo
        echo "Saved to $OUT. Take the screenshot now."
        exit 0
    fi
    sleep 0.5
done
