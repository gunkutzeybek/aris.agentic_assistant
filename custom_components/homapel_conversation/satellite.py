"""Resolves which satellite a voice turn came from.

The cloud validates its speculative LLM run against the ``device_id`` and
``area_id`` the STT stage supplied; a mismatch with what the conversation stage
later sends is read as pipeline misconfiguration and the speculation is
discarded. So both stages must resolve *identical* values — hence one code
path, used by both entities.

HA gives the STT entity no device context: ``SpeechMetadata`` carries only
audio parameters, and the pipeline's ``device_id`` is only attached to
``ConversationInput`` further down. The exact fix is one STT entity per
satellite, but that needs a pipeline per satellite, and auto-provisioning those
means reaching into HA internals that are not a stable API.

Instead we read it back off the satellites themselves: exactly one
``assist_satellite`` entity is listening or processing while its audio is being
transcribed. Where that is ambiguous — two people speaking at once, or a source
with no satellite entity at all, such as the companion app — we send nothing
and the turn runs cold.

Guessing *wrong* is no worse than not guessing: the conversation stage supplies
the authoritative ``device_id``, so a mismatch costs the speculation, never the
answer.
"""
from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

_LOGGER = logging.getLogger(__name__)

SATELLITE_DOMAIN = "assist_satellite"
# AssistSatelliteState values that mean "this satellite owns the current turn".
# `responding` is excluded: by then transcription is long finished, so a
# satellite still playing a previous reply must not claim a new utterance.
ACTIVE_STATES = frozenset({"listening", "processing"})


@callback
def async_active_satellite_device(hass: HomeAssistant) -> str | None:
    """Device id of the satellite running a pipeline, if unambiguous."""
    registry = er.async_get(hass)
    devices: set[str] = set()

    for state in hass.states.async_all(SATELLITE_DOMAIN):
        if state.state not in ACTIVE_STATES:
            continue
        entry = registry.async_get(state.entity_id)
        if entry and entry.device_id:
            devices.add(entry.device_id)

    if len(devices) == 1:
        return next(iter(devices))
    if devices:
        _LOGGER.debug(
            "%d satellites active at once; sending no device context", len(devices)
        )
    return None


@callback
def async_area_id_for(hass: HomeAssistant, device_id: str | None) -> str | None:
    """Area of a device. The single definition both voice stages resolve through."""
    if not device_id:
        return None
    device = dr.async_get(hass).async_get(device_id)
    return device.area_id if device else None
