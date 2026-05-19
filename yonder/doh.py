"""DoH-toggle on Keenetic, synchronized with VPN on/off.

When VPN is on we want encrypted DNS (so the ISP can't poison `instagram.com`
etc), but when the user turns VPN off they need to get back to whatever their
provider was serving — some local services only resolve through the ISP's
own DNS. This module runs `enable_doh` before xkeen -start and `disable_doh`
after xkeen -stop, snapshotting whatever the user already had configured so
we can restore it.

All operations write through `state.dns` (`active_url`, `previous_upstreams`)
so a crashed daemon recovers correctly on next start: if `active_url` matches
the router's actual upstream, enable_doh is a no-op.
"""

from __future__ import annotations

from yonder.keenetic import KeeneticClient
from yonder.state import Data, DnsState, State


async def enable_doh(state: State, client: KeeneticClient) -> tuple[bool, str]:
    """Push the user's DoH URL to the router, preserving prior upstreams.

    Idempotent: if we've already applied this exact URL, returns (True, "").
    If the URL has changed (user edited it while VPN was on), the old one is
    removed before the new one is added.

    Returns (success, message). On failure, state is left consistent with
    whatever step did/didn't succeed — a subsequent enable_doh call can
    retry safely.
    """
    snapshot = state.snapshot()
    target = snapshot.dns.doh_url.strip()
    if not target:
        return False, "DoH URL is empty"

    current = await client.list_doh_upstreams()
    current_urls = [u.url for u in current]

    # Recovery / no-op: we already pushed this URL and the router agrees.
    if snapshot.dns.active_url == target and target in current_urls:
        return True, ""

    # First-time apply OR user changed their settings mid-session.
    if snapshot.dns.active_url is None:
        # Snapshot whatever the user already had — but exclude our target if
        # it happens to be there already (we don't want to "restore" our own
        # URL on disable).
        previous = [u for u in current_urls if u != target]
    else:
        # Mid-session edit: keep the previous_upstreams we already captured.
        previous = list(snapshot.dns.previous_upstreams)

    # Remove every upstream we don't want active (including the stale
    # active_url if doh_url changed, and any user-set ones we're temporarily
    # displacing).
    for url in current_urls:
        if url != target:
            await client.remove_doh_upstream(url)

    # Add our target.
    await client.add_doh_upstream(target)

    def mutate(d: Data) -> None:
        d.dns.active_url = target
        d.dns.previous_upstreams = previous

    await state.update(mutate)
    return True, ""


async def disable_doh(state: State, client: KeeneticClient) -> tuple[bool, str]:
    """Remove our DoH URL and restore whatever was there before.

    Idempotent: if active_url is None we don't touch the router (safe to
    call on a fresh boot or after a no-op apply).
    """
    snapshot = state.snapshot()
    if snapshot.dns.active_url is None:
        return True, ""

    await client.remove_doh_upstream(snapshot.dns.active_url)
    # Restore any user-set upstreams we displaced.
    for url in snapshot.dns.previous_upstreams:
        await client.add_doh_upstream(url)

    def mutate(d: Data) -> None:
        d.dns = DnsState(doh_url=d.dns.doh_url)

    await state.update(mutate)
    return True, ""
