"""The local-IP watch: HA's automatic local URL stays on the satellites' network."""
from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from homeassistant.components.http import ApiConfig
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.network import get_url

from custom_components.homapel_conversation.local_ip import (
    LOCAL_IP_CHECK_INTERVAL,
    async_track_local_ip,
)

MODULE = "custom_components.homapel_conversation.local_ip"

# The owner's layout: a home LAN with internet (where Laris Voice lives) and an
# IoT network without it, both on the HA host.
HOME_IP = "192.168.68.53"
HOME_IP_NEW = "192.168.68.71"
IOT_IP = "10.0.20.5"
VOICE_IP = "192.168.68.90"


def _adapters(*cidrs: str) -> list[dict[str, Any]]:
    """One HA network `Adapter` per address, e.g. "192.168.68.53/24"."""
    return [
        {
            "name": f"eth{i}",
            "index": i,
            "enabled": True,
            "auto": True,
            "default": False,
            "ipv4": [{"address": cidr.split("/")[0], "network_prefix": int(cidr.split("/")[1])}],
            "ipv6": [],
        }
        for i, cidr in enumerate(cidrs)
    ]


@pytest.fixture
def host() -> Iterator[dict[str, Any]]:
    """Control the host's interfaces and the address it routes from."""
    state: dict[str, Any] = {
        "adapters": _adapters(f"{HOME_IP}/24", f"{IOT_IP}/24"),
        "routed": HOME_IP,
    }

    async def _load() -> list[dict[str, Any]]:
        return state["adapters"]

    with (
        patch(f"{MODULE}.async_load_adapters", side_effect=_load),
        patch(f"{MODULE}.async_get_source_ip", side_effect=lambda _t: state["routed"]),
    ):
        yield state


def _api(hass: HomeAssistant, local_ip: str) -> ApiConfig:
    hass.config.api = ApiConfig(local_ip, "0.0.0.0", 8123, False)
    return hass.config.api


def _esphome(
    hass: HomeAssistant, host: str, *, satellite: bool = True, connected: str | None = None
) -> MockConfigEntry:
    """An ESPHome device entry; `satellite` gives it an assist_satellite entity."""
    entry = MockConfigEntry(
        domain="esphome",
        data={"host": host, "port": 6053},
        state=ConfigEntryState.LOADED if connected else ConfigEntryState.NOT_LOADED,
    )
    entry.add_to_hass(hass)
    if connected:
        entry.runtime_data = SimpleNamespace(client=SimpleNamespace(connected_address=connected))
    er.async_get(hass).async_get_or_create(
        "assist_satellite" if satellite else "switch",
        "esphome",
        f"{entry.entry_id}-ent",
        config_entry=entry,
    )
    return entry


async def _start(hass: HomeAssistant) -> None:
    hass.data["unsub"] = async_track_local_ip(hass)
    await hass.async_block_till_done()


