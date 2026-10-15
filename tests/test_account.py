"""Account lifetime, reminders, gate availability and concurrent presses."""

import asyncio
from datetime import UTC, datetime
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest
from conftest import CLOCK, PHONE, RESPONSES, FakeResponse, make_entry
from homeassistant.components import persistent_notification
from homeassistant.util import dt as dt_util

from custom_components.gate_link_hass.account import (
    LOGIN_CHECK_RETRY_SECONDS,
    AccountRuntime,
    clear_account_reminder,
    create_client,
)
from custom_components.gate_link_hass.adapters.app_client import (
    CHECK_PATH,
    COMMUNITIES_PATH,
    OPEN_PATH,
)
from custom_components.gate_link_hass.models import (
    AccountSession,
    ApiError,
    AuthenticationExpired,
    Gate,
)


@pytest.fixture
def runtime(hass, entry, client):
    return AccountRuntime(hass, entry, client)


async def test_account_timer_checks_and_reschedules_valid_session(
    hass, runtime, entry, clock, monkeypatch, transport
):
    cancel = Mock(return_value=None)
    tracker = Mock(return_value=cancel)
    monkeypatch.setattr(
        "custom_components.gate_link_hass.account.async_track_point_in_utc_time",
        tracker,
    )
    listener = Mock()
    unsubscribe = runtime.subscribe(listener)
    await runtime.check_login()
    runtime.start_login_check_timer()
    _, callback, when = tracker.call_args.args
    assert when == datetime(2027, 1, 15, 22, tzinfo=UTC)
    assert not runtime.expired
    persistent_notification.async_create.assert_not_called()
    clock.now = when.timestamp()
    original_auth = runtime.client.auth
    await callback(when)
    assert runtime.authenticated() and not entry.data["invalidated"]
    assert runtime.client.auth.issued_at == original_auth.issued_at
    assert runtime.client.auth.session_id == original_auth.session_id
    assert entry.data["auth"]["checked_at"] == clock.now
    assert tracker.call_args.args[2] == datetime(2027, 1, 16, 22, tzinfo=UTC)
    assert runtime.client.auth.next_check_at == clock.now + 7776000
    persistent_notification.async_create.assert_not_called()
    listener.assert_not_called()
    unsubscribe()
    assert all(call[1].endswith(CHECK_PATH) for call in transport.calls)
    await entry._async_process_on_unload(hass)
    assert cancel.call_count == 2


@pytest.mark.parametrize(
    "now,expected",
    [
        ("2027-01-15T21:59:59+00:00", "2027-01-15T22:00:00+00:00"),
        ("2027-01-15T22:00:00+00:00", "2027-01-16T22:00:00+00:00"),
        ("2027-01-15T22:01:00+00:00", "2027-01-16T22:00:00+00:00"),
        ("2027-12-31T23:30:00+00:00", "2028-01-01T22:00:00+00:00"),
    ],
)
async def test_daily_check_uses_beijing_six_even_in_another_ha_time_zone(
    hass, runtime, clock, monkeypatch, now, expected
):
    monkeypatch.setattr(hass.config, "time_zone", "America/New_York")
    monkeypatch.setattr(dt_util, "DEFAULT_TIME_ZONE", ZoneInfo(hass.config.time_zone))
    clock.now = datetime.fromisoformat(now).timestamp()
    tracker = Mock(return_value=Mock())
    monkeypatch.setattr(
        "custom_components.gate_link_hass.account.async_track_point_in_utc_time",
        tracker,
    )
    await runtime.check_login()
    runtime.start_login_check_timer()
    assert tracker.call_args.args[2] == datetime.fromisoformat(expected)
    await runtime.entry._async_process_on_unload(hass)


