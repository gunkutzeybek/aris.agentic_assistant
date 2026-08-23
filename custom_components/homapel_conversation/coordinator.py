"""Coordinator that keeps activation/tier/connector state in sync with the cloud."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    HomapelApiError,
    HomapelAuthError,
    HomapelCloudClient,
    SttCapability,
    TtsCapability,
)
from .const import (
    CONF_CONNECTOR_SOURCE,
    DASHBOARD_URL,
    DOMAIN,
    ISSUE_HOME_NOT_CONNECTED,
    ISSUE_HOME_UNREACHABLE,
    POLL_INTERVAL,
    UNREACHABLE_GRACE,
)
from .turns import TurnRegistry

if TYPE_CHECKING:
    from .connector import ConnectorManager

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
    # Voice capability blocks. Always present on a 200 from the current cloud;
    # None means an older server (or a malformed block) — treated as disabled.
    stt: SttCapability | None = None
    tts: TtsCapability | None = None
    last_stt_provider_ms: int | None = None
    last_stt_audio_seconds: float | None = None
    last_tts_characters: int | None = None
    last_tokens_in: int | None = None
    last_tokens_out: int | None = None
    # Observability for the overlap: a discarded speculation is indistinguishable
    # from a cold run except in timing, so surface what we actually sent.
    last_eager: bool | None = None
    last_stt_device_id: str | None = None
    last_tts_mode: str | None = None  # "attached" (fast path) | "standalone"
    # Home connector as the cloud sees it (``connector`` block of /units/status).
    # None = the cloud did not report it (older server).
    connector_configured: bool | None = None
    connector_reachable: bool | None = None
    # When the cloud first reported configured-but-unreachable; drives the
    # "home unreachable" repair issue after UNREACHABLE_GRACE.
    connector_unreachable_since: datetime | None = None


class HomapelCoordinator(DataUpdateCoordinator[HomapelState]):
    """Polls /v1/units/status (§7.3.3) and merges webhook pushes (§7.3.6)."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: HomapelCloudClient,
        api_key: str,
        unit_id: str,
        *,
        config_entry: ConfigEntry | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} ({unit_id})",
            update_interval=POLL_INTERVAL,
            config_entry=config_entry,
        )
        self._client = client
        self._api_key = api_key
        self._unit_id = unit_id
        self._etag: str | None = None
        # Correlates stt → conversation → tts for a single voice turn.
        self.turns = TurnRegistry()
        # Attached by __init__ once the entry is set up; None while loading.
        self.connector_manager: ConnectorManager | None = None

    @property
    def client(self) -> HomapelCloudClient:
        return self._client

    @property
    def api_key(self) -> str:
        return self._api_key

    async def _async_update_data(self) -> HomapelState:
        try:
            status = await self._client.get_status(self._api_key, etag=self._etag)
        except HomapelAuthError as err:
            # Rotated from the dashboard → HA starts the reauth flow for us.
            raise ConfigEntryAuthFailed(str(err)) from err
        except HomapelApiError as err:
            raise UpdateFailed(f"cloud status fetch failed: {err}") from err

        if status.not_modified and self.data is not None:
            # 304 — keep the previous state, just refresh the "last poll OK" tracker
            self._async_update_issues(self.data)
            return self.data

        if status.etag:
            self._etag = status.etag

        prev = self.data
        connector_configured = status.connector.configured if status.connector else None
        connector_reachable = status.connector.reachable if status.connector else None
        state = HomapelState(
            active=status.active,
            tier=status.tier,
            unit_id=status.unit_id or self._unit_id,
            webhook_token=status.webhook_token,
            cost_ceiling_reached=status.cost_ceiling_reached,
            last_latency_ms=prev.last_latency_ms if prev else None,
            last_error=prev.last_error if prev else None,
            last_converse_at=prev.last_converse_at if prev else None,
            last_webhook_timestamp=prev.last_webhook_timestamp if prev else None,
            stt=status.stt,
            tts=status.tts,
            last_stt_provider_ms=prev.last_stt_provider_ms if prev else None,
            last_stt_audio_seconds=prev.last_stt_audio_seconds if prev else None,
            last_tts_characters=prev.last_tts_characters if prev else None,
            last_tokens_in=prev.last_tokens_in if prev else None,
            last_tokens_out=prev.last_tokens_out if prev else None,
            last_eager=prev.last_eager if prev else None,
            last_stt_device_id=prev.last_stt_device_id if prev else None,
            last_tts_mode=prev.last_tts_mode if prev else None,
            connector_configured=connector_configured,
            connector_reachable=connector_reachable,
            connector_unreachable_since=_unreachable_since(
                prev, connector_configured, connector_reachable
            ),
        )
        self._async_update_issues(state)
        return state

    # --- Home connector -----------------------------------------------------

    @callback
    def record_connector(self, *, configured: bool, reachable: bool) -> None:
        """Apply the result of a PUT /v1/units/connector we just made."""
        if self.data is None:
            return
        self.data.connector_configured = configured
        self.data.connector_reachable = reachable
        self.data.connector_unreachable_since = _unreachable_since(
            self.data, configured, reachable
        )
        self._async_update_issues(self.data)
        self.async_update_listeners()

    @callback
    def _async_update_issues(self, state: HomapelState) -> None:
        """Raise / clear the two connector repair issues from the latest state."""
        entry = self.config_entry
        if entry is None:
            return

        not_connected = state.connector_configured is False or (
            state.connector_configured is None
            and not entry.data.get(CONF_CONNECTOR_SOURCE)
        )
        if not_connected:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                ISSUE_HOME_NOT_CONNECTED,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_HOME_NOT_CONNECTED,
                translation_placeholders={"dashboard_url": DASHBOARD_URL},
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE_HOME_NOT_CONNECTED)

        unreachable = (
            state.connector_configured is True
            and state.connector_reachable is False
            and state.connector_unreachable_since is not None
            and dt_util.utcnow() - state.connector_unreachable_since >= UNREACHABLE_GRACE
        )
        if unreachable:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                ISSUE_HOME_UNREACHABLE,
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key=ISSUE_HOME_UNREACHABLE,
                translation_placeholders={"dashboard_url": DASHBOARD_URL},
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE_HOME_UNREACHABLE)

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
            stt=self.data.stt,
            tts=self.data.tts,
            last_stt_provider_ms=self.data.last_stt_provider_ms,
            last_stt_audio_seconds=self.data.last_stt_audio_seconds,
            last_tts_characters=self.data.last_tts_characters,
            last_tokens_in=self.data.last_tokens_in,
            last_tokens_out=self.data.last_tokens_out,
            last_eager=self.data.last_eager,
            last_stt_device_id=self.data.last_stt_device_id,
            last_tts_mode=self.data.last_tts_mode,
            connector_configured=self.data.connector_configured,
            connector_reachable=self.data.connector_reachable,
            connector_unreachable_since=self.data.connector_unreachable_since,
        )
        self.async_set_updated_data(updated)
        return True

    def record_tokens(self, tokens_in: int, tokens_out: int) -> None:
        """Record LLM token usage from the converse ``meta`` event.

        Attached (speculated) turns report usage the same way as cold ones.
        """
        if self.data is None:
            return
        self.data.last_tokens_in = tokens_in
        self.data.last_tokens_out = tokens_out
        self.async_update_listeners()

    def record_stt(
        self,
        audio_seconds: float,
        provider_ms: int | None,
        *,
        device_id: str | None = None,
        eager: bool = False,
    ) -> None:
        """Record metering/latency/context from the most recent /v1/stt call."""
        if self.data is None:
            return
        self.data.last_stt_audio_seconds = audio_seconds
        self.data.last_stt_provider_ms = provider_ms
        self.data.last_stt_device_id = device_id
        self.data.last_eager = eager
        self.async_update_listeners()

    def record_tts(self, characters: int, *, mode: str | None = None) -> None:
        """Record billable characters and which /v1/tts path served the audio."""
        if self.data is None:
            return
        self.data.last_tts_characters = characters
        if mode is not None:
            self.data.last_tts_mode = mode
        self.async_update_listeners()

    def record_converse_latency(self, latency_ms: int, error: str | None = None) -> None:
        """Record latency/error of the most recent /v1/converse call."""
        if self.data is None:
            return
        self.data.last_latency_ms = latency_ms
        self.data.last_error = error
        self.data.last_converse_at = dt_util.utcnow()
        self.async_update_listeners()


def _unreachable_since(
    prev: HomapelState | None, configured: bool | None, reachable: bool | None
) -> datetime | None:
    """Carry the first-seen-unreachable timestamp while the condition holds."""
    if not (configured is True and reachable is False):
        return None
    if prev is not None and prev.connector_unreachable_since is not None:
        return prev.connector_unreachable_since
    return dt_util.utcnow()
