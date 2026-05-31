"""sing-box data-plane support.

These modules generate a complete sing-box configuration from yonder's state
and talk to its Clash API. They replace the xkeen+xray data plane: instead of
"write xray config files + xkeen restart", sing-box runs once with a tun
inbound and a `selector` outbound, and server-switch / on-off happen as live
Clash API calls (no process restart, no netfilter flush).

All builders here are pure (state in, dict out) so they're fully unit-testable
without a router. I/O and process control live in `service.py` / `clash.py`.
"""
