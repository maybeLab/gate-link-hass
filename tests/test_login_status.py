"""Real HA diagnostics, Activity filtering and Recorder history queries."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from conftest import CLOCK, PHONE, FakeResponse, make_entry
from homeassistant.components import persistent_notification
from homeassistant.components.logbook.helpers import (
    async_determine_event_types,
    async_filter_entities,
    event_forwarder_filtered,
)
from homeassistant.components.logbook.models import LogbookConfig, async_event_to_row
from homeassistant.components.logbook.processor import EventProcessor
from homeassistant.components.logbook.queries import statement_for_request
from homeassistant.components.recorder.db_schema import (
    Base,
    EventData,
    Events,
    EventTypes,
)
from homeassistant.const import EntityCategory
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from custom_components.gate_link_hass.adapters.app_client import (
    CHECK_PATH,
    COMMUNITIES_PATH,
    OPEN_PATH,
)
from custom_components.gate_link_hass.const import DOMAIN
from custom_components.gate_link_hass.logbook import async_describe_events
from custom_components.gate_link_hass.login_status import (
    EVENT_LOGIN_CHECK,
    LOGIN_CHECK,
    LoginCheckSource,
    async_check_login,
    async_register_account,
    login_status_unique_id,
)
from custom_components.gate_link_hass.models import (
    AccountSession,
    ApiError,
    AuthenticationExpired,
)


@pytest.fixture
def check_events(hass):
    events = []

    @callback
    def capture(event):
        events.append(event)

    unsubscribe = hass.bus.async_listen(EVENT_LOGIN_CHECK, capture)
    yield events
    unsubscribe()


def status_entity(hass, entry):
    registry = er.async_get(hass)
    return registry.async_get(
        registry.async_get_entity_id("sensor", DOMAIN, login_status_unique_id(entry))
    )


def button_entity(hass, entry):
    return next(
        entity
        for entity in er.async_entries_for_config_entry(
            er.async_get(hass), entry.entry_id
        )
        if entity.domain == "button"
    )


def activity_processor(hass, *, entity_ids=None, device_ids=None):
    config = LogbookConfig({})
    hass.data["logbook"] = config
    async_describe_events(
        hass,
        lambda domain, event, describe: config.external_events.update(
            {event: (domain, describe)}
        ),
    )
    types = async_determine_event_types(hass, entity_ids, device_ids)
    assert EVENT_LOGIN_CHECK in types
    return EventProcessor(hass, types, entity_ids=entity_ids, device_ids=device_ids)


async def test_account_status_is_enabled_diagnostic_and_masks_credentials(
    hass, entry, check_events
):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    entity = status_entity(hass, entry)
    state = hass.states.get(entity.entity_id)
    assert entity.entity_category == EntityCategory.DIAGNOSTIC
    assert entity.disabled_by is None and entity.unique_id == "42:login_status"
    assert state.state == "valid"
    assert (
        state.attributes["last_success_at"]
        == datetime.fromtimestamp(CLOCK, UTC).isoformat()
    )
    assert state.attributes["last_check_source"] == "startup"
    assert len(check_events) == 1
    assert check_events[0].data["entity_id"] == entity.entity_id
    assert check_events[0].data["device_id"] == entity.device_id
    assert async_filter_entities(hass, [entity.entity_id]) == [entity.entity_id]
    exposed = (
        str(state.attributes) + str(check_events[0].data) + str(entry.data[LOGIN_CHECK])
    )
    assert PHONE not in exposed and entry.data["auth"]["session_id"] not in exposed
    assert "****0000" in state.attributes["friendly_name"]
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    "language,expected", [("en", "Sign-in valid"), ("zh-Hans", "登录有效")]
)
async def test_repeated_passes_each_have_localized_activity(
    hass, entry, check_events, clock, language, expected
):
    hass.config.language = language
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    check_events.clear()
    for _ in range(2):
        clock.now += 60
        await entry.runtime_data.check_login()
    await hass.async_block_till_done()
    assert len(check_events) == 2
    assert all(event.data["result"] == "valid" for event in check_events)
    entity = status_entity(hass, entry)
    processor = activity_processor(hass, entity_ids=[entity.entity_id])
    entries = processor.humanify([async_event_to_row(event) for event in check_events])
    assert len(entries) == 2
    assert all(expected in record["message"] for record in entries)
    assert all(record["entity_id"] == entity.entity_id for record in entries)


@pytest.mark.parametrize(
    "response",
    [
        FakeResponse({}, error=TimeoutError()),
        FakeResponse({}, status=503),
        FakeResponse({"code": 0, "data": "bad"}),
    ],
)
async def test_failed_check_has_visible_state_and_recovers_without_reauth(
    hass, entry, check_events, clock, transport, response
):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    check_events.clear()
    original_auth = dict(entry.data["auth"])
    entity = status_entity(hass, entry)
    success_time = hass.states.get(entity.entity_id).attributes["last_success_at"]
    transport.overrides[CHECK_PATH] = response
    clock.now += 60
    with pytest.raises(ApiError):
        await entry.runtime_data.check_login()
    await hass.async_block_till_done()
    state = hass.states.get(entity.entity_id)
    assert (
        state.state == "error" and state.attributes["last_success_at"] == success_time
    )
    assert state.attributes["last_check_at"] != success_time
    assert state.attributes["last_error_reason"]
    assert entry.data["auth"] == original_auth and entry.runtime_data.authenticated()
    assert hass.states.get(button_entity(hass, entry).entity_id).state != "unavailable"
    assert [event.data["result"] for event in check_events] == ["error"]
    persistent_notification.async_create.assert_not_called()
    transport.overrides.clear()
    clock.now += 300
    await entry.runtime_data.check_login(source=LoginCheckSource.RETRY)
    await hass.async_block_till_done()
    assert hass.states.get(entity.entity_id).state == "valid"
    assert hass.states.get(entity.entity_id).attributes["last_error"] is None
    assert [event.data["result"] for event in check_events] == ["error", "valid"]


async def test_open_rejection_records_once_without_a_login_request(
    hass, entry, transport, check_events
):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    transport.calls.clear()
    check_events.clear()
    transport.overrides[OPEN_PATH] = FakeResponse({"code": 401})
    for _ in range(2):
        with pytest.raises(AuthenticationExpired):
            await entry.runtime_data.open("101:1")
    await hass.async_block_till_done()
    assert len(transport.calls) == 1 and transport.calls[0][1].endswith(OPEN_PATH)
    assert len(check_events) == 1 and check_events[0].data["source"] == "open_response"
    assert hass.states.get(status_entity(hass, entry).entity_id).state == "expired"
    assert hass.states.get(button_entity(hass, entry).entity_id).state == "unavailable"
    assert entry.runtime_data._cancel_login_check is None


async def test_options_check_rejection_updates_live_account_and_buttons(
    hass, entry, transport, check_events
):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    check_events.clear()
    transport.overrides[CHECK_PATH] = FakeResponse({"code": 401})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.async_block_till_done()
    assert result["reason"] == "reauth_required"
    assert len(check_events) == 1 and check_events[0].data["source"] == "options_open"
    assert hass.states.get(status_entity(hass, entry).entity_id).state == "expired"
    assert hass.states.get(button_entity(hass, entry).entity_id).state == "unavailable"
    assert entry.runtime_data._cancel_login_check is None


async def test_scheduled_failure_and_retry_record_their_actual_source(
    hass, entry, transport, check_events, clock, monkeypatch
):
    tracker = Mock(return_value=Mock())
    monkeypatch.setattr(
        "custom_components.gate_link_hass.account.async_track_point_in_utc_time",
        tracker,
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    check_events.clear()
    original_success = entry.data["auth"]["checked_at"]
    _, scheduled, when = tracker.call_args.args
    assert when == datetime(2027, 1, 15, 22, tzinfo=UTC)
    clock.now = when.timestamp()
    transport.overrides[CHECK_PATH] = FakeResponse({}, error=TimeoutError())
    await scheduled(when)
    await hass.async_block_till_done()
    assert entry.data["auth"]["checked_at"] == original_success
    assert hass.states.get(status_entity(hass, entry).entity_id).state == "error"
    _, retry, when = tracker.call_args.args
    assert when.timestamp() == clock.now + 300
    clock.now = when.timestamp()
    transport.overrides.clear()
    await retry(when)
    await hass.async_block_till_done()
    assert [(event.data["source"], event.data["result"]) for event in check_events] == [
        ("daily", "error"),
        ("retry", "valid"),
    ]
    assert hass.states.get(status_entity(hass, entry).entity_id).state == "valid"


async def test_options_open_and_save_each_record_success(hass, entry, check_events):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    check_events.clear()
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == "form"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"selected_gates": ["101:1"], "timeout": 10}
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert [event.data["source"] for event in check_events] == [
        "options_open",
        "options_save",
        "startup",
    ]
    assert all(event.data["result"] == "valid" for event in check_events)


@pytest.mark.parametrize(
    "path,expected", [(CHECK_PATH, "error"), (COMMUNITIES_PATH, "valid")]
)
async def test_startup_temporary_failure_keeps_diagnostics_and_cached_bindings(
    hass, entry, transport, monkeypatch, check_events, path, expected
):
    tracker = Mock(return_value=Mock())
    monkeypatch.setattr(
        "custom_components.gate_link_hass.account.async_track_point_in_utc_time",
        tracker,
    )
    transport.overrides[path] = FakeResponse({}, error=TimeoutError())
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(status_entity(hass, entry).entity_id).state == expected
    assert hass.states.get(button_entity(hass, entry).entity_id).state != "unavailable"
    assert len(check_events) == 1 and check_events[0].data["result"] == expected
    assert tracker.call_args.args[2].timestamp() == CLOCK + 300
    assert entry.runtime_data.gates["101:1"].name == "入口"
    assert not entry.data["invalidated"]


async def test_persisted_expiry_loads_without_rechecking_or_duplicate_activity(
    hass, entry, transport, check_events
):
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "invalidated": True}
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert not transport.calls and not check_events
    assert hass.states.get(status_entity(hass, entry).entity_id).state == "expired"
    persistent_notification.async_create.assert_called_once()
    persistent_notification.async_dismiss.assert_not_called()


async def test_reauth_keeps_status_entity_and_device_and_restores_buttons(
    hass, entry, check_events
):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    original = status_entity(hass, entry)
    button = button_entity(hass, entry)
    entry.runtime_data.invalidate()
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": "reauth",
            "entry_id": entry.entry_id,
            "unique_id": entry.unique_id,
        },
        data=dict(entry.data),
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"phone": PHONE}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"verification_code": "012345"}
    )
    await hass.async_block_till_done()
    assert result["reason"] == "reauth_successful"
    current = status_entity(hass, entry)
    assert (current.entity_id, current.device_id, current.unique_id) == (
        original.entity_id,
        original.device_id,
        original.unique_id,
    )
    assert hass.states.get(current.entity_id).state == "valid"
    assert hass.states.get(button.entity_id).state != "unavailable"
    assert any(event.data["source"] == "reauth" for event in check_events)


async def test_rename_and_unbinding_keep_account_and_use_real_entity_id(
    hass, entry, check_events
):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    original = status_entity(hass, entry)
    registry = er.async_get(hass)
    registry.async_update_entity(
        original.entity_id, new_entity_id="sensor.my_gate_account", name="我的账号状态"
    )
    dr.async_get(hass).async_update_device(
        original.device_id, name_by_user="我的门禁账号"
    )
    await hass.async_block_till_done()
    check_events.clear()
    await entry.runtime_data.check_login()
    await hass.async_block_till_done()
    assert check_events[0].data["entity_id"] == "sensor.my_gate_account"
    assert check_events[0].data["device_id"] == original.device_id
    assert (
        hass.states.get("sensor.my_gate_account").attributes["friendly_name"]
        == "我的账号状态"
    )
    removed_button = button_entity(hass, entry)
    # Startup queries return this gate; replacing the selection tests real cleanup.
    gate = dict(entry.data["gates"][0], door_id="2", name="常用门")
    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, "gates": [*entry.data["gates"], gate]},
        options={**entry.options, "selected_gates": ["101:2"]},
    )
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert registry.async_get(removed_button.entity_id) is None
    current = status_entity(hass, entry)
    assert (
        current.entity_id == "sensor.my_gate_account"
        and current.device_id == original.device_id
    )
    assert (
        dr.async_get(hass).async_get(current.device_id).name_by_user == "我的门禁账号"
    )
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert registry.async_get(current.entity_id) is None
    assert dr.async_get(hass).async_get(current.device_id) is None


@pytest.mark.parametrize("filter_kind", ["entity", "device"])
async def test_activity_live_and_persisted_queries_isolate_accounts(
    hass, entry, check_events, tmp_path, filter_kind
):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    other = make_entry(auth=AccountSession("99", "fixture-other", CLOCK, None))
    hass.config_entries._entries[other.entry_id] = other
    assert await hass.config_entries.async_setup(other.entry_id)
    await hass.async_block_till_done()
    target = status_entity(hass, entry)
    other_status = status_entity(hass, other)
    assert (
        target.entity_id != other_status.entity_id
        and target.device_id != other_status.device_id
    )
    filters = (
        {"entity_ids": [target.entity_id]}
        if filter_kind == "entity"
        else {"device_ids": [target.device_id]}
    )
    processor = activity_processor(hass, **filters)
    forwarded = []
    forward = event_forwarder_filtered(
        forwarded.append, None, filters.get("entity_ids"), filters.get("device_ids")
    )
    for event in check_events:
        forward(event)
    assert len(forwarded) == 1 and forwarded[0].data["entity_id"] == target.entity_id
    # Persist genuine events using Recorder's schema, then reopen the database:
    # this checks HA's actual historical filters, not only our event payload.
    url = f"sqlite:///{tmp_path / 'activity.db'}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        event_type = EventTypes(event_type=EVENT_LOGIN_CHECK)
        for event in check_events:
            row = Events.from_event(event)
            row.event_type_rel = event_type
            row.event_data_rel = EventData(shared_data=json.dumps(dict(event.data)))
            session.add(row)
        session.commit()
        type_id = event_type.event_type_id
    engine.dispose()
    engine = create_engine(url)
    try:
        now = datetime.now(UTC)
        statement = statement_for_request(
            now - timedelta(hours=1), now + timedelta(hours=1), (type_id,), **filters
        )
        with Session(engine) as session:
            history = processor.humanify(session.execute(statement).all())
        assert len(history) == 1 and history[0]["entity_id"] == target.entity_id
        assert "Sign-in valid" in history[0]["message"]
    finally:
        engine.dispose()


async def test_cancelled_check_has_no_false_success_or_record(
    hass, entry, client, check_events
):
    client.check_login = AsyncMock(side_effect=asyncio.CancelledError)
    with pytest.raises(asyncio.CancelledError):
        await async_check_login(hass, entry, client, LoginCheckSource.MANUAL)
    await hass.async_block_till_done()
    assert not check_events and LOGIN_CHECK not in entry.data


async def test_unknown_error_key_and_initial_setup_do_not_expose_credentials(
    hass, entry, client, check_events
):
    await async_check_login(hass, None, client, LoginCheckSource.MANUAL)
    assert not check_events and not er.async_get(hass).entities
    client.check_login = AsyncMock(side_effect=ApiError(client.auth.session_id + PHONE))
    with pytest.raises(ApiError):
        await async_check_login(hass, entry, client, LoginCheckSource.MANUAL)
    await hass.async_block_till_done()
    assert (
        len(check_events) == 1 and check_events[0].data["error"] == "request_rejected"
    )
    assert PHONE not in str(check_events[0].data) and client.auth.session_id not in str(
        check_events[0].data
    )
    assert (
        async_register_account(hass, entry).entity_id
        == check_events[0].data["entity_id"]
    )
