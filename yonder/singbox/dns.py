"""sing-box `dns` block builder — replaces the router-side DoH dance.

Two resolvers, mirroring the routing split:
  * `dns-direct` — a Russian UDP resolver over the `direct` detour. RU domains
    resolve here (correct RU-CDN selection; queries stay on the ISP path).
  * `dns-proxy` — DoH (the user's `doh_url`) over the proxy detour. Everything
    else resolves here, so the ISP never sees which foreign sites are queried.

Uses the sing-box 1.12+ typed-server DNS format (the legacy format is removed
in 1.14).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from yonder.singbox.route import GEOSITE_RU

# Russian resolver for RU-domain lookups over the direct path (Yandex DNS).
RU_DIRECT_RESOLVER = "77.88.8.8"

DNS_PROXY = "dns-proxy"
DNS_DIRECT = "dns-direct"


def _doh_server(doh_url: str) -> dict[str, Any]:
    """Parse a DoH URL into a sing-box typed https server (detour=selector).

    `https://cloudflare-dns.com/dns-query` → host `cloudflare-dns.com`,
    path `/dns-query`. The detour is filled in by build_dns (the selector).
    """
    parts = urlsplit(doh_url)
    server: dict[str, Any] = {
        "type": "https",
        "tag": DNS_PROXY,
        "server": parts.hostname or "cloudflare-dns.com",
    }
    if parts.port:
        server["server_port"] = parts.port
    if parts.path and parts.path != "/":
        server["path"] = parts.path
    return server


def build_dns(doh_url: str, selector_tag: str) -> dict[str, Any]:
    """Build the `dns` block.

    `doh_url` is the foreign-traffic DoH upstream (from state.dns.doh_url).
    `selector_tag` is the proxy detour for that DoH server.
    """
    proxy_server = _doh_server(doh_url)
    proxy_server["detour"] = selector_tag

    return {
        "servers": [
            proxy_server,
            {
                # No `detour: direct` — sing-box 1.12+ rejects detouring to a
                # plain direct outbound ("makes no sense"). The query to a RU
                # resolver routes direct anyway via the geoip-ru rule.
                "type": "udp",
                "tag": DNS_DIRECT,
                "server": RU_DIRECT_RESOLVER,
            },
        ],
        "rules": [
            # RU domains resolve directly; everything else falls through to
            # `final` (DoH over the proxy).
            {"rule_set": [GEOSITE_RU], "server": DNS_DIRECT},
        ],
        "final": DNS_PROXY,
        "strategy": "prefer_ipv4",
    }
