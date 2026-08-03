"""Homapel text-to-speech entity (VOICE_API_AS_BUILT.md §4/§5).

Two paths:

* **Mode B** — when the conversation stage advertised a ``turn_id``, the cloud
  already synthesized this speech *during* the LLM stream. We attach and the
  audio is largely waiting for us.
* **Mode A** — standalone synthesis, for ``tts.speak`` calls, automations, and
  any turn the feed did not run for.

The 410 → Mode A fallback is mandatory, not exceptional: turns expire after
30 s and can only be claimed once.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.tts import TextToSpeechEntity, TtsAudioType, Voice
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import HomapelApiError, HomapelTurnNotFoundError, TtsStream
from .const import CONF_UNIT_ID, DOMAIN, TTS_AUDIO_FORMAT
from .coordinator import HomapelCoordinator

_LOGGER = logging.getLogger(__name__)

# Streaming output landed in newer HA releases. Absent it, async_get_tts_audio
# still serves the full clip — Mode B's head start is preserved either way.
try:
    from homeassistant.components.tts import TTSAudioResponse
except ImportError:  # pragma: no cover - older HA
    TTSAudioResponse = None  # type: ignore[assignment]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: HomapelCoordinator = hass.data[DOMAIN][entry.entry_id]
    capability = coordinator.data.tts if coordinator.data else None
    if capability is None or not capability.enabled:
        _LOGGER.debug("Voice TTS not enabled for this unit; skipping entity")
        return
    async_add_entities([HomapelTtsEntity(coordinator, entry)])


class HomapelTtsEntity(CoordinatorEntity[HomapelCoordinator], TextToSpeechEntity):
    """Synthesizes speech through the Homapel voice gateway."""

    _attr_has_entity_name = True
    _attr_name = "Homapel"

    def __init__(self, coordinator: HomapelCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_tts"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.data[CONF_UNIT_ID])})

    @property
    def _capability(self):
        return self.coordinator.data.tts if self.coordinator.data else None

    @property
    def default_language(self) -> str:
        capability = self._capability
        if capability and capability.default_language:
            return capability.default_language
        return self.supported_languages[0] if self.supported_languages else "tr-TR"

    @property
    def supported_languages(self) -> list[str]:
        capability = self._capability
        return list(capability.languages) if capability else []

    @property
    def supported_options(self) -> list[str]:
        return ["voice"]

    @callback
    def async_get_supported_voices(self, language: str) -> list[Voice] | None:
        capability = self._capability
        if capability is None:
            return None
        voices = capability.voices.get(language)
        if not voices:
            return None
        return [Voice(voice.id, voice.name) for voice in voices]

    async def async_get_tts_audio(
        self, message: str, language: str, options: dict[str, Any] | None = None
    ) -> TtsAudioType:
        stream = self._build_stream(message, language, options)
        try:
            data = await stream.collect()
        except HomapelTurnNotFoundError:
            # Turn expired or already claimed — synthesize from scratch.
            _LOGGER.debug("Attach rejected (410); falling back to standalone")
            self.coordinator.record_tts(0, mode="standalone")
            stream = self._mode_a(message, language, options)
            data = await self._collect_or_raise(stream)
        except HomapelApiError as err:
            raise HomeAssistantError(f"Homapel text-to-speech failed: {err}") from err

        self.coordinator.record_tts(stream.characters)
        return TTS_AUDIO_FORMAT, data

    async def async_stream_tts_audio(self, request: Any) -> Any:
        """Progressive playback where HA supports it.

        The message generator is drained first because the turn lookup and the
        Mode A body both need the complete text; the win here is streaming the
        *response*, which for Mode B is already-synthesized audio.
        """
        if TTSAudioResponse is None:  # pragma: no cover - older HA
            return await super().async_stream_tts_audio(request)

        message = "".join([chunk async for chunk in request.message_gen])
        stream = self._build_stream(message, request.language, request.options)

        async def _chunks():
            started = False
            try:
                async for chunk in stream:
                    started = True
                    yield chunk
                self.coordinator.record_tts(stream.characters)
                return
            except HomapelTurnNotFoundError:
                # Guaranteed to arrive before the first byte, but never replay
                # a partially-played clip if that guarantee is ever broken.
                if started:
                    raise HomeAssistantError("Homapel text-to-speech stream truncated")
                _LOGGER.debug("Attach rejected (410); falling back to standalone")
                self.coordinator.record_tts(0, mode="standalone")
            except HomapelApiError as err:
                raise HomeAssistantError(f"Homapel text-to-speech failed: {err}") from err

            fallback = self._mode_a(message, request.language, request.options)
            try:
                async for chunk in fallback:
                    yield chunk
            except HomapelApiError as err:
                raise HomeAssistantError(f"Homapel text-to-speech failed: {err}") from err
            self.coordinator.record_tts(fallback.characters)

        return TTSAudioResponse(TTS_AUDIO_FORMAT, _chunks())

    def _build_stream(
        self, message: str, language: str, options: dict[str, Any] | None
    ) -> TtsStream:
        """Mode B when the conversation stage advertised a turn, else Mode A."""
        turn_id = self.coordinator.turns.take_speech(message)
        if turn_id is None:
            _LOGGER.debug("TTS standalone (no live audio advertised for this reply)")
            self.coordinator.record_tts(0, mode="standalone")
            return self._mode_a(message, language, options)

        _LOGGER.debug("TTS attached to turn %s", turn_id)
        self.coordinator.record_tts(0, mode="attached")

        # The feed synthesized with the language default, so name the voice
        # only when it differs — that makes the cloud re-synthesize correctly
        # instead of playing the wrong one.
        return self.coordinator.client.synthesize(
            self.coordinator.api_key,
            turn_id=turn_id,
            voice=self._non_default_voice(options),
        )

    def _mode_a(
        self, message: str, language: str, options: dict[str, Any] | None
    ) -> TtsStream:
        return self.coordinator.client.synthesize(
            self.coordinator.api_key,
            text=message,
            language=language,
            voice=(options or {}).get("voice"),
        )

    def _non_default_voice(self, options: dict[str, Any] | None) -> str | None:
        voice = (options or {}).get("voice")
        capability = self._capability
        if not voice or (capability and voice == capability.default_voice):
            return None
        return voice

    async def _collect_or_raise(self, stream: TtsStream) -> bytes:
        try:
            return await stream.collect()
        except HomapelApiError as err:
            raise HomeAssistantError(f"Homapel text-to-speech failed: {err}") from err