async def test_foreground_check_does_not_postpone_daily_check(
    hass, runtime, clock, monkeypatch
):
    clock.now = datetime(2027, 1, 15, 21, 55, tzinfo=UTC).timestamp()
    tracker = Mock(return_value=Mock())
    monkeypatch.setattr(
        "custom_components.gate_link_hass.account.async_track_point_in_utc_time",
        tracker,
    )
    await runtime.check_login()
    runtime.start_login_check_timer()
    clock.now += 60
    await runtime.check_login()
    assert tracker.call_args.args[2] == datetime(2027, 1, 15, 22, tzinfo=UTC)
    await runtime.entry._async_process_on_unload(hass)


async def test_earlier_server_reference_is_checked_before_daily_time(
    hass, runtime, monkeypatch, transport
):
    tracker = Mock(return_value=Mock())
    monkeypatch.setattr(
        "custom_components.gate_link_hass.account.async_track_point_in_utc_time",
        tracker,
    )
    transport.overrides[CHECK_PATH] = FakeResponse({"code": 0, "data": 3600})
    await runtime.check_login()
    runtime.start_login_check_timer()
    assert tracker.call_args.args[2] == datetime(2027, 1, 15, 9, tzinfo=UTC)
    await runtime.entry._async_process_on_unload(hass)


async def test_retry_does_not_skip_daily_check_near_six(
    hass, runtime, clock, monkeypatch, transport
):
    clock.now = datetime(2027, 1, 15, 21, 59, tzinfo=UTC).timestamp()
    tracker = Mock(return_value=Mock())
    monkeypatch.setattr(
        "custom_components.gate_link_hass.account.async_track_point_in_utc_time",
        tracker,
    )
    await runtime.check_login()
    runtime.start_login_check_timer()
    transport.overrides[CHECK_PATH] = FakeResponse({}, error=TimeoutError())
    with pytest.raises(ApiError, match="timeout"):
        await runtime.check_login()
    _, callback, when = tracker.call_args.args
    assert when == datetime(2027, 1, 15, 22, tzinfo=UTC)
    clock.now = when.timestamp()
    await callback(when)
    assert tracker.call_args.args[2] == datetime(2027, 1, 15, 22, 5, tzinfo=UTC)
    assert runtime.authenticated()
    await runtime.entry._async_process_on_unload(hass)


async def test_daily_checks_invalidate_only_the_rejected_account(
    hass, runtime, clock, monkeypatch, transport
):
    other_entry = make_entry(
        auth=AccountSession("99", "fixture-other-session", CLOCK, None)
    )
    hass.config_entries._entries[other_entry.entry_id] = other_entry
    other = AccountRuntime(hass, other_entry, create_client(hass))
    tracker = Mock(return_value=Mock())
    monkeypatch.setattr(
        "custom_components.gate_link_hass.account.async_track_point_in_utc_time",
        tracker,
    )
    for account in (runtime, other):
        await account.check_login()
        account.start_login_check_timer()
    scheduled = list(tracker.call_args_list)
    transport.overrides[CHECK_PATH] = lambda request: FakeResponse(
        {"code": 0, "data": 7776000}
        if request["params"]["sessionId"] == "fixture-other-session"
        else {"code": 401}
    )
    transport.calls.clear()
    clock.now = datetime(2027, 1, 15, 22, tzinfo=UTC).timestamp()
    for call in scheduled:
        assert call.args[2].timestamp() == clock.now
        await call.args[1](call.args[2])
    assert runtime.expired and runtime.entry.data["invalidated"]
    assert other.authenticated() and not other_entry.data["invalidated"]
    assert len(transport.calls) == 2
    assert all(call[1].endswith(CHECK_PATH) for call in transport.calls)
    assert tracker.call_count == 3
    assert tracker.call_args.args[2] == datetime(2027, 1, 16, 22, tzinfo=UTC)
    persistent_notification.async_create.assert_called_once()
    for entry in (runtime.entry, other_entry):
        await entry._async_process_on_unload(hass)


