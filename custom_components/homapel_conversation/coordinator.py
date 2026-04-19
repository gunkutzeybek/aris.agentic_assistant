"""Coordinator that keeps activation/tier state in sync with the cloud."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import HomapelApiError, HomapelCloudClient
from .const import DOMAIN, POLL_INTERVAL

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class HomapelState:
    active: bool
    tier: str | None
    unit_id: str
    webhook_token: str | None
    cost_ceiling_reached: bool
    last_latency_ms: int | None = None
    last_error: str | None = None
    last_converse_at: datetime | None = None
    last_webhook_timestamp: str | None = None  # For §7.3.6 idempotency


class HomapelCoordinator(DataUpdateCoordinator[HomapelState]):
    """Polls /v1/units/status (§7.3.3) and merges webhook pushes (§7.3.6)."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: HomapelCloudClient,
        api_key: str,
        unit_id: str,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} ({unit_id})",
            update_interval=POLL_INTERVAL,
        )
        self._client = client
        self._api_key = api_key
        self._unit_id = unit_id
        self._etag: str | None = None

    @property
    def client(self) -> HomapelCloudClient:
        return self._client

    @property
    def api_key(self) -> str:
        return self._api_key

    async def _async_update_data(self) -> HomapelState:
        try:
            status = await self._client.get_status(self._api_key, etag=self._etag)
        except HomapelApiError as err:
            raise UpdateFailed(f"cloud status fetch failed: {err}") from err

        if status.not_modified and self.data is not None:
            # 304 — keep the previous state, just refresh the "last poll OK" tracker
            return self.data

        if status.etag:
            self._etag = status.etag

        prev = self.data
        return HomapelState(
            active=status.active,
            tier=status.tier,
            unit_id=status.unit_id or self._unit_id,
            webhook_token=status.webhook_token,
            cost_ceiling_reached=status.cost_ceiling_reached,
            last_latency_ms=prev.last_latency_ms if prev else None,
            last_error=prev.last_error if prev else None,
            last_converse_at=prev.last_converse_at if prev else None,
            last_webhook_timestamp=prev.last_webhook_timestamp if prev else None,
        )

    def async_apply_webhook_update(self, payload: dict[str, Any]) -> bool:
        """Apply a cloud-pushed status change (§7.3.6).

        Returns ``True`` if the push was applied, ``False`` if it was ignored
        (older-or-equal timestamp — idempotent replay).
        """
        if self.data is None:
            return False

        pushed_ts = payload.get("timestamp")
        if (
            isinstance(pushed_ts, str)
            and self.data.last_webhook_timestamp is not None
            and pushed_ts <= self.data.last_webhook_timestamp
        ):
            _LOGGER.debug("Ignoring duplicate webhook push (timestamp %s)", pushed_ts)
            return False

        updated = HomapelState(
            active=bool(payload.get("active", self.data.active)),
            tier=payload.get("tier", self.data.tier),
            unit_id=self.data.unit_id,
            webhook_token=self.data.webhook_token,
            cost_ceiling_reached=self.data.cost_ceiling_reached,
            last_latency_ms=self.data.last_latency_ms,
            last_error=self.data.last_error,
            last_converse_at=self.data.last_converse_at,
            last_webhook_timestamp=pushed_ts if isinstance(pushed_ts, str) else self.data.last_webhook_timestamp,
        )
        self.async_set_updated_data(updated)
        return True

    def record_converse_latency(self, latency_ms: int, error: str | None = None) -> None:
        """Record latency/error of the most recent /v1/converse call."""
        if self.data is None:
            return
        self.data.last_latency_ms = latency_ms
        self.data.last_error = error
        self.data.last_converse_at = datetime.utcnow()
        self.async_update_listeners()
