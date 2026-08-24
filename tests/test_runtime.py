"""Runtime behaviour: connector state, repair issues, connector manager, removal,
the "Laris" pipeline and the dormant prompt."""
from __future__ import annotations

from typing import Any

from freezegun.api import FrozenDateTimeFactory
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from homeassistant.components import assist_pipeline, conversation, webhook
from homeassistant.components.assist_pipeline.pipeline import KEY_ASSIST_PIPELINE
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component

from custom_components.homapel_conversation.connector import (
    async_provision_cloud_credential,
)
from custom_components.homapel_conversation.const import (
    CLOUD_USER_NAME,
    CONF_API_BASE,
    CONF_API_KEY,
    CONF_CLOUD_REFRESH_TOKEN_ID,
    CONF_CLOUD_USER_ID,
    CONF_CONNECTOR_BASE_URL,
    CONF_CONNECTOR_SOURCE,
    CONF_DEFAULT_LANGUAGE,
    CONF_MCP_ENTRY_ID,
    CONF_MCP_WEBHOOK_ID,
    CONF_PIPELINE_CREATED,
    CONF_UNIT_ID,
    CONNECTOR_SOURCE_EXTERNAL_URL,
    DOMAIN,
    DORMANT_PROMPT,
    ERROR_SPEECH,
    ISSUE_HOME_NOT_CONNECTED,
    ISSUE_HOME_UNREACHABLE,
    MCP_DOMAIN,
    PIPELINE_NAME,
    POLL_INTERVAL,
    UNREACHABLE_GRACE,
)
from custom_components.homapel_conversation.coordinator import HomapelCoordinator

from .conftest import (
    API_BASE,
    API_KEY,
    EXTERNAL_URL,
    MCP_WEBHOOK_ID,
    UNIT_ID,
    CloudMock,
    status_payload,
)

HOME_CONNECTED = "binary_sensor.homapel_aris_home_connected"
CONVERSATION_AGENT = "conversation.homapel_aris_homapel"
STT_ENTITY = "stt.homapel_aris_homapel"
TTS_ENTITY = "tts.homapel_aris_homapel"


# --- helpers ------------------------------------------------------------------


def _connected_entry(hass: HomeAssistant, mcp_entry: MockConfigEntry, **extra: Any) -> MockConfigEntry:
    """An entry as the config flow leaves it after a successful connector step."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=UNIT_ID,
        title=f"Homapel ({UNIT_ID})",
        data={
            CONF_API_BASE: API_BASE,
            CONF_API_KEY: API_KEY,
            CONF_UNIT_ID: UNIT_ID,
            CONF_DEFAULT_LANGUAGE: "tr",
            CONF_CONNECTOR_SOURCE: CONNECTOR_SOURCE_EXTERNAL_URL,
            CONF_CONNECTOR_BASE_URL: EXTERNAL_URL,
            CONF_MCP_ENTRY_ID: mcp_entry.entry_id,
            CONF_MCP_WEBHOOK_ID: MCP_WEBHOOK_ID,
            **extra,
        },
    )
    entry.add_to_hass(hass)
    return entry


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> HomapelCoordinator:
    """Set the entry up and let the connector manager's background re-probe finish."""
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.state is ConfigEntryState.LOADED
    return hass.data[DOMAIN][entry.entry_id]


async def _refresh(hass: HomeAssistant, coordinator: HomapelCoordinator) -> None:
    await coordinator.async_refresh()
    await hass.async_block_till_done()


def _issue(hass: HomeAssistant, issue_id: str) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, issue_id)


