"""
Broadcast Traffic Control - Custom Mininet Topology (POX version)
=================================================================
Topology:
                     [Controller (POX)]
                            |
                          [s1]
                    /----/  |  \\----\\
                  [s2]     [s3]    [s4]
                 /   \\    /   \\   /   \\
               h1   h2  h3   h4 h5   h6

  - 4 switches (s1 = core, s2/s3/s4 = edge)
  - 6 hosts spread across edge switches
  - h1 is the designated "noisy" host (sends excessive broadcasts in tests)

Usage:
    sudo python3 topology.py
    (POX controller must be running first on port 6633)
"""

from mininet.net   import Mininet
from mininet.node  import RemoteController, OVSKernelSwitch
from mininet.topo  import Topo
from mininet.log   import setLogLevel, info
from mininet.cli   import CLI
from mininet.link  import TCLink


class BroadcastTopo(Topo):
    """
    Tree-like topology: one core switch, three edge switches, six hosts.
    """

    def build(self):
        # ── Core switch ───────────────────────────────────────────────────────
        s1 = self.addSwitch("s1", cls=OVSKernelSwitch, protocols="OpenFlow10")

        # ── Edge switches ─────────────────────────────────────────────────────
        # POX uses OpenFlow 1.0 by default
        s2 = self.addSwitch("s2", cls=OVSKernelSwitch, protocols="OpenFlow10")
        s3 = self.addSwitch("s3", cls=OVSKernelSwitch, protocols="OpenFlow10")
        s4 = self.addSwitch("s4", cls=OVSKernelSwitch, protocols="OpenFlow10")

        # ── Hosts ─────────────────────────────────────────────────────────────
        link_opts = dict(bw=10, delay="5ms", loss=0, use_htb=True)

        h1 = self.addHost("h1", ip="10.0.0.1/24")   # designated noisy broadcaster
        h2 = self.addHost("h2", ip="10.0.0.2/24")
        h3 = self.addHost("h3", ip="10.0.0.3/24")
        h4 = self.addHost("h4", ip="10.0.0.4/24")
        h5 = self.addHost("h5", ip="10.0.0.5/24")
        h6 = self.addHost("h6", ip="10.0.0.6/24")

        # ── Links: core ↔ edge ────────────────────────────────────────────────
        self.addLink(s1, s2, **link_opts)
        self.addLink(s1, s3, **link_opts)
        self.addLink(s1, s4, **link_opts)

        # ── Links: edge ↔ hosts ───────────────────────────────────────────────
        self.addLink(s2, h1, **link_opts)
        self.addLink(s2, h2, **link_opts)
        self.addLink(s3, h3, **link_opts)
        self.addLink(s3, h4, **link_opts)
        self.addLink(s4, h5, **link_opts)
        self.addLink(s4, h6, **link_opts)


def run():
    setLogLevel("info")
    topo = BroadcastTopo()

    net = Mininet(
        topo=topo,
        controller=RemoteController("c0", ip="127.0.0.1", port=6633),
        switch=OVSKernelSwitch,
        link=TCLink,
        autoSetMacs=True,     # unique MACs for learning switch
        autoStaticArp=False,  # keep ARP so we see broadcast traffic
    )

    net.start()

    info("\n" + "="*60 + "\n")
    info("Topology ready — 4 switches, 6 hosts\n")
    info("Controller expected at 127.0.0.1:6633 (POX)\n")
    info("="*60 + "\n")
    info("\nHosts:\n")
    for host in net.hosts:
        info(f"  {host.name}: IP={host.IP()}  MAC={host.MAC()}\n")

    info("\nQuick-start test commands (run inside Mininet CLI):\n")
    info("  pingall                              - baseline connectivity\n")
    info("  h3 ping -c 10 h4                    - unicast latency\n")
    info("  h1 arping -c 25 -b -I h1-eth0 10.0.0.255  - broadcast storm\n")
    info("  h5 iperf -s & ; h6 iperf -c 10.0.0.5 -t 10 - throughput\n")
    info("  sh ovs-ofctl dump-flows s1          - inspect flow table\n\n")

    CLI(net)
    net.stop()


if __name__ == "__main__":
    run()
