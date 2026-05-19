"""Pure-function tests for installer/steps.py.

The bulk of steps.py needs a real router to exercise — those are tested by
running --probe and --install against actual hardware. This file covers the
small bits that are safe to unit-test: the env-file value validation regex
(security-relevant: a maliciously-crafted password must not be silently
written to a file that gets `.`-sourced by the init script).
"""

import pytest
from installer.steps import _ENV_VALUE_RE


@pytest.mark.parametrize(
    "pw",
    [
        "simple",
        "with-dashes",
        "with_underscores",
        "with.dots",
        "with@signs",
        "with:colons",
        "with+plus=signs",
        "MixedCase123",
        "fissox-napny3-zyTnum",  # the test-router password shape
    ],
)
def test_safe_passwords_accepted(pw):
    assert _ENV_VALUE_RE.match(pw) is not None


@pytest.mark.parametrize(
    "pw",
    [
        "has space",
        "has\nnewline",
        "has`backtick`",
        "has$dollar",
        "has;semicolon",
        'has"quote',
        "has'singlequote",
        "has\\backslash",
        "has|pipe",
        "has&amp",
        "has(paren)",
        "has<redir>",
    ],
)
def test_dangerous_passwords_rejected(pw):
    assert _ENV_VALUE_RE.match(pw) is None
