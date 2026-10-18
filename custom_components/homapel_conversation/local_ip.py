"""Keep HA's automatic local URL on an address the voice satellites can reach.

With the "Local network" URL on Automatic (`internal_url` unset), HA builds it
from `hass.config.api.local_ip`, which it detects once at startup. When DHCP
later hands the host a new address, every TTS link HA gives a satellite
(`http://<old ip>:8123/api/tts_proxy/...`) points nowhere: Laris Voice lights
its ring for the reply but fetches no audio. HA itself only notices on restart.

So once a minute we re-read the host's interfaces and pick the address on the
same subnet as the ESPHome voice satellites — on a multi-homed host (say an IoT
VLAN without internet next to the home LAN) that is the only reliable choice;
routing and gateway metrics say nothing about where the satellites live. With
no satellite on a directly attached subnet we fall back to the address the host
routes from, and only once HA's current address has left the host (it may come
from the adapter selection in HA's network settings). A user-pinned
`internal_url` is never touched.
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from ipaddress import IPv4Address, IPv4Interface, ip_address
import logging
from typing import Final

from homeassistant.components.network.const import MDNS_TARGET_IP, PUBLIC_TARGET_IP
from homeassistant.components.network.models import Adapter
from homeassistant.components.network.util import (
    async_get_source_ip,
    async_load_adapters,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_time_interval

_LOGGER = logging.getLogger(__name__)

LOCAL_IP_CHECK_INTERVAL: Final = timedelta(minutes=1)

ESPHOME_DOMAIN: Final = "esphome"
SATELLITE_DOMAIN: Final = "assist_satellite"


def _host_interfaces(adapters: Iterable[Adapter]) -> list[IPv4Interface]:
    """Usable IPv4 addresses of this host, with their subnets."""
    interfaces = []
    for adapter in adapters:
        for ipv4 in adapter["ipv4"]:
            iface = IPv4Interface(f"{ipv4['address']}/{ipv4['network_prefix']}")
            if not (iface.ip.is_loopback or iface.ip.is_link_local):
                interfaces.append(iface)
    return interfaces


@callback
def _async_satellite_ips(hass: HomeAssistant) -> set[IPv4Address]:
    """IPv4 addresses of ESPHome devices that are Assist satellites (Laris Voice)."""
    ent_reg = er.async_get(hass)
    ips: set[IPv4Address] = set()
    for entry in hass.config_entries.async_entries(ESPHOME_DOMAIN):
        if not any(
            ent.domain == SATELLITE_DOMAIN
            for ent in er.async_entries_for_config_entry(ent_reg, entry.entry_id)
        ):
            continue
        # The address the live API connection uses; else the configured host,
        # which is usually an IP (a `.local` name cannot be matched to a subnet).
        host = None
        if entry.state is ConfigEntryState.LOADED:
            client = getattr(getattr(entry, "runtime_data", None), "client", None)
            host = getattr(client, "connected_address", None)
        for candidate in (host, entry.data.get(CONF_HOST)):
            try:
                addr = ip_address(candidate) if candidate else None
            except ValueError:
                continue
            if isinstance(addr, IPv4Address):
                ips.add(addr)
                break
    return ips


@callback
def _async_routed_source_ip() -> str | None:
    """The address the host routes from now (LAN fallback when offline)."""
    for target in (PUBLIC_TARGET_IP, MDNS_TARGET_IP):
        if (ip := async_get_source_ip(target)) and not ip.startswith("127."):
            return ip
    return None


def _pick_for_satellites(
    interfaces: list[IPv4Interface], satellites: set[IPv4Address], current: str | None
) -> tuple[str, int] | None:
    """The host address sharing a subnet with the most satellites, and that count.

    Keeps `current` on a tie so a host with two addresses on one LAN does not
    flap. None when no satellite is on a directly attached subnet.
    """
    reach = {
        str(iface.ip): sum(sat in iface.network for sat in satellites)
        for iface in interfaces
    }
    best = max(reach.values(), default=0)
    if best == 0:
        return None
    if reach.get(current or "") == best:
        return current, best  # type: ignore[return-value]
    return next(ip for ip, n in reach.items() if n == best), best


@callback
def async_track_local_ip(hass: HomeAssistant) -> CALLBACK_TYPE:
    """Check now and every minute; returns the unsubscribe callback."""
    warned_split: set[frozenset[IPv4Address]] = set()

    async def _async_check(_now: datetime | None = None) -> None:
        api = hass.config.api
        if api is None or hass.config.internal_url:
            return
        interfaces = _host_interfaces(await async_load_adapters())
        if not interfaces:
            return
        satellites = _async_satellite_ips(hass)

        target: str | None
        if picked := _pick_for_satellites(interfaces, satellites, api.local_ip):
            target, reached = picked
            if reached < len(satellites) and (key := frozenset(satellites)) not in warned_split:
                warned_split.add(key)
                _LOGGER.warning(
                    "Voice satellites %s are not all on one of Home Assistant's "
                    "networks; using %s, which reaches %d of them",
                    sorted(map(str, satellites)),
                    target,
                    reached,
                )
        elif api.local_ip in {str(iface.ip) for iface in interfaces}:
            return
        else:
            target = _async_routed_source_ip()

        if target is None or target == api.local_ip:
            return
        _LOGGER.info(
            "Home Assistant's local address changed from %s to %s; local URL updated",
            api.local_ip,
            target,
        )
        api.local_ip = target

    hass.async_create_task(_async_check(), "Homapel local IP check", eager_start=True)
    return async_track_time_interval(
        hass, _async_check, LOCAL_IP_CHECK_INTERVAL, name="Homapel local IP watch"
    )
