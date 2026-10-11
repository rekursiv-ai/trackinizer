"""The audience rules for a service account and the keys minted for it."""

from __future__ import annotations

import uuid

import pytest

from trackinizer.server.auth import AudienceCeilingError, AuthIdentity
from trackinizer.server.service_accounts import (
    require_grantable,
    service_token_pin,
)


_OPEN = frozenset({"open"})
_BOTH = frozenset({"open", "staff"})


def _caller(
    *,
    reads: frozenset[str],
    opens: frozenset[str] | None = None,
    pin: str | None = None,
    key: bool = True,
) -> AuthIdentity:
    """Return an admin holding these audiences, through a key or a browser session."""
    return AuthIdentity(
        user_id=uuid.uuid4(),
        api_key_id=uuid.uuid4() if key else None,
        email="boss@example.com",
        role="admin",
        read_audiences=reads,
        open_audiences=reads if opens is None else opens,
        pinned_audience=pin,
    )


class TestServiceTokenPin:
    def test_a_token_cannot_pin_an_audience_its_account_does_not_hold(self) -> None:
        with pytest.raises(AudienceCeilingError, match="account"):
            _ = service_token_pin(
                _OPEN,
                minter=_caller(reads=_BOTH),
                open_into="staff",
            )

    def test_a_staff_account_holds_a_token_pinned_to_staff(self) -> None:
        pin = service_token_pin(
            _BOTH,
            minter=_caller(reads=_BOTH),
            open_into="staff",
        )
        assert pin == "staff"

    def test_a_token_named_no_pin_stays_unpinned_for_a_caller_holding_all(self) -> None:
        pin = service_token_pin(
            _BOTH,
            minter=_caller(reads=_BOTH, key=False),
            open_into=None,
        )
        assert pin is None

    def test_a_caller_cannot_mint_a_token_that_reaches_beyond_its_own_audiences(
        self,
    ) -> None:
        """An admin who does not open into ``staff`` cannot hand that to a token."""
        reads_both = _caller(reads=_BOTH, opens=_OPEN)
        # Unpinned, the token could open sessions into staff.
        with pytest.raises(AudienceCeilingError, match="opens"):
            _ = service_token_pin(_BOTH, minter=reads_both, open_into=None)
        # Pinned to what the caller opens into, it opens no further.
        assert service_token_pin(_BOTH, minter=reads_both, open_into="open") == "open"
        # A pin does not narrow what a key reads, so a caller reading less is refused.
        with pytest.raises(AudienceCeilingError, match="reads"):
            _ = service_token_pin(
                _BOTH,
                minter=_caller(reads=_OPEN),
                open_into="open",
            )

    def test_a_caller_naming_staff_beyond_its_audiences_is_refused(self) -> None:
        with pytest.raises(AudienceCeilingError):
            _ = service_token_pin(
                _BOTH,
                minter=_caller(reads=_OPEN),
                open_into="staff",
            )

    def test_a_pinned_key_hands_its_pin_on(self) -> None:
        pinned = _caller(reads=_BOTH, opens=_OPEN, pin="open")
        assert service_token_pin(_BOTH, minter=pinned, open_into=None) == "open"
        with pytest.raises(AudienceCeilingError):
            _ = service_token_pin(_BOTH, minter=pinned, open_into="staff")


class TestRequireGrantable:
    def test_a_caller_may_grant_what_it_holds(self) -> None:
        require_grantable(_caller(reads=_BOTH), reads=_BOTH, opens=_BOTH)
        require_grantable(_caller(reads=_BOTH), reads=_OPEN, opens=frozenset())

    def test_a_caller_may_not_grant_a_read_it_lacks(self) -> None:
        caller = _caller(reads=_OPEN, opens=_BOTH)
        require_grantable(caller, reads=_OPEN, opens=_BOTH)
        with pytest.raises(AudienceCeilingError, match="reads"):
            require_grantable(caller, reads=_BOTH, opens=_OPEN)

    def test_a_caller_may_not_grant_an_open_it_lacks(self) -> None:
        caller = _caller(reads=_BOTH, opens=_OPEN)
        with pytest.raises(AudienceCeilingError, match="opens"):
            require_grantable(caller, reads=_OPEN, opens=_BOTH)


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
