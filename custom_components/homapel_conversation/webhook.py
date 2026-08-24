"""Webhook receiver for cloud-pushed activation/tier changes (§7.3.6).

Wire contract:
    POST {webhook_url}
    { unit_id, active, tier, token, reason, timestamp }

Responses:
    200 — accepted
    401 — bad/missing token
    410 — webhook_url no longer valid (cloud should clear it)
"""
from __future__ import annotations

import hmac
from http import HTTPStatus
import logging

from aiohttp.web import Request, Response

from homeassistant.components import webhook
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN
from .coordinator import HomapelCoordinator

_LOGGER = logging.getLogger(__name__)


def webhook_id_for(entry_id: str) -> str:
    return f"homapel_{entry_id}"


async def async_register_webhook(
    hass: HomeAssistant,
    entry_id: str,
    coordinator: HomapelCoordinator,
) -> str:
    """Register the HA webhook and return its public URL."""
    wh_id = webhook_id_for(entry_id)

    async def handler(hass: HomeAssistant, webhook_id: str, request: Request) -> Response:
        try:
            payload = await request.json()
        except ValueError:
            return Response(status=HTTPStatus.BAD_REQUEST)
        if not isinstance(payload, dict):
            return Response(status=HTTPStatus.BAD_REQUEST)

        if coordinator.data is None:
            # Coordinator hasn't finished its first refresh — we can't validate
            # the token yet. Tell the cloud the URL is stale so it retries later.
            return Response(status=HTTPStatus.GONE)

        expected = coordinator.data.webhook_token
        supplied = payload.get("token")
        if (
            not isinstance(expected, str)
            or not isinstance(supplied, str)
            or not hmac.compare_digest(expected, supplied)
        ):
            _LOGGER.warning(
                "Rejected webhook call: bad/missing token (reason=%s)",
                payload.get("reason"),
            )
            return Response(status=HTTPStatus.UNAUTHORIZED)

        applied = coordinator.async_apply_webhook_update(payload)
        if not applied:
            _LOGGER.debug("Webhook push ignored (idempotent replay)")
        return Response(status=HTTPStatus.OK)

    webhook.async_register(
        hass,
        domain=DOMAIN,
        name="Homapel Conversation",
        webhook_id=wh_id,
        handler=handler,
        local_only=False,
    )
    return webhook.async_generate_url(hass, wh_id)


@callback
def async_unregister_webhook(hass: HomeAssistant, entry_id: str) -> None:
    webhook.async_unregister(hass, webhook_id_for(entry_id))
