"""Headless provisioning: put a Pi onto a WiFi network from a phone over BLE.

Roadmap N1.1-N1.3 (``docs/roadmap/network-and-provisioning.md``); the
operator-facing guide is ``docs/ble-provisioning.md``.

The service runs as root, standalone from the daemon (``systemd/
canary-provision.service``), because it owns BlueZ objects and writes
NetworkManager profiles. It and the daemon share one file, the provisioning
window under ``/run/canary`` (:mod:`src.provisioning.gate`), the same way the
UPS watchdog and the daemon share the keep-alive hold. The daemon is
unprivileged and NetworkManager's system connections are root's, so every WiFi
change it makes (``/control/network/wifi``) is a request to this service over
a Unix socket.

- :mod:`.improv` -- the Improv WiFi wire format. Pure.
- :mod:`.network` -- NetworkManager, through ``nmcli`` and keyfiles.
- :mod:`.gate` -- who may provision right now (N1.2).
- :mod:`.status` -- the status characteristic's payload (N1.3).
- :mod:`.reader` -- the same facts, cached, for the daemon's dashboard (P2.3).
- :mod:`.controller` -- the Improv state machine, transport-agnostic.
- :mod:`.bluez` -- the BlueZ GATT server and advertisement over D-Bus.
- :mod:`.netsock` -- the network-control socket the dashboard's saved-networks
  page asks (N5.1): ``/run/canary/network.sock``, root:canary 0660.
- :mod:`.service` -- keeps the socket and the BLE server running side by side
  (``run``); either may fail without stopping the other.
- :mod:`.client` -- the daemon's side of that socket (N5.3).
"""