@pytest.mark.parametrize(
    "response",
    [
        FakeResponse({"code": 0, "data": 0}),
        FakeResponse({"code": 401}),
        FakeResponse({}, status=401),
    ],
)
async def test_timer_invalidates_only_on_server_rejection(
    hass, runtime, entry, clock, monkeypatch, transport, response
):
    cancel = Mock()
    tracker = Mock(return_value=cancel)
    monkeypatch.setattr(
        "custom_components.gate_link_hass.account.async_track_point_in_utc_time",
        tracker,
    )
    listener = Mock()
    runtime.subscribe(listener)
    await runtime.check_login()
    runtime.start_login_check_timer()
    _, callback, when = tracker.call_args.args
    clock.now = when.timestamp()
    transport.overrides[CHECK_PATH] = response
    transport.calls.clear()
    await callback(when)
    assert runtime.expired and entry.data["invalidated"]
    listener.assert_called_once()
    persistent_notification.async_create.assert_called_once()
    message = persistent_notification.async_create.call_args.args[1]
    assert PHONE not in message and "0000" in message
    entry.async_start_reauth.assert_called_once_with(hass)
    transport.calls.clear()
    await callback(when)
    with pytest.raises(AuthenticationExpired):
        await runtime.open("101:1")
    assert not transport.calls
    persistent_notification.async_create.assert_called_once()
    await entry._async_process_on_unload(hass)
    cancel.assert_called_once()


@pytest.mark.parametrize("lifetime", [1, 365, "obsolete"])
async def test_timer_ignores_old_local_lifetime_option(
    hass, entry, client, monkeypatch, lifetime
):
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "lifetime_days": lifetime}
    )
    runtime = AccountRuntime(hass, entry, client)
    tracker = Mock(return_value=Mock(return_value=None))
    monkeypatch.setattr(
        "custom_components.gate_link_hass.account.async_track_point_in_utc_time",
        tracker,
    )
    await runtime.check_login()
    runtime.start_login_check_timer()
    assert tracker.call_args.args[2] == datetime(2027, 1, 15, 22, tzinfo=UTC)
    await entry._async_process_on_unload(hass)


@pytest.mark.parametrize(
    "response",
    [
        FakeResponse({}, error=TimeoutError()),
        FakeResponse({}, status=503),
        FakeResponse({"code": 0, "data": "unexpected"}),
    ],
)
async def test_timer_errors_preserve_session_and_retry(
    hass, runtime, entry, clock, monkeypatch, transport, response
):
    tracker = Mock(return_value=Mock())
    monkeypatch.setattr(
        "custom_components.gate_link_hass.account.async_track_point_in_utc_time",
        tracker,
    )
    await runtime.check_login()
    runtime.start_login_check_timer()
    _, callback, when = tracker.call_args.args
    clock.now = when.timestamp()
    auth = dict(entry.data["auth"])
    transport.overrides[CHECK_PATH] = response
    await callback(when)
    assert runtime.authenticated() and not entry.data["invalidated"]
    assert entry.data["auth"] == auth
    assert (
        tracker.call_args.args[2].timestamp() == clock.now + LOGIN_CHECK_RETRY_SECONDS
    )
    persistent_notification.async_create.assert_not_called()
    entry.async_start_reauth.assert_not_called()
    transport.overrides.clear()
    clock.now += LOGIN_CHECK_RETRY_SECONDS
    await tracker.call_args.args[1](datetime.fromtimestamp(clock.now, UTC))
    assert entry.data["auth"]["checked_at"] == clock.now
    assert tracker.call_args.args[2] == datetime(2027, 1, 16, 22, tzinfo=UTC)
    await entry._async_process_on_unload(hass)


async def test_persisted_check_reference_survives_restart(
    hass, runtime, entry, client, clock, monkeypatch, transport
):
    transport.overrides[CHECK_PATH] = FakeResponse({"code": 0, "data": 86400})
    await runtime.check_login()
    tracker = Mock(return_value=Mock())
    monkeypatch.setattr(
        "custom_components.gate_link_hass.account.async_track_point_in_utc_time",
        tracker,
    )
    clock.now += 100
    restored = AccountRuntime(hass, entry, client)
    restored.start_login_check_timer()
    assert tracker.call_args.args[2] == datetime(2027, 1, 15, 22, tzinfo=UTC)
    assert restored.client.auth.next_check_at == CLOCK + 86400
    assert restored.authenticated()
    await entry._async_process_on_unload(hass)


