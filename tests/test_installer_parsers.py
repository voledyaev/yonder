import pytest
from installer.parsers import (
    UsbDrive,
    doh_upstream_present,
    parse_arch,
    parse_installed_components,
    parse_usb_drives,
    ping_succeeded,
)

# --- parse_arch ------------------------------------------------------------


def test_parse_arch_basic():
    out = """KeeneticOS version 5.00.C.11.0-0
release:        5.00.C.11.0-0
arch:           aarch64
ndm:            ..."""
    assert parse_arch(out) == "aarch64"


def test_parse_arch_normalizes_armv7():
    assert parse_arch("arch: armv7sf-k3.2\n") == "armv7"
    assert parse_arch("arch: armv7hf\n") == "armv7"


def test_parse_arch_missing_raises():
    with pytest.raises(ValueError):
        parse_arch("no arch field here")


# --- parse_installed_components --------------------------------------------


def test_parse_installed_components_single_line():
    out = """release: 5.0.11
components:     opkg, ext, dns-https, foo, bar
ndm: ..."""
    comps = parse_installed_components(out)
    assert {"opkg", "ext", "dns-https", "foo", "bar"} <= comps


def test_parse_installed_components_multiline():
    # Continuation lines need >=16 leading spaces.
    out = (
        "components:     opkg, ext,\n"
        + " " * 16
        + "dns-https, foo, bar,\n"
        + " " * 16
        + "baz\n"
        + "next-field: x\n"
    )
    comps = parse_installed_components(out)
    assert comps == {"opkg", "ext", "dns-https", "foo", "bar", "baz"}


def test_parse_installed_components_empty_when_missing():
    assert parse_installed_components("no components field here") == set()


# --- parse_usb_drives ------------------------------------------------------


def _ls_block(**fields) -> str:
    """Helper: assemble one Keenetic `ls` entry-block."""
    lines = ["entry, type = V:"]
    for k, v in fields.items():
        lines.append(f"     {k}: {v}")
    return "\n".join(lines) + "\n"


def test_parse_usb_drives_finds_mounted_ext4():
    block = _ls_block(
        name="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee:",
        fstype="ext4",
        uuid="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee:",
        storage="usb",
        mounted="yes",
        free="123456789",
        total="128000000000",
    )
    drives = parse_usb_drives("\n" + block)
    assert len(drives) == 1
    d = drives[0]
    assert d.fstype == "ext4"
    assert d.uuid == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"  # trailing : stripped
    assert d.free == "123456789"


def test_parse_usb_drives_skips_non_usb():
    block = _ls_block(
        name="flash:",
        fstype="ext4",
        uuid="x",
        storage="flash",
        mounted="yes",
        free="1",
        total="2",
    )
    assert parse_usb_drives("\n" + block) == []


def test_parse_usb_drives_skips_unmounted():
    block = _ls_block(
        name="x:",
        fstype="ext4",
        uuid="u",
        storage="usb",
        mounted="no",
        free="1",
        total="2",
    )
    assert parse_usb_drives("\n" + block) == []


def test_parse_usb_drives_skips_non_ext():
    block = _ls_block(
        name="x:",
        fstype="ntfs",
        uuid="u",
        storage="usb",
        mounted="yes",
        free="1",
        total="2",
    )
    assert parse_usb_drives("\n" + block) == []


def test_parse_usb_drives_multiple_entries():
    a = _ls_block(
        name="a:", fstype="ext4", uuid="a", storage="usb", mounted="yes", free="1", total="2"
    )
    b = _ls_block(
        name="b:", fstype="ext4", uuid="b", storage="usb", mounted="yes", free="3", total="4"
    )
    drives = parse_usb_drives("\n" + a + "\n" + b)
    assert [d.uuid for d in drives] == ["a", "b"]


# --- ping_succeeded --------------------------------------------------------


def test_ping_succeeded_true():
    out = """PING bin.entware.net (1.2.3.4): 56 data bytes
64 bytes from 1.2.3.4: seq=0 ttl=64 time=12.345 ms
1 packets transmitted, 1 received, 0% packet loss"""
    assert ping_succeeded(out)


def test_ping_succeeded_false_on_loss():
    out = "1 packets transmitted, 0 received, 100% packet loss"
    assert not ping_succeeded(out)


def test_ping_succeeded_false_on_error():
    assert not ping_succeeded("Host not reachable")


# --- doh_upstream_present --------------------------------------------------


CF_URL = "https://cloudflare-dns.com/dns-query"


def test_doh_upstream_present_true():
    cfg = f"""dns-proxy
    rebind-protect auto
    https upstream {CF_URL}
"""
    assert doh_upstream_present(cfg, CF_URL)


def test_doh_upstream_present_false_when_different_url():
    cfg = "    https upstream https://dns.google/dns-query\n"
    assert not doh_upstream_present(cfg, CF_URL)


def test_doh_upstream_present_false_when_absent():
    cfg = """dns-proxy
    rebind-protect auto"""
    assert not doh_upstream_present(cfg, CF_URL)
