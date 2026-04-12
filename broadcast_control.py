"""
Broadcast Traffic Control - POX SDN Controller
===============================================
Project: SDN-based Broadcast Traffic Control using Mininet + POX
Author:  [Your Name]
Course:  Computer Networks - UE24CS252B

Usage:
    python pox.py log.level --DEBUG misc.broadcast_control

Description:
    This controller detects excessive broadcast traffic, limits flooding,
    and installs selective unicast forwarding rules via OpenFlow.

Key behaviors:
    1. Learns MAC-to-port mappings from incoming packets (learning switch)
    2. Tracks broadcast packet counts per source host per switch
    3. Enforces a per-host broadcast rate limit (BROADCAST_LIMIT)
    4. Installs proactive unicast flow rules when dst MAC is known
    5. Drops excessive broadcasts from offending hosts via flow rules
    6. Logs all events for analysis
"""

from pox.core import core
from pox.lib.util import dpid_to_str
from pox.lib.addresses import EthAddr
import pox.openflow.libopenflow_01 as of
from pox.lib.revent import EventMixin
from pox.lib.recoco import Timer
import time

log = core.getLogger()

# ── Configuration ──────────────────────────────────────────────────────────────
BROADCAST_LIMIT   = 10    # max broadcast packets per host per time window
RATE_WINDOW       = 10    # seconds for the broadcast rate window
FLOW_IDLE_TIMEOUT = 30    # idle timeout for unicast flow rules (seconds)
FLOW_HARD_TIMEOUT = 120   # hard timeout for unicast flow rules (seconds)
DROP_DURATION     = 30    # hard timeout for drop rules (seconds)
MONITOR_INTERVAL  = 5     # statistics logging interval (seconds)

BROADCAST_MAC = EthAddr("ff:ff:ff:ff:ff:ff")


