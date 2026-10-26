"""Permission timestamps and event-loop-safe gate discovery."""

from datetime import UTC, datetime

import pytest

from custom_components.gate_link_hass import models


@pytest.mark.parametrize(
    "value",
    [
        "2080-01-01 00:00:00",
        "2080-01-01T00:00:00+08:00",
        "2079-12-31T16:00:00Z",
        "2079-12-31T11:00:00-05:00",
    ],
)
def test_permission_deadline_time_zones(value):
    expected = datetime(2079, 12, 31, 16, tzinfo=UTC).timestamp()
    assert models.permission_deadline(value) == expected


@pytest.mark.parametrize("client_fixture", ["client", "legacy_client"])
async def test_discovery_does_not_load_time_zone_data(
    request, monkeypatch, client_fixture
):
    client = request.getfixturevalue(client_fixture)

    def unexpected_zone_info(*args, **kwargs):
        raise AssertionError(
            "Gate discovery must not load time zone data on the event loop"
        )

    monkeypatch.setattr(models, "ZoneInfo", unexpected_zone_info, raising=False)
    gates = await client.discover_gates()
    assert set(gates) == {"101:1", "101:2", "102:1"}
    expected = datetime(2079, 12, 31, 16, tzinfo=UTC).timestamp()
    assert all(gate.permission_expires_at == expected for gate in gates.values())