async def test_missing_check_reference_schedules_check_without_invalidation(
    hass, runtime, clock, monkeypatch
):
    clock.now += 400 * 86400
    tracker = Mock(return_value=Mock())
    monkeypatch.setattr(
        "custom_components.gate_link_hass.account.async_track_point_in_utc_time",
        tracker,
    )
    runtime.start_login_check_timer()
    assert tracker.call_args.args[2].timestamp() == clock.now + 1
    await tracker.call_args.args[1](datetime.fromtimestamp(clock.now + 1, UTC))
    assert runtime.authenticated()
    await runtime.entry._async_process_on_unload(hass)


async def test_unload_cancels_timer_and_queued_callback(
    hass, runtime, monkeypatch, transport
):
    cancel = Mock()
    tracker = Mock(return_value=cancel)
    monkeypatch.setattr(
        "custom_components.gate_link_hass.account.async_track_point_in_utc_time",
        tracker,
    )
    await runtime.check_login()
    runtime.start_login_check_timer()
    _, callback, when = tracker.call_args.args
    await runtime.entry._async_process_on_unload(hass)
    transport.calls.clear()
    await callback(when)
    cancel.assert_called_once()
    assert not transport.calls
    assert tracker.call_count == 1


@pytest.mark.parametrize(
    "response",
    [
        FakeResponse({"code": 401}),
        FakeResponse({}, error=TimeoutError()),
        FakeResponse({"code": 0, "data": None}),
    ],
)
async def test_open_does_not_check_login_or_discover_gates(
    runtime, transport, clock, response
):
    original_auth = dict(runtime.entry.data["auth"])
    transport.overrides[CHECK_PATH] = response
    transport.overrides[COMMUNITIES_PATH] = FakeResponse({}, error=TimeoutError())
    clock.now += 365 * 86400
    for attempt in range(2):
        await runtime.open("101:1")
        assert len(transport.calls) == attempt + 1
        assert transport.calls[-1][1].endswith(OPEN_PATH)
    assert not runtime.entry.data["invalidated"]
    assert runtime.authenticated()
    assert runtime.entry.data["auth"] == original_auth


@pytest.mark.parametrize(
    "response",
    [
        FakeResponse({"code": 401}),
        FakeResponse({}, status=401),
        FakeResponse({"code": 9, "message": "session expired"}),
    ],
)
async def test_open_rejection_persists_invalidation_and_requests_reauth(
    hass, runtime, transport, response
):
    transport.overrides[OPEN_PATH] = response
    with pytest.raises(AuthenticationExpired):
        await runtime.open("101:1")
    assert len(transport.calls) == 1
    assert transport.calls[0][1].endswith(OPEN_PATH)
    assert runtime.entry.data["invalidated"] and not runtime.authenticated()
    persistent_notification.async_create.assert_called_once()
    runtime.entry.async_start_reauth.assert_called_once_with(hass)
    with pytest.raises(AuthenticationExpired):
        await runtime.open("101:1")
    assert len(transport.calls) == 1


async def test_unbound_gate_does_not_send_open_request(runtime, transport):
    with pytest.raises(ApiError, match="permission_denied"):
        await runtime.open("102:1")
    assert not transport.calls


async def test_early_invalidation_persists_across_restart(
    hass, entry, runtime, client, transport
):
    transport.overrides[COMMUNITIES_PATH] = FakeResponse({}, status=401)
    with pytest.raises(AuthenticationExpired):
        await runtime.refresh()
    assert entry.data["invalidated"]
    restored = AccountRuntime(hass, entry, client)
    transport.calls.clear()
    transport.overrides.clear()
    with pytest.raises(AuthenticationExpired):
        await restored.refresh()
    assert not transport.calls
    entry.async_start_reauth.assert_called_once()
    clear_account_reminder(hass, entry)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "invalidated": False}
    )
    renewed = AccountRuntime(hass, entry, client)
    await renewed.refresh()
    assert renewed.authenticated()
    renewed.invalidate()
    assert persistent_notification.async_create.call_count == 2