class BroadcastControlSwitch(EventMixin):
    """
    Per-switch instance that handles all OpenFlow events for one datapath.
    Instantiated by BroadcastController when a switch connects.
    """

    def __init__(self, connection):
        self.connection = connection
        self.dpid       = connection.dpid

        # mac_to_port[mac] = port_number
        self.mac_to_port = {}

        # broadcast_count[src_mac] = {"count": int, "window_start": float}
        self.broadcast_count = {}

        # blocked_macs: set of currently rate-limited source MACs
        self.blocked_macs = set()

        # Statistics
        self.stats = {
            "total_broadcasts"     : 0,
            "dropped_broadcasts"   : 0,
            "unicast_rules_installed": 0,
        }

        # Listen to OpenFlow messages from this switch
        self.listenTo(connection)
        log.info("Switch connected: dpid=%s", dpid_to_str(self.dpid))

        # Install table-miss rule: send all unmatched packets to controller
        self._install_table_miss()

    # ── Setup ──────────────────────────────────────────────────────────────────

    def _install_table_miss(self):
        """
        Lowest-priority rule: any packet with no matching flow → controller.
        This is the OpenFlow equivalent of a table-miss entry.
        """
        msg = of.ofp_flow_mod()
        msg.priority = 0
        msg.match    = of.ofp_match()   # wildcard everything
        msg.actions.append(of.ofp_action_output(port=of.OFPP_CONTROLLER))
        self.connection.send(msg)

    # ── Packet-In Handler ──────────────────────────────────────────────────────

    def _handle_PacketIn(self, event):
        """
        Core packet_in logic:
          1. Parse Ethernet frame
          2. Learn src MAC → port
          3. Detect broadcast destination
          4. Enforce broadcast rate limit
          5. Install unicast rules or flood selectively
        """
        packet  = event.parsed
        in_port = event.port

        if not packet.parsed:
            log.warning("Unparsed packet on dpid=%s", dpid_to_str(self.dpid))
            return

        # Ignore LLDP
        if packet.type == packet.LLDP_TYPE:
            return

        src = packet.src   # EthAddr
        dst = packet.dst   # EthAddr

        # ── 1. MAC Learning ───────────────────────────────────────────────────
        self.mac_to_port[src] = in_port

        # ── 2. Broadcast Detection ────────────────────────────────────────────
        is_broadcast = (dst == BROADCAST_MAC)
        is_multicast = dst.isMulticast()

        if is_broadcast or is_multicast:
            self.stats["total_broadcasts"] += 1
            log.info(
                "[BROADCAST] dpid=%s src=%s dst=%s in_port=%s",
                dpid_to_str(self.dpid), src, dst, in_port
            )

            # ── 3. Rate-Limit Check ───────────────────────────────────────────
            if self._is_rate_exceeded(src):
                log.warning(
                    "[BLOCKED] Excessive broadcast from %s on dpid=%s — installing drop rule",
                    src, dpid_to_str(self.dpid)
                )
                self.stats["dropped_broadcasts"] += 1
                self._install_drop_rule(src)
                return  # Drop this packet immediately (no packet_out sent)

            # Within limit — flood out all ports except in_port
            self._send_packet_out(event, of.OFPP_FLOOD)
            return

        # ── 4. Unicast Forwarding ─────────────────────────────────────────────
        if dst in self.mac_to_port:
            out_port = self.mac_to_port[dst]

            # Install a flow rule so future packets bypass the controller
            self._install_unicast_rule(src, dst, in_port, out_port)
            self.stats["unicast_rules_installed"] += 1
            log.info(
                "[UNICAST RULE] dpid=%s %s→%s via port %s",
                dpid_to_str(self.dpid), src, dst, out_port
            )
            self._send_packet_out(event, out_port)
        else:
            # Destination unknown — flood (will learn on reply)
            self._send_packet_out(event, of.OFPP_FLOOD)

    # ── Broadcast Rate Tracking ────────────────────────────────────────────────

    def _is_rate_exceeded(self, src_mac):
        """
        Returns True if src_mac has exceeded BROADCAST_LIMIT broadcasts
        in the current RATE_WINDOW. Resets the window when it expires.
        """
        now = time.time()
        key = str(src_mac)

        if key not in self.broadcast_count:
            self.broadcast_count[key] = {"count": 0, "window_start": now}

        entry = self.broadcast_count[key]

        # Reset window if expired
        if now - entry["window_start"] > RATE_WINDOW:
            entry["count"]        = 0
            entry["window_start"] = now

        entry["count"] += 1

        if entry["count"] > BROADCAST_LIMIT:
            self.blocked_macs.add(key)
            return True

        return False

    # ── Flow Rule Helpers ──────────────────────────────────────────────────────

    def _install_unicast_rule(self, src, dst, in_port, out_port):
        """
        Installs a precise match+action flow rule for known unicast traffic.
        Priority 10 · idle_timeout=30s · hard_timeout=120s
        """
        msg              = of.ofp_flow_mod()
        msg.priority     = 10
        msg.idle_timeout = FLOW_IDLE_TIMEOUT
        msg.hard_timeout = FLOW_HARD_TIMEOUT
        msg.match        = of.ofp_match(
            in_port  = in_port,
            dl_src   = src,
            dl_dst   = dst,
        )
        msg.actions.append(of.ofp_action_output(port=out_port))
        self.connection.send(msg)

    def _install_drop_rule(self, src_mac):
        """
        Installs a high-priority flow rule that drops all broadcast packets
        from src_mac for DROP_DURATION seconds.
        Match: dl_src=src_mac, dl_dst=ff:ff:ff:ff:ff:ff
        Action: none (empty action list = drop in OpenFlow)
        """
        msg              = of.ofp_flow_mod()
        msg.priority     = 100          # higher than unicast (10) and table-miss (0)
        msg.hard_timeout = DROP_DURATION
        msg.idle_timeout = 0
        msg.match        = of.ofp_match(
            dl_src = src_mac,
            dl_dst = BROADCAST_MAC,
        )
        # No actions appended → drop
        self.connection.send(msg)

        # Schedule automatic unblock after DROP_DURATION
        Timer(DROP_DURATION, self._unblock, args=[str(src_mac)])

    def _unblock(self, mac_str):
        """Called by timer after the drop rule expires."""
        self.blocked_macs.discard(mac_str)
        log.info("[UNBLOCKED] %s on dpid=%s", mac_str, dpid_to_str(self.dpid))

    def _send_packet_out(self, event, out_port):
        """
        Sends a packet_out to the switch to forward the current buffered packet.
        """
        msg         = of.ofp_packet_out()
        msg.data    = event.ofp
        msg.in_port = event.port
        msg.actions.append(of.ofp_action_output(port=out_port))
        self.connection.send(msg)

    def log_stats(self):
        """Called periodically by BroadcastController monitor."""
        log.info(
            "[STATS] dpid=%s | Broadcasts=%d | Dropped=%d | Unicast rules=%d | Blocked hosts=%d",
            dpid_to_str(self.dpid),
            self.stats["total_broadcasts"],
            self.stats["dropped_broadcasts"],
            self.stats["unicast_rules_installed"],
            len(self.blocked_macs),
        )


class BroadcastController(EventMixin):
    """
    Top-level POX component. Listens for new switch connections and
    creates a BroadcastControlSwitch instance for each one.
    """

    def __init__(self):
        self.switches = {}   # dpid → BroadcastControlSwitch
        self.listenTo(core.openflow)

        # Periodic stats logging
        Timer(MONITOR_INTERVAL, self._monitor, recurring=True)
        log.info("BroadcastController started — waiting for switches")

    def _handle_ConnectionUp(self, event):
        """Fires when a switch connects."""
        sw = BroadcastControlSwitch(event.connection)
        self.switches[event.dpid] = sw

    def _handle_ConnectionDown(self, event):
        """Fires when a switch disconnects."""
        if event.dpid in self.switches:
            del self.switches[event.dpid]
            log.info("Switch disconnected: dpid=%s", dpid_to_str(event.dpid))

    def _monitor(self):
        """Periodically logs stats for every connected switch."""
        for sw in self.switches.values():
            sw.log_stats()


def launch():
    """
    POX entry point — called by `pox.py` when this module is loaded.
    Usage: python pox.py log.level --DEBUG misc.broadcast_control
    """
    core.registerNew(BroadcastController)
