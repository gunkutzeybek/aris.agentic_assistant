"""Homapel speech-to-text entity (VOICE_API_AS_BUILT.md §3).

Audio is piped straight from HA's Assist pipeline into a chunked request body
so the cloud can forward to the provider while the user is still speaking —
nothing is buffered locally.

Deliberately *not* gated on unit dormancy: the cloud serves STT to dormant
units precisely so a brand-new user can speak, hear the spoken activation
prompt, and activate. `unit_not_active` never arrives on this path.
"""
from __future__ import annotations

from collections.abc import AsyncIterable
import logging

from homeassistant.components.stt import (
    AudioBitRates,
    AudioChannels,
    AudioCodecs,
    AudioFormats,
    AudioSampleRates,
    SpeechMetadata,
    SpeechResult,
    SpeechResultState,
    SpeechToTextEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import HomapelApiError
from .const import (
    CONF_UNIFIED_PIPELINE,
    DEFAULT_UNIFIED_PIPELINE,
    DOMAIN,
)
from .coordinator import HomapelCoordinator
from .entity import homapel_device_info
from .satellite import async_active_satellite_device, async_area_id_for

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: HomapelCoordinator = hass.data[DOMAIN][entry.entry_id]
    capability = coordinator.data.stt if coordinator.data else None
    if capability is None or not capability.enabled:
        _LOGGER.debug("Voice STT not enabled for this unit; skipping entity")
        return
    async_add_entities([HomapelSttEntity(coordinator, entry)])


class HomapelSttEntity(CoordinatorEntity[HomapelCoordinator], SpeechToTextEntity):
    """Proxies HA's audio stream to the Homapel voice gateway."""

    _attr_has_entity_name = True
    _attr_translation_key = "homapel"

    def __init__(self, coordinator: HomapelCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_stt"
        self._attr_device_info = homapel_device_info(entry)

    @property
    def supported_languages(self) -> list[str]:
        capability = self.coordinator.data.stt if self.coordinator.data else None
        return list(capability.languages) if capability else []

    # Assist delivers 16 kHz 16-bit mono PCM. These are declared rather than
    # assumed anywhere else; the actual values are read back off SpeechMetadata
    # and sent to the cloud as Content-Type media params.
    @property
    def supported_formats(self) -> list[AudioFormats]:
        return [AudioFormats.WAV]

    @property
    def supported_codecs(self) -> list[AudioCodecs]:
        return [AudioCodecs.PCM]

    @property
    def supported_bit_rates(self) -> list[AudioBitRates]:
        return [AudioBitRates.BITRATE_16]

    @property
    def supported_sample_rates(self) -> list[AudioSampleRates]:
        return [AudioSampleRates.SAMPLERATE_16000]

    @property
    def supported_channels(self) -> list[AudioChannels]:
        return [AudioChannels.CHANNEL_MONO]

    async def async_process_audio_stream(
        self, metadata: SpeechMetadata, stream: AsyncIterable[bytes]
    ) -> SpeechResult:
        capability = self.coordinator.data.stt if self.coordinator.data else None

        # Must match what the conversation stage sends, or the cloud discards
        # its speculative run every turn and the overlap is silently lost.
        # Unresolvable is fine — that just costs the speculation.
        device_id = async_active_satellite_device(self.hass)
        eager = self._eager_enabled()

        try:
            result = await self.coordinator.client.transcribe(
                self.coordinator.api_key,
                stream=stream,
                language=metadata.language,
                sample_rate=int(metadata.sample_rate),
                channels=int(metadata.channel),
                max_audio_seconds=capability.max_audio_seconds if capability else 0,
                device_id=device_id,
                area_id=async_area_id_for(self.hass, device_id),
                eager=eager,
            )
        except HomapelApiError as err:
            _LOGGER.warning("Speech-to-text failed: %s", err)
            return SpeechResult(None, SpeechResultState.ERROR)

        self.coordinator.record_stt(
            result.audio_seconds, result.provider_ms, device_id=device_id, eager=eager
        )
        _LOGGER.debug(
            "STT %.2fs in %sms (device=%s eager=%s turn=%s) -> %r",
            result.audio_seconds,
            result.provider_ms,
            device_id or "unresolved",
            eager,
            result.turn_id,
            result.text,
        )

        # Handed to the conversation entity, which echoes it back to the cloud
        # so the turn's TTS feed can run. Skipped for silence — an empty
        # transcript produces no conversation turn to correlate with.
        if result.turn_id and result.text:
            self.coordinator.turns.remember_transcript(result.text, result.turn_id)

        return SpeechResult(result.text, SpeechResultState.SUCCESS)

    @callback
    def _eager_enabled(self) -> bool:
        """Whether the cloud may pre-synthesize speech for this turn.

        `eager` is a consent flag: it authorizes billable TTS work that only
        pays off if a Homapel TTS entity actually claims the turn. So it is
        sent only when every pipeline routing audio through this entity also
        uses Homapel for conversation *and* TTS.
        """
        if not self._entry.options.get(CONF_UNIFIED_PIPELINE, DEFAULT_UNIFIED_PIPELINE):
            return False

        try:
            from homeassistant.components import assist_pipeline

            pipelines = assist_pipeline.async_get_pipelines(self.hass)
        except Exception:
            # assist_pipeline unavailable means no pipeline is running this
            # entity at all, so the question is moot; default to the useful
            # behaviour rather than silently disabling the overlap.
            _LOGGER.debug("Could not enumerate pipelines; assuming unified", exc_info=True)
            return True

        relevant = [p for p in pipelines if p.stt_engine == self.entity_id]
        if not relevant:
            return True

        conversation_id = self._sibling_entity_id("conversation", "conversation")
        tts_id = self._sibling_entity_id("tts", "tts")
        if conversation_id is None or tts_id is None:
            return False

        return all(
            p.conversation_engine == conversation_id and p.tts_engine == tts_id
            for p in relevant
        )

    @callback
    def _sibling_entity_id(self, domain: str, suffix: str) -> str | None:
        """Resolve one of our own entities by the unique_id convention."""
        registry = er.async_get(self.hass)
        return registry.async_get_entity_id(
            domain, DOMAIN, f"{self._entry.entry_id}_{suffix}"
        )