async def test_selected_missing_gate_permissions_are_decided_by_open_response(
    runtime, transport
):
    transport.overrides[COMMUNITIES_PATH] = FakeResponse({"code": 0, "data": []})
    await runtime.refresh()
    assert list(runtime.gates) == ["101:1"]
    assert not runtime.gates["101:1"].available(CLOCK)
    transport.calls.clear()
    transport.overrides[OPEN_PATH] = FakeResponse({}, status=403)
    with pytest.raises(ApiError, match="permission_denied"):
        await runtime.open("101:1")
    assert len(transport.calls) == 1 and transport.calls[0][1].endswith(OPEN_PATH)
    assert runtime.authenticated()
    persistent_notification.async_create.assert_not_called()


async def test_same_gate_press_rejected_until_request_finishes(runtime, transport):
    release = asyncio.Event()
    transport.overrides[OPEN_PATH] = FakeResponse(RESPONSES["open"], wait=release)
    open_started = asyncio.Event()

    def response(_):
        open_started.set()
        return FakeResponse(RESPONSES["open"], wait=release)

    transport.overrides[OPEN_PATH] = response
    first = asyncio.create_task(runtime.open("101:1"))
    try:
        await asyncio.wait_for(open_started.wait(), 1)
        with pytest.raises(ApiError, match="request_in_progress"):
            await runtime.open("101:1")
        assert sum("/open" in c[1] for c in transport.calls) == 1
    finally:
        release.set()
        await first
    await runtime.open("101:1")
    assert sum("/open" in c[1] for c in transport.calls) == 2


async def test_different_gates_have_independent_requests(hass, client, transport):
    gates = [
        Gate("101", "测试小区一", "1", "入口", "000000000001"),
        Gate("102", "测试小区二", "1", "入口", "000000000003"),
    ]
    entry = make_entry(gates=gates)
    hass.config_entries._entries[entry.entry_id] = entry
    runtime = AccountRuntime(hass, entry, client)
    release = asyncio.Event()
    both_started = asyncio.Event()
    opened = []

    def response(request):
        opened.append(request["data"])
        if len(opened) == 2:
            both_started.set()
        return FakeResponse(RESPONSES["open"], wait=release)

    transport.overrides[OPEN_PATH] = response
    tasks = [asyncio.create_task(runtime.open(g.key)) for g in gates]
    try:
        await asyncio.wait_for(both_started.wait(), 1)
        assert len(opened) == 2
    finally:
        release.set()
        await asyncio.gather(*tasks)


async def test_timeout_releases_lock_and_never_retries(runtime, transport):
    transport.overrides[OPEN_PATH] = FakeResponse({}, error=TimeoutError())
    with pytest.raises(ApiError, match="timeout"):
        await runtime.open("101:1")
    assert sum("/open" in c[1] for c in transport.calls) == 1
    assert not runtime._locks["101:1"].locked()
    transport.overrides.clear()
    await runtime.open("101:1")
    assert sum("/open" in c[1] for c in transport.calls) == 2


async def test_permission_error_does_not_invalidate_account(runtime, transport):
    transport.overrides[OPEN_PATH] = FakeResponse({}, status=403)
    with pytest.raises(ApiError, match="permission_denied"):
        await runtime.open("101:1")
    assert runtime.authenticated()
    persistent_notification.async_create.assert_not_called()


async def test_elapsed_time_during_discovery_does_not_invalidate(
    runtime, clock, transport
):
    def response(_):
        clock.now += 30 * 86400
        return FakeResponse({"code": 0, "data": []})

    transport.overrides[COMMUNITIES_PATH] = response
    await runtime.refresh()
    assert not runtime.entry.data["invalidated"]
    assert runtime.authenticated()
    persistent_notification.async_create.assert_not_called()
