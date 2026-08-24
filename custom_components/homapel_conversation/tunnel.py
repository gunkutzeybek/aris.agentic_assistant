"""Cloud-issued Cloudflare tunnel for homes with no public URL.

Only for Supervisor installs (HAOS / Supervised): the cloud creates a
per-unit tunnel (``POST /v1/units/tunnel``) whose ingress forwards
``/api/webhook/*`` to ``http://homeassistant:8123`` and nothing else; this
module installs the community Cloudflared add-on, hands it the tunnel token
and waits for it to run. The resulting base URL is ``https://<hostname>``.

Built on ``homeassistant.components.hassio.AddonManager`` the way the
zwave_js config flow manages its add-on, plus the Supervisor store API for
the add-on repository (the add-on lives outside the official store).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.components.hassio import AddonError, AddonManager, AddonState
from homeassistant.core import HomeAssistant

from .const import (
    CLOUDFLARED_ADDON_NAME,
    CLOUDFLARED_ADDON_SLUG,
    CLOUDFLARED_OPT_TUNNEL_TOKEN,
    CLOUDFLARED_REPOSITORY_URL,
    CLOUDFLARED_START_TIMEOUT,
)

_LOGGER = logging.getLogger(__name__)

_START_POLL_SECONDS = 2.0


class TunnelError(Exception):
    """Installing / starting the Cloudflared add-on failed."""


def get_addon_manager(hass: HomeAssistant) -> AddonManager:
    """One manager per add-on (HA keeps task state on the instance)."""
    return AddonManager(hass, _LOGGER, CLOUDFLARED_ADDON_NAME, CLOUDFLARED_ADDON_SLUG)


async def async_existing_tunnel_token(hass: HomeAssistant) -> str | None:
    """The tunnel token the add-on is already configured with, if installed.

    A non-empty token that is not ours means the customer set the add-on up
    for their own domain — the flow must ask before replacing it.
    """
    manager = get_addon_manager(hass)
    try:
        info = await manager.async_get_addon_info()
    except AddonError as err:
        raise TunnelError(str(err)) from err
    if info.state is AddonState.NOT_INSTALLED:
        return None
    token = info.options.get(CLOUDFLARED_OPT_TUNNEL_TOKEN)
    return str(token) if token else None


async def async_ensure_repository(hass: HomeAssistant) -> None:
    """Add the Cloudflared add-on repository to the store if it is missing."""
    from aiohasupervisor import SupervisorError
    from aiohasupervisor.models import StoreAddRepository

    from homeassistant.components.hassio import get_supervisor_client

    client = get_supervisor_client(hass)
    try:
        repositories = await client.store.repositories_list()
        if any(
            repo.source.rstrip("/").lower() == CLOUDFLARED_REPOSITORY_URL.lower()
            for repo in repositories
        ):
            return
        await client.store.add_repository(
            StoreAddRepository(repository=CLOUDFLARED_REPOSITORY_URL)
        )
    except SupervisorError as err:
        raise TunnelError(f"Could not add the Cloudflared repository: {err}") from err


async def async_install_tunnel(hass: HomeAssistant, tunnel_token: str) -> None:
    """Install (if needed), configure and start the Cloudflared add-on.

    Blocks until the add-on reports running or ``CLOUDFLARED_START_TIMEOUT``
    elapses. Raises ``TunnelError`` with a user-facing reason.
    """
    manager = get_addon_manager(hass)
    try:
        info = await manager.async_get_addon_info()
        if info.state is AddonState.NOT_INSTALLED:
            await async_ensure_repository(hass)
            # The store lists the add-on only after the repository loads.
            info = await manager.async_get_addon_info()
            if info.state is AddonState.NOT_INSTALLED:
                await manager.async_install_addon()
            info = await manager.async_get_addon_info()

        options: dict[str, Any] = {**info.options, CLOUDFLARED_OPT_TUNNEL_TOKEN: tunnel_token}
        if options != info.options:
            await manager.async_set_addon_options(options)
            if info.state is AddonState.RUNNING:
                # Options only apply on (re)start.
                await manager.async_restart_addon()
        if info.state is not AddonState.RUNNING:
            await manager.async_start_addon()

        await _async_wait_running(manager)
    except AddonError as err:
        raise TunnelError(str(err)) from err


async def _async_wait_running(manager: AddonManager) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + CLOUDFLARED_START_TIMEOUT
    while True:
        info = await manager.async_get_addon_info()
        if info.state is AddonState.RUNNING:
            return
        if loop.time() >= deadline:
            raise TunnelError(f"{CLOUDFLARED_ADDON_NAME} add-on did not start in time")
        await asyncio.sleep(_START_POLL_SECONDS)
