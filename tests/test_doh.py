import pytest
from yonder.doh import disable_doh, enable_doh
from yonder.keenetic import DohUpstream
from yonder.state import DEFAULT_DOH_URL, Data, State


class FakeRouter:
    """In-memory stand-in for KeeneticClient's DoH-upstream surface."""

    def __init__(self, upstreams: list[str] | None = None):
        self._upstreams: list[str] = list(upstreams or [])
        self.calls: list[tuple[str, str]] = []  # ("add"|"remove", url)

    async def list_doh_upstreams(self) -> list[DohUpstream]:
        return [DohUpstream(url=u) for u in self._upstreams]

    async def add_doh_upstream(self, url: str) -> None:
        self.calls.append(("add", url))
        if url not in self._upstreams:
            self._upstreams.append(url)

    async def remove_doh_upstream(self, url: str) -> None:
        self.calls.append(("remove", url))
        if url in self._upstreams:
            self._upstreams.remove(url)


@pytest.fixture
async def state(tmp_path):
    return State(tmp_path / "state.json")


CF = DEFAULT_DOH_URL
GOOGLE = "https://dns.google/dns-query"
QUAD9 = "https://dns.quad9.net/dns-query"


# --- enable_doh -------------------------------------------------------------


async def test_enable_on_clean_router(state):
    router = FakeRouter()
    ok, _ = await enable_doh(state, router)
    assert ok
    snap = state.snapshot()
    assert snap.dns.active_url == CF
    assert snap.dns.previous_upstreams == []
    assert router._upstreams == [CF]


async def test_enable_snapshots_user_upstreams(state):
    # User had their own DoH before yonder ever ran.
    router = FakeRouter([GOOGLE])
    await enable_doh(state, router)
    snap = state.snapshot()
    assert snap.dns.active_url == CF
    assert snap.dns.previous_upstreams == [GOOGLE]
    assert router._upstreams == [CF]
    # Order of operations matters: remove user's before adding ours, so a
    # crash mid-way never leaves two competing DoH endpoints active.
    assert router.calls == [("remove", GOOGLE), ("add", CF)]


async def test_enable_idempotent_when_already_applied(state):
    router = FakeRouter([CF])
    await state.update(lambda d: setattr(d.dns, "active_url", CF))
    router.calls.clear()
    ok, _ = await enable_doh(state, router)
    assert ok
    assert router.calls == []  # nothing pushed
    assert state.snapshot().dns.active_url == CF


async def test_enable_swaps_url_when_user_edited_settings(state):
    # User had VPN on with CF, then in settings changed to Quad9 — apply
    # pipeline calls enable_doh again. Old (CF) must be removed; new (Quad9)
    # added; previous_upstreams must NOT be re-snapshot (it already holds
    # whatever was there before the first enable).

    def mutate(d: Data) -> None:
        d.dns.active_url = CF
        d.dns.doh_url = QUAD9
        d.dns.previous_upstreams = [GOOGLE]

    await state.update(mutate)
    router = FakeRouter([CF])

    await enable_doh(state, router)
    snap = state.snapshot()
    assert snap.dns.active_url == QUAD9
    assert snap.dns.previous_upstreams == [GOOGLE]  # preserved
    assert router._upstreams == [QUAD9]


async def test_enable_with_empty_doh_url_fails(state):
    await state.update(lambda d: setattr(d.dns, "doh_url", "   "))
    router = FakeRouter()
    ok, msg = await enable_doh(state, router)
    assert not ok
    assert "empty" in msg.lower()
    assert router.calls == []


async def test_enable_excludes_target_from_previous_upstreams(state):
    # Router already happens to have our target URL (maybe leftover from a
    # previous run that never disabled cleanly). Don't snapshot it as
    # "previous" — disable would then mistakenly re-add it after removing.
    router = FakeRouter([CF, GOOGLE])
    await enable_doh(state, router)
    snap = state.snapshot()
    assert snap.dns.previous_upstreams == [GOOGLE]
    assert CF not in snap.dns.previous_upstreams


# --- disable_doh ------------------------------------------------------------


async def test_disable_restores_previous_upstreams(state):
    router = FakeRouter([CF])

    def mutate(d: Data) -> None:
        d.dns.active_url = CF
        d.dns.previous_upstreams = [GOOGLE]

    await state.update(mutate)

    ok, _ = await disable_doh(state, router)
    assert ok
    assert router._upstreams == [GOOGLE]
    snap = state.snapshot()
    assert snap.dns.active_url is None
    assert snap.dns.previous_upstreams == []
    # doh_url (user's preference) survives the disable.
    assert snap.dns.doh_url == CF


async def test_disable_is_noop_when_inactive(state):
    router = FakeRouter()
    ok, _ = await disable_doh(state, router)
    assert ok
    assert router.calls == []
    assert state.snapshot().dns.active_url is None


async def test_disable_with_no_previous_upstreams_leaves_router_clean(state):
    router = FakeRouter([CF])

    def mutate(d: Data) -> None:
        d.dns.active_url = CF
        # previous_upstreams stays empty — user had nothing configured

    await state.update(mutate)

    await disable_doh(state, router)
    assert router._upstreams == []
    assert state.snapshot().dns.active_url is None


async def test_disable_then_enable_round_trip(state):
    # Full off→on→off cycle with a user-configured upstream that must
    # survive both transitions intact.
    router = FakeRouter([GOOGLE])

    await enable_doh(state, router)
    assert router._upstreams == [CF]
    assert state.snapshot().dns.previous_upstreams == [GOOGLE]

    await disable_doh(state, router)
    assert router._upstreams == [GOOGLE]
    assert state.snapshot().dns.active_url is None
    assert state.snapshot().dns.previous_upstreams == []