async def _tick(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    freezer.tick(LOCAL_IP_CHECK_INTERVAL)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


@pytest.fixture(autouse=True)
def _stop_watch(hass: HomeAssistant) -> Iterator[None]:
    yield
    if unsub := hass.data.pop("unsub", None):
        unsub()


async def test_moves_to_the_satellites_network(hass: HomeAssistant, host: dict) -> None:
    """HA picked the IoT address at startup; Laris Voice is on the home LAN."""
    api = _api(hass, IOT_IP)
    _esphome(hass, VOICE_IP)
    await _start(hass)
    assert api.local_ip == HOME_IP
    assert get_url(hass, prefer_external=False) == f"http://{HOME_IP}:8123"


async def test_follows_home_ip_change_even_if_iot_holds_the_route(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, host: dict
) -> None:
    api = _api(hass, HOME_IP)
    _esphome(hass, VOICE_IP)
    await _start(hass)

    host["adapters"] = _adapters(f"{HOME_IP_NEW}/24", f"{IOT_IP}/24")
    host["routed"] = IOT_IP  # e.g. the IoT DHCP also hands out a gateway
    await _tick(hass, freezer)
    assert api.local_ip == HOME_IP_NEW


async def test_iot_ip_change_ignored(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, host: dict
) -> None:
    api = _api(hass, HOME_IP)
    _esphome(hass, VOICE_IP)
    await _start(hass)
    host["adapters"] = _adapters(f"{HOME_IP}/24", "10.0.20.77/24")
    await _tick(hass, freezer)
    assert api.local_ip == HOME_IP


async def test_live_connection_address_beats_mdns_host(hass: HomeAssistant, host: dict) -> None:
    api = _api(hass, IOT_IP)
    entry = _esphome(hass, "laris-voice.local", connected=VOICE_IP)
    await _start(hass)
    assert api.local_ip == HOME_IP
    # Teardown would otherwise unload it through the real esphome integration.
    entry.mock_state(hass, ConfigEntryState.NOT_LOADED)


async def test_non_satellite_esphome_devices_ignored(hass: HomeAssistant, host: dict) -> None:
    """A plug on the IoT network must not drag the URL there."""
    api = _api(hass, HOME_IP)
    _esphome(hass, "10.0.20.40", satellite=False)
    _esphome(hass, VOICE_IP)
    await _start(hass)
    assert api.local_ip == HOME_IP


async def test_split_satellites_majority_wins_and_warns_once(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, host: dict, caplog: pytest.LogCaptureFixture
) -> None:
    api = _api(hass, IOT_IP)
    _esphome(hass, VOICE_IP)
    _esphome(hass, "192.168.68.91")
    _esphome(hass, "10.0.20.41")
    await _start(hass)
    await _tick(hass, freezer)
    assert api.local_ip == HOME_IP
    assert caplog.text.count("are not all on one of Home Assistant's networks") == 1


async def test_tie_keeps_current_address(hass: HomeAssistant, host: dict) -> None:
    """Two addresses on the same LAN: no flapping."""
    host["adapters"] = _adapters(f"{HOME_IP}/24", "192.168.68.54/24")
    api = _api(hass, "192.168.68.54")
    _esphome(hass, VOICE_IP)
    await _start(hass)
    assert api.local_ip == "192.168.68.54"


async def test_without_satellites_follows_route_once_old_ip_is_gone(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, host: dict
) -> None:
    api = _api(hass, HOME_IP)
    await _start(hass)

    host["routed"] = IOT_IP  # old address still on the host: leave HA's choice
    await _tick(hass, freezer)
    assert api.local_ip == HOME_IP

    host["adapters"] = _adapters(f"{HOME_IP_NEW}/24", f"{IOT_IP}/24")
    host["routed"] = HOME_IP_NEW
    await _tick(hass, freezer)
    assert api.local_ip == HOME_IP_NEW


async def test_satellite_off_link_falls_back_to_route(hass: HomeAssistant, host: dict) -> None:
    """A satellite behind a router matches no subnet; the route rule applies."""
    host["adapters"] = _adapters(f"{HOME_IP_NEW}/24", f"{IOT_IP}/24")
    host["routed"] = HOME_IP_NEW
    api = _api(hass, HOME_IP)
    _esphome(hass, "172.16.5.9")
    await _start(hass)
    assert api.local_ip == HOME_IP_NEW


async def test_pinned_internal_url_left_alone(hass: HomeAssistant, host: dict) -> None:
    await hass.config.async_update(internal_url=f"http://{IOT_IP}:8123")
    api = _api(hass, IOT_IP)
    _esphome(hass, VOICE_IP)
    await _start(hass)
    assert api.local_ip == IOT_IP


async def test_no_route_or_loopback_ignored(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, host: dict
) -> None:
    api = _api(hass, HOME_IP)
    await _start(hass)
    host["adapters"] = _adapters(f"{IOT_IP}/24")
    for routed in (None, "127.0.0.1"):
        host["routed"] = routed
        await _tick(hass, freezer)
        assert api.local_ip == HOME_IP


async def test_stops_after_unsubscribe(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, host: dict
) -> None:
    api = _api(hass, HOME_IP)
    await _start(hass)
    hass.data.pop("unsub")()
    host["adapters"] = _adapters(f"{HOME_IP_NEW}/24")
    host["routed"] = HOME_IP_NEW
    await _tick(hass, freezer)
    assert api.local_ip == HOME_IP
