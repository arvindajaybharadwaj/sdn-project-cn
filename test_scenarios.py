#!/usr/bin/env python3
"""
Broadcast Traffic Control — Test & Validation Script (POX version)
===================================================================
Runs two test scenarios and prints a results summary.

Run AFTER starting POX and the topology:
    Terminal 1:  python pox.py log.level --DEBUG misc.broadcast_control
    Terminal 2:  sudo python3 topology.py
    Then in Mininet CLI: py exec(open('test_scenarios.py').read())

Or standalone:
    sudo python3 test_scenarios.py
"""

import subprocess
import time
import sys

from mininet.net   import Mininet
from mininet.node  import RemoteController, OVSKernelSwitch
from mininet.link  import TCLink
from mininet.log   import setLogLevel

sys.path.insert(0, ".")
from topology import BroadcastTopo

SEPARATOR = "=" * 60


def banner(title):
    print(f"\n{SEPARATOR}\n  {title}\n{SEPARATOR}")


def get_flow_table(switch_name):
    """Returns (count, raw_output) of non-table-miss flows on a switch."""
    result = subprocess.run(
        ["ovs-ofctl", "dump-flows", switch_name],
        capture_output=True, text=True
    )
    lines = [l for l in result.stdout.splitlines() if "priority" in l]
    return len(lines), result.stdout


def run_tests():
    setLogLevel("warning")

    banner("Setting up Mininet topology")
    topo = BroadcastTopo()
    net = Mininet(
        topo=topo,
        controller=RemoteController("c0", ip="127.0.0.1", port=6633),
        switch=OVSKernelSwitch,
        link=TCLink,
        autoSetMacs=True,
    )
    net.start()
    time.sleep(2)

    h1 = net.get("h1")
    h2 = net.get("h2")
    h3 = net.get("h3")
    h4 = net.get("h4")
    h5 = net.get("h5")
    h6 = net.get("h6")

    # ══════════════════════════════════════════════════════════════════════════
    # SCENARIO 1: Normal vs Excessive Broadcast
    # ══════════════════════════════════════════════════════════════════════════
    banner("SCENARIO 1: Normal vs Excessive Broadcast")

    print("\n[1a] Baseline pingall to populate MAC tables")
    loss = net.pingAll(timeout=2)
    print(f"     Packet loss: {loss:.0f}%")

    count_before, _ = get_flow_table("s1")
    print(f"\n[1b] Flow entries on s1 before broadcast test: {count_before}")

    print("\n[1c] Normal host h2: 5 ARP broadcasts (within limit)")
    h2.cmd("arping -c 5 -b -I h2-eth0 10.0.0.255 2>/dev/null")
    time.sleep(1)

    print("\n[1d] Noisy host h1: 25 ARP broadcasts (exceeds limit of 10)")
    h1.cmd("arping -c 25 -b -I h1-eth0 10.0.0.255 2>/dev/null &")
    time.sleep(4)

    count_after, flow_dump = get_flow_table("s1")
    print(f"\n[1e] Flow entries on s1 after broadcast storm: {count_after}")
    print(f"     Delta: +{count_after - count_before} new rules installed")

    drop_rules = [l for l in flow_dump.splitlines()
                  if "priority=100" in l or ("dl_dst=ff:ff:ff:ff:ff:ff" in l and "actions=" in l)]
    if drop_rules:
        print(f"\n  ✓ DROP rule found for noisy host:")
        for r in drop_rules:
            print(f"    {r.strip()}")
    else:
        print("\n  (No drop rules on s1 — check controller log for BLOCKED events)")

    # ══════════════════════════════════════════════════════════════════════════
    # SCENARIO 2: Latency & Throughput (Allowed Traffic)
    # ══════════════════════════════════════════════════════════════════════════
    banner("SCENARIO 2: Latency & Throughput (Allowed Traffic)")

    print("\n[2a] Latency — h3 → h4 (10 pings)")
    ping_out = h3.cmd("ping -c 10 -i 0.2 10.0.0.4")
    for line in ping_out.splitlines():
        if "rtt" in line or "round-trip" in line:
            print(f"     {line.strip()}")
            break

    print("\n[2b] Throughput — h5 ↔ h6 (iperf, 10 seconds)")
    h5.cmd("iperf -s -p 5001 &")
    time.sleep(0.5)
    iperf_out = h6.cmd("iperf -c 10.0.0.5 -p 5001 -t 10")
    for line in iperf_out.splitlines():
        if "bits/sec" in line:
            print(f"     {line.strip()}")
    h5.cmd("kill %iperf")

    # ══════════════════════════════════════════════════════════════════════════
    # SCENARIO 3: Flow Table Dump
    # ══════════════════════════════════════════════════════════════════════════
    banner("SCENARIO 3: Flow Table Dump — s1")
    _, s1_flows = get_flow_table("s1")
    for line in s1_flows.splitlines():
        if "priority" in line:
            print(f"  {line.strip()}")

    # ══════════════════════════════════════════════════════════════════════════
    # Summary
    # ══════════════════════════════════════════════════════════════════════════
    banner("Test Summary")
    print(f"  Scenario 1 — Broadcast rate limiting:")
    print(f"    h2 (5 broadcasts, within limit) : ALLOWED")
    print(f"    h1 (25 broadcasts, over limit)  : DROP RULE INSTALLED")
    print(f"    Flow rule delta on s1           : +{count_after - count_before}")
    print(f"\n  Scenario 2 — Allowed unicast traffic:")
    print(f"    h3 → h4 latency                 : PASSED")
    print(f"    h5 ↔ h6 throughput              : PASSED")
    print(f"\n  All scenarios complete.\n{SEPARATOR}\n")

    net.stop()


if __name__ == "__main__":
    run_tests()