async def _poll(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    """Advance time by one poll interval and let the coordinator poll."""
    freezer.tick(POLL_INTERVAL)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


def _laris_pipelines(hass: HomeAssistant) -> list[assist_pipeline.Pipeline]:
    return [p for p in assist_pipeline.async_get_pipelines(hass) if p.name == PIPELINE_NAME]


async def _mcp_webhook_handler(hass: HomeAssistant, webhook_id: str, request: Any) -> Any:
    return None


# --- 1. coordinator → binary sensor -------------------------------------------


async def test_home_connected_on_when_cloud_reports_reachable(
    hass: HomeAssistant, cloud: CloudMock, config_entry: MockConfigEntry
) -> None:
    cloud.status = status_payload(connector={"configured": True, "reachable": True})
    coordinator = await _setup(hass, config_entry)

    assert coordinator.data.connector_configured is True
    assert coordinator.data.connector_reachable is True
    state = hass.states.get(HOME_CONNECTED)
    assert state is not None
    assert state.state == "on"
    assert state.attributes["configured"] is True
    # This entry has no connector of its own (cloud-reported only).
    assert state.attributes["source"] is None
    assert state.attributes["base_url"] is None
    assert state.attributes["unreachable_since"] is None


async def test_home_connected_off_when_configured_but_unreachable(
    hass: HomeAssistant, cloud: CloudMock, mcp_entry: MockConfigEntry
) -> None:
    # "off" is only claimed for a connector we registered — see
    # test_legacy_home_never_raises_unreachable for the other case.
    cloud.status = status_payload(connector={"configured": True, "reachable": False})
    # Our connector re-probes on setup, so the PUT must agree with the poll.
    cloud.connector_response = {
        "reachable": False,
        "checked_at": "2026-08-24T00:00:00Z",
        "error": "timeout",
    }
    config_entry = _connected_entry(hass, mcp_entry)
    coordinator = await _setup(hass, config_entry)

    assert coordinator.data.connector_reachable is False
    state = hass.states.get(HOME_CONNECTED)
    assert state.state == "off"
    assert state.attributes["configured"] is True
    assert state.attributes["unreachable_since"] is not None


async def test_home_connected_unknown_without_connector_block(
    hass: HomeAssistant, cloud: CloudMock, config_entry: MockConfigEntry
) -> None:
    cloud.status = status_payload()  # older cloud: no connector block
    coordinator = await _setup(hass, config_entry)

    assert coordinator.data.connector_configured is None
    assert coordinator.data.connector_reachable is None
    assert hass.states.get(HOME_CONNECTED).state == "unknown"


async def test_home_connected_attributes_follow_entry_connector(
    hass: HomeAssistant, cloud: CloudMock, mcp_entry: MockConfigEntry
) -> None:
    cloud.status = status_payload(connector={"configured": True, "reachable": True})
    entry = _connected_entry(hass, mcp_entry)
    await _setup(hass, entry)

    state = hass.states.get(HOME_CONNECTED)
    assert state.state == "on"
    assert state.attributes["source"] == CONNECTOR_SOURCE_EXTERNAL_URL
    assert state.attributes["base_url"] == EXTERNAL_URL


async def test_home_connected_follows_status_changes(
    hass: HomeAssistant, cloud: CloudMock, mcp_entry: MockConfigEntry
) -> None:
    cloud.status = status_payload(connector={"configured": True, "reachable": True})
    coordinator = await _setup(hass, _connected_entry(hass, mcp_entry))
    assert hass.states.get(HOME_CONNECTED).state == "on"

    cloud.status = status_payload(connector={"configured": True, "reachable": False})
    await _refresh(hass, coordinator)
    assert hass.states.get(HOME_CONNECTED).state == "off"

    cloud.status = status_payload(connector={"configured": False, "reachable": False})
    await _refresh(hass, coordinator)
    assert hass.states.get(HOME_CONNECTED).state == "off"


# --- 2. repair issue: home_not_connected --------------------------------------


async def test_not_connected_issue_when_cloud_reports_unconfigured(
    hass: HomeAssistant, cloud: CloudMock, config_entry: MockConfigEntry
) -> None:
    cloud.status = status_payload(connector={"configured": False, "reachable": False})
    coordinator = await _setup(hass, config_entry)

    issue = _issue(hass, ISSUE_HOME_NOT_CONNECTED)
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.translation_key == ISSUE_HOME_NOT_CONNECTED
    assert _issue(hass, ISSUE_HOME_UNREACHABLE) is None

    cloud.status = status_payload(connector={"configured": True, "reachable": True})
    await _refresh(hass, coordinator)
    assert _issue(hass, ISSUE_HOME_NOT_CONNECTED) is None


async def test_no_issue_when_block_absent_on_an_older_cloud(
    hass: HomeAssistant, cloud: CloudMock, config_entry: MockConfigEntry
) -> None:
    """A cloud that reports no connector block says nothing about the home.

    Legacy installs (Homapel-run tunnel, no connector keys in entry.data) are
    reachable and must not be told to reconfigure.
    """
    cloud.status = status_payload()
    await _setup(hass, config_entry)

    assert _issue(hass, ISSUE_HOME_NOT_CONNECTED) is None
    assert _issue(hass, ISSUE_HOME_UNREACHABLE) is None


async def test_no_not_connected_issue_when_block_absent_but_entry_connected(
    hass: HomeAssistant, cloud: CloudMock, mcp_entry: MockConfigEntry
) -> None:
    cloud.status = status_payload()
    entry = _connected_entry(hass, mcp_entry)
    await _setup(hass, entry)

    assert _issue(hass, ISSUE_HOME_NOT_CONNECTED) is None


async def test_not_connected_issue_cleared_on_configured_status(
    hass: HomeAssistant, cloud: CloudMock, config_entry: MockConfigEntry
) -> None:
    cloud.status = status_payload(connector={"configured": True, "reachable": True})
    coordinator = await _setup(hass, config_entry)
    assert _issue(hass, ISSUE_HOME_NOT_CONNECTED) is None

    cloud.status = status_payload(connector={"configured": False, "reachable": False})
    await _refresh(hass, coordinator)
    assert _issue(hass, ISSUE_HOME_NOT_CONNECTED) is not None

    cloud.status = status_payload(connector={"configured": True, "reachable": True})
    await _refresh(hass, coordinator)
    assert _issue(hass, ISSUE_HOME_NOT_CONNECTED) is None


# --- 3. repair issue: home_unreachable (after the grace period) ---------------


async def test_unreachable_issue_only_after_grace_period(
    hass: HomeAssistant,
    cloud: CloudMock,
    mcp_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    cloud.status = status_payload(connector={"configured": True, "reachable": False})
    # Our connector re-probes on setup, so the PUT must agree with the poll.
    cloud.connector_response = {
        "reachable": False,
        "checked_at": "2026-08-24T00:00:00Z",
        "error": "timeout",
    }
    coordinator = await _setup(hass, _connected_entry(hass, mcp_entry))
    assert coordinator.data.connector_unreachable_since is not None
    since = coordinator.data.connector_unreachable_since
    assert _issue(hass, ISSUE_HOME_UNREACHABLE) is None
    assert _issue(hass, ISSUE_HOME_NOT_CONNECTED) is None
    assert len(cloud.calls("GET", "/v1/units/status")) == 1

    # 5 and 10 minutes in: still inside the grace period.
    await _poll(hass, freezer)
    assert len(cloud.calls("GET", "/v1/units/status")) == 2
    assert _issue(hass, ISSUE_HOME_UNREACHABLE) is None
    await _poll(hass, freezer)
    assert len(cloud.calls("GET", "/v1/units/status")) == 3
    assert _issue(hass, ISSUE_HOME_UNREACHABLE) is None
    # first-seen timestamp is carried across polls, not reset
    assert coordinator.data.connector_unreachable_since == since

    # 15 minutes in: the issue is raised.
    assert 3 * POLL_INTERVAL >= UNREACHABLE_GRACE
    await _poll(hass, freezer)
    issue = _issue(hass, ISSUE_HOME_UNREACHABLE)
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.ERROR
    assert issue.translation_key == ISSUE_HOME_UNREACHABLE
    assert hass.states.get(HOME_CONNECTED).state == "off"

    # Reachable again → cleared immediately, timestamp reset.
    cloud.status = status_payload(connector={"configured": True, "reachable": True})
    await _poll(hass, freezer)
    assert _issue(hass, ISSUE_HOME_UNREACHABLE) is None
    assert coordinator.data.connector_unreachable_since is None
    assert hass.states.get(HOME_CONNECTED).state == "on"


async def test_unreachable_issue_not_raised_when_unconfigured(
    hass: HomeAssistant,
    cloud: CloudMock,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """An unconfigured connector is "not connected", never "unreachable"."""
    cloud.status = status_payload(connector={"configured": False, "reachable": False})
    await _setup(hass, config_entry)
    for _ in range(4):
        await _poll(hass, freezer)
    assert _issue(hass, ISSUE_HOME_UNREACHABLE) is None
    assert _issue(hass, ISSUE_HOME_NOT_CONNECTED) is not None


# --- 4 / 5. ConnectorManager ---------------------------------------------------


async def test_connector_manager_reprobes_on_setup(
    hass: HomeAssistant, cloud: CloudMock, mcp_entry: MockConfigEntry
) -> None:
    cloud.status = status_payload(connector={"configured": True, "reachable": True})
    cloud.connector_response = {
        "reachable": False,
        "checked_at": "2026-08-23T00:00:01Z",
        "error": "connect timeout",
    }
    entry = _connected_entry(hass, mcp_entry)
    coordinator = await _setup(hass, entry)

    put = cloud.calls("PUT", "/v1/units/connector")
    assert len(put) == 1
    body = put[0][2]
    assert body["mcp_url"] == f"{EXTERNAL_URL}/api/webhook/{MCP_WEBHOOK_ID}"
    assert body["source"] == CONNECTOR_SOURCE_EXTERNAL_URL
    assert body["ha_version"] == HA_VERSION
    assert body["bearer"].count(".") == 2  # a JWT, not a refresh token
    refresh = hass.auth.async_validate_access_token(body["bearer"])
    assert refresh is not None
    assert refresh.user.name == CLOUD_USER_NAME
    assert put[0][3]["Authorization"] == f"Bearer {API_KEY}"

    # The PUT response (not the earlier /units/status) is what we now believe.
    assert coordinator.data.connector_configured is True
    assert coordinator.data.connector_reachable is False
    assert hass.states.get(HOME_CONNECTED).state == "off"

    # The ids of the freshly provisioned credential are persisted, the token is not.
    assert entry.data[CONF_CLOUD_USER_ID] == refresh.user.id
    assert entry.data[CONF_CLOUD_REFRESH_TOKEN_ID] == refresh.id
    assert body["bearer"] not in str(entry.data)


async def test_connector_manager_reregisters_on_webhook_id_change(
    hass: HomeAssistant, cloud: CloudMock, mcp_entry: MockConfigEntry
) -> None:
    cloud.status = status_payload(connector={"configured": True, "reachable": True})
    entry = _connected_entry(hass, mcp_entry)
    await _setup(hass, entry)
    assert len(cloud.calls("PUT", "/v1/units/connector")) == 1

    # ha-mcp regenerates its webhook id. The fake integration only registers
    # its webhook on setup, so bring the new endpoint up by hand first.
    new_id = "mcp_new"
    webhook.async_register(hass, MCP_DOMAIN, "HA-MCP", new_id, _mcp_webhook_handler)
    hass.config_entries.async_update_entry(
        mcp_entry, data={**mcp_entry.data, "webhook_id": new_id}
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    put = cloud.calls("PUT", "/v1/units/connector")
    assert len(put) == 2
    assert put[1][2]["mcp_url"] == f"{EXTERNAL_URL}/api/webhook/{new_id}"
    assert put[1][2]["source"] == CONNECTOR_SOURCE_EXTERNAL_URL
    assert entry.data[CONF_MCP_WEBHOOK_ID] == new_id
    assert entry.data[CONF_MCP_ENTRY_ID] == mcp_entry.entry_id


async def test_connector_manager_ignores_unrelated_entry_updates(
    hass: HomeAssistant, cloud: CloudMock, mcp_entry: MockConfigEntry
) -> None:
    cloud.status = status_payload(connector={"configured": True, "reachable": True})
    entry = _connected_entry(hass, mcp_entry)
    await _setup(hass, entry)
    assert len(cloud.calls("PUT", "/v1/units/connector")) == 1

    # Same webhook id, other data changed → nothing to re-register.
    hass.config_entries.async_update_entry(mcp_entry, data={**mcp_entry.data, "other": 1})
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(cloud.calls("PUT", "/v1/units/connector")) == 1


async def test_connector_manager_idle_without_connector(
    hass: HomeAssistant, cloud: CloudMock, config_entry: MockConfigEntry, mcp_entry: MockConfigEntry
) -> None:
    """"Skip for now" entries never touch /units/connector, even with ha-mcp present."""
    cloud.status = status_payload(connector={"configured": False, "reachable": False})
    coordinator = await _setup(hass, config_entry)

    assert cloud.calls("PUT", "/v1/units/connector") == []
    assert coordinator.connector_manager is not None
    assert coordinator.connector_manager.configured is False

    # A webhook-id change on ha-mcp does not wake it up either.
    hass.config_entries.async_update_entry(
        mcp_entry, data={**mcp_entry.data, "webhook_id": "mcp_other"}
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert cloud.calls("PUT", "/v1/units/connector") == []
    assert [u for u in await hass.auth.async_get_users() if u.name == CLOUD_USER_NAME] == []


# --- 6. the "Laris Cloud" credential -------------------------------------------


async def test_cloud_bearer_is_accepted_by_ha_auth_gate(
    hass: HomeAssistant, cloud: CloudMock, mcp_entry: MockConfigEntry
) -> None:
    cloud.status = status_payload(connector={"configured": True, "reachable": True})
    entry = _connected_entry(hass, mcp_entry)
    await _setup(hass, entry)

    bearer = cloud.calls("PUT", "/v1/units/connector")[0][2]["bearer"]
    refresh = hass.auth.async_validate_access_token(bearer)
    assert refresh is not None
    user = refresh.user
    # Exactly what ha-mcp's ha_auth mode checks before it serves a request.
    assert user.is_active
    assert user.is_admin
    assert not user.system_generated
    assert user.name == CLOUD_USER_NAME
    # The cloud dials in from the internet.
    assert not user.local_only


async def test_provisioning_reuses_stored_credential(
    hass: HomeAssistant, cloud: CloudMock, mcp_entry: MockConfigEntry
) -> None:
    cloud.status = status_payload(connector={"configured": True, "reachable": True})
    first = await async_provision_cloud_credential(hass)
    entry = _connected_entry(
        hass,
        mcp_entry,
        **{
            CONF_CLOUD_USER_ID: first.user_id,
            CONF_CLOUD_REFRESH_TOKEN_ID: first.refresh_token_id,
        },
    )
    coordinator = await _setup(hass, entry)

    # The manager's re-probe minted a new access token from the *same* refresh token.
    put = cloud.calls("PUT", "/v1/units/connector")
    assert len(put) == 1
    refresh = hass.auth.async_validate_access_token(put[0][2]["bearer"])
    assert refresh is not None
    assert refresh.id == first.refresh_token_id
    assert refresh.user.id == first.user_id
    assert entry.data[CONF_CLOUD_USER_ID] == first.user_id
    assert entry.data[CONF_CLOUD_REFRESH_TOKEN_ID] == first.refresh_token_id

    # Explicit re-provisioning with the stored ids: same user, same refresh token.
    again = await async_provision_cloud_credential(
        hass, user_id=first.user_id, refresh_token_id=first.refresh_token_id
    )
    assert again.user_id == first.user_id
    assert again.refresh_token_id == first.refresh_token_id

    # And another manager re-probe changes nothing.
    manager = coordinator.connector_manager
    assert manager is not None
    registration = await manager.async_reprobe()
    assert registration is not None
    assert registration.credential.user_id == first.user_id
    assert registration.credential.refresh_token_id == first.refresh_token_id
    assert len(cloud.calls("PUT", "/v1/units/connector")) == 2

    users = [u for u in await hass.auth.async_get_users() if u.name == CLOUD_USER_NAME]
    assert len(users) == 1
    tokens = [t for t in users[0].refresh_tokens.values() if t.client_name == CLOUD_USER_NAME]
    assert len(tokens) == 1


# --- 7. entry removal ----------------------------------------------------------


async def test_remove_entry_clears_connector_and_credential(
    hass: HomeAssistant, cloud: CloudMock, mcp_entry: MockConfigEntry
) -> None:
    cloud.status = status_payload(connector={"configured": True, "reachable": True})
    entry = _connected_entry(hass, mcp_entry)
    await _setup(hass, entry)

    user_id = entry.data[CONF_CLOUD_USER_ID]
    token_id = entry.data[CONF_CLOUD_REFRESH_TOKEN_ID]
    bearer = cloud.calls("PUT", "/v1/units/connector")[0][2]["bearer"]
    assert await hass.auth.async_get_user(user_id) is not None
    assert hass.auth.async_get_refresh_token(token_id) is not None

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    delete = cloud.calls("DELETE", "/v1/units/connector")
    assert len(delete) == 1
    assert delete[0][3]["Authorization"] == f"Bearer {API_KEY}"
    assert hass.auth.async_get_refresh_token(token_id) is None
    assert await hass.auth.async_get_user(user_id) is None
    assert hass.auth.async_validate_access_token(bearer) is None
    assert [u for u in await hass.auth.async_get_users() if u.name == CLOUD_USER_NAME] == []
    assert hass.config_entries.async_get_entry(entry.entry_id) is None


async def test_remove_entry_without_connector_still_tells_cloud(
    hass: HomeAssistant, cloud: CloudMock, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)
    await hass.config_entries.async_remove(config_entry.entry_id)
    await hass.async_block_till_done()
    assert len(cloud.calls("DELETE", "/v1/units/connector")) == 1


# --- 8. the "Laris" Assist pipeline -------------------------------------------


async def test_laris_pipeline_created_once_and_preferred(
    hass: HomeAssistant, cloud: CloudMock, config_entry: MockConfigEntry
) -> None:
    hass.config.language = "tr"
    assert await async_setup_component(hass, "assist_pipeline", {})
    await _setup(hass, config_entry)

    pipelines = _laris_pipelines(hass)
    assert len(pipelines) == 1
    pipeline = pipelines[0]
    assert pipeline.conversation_engine == CONVERSATION_AGENT
    assert pipeline.stt_engine == STT_ENTITY
    assert pipeline.tts_engine == TTS_ENTITY
    assert pipeline.language == "tr"
    assert pipeline.conversation_language == "tr"
    assert pipeline.stt_language == "tr-TR"
    assert pipeline.tts_language == "tr-TR"
    assert pipeline.tts_voice == "tr-voice"
    assert pipeline.wake_word_entity is None
    assert assist_pipeline.async_get_pipeline(hass).id == pipeline.id
    assert config_entry.data[CONF_PIPELINE_CREATED] is True

    # A reload (or restart) never creates a second one.
    await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert config_entry.state is ConfigEntryState.LOADED
    assert [p.id for p in _laris_pipelines(hass)] == [pipeline.id]


async def test_laris_pipeline_english_home(
    hass: HomeAssistant, cloud: CloudMock, config_entry: MockConfigEntry
) -> None:
    assert hass.config.language == "en"
    assert await async_setup_component(hass, "assist_pipeline", {})
    await _setup(hass, config_entry)

    (pipeline,) = _laris_pipelines(hass)
    assert pipeline.conversation_language == "en"
    assert pipeline.stt_language == "en-US"
    assert pipeline.tts_language == "en-US"
    assert pipeline.tts_voice == "en-voice"


async def test_existing_laris_pipeline_adopted_untouched(
    hass: HomeAssistant, cloud: CloudMock, config_entry: MockConfigEntry
) -> None:
    assert await async_setup_component(hass, "assist_pipeline", {})
    store = hass.data[KEY_ASSIST_PIPELINE].pipeline_store
    existing = await store.async_create_item(
        {
            "conversation_engine": conversation.HOME_ASSISTANT_AGENT,
            "conversation_language": "en",
            "language": "en",
            "name": PIPELINE_NAME,
            "stt_engine": None,
            "stt_language": None,
            "tts_engine": None,
            "tts_language": None,
            "tts_voice": None,
            "wake_word_entity": None,
            "wake_word_id": None,
        }
    )
    preferred_before = store.async_get_preferred_item()
    assert preferred_before != existing.id

    await _setup(hass, config_entry)

    (pipeline,) = _laris_pipelines(hass)
    assert pipeline.id == existing.id
    assert pipeline.conversation_engine == conversation.HOME_ASSISTANT_AGENT
    assert pipeline.stt_engine is None
    assert pipeline.tts_engine is None
    assert store.async_get_preferred_item() == preferred_before
    assert config_entry.data[CONF_PIPELINE_CREATED] is True


async def test_deleted_laris_pipeline_not_recreated(
    hass: HomeAssistant, cloud: CloudMock, config_entry: MockConfigEntry
) -> None:
    assert await async_setup_component(hass, "assist_pipeline", {})
    await _setup(hass, config_entry)
    (pipeline,) = _laris_pipelines(hass)

    store = hass.data[KEY_ASSIST_PIPELINE].pipeline_store
    # The preferred pipeline cannot be deleted: the customer first makes
    # another one preferred (HA's own default pipeline here), then deletes Laris.
    other = next(p for p in store.async_items() if p.id != pipeline.id)
    store.async_set_preferred_item(other.id)
    await store.async_delete_item(pipeline.id)
    await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert _laris_pipelines(hass) == []
    assert config_entry.data[CONF_PIPELINE_CREATED] is True


async def test_no_pipeline_without_voice(
    hass: HomeAssistant, cloud: CloudMock, config_entry: MockConfigEntry
) -> None:
    cloud.status = status_payload(voice=False)
    assert await async_setup_component(hass, "assist_pipeline", {})
    await _setup(hass, config_entry)

    assert hass.states.get(CONVERSATION_AGENT) is not None
    assert hass.states.get(STT_ENTITY) is None
    assert hass.states.get(TTS_ENTITY) is None
    assert _laris_pipelines(hass) == []
    assert CONF_PIPELINE_CREATED not in config_entry.data


async def test_no_pipeline_without_assist_pipeline_loaded(
    hass: HomeAssistant, cloud: CloudMock, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)
    assert KEY_ASSIST_PIPELINE not in hass.data
    assert CONF_PIPELINE_CREATED not in config_entry.data


# --- 9. dormant prompt ----------------------------------------------------------


async def test_dormant_prompt_turkish_without_cloud_call(
    hass: HomeAssistant, cloud: CloudMock, config_entry: MockConfigEntry
) -> None:
    cloud.status = status_payload(active=False, tier="dormant")
    await _setup(hass, config_entry)
    assert hass.states.get("binary_sensor.homapel_aris_active").state == "off"

    result = await conversation.async_converse(
        hass,
        text="merhaba",
        conversation_id=None,
        context=Context(),
        language="tr",
        agent_id=CONVERSATION_AGENT,
    )

    assert result.response.speech["plain"]["speech"] == DORMANT_PROMPT["tr"]
    assert result.continue_conversation is False
    assert result.conversation_id
    assert cloud.calls("POST", "/v1/converse") == []
    assert all(
        "/v1/converse" not in str(call[1]) for call in cloud.mocker.mock_calls
    )


async def test_dormant_prompt_english_without_cloud_call(
    hass: HomeAssistant, cloud: CloudMock, config_entry: MockConfigEntry
) -> None:
    cloud.status = status_payload(active=False, tier="dormant")
    await _setup(hass, config_entry)

    result = await conversation.async_converse(
        hass,
        text="hello",
        conversation_id=None,
        context=Context(),
        language="en-US",
        agent_id=CONVERSATION_AGENT,
    )

    assert result.response.speech["plain"]["speech"] == DORMANT_PROMPT["en"]
    assert result.continue_conversation is False
    assert cloud.calls("POST", "/v1/converse") == []


async def test_dormant_prompt_falls_back_to_default_language(
    hass: HomeAssistant, cloud: CloudMock, config_entry: MockConfigEntry
) -> None:
    """An unsupported language gets the entry's default (tr) prompt, still offline."""
    cloud.status = status_payload(active=False, tier="dormant")
    await _setup(hass, config_entry)

    result = await conversation.async_converse(
        hass,
        text="hallo",
        conversation_id=None,
        context=Context(),
        language="de",
        agent_id=CONVERSATION_AGENT,
    )
    assert result.response.speech["plain"]["speech"] == DORMANT_PROMPT["tr"]
    assert cloud.calls("POST", "/v1/converse") == []


# --- 10. customer-facing copy ----------------------------------------------------


def test_prompts_point_at_the_laris_dashboard() -> None:
    for lang in ("tr", "en"):
        for text in (DORMANT_PROMPT[lang], ERROR_SPEECH["auth"][lang]):
            assert "laris.homapel.com" in text
            # no bare homapel.com (only as part of laris.homapel.com)
            assert text.replace("laris.homapel.com", "") .find("homapel.com") == -1
            lowered = text.lower()
            assert "activation code" not in lowered
            assert "aktivasyon kodu" not in lowered
            assert "etkinleştirme kodu" not in lowered
    assert "abonel" in DORMANT_PROMPT["tr"].lower()  # "aboneliğinizi" (k→ğ mutation)
    assert "subscription" in DORMANT_PROMPT["en"].lower()


async def test_existing_homapel_pipeline_adopted_by_engine(
    hass: HomeAssistant, cloud: CloudMock, config_entry: MockConfigEntry
) -> None:
    """A pre-0.5 install has a hand-made pipeline under any name.

    Reproduces a real 0.4.0 install: one preferred pipeline named "Homapel",
    Homapel end-to-end, prefer_local_intents on. Upgrading must not create a
    second pipeline or move the preferred flag.
    """
    assert await async_setup_component(hass, "assist_pipeline", {})
    await _setup(hass, config_entry)
    # Setup created "Laris"; simulate the older install by renaming it and
    # clearing the flag, then reload.
    store = hass.data[KEY_ASSIST_PIPELINE].pipeline_store
    (laris,) = _laris_pipelines(hass)
    updates = {k: v for k, v in laris.to_json().items() if k != "id"}
    await store.async_update_item(
        laris.id, {**updates, "name": "Homapel", "prefer_local_intents": True}
    )
    store.async_set_preferred_item(laris.id)
    hass.config_entries.async_update_entry(
        config_entry,
        data={k: v for k, v in config_entry.data.items() if k != CONF_PIPELINE_CREATED},
    )
    before = {p.id for p in store.async_items()}

    await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert {p.id for p in store.async_items()} == before, "a duplicate pipeline was created"
    assert _laris_pipelines(hass) == []
    adopted = next(p for p in store.async_items() if p.id == laris.id)
    assert adopted.name == "Homapel"
    assert adopted.prefer_local_intents is True
    assert store.async_get_preferred_item() == laris.id
    assert config_entry.data[CONF_PIPELINE_CREATED] is True


# --- legacy homes: "never probed" must not read as "broken" -------------------


def _legacy_status() -> dict:
    """What the cloud reports for an installer-era home.

    Migration 0012 back-fills `mcp_url` (so `configured` is true) but never
    `connector_last_ok_at`, and `connector_summary()` derives `reachable` from
    that timestamp — so a perfectly working legacy home reads as
    configured-but-unreachable forever.
    """
    return status_payload(connector={"configured": True, "reachable": False})


async def test_legacy_home_never_raises_unreachable(
    hass: HomeAssistant,
    cloud: CloudMock,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    cloud.status = _legacy_status()
    await _setup(hass, config_entry)
    assert CONF_CONNECTOR_SOURCE not in config_entry.data

    # Well past the grace period the cloud still says the same thing.
    for _ in range(6):
        freezer.tick(POLL_INTERVAL)
        async_fire_time_changed(hass)
        await hass.async_block_till_done()

    assert _issue(hass, ISSUE_HOME_UNREACHABLE) is None
    assert _issue(hass, ISSUE_HOME_NOT_CONNECTED) is None
    # "we don't know", not "disconnected"
    assert hass.states.get(HOME_CONNECTED).state == "unknown"
    assert (
        hass.states.get(HOME_CONNECTED).attributes["registered_by_integration"] is False
    )


async def test_our_connector_still_raises_unreachable(
    hass: HomeAssistant,
    cloud: CloudMock,
    mcp_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The same cloud answer *is* actionable when we registered the connector."""
    cloud.status = _legacy_status()
    # Our connector re-probes on setup, so the PUT must agree with the poll.
    cloud.connector_response = {
        "reachable": False,
        "checked_at": "2026-08-24T00:00:00Z",
        "error": "timeout",
    }
    entry = _connected_entry(hass, mcp_entry)
    await _setup(hass, entry)
    assert entry.data[CONF_CONNECTOR_SOURCE]

    assert _issue(hass, ISSUE_HOME_UNREACHABLE) is None  # inside the grace period
    for _ in range(6):
        freezer.tick(POLL_INTERVAL)
        async_fire_time_changed(hass)
        await hass.async_block_till_done()

    issue = _issue(hass, ISSUE_HOME_UNREACHABLE)
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.ERROR
    assert hass.states.get(HOME_CONNECTED).state == "off"
