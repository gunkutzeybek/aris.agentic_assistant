"""Homapel ConversationEntity — dormant prompt or cloud proxy (§7.3.2)."""
from __future__ import annotations

import logging
import time
from typing import Any

from homeassistant.components import conversation
from homeassistant.components.conversation import (
    ConversationEntity,
    ConversationInput,
    ConversationResult,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import intent
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import ulid as ulid_util

from .api import (
    HomapelApiError,
    HomapelAuthError,
    HomapelCostCeilingError,
    HomapelNetworkError,
    HomapelRateLimitedError,
    HomapelTimeoutError,
    HomapelUnitNotActiveError,
)
from .const import (
    CONF_CONVERSE_SOCK_READ,
    CONF_DEFAULT_LANGUAGE,
    DEFAULT_CONVERSE_SOCK_READ,
    DEFAULT_LANGUAGE,
    DOMAIN,
    DORMANT_PROMPT,
    ERROR_SPEECH,
    SUPPORTED_LANGUAGES,
)
from .coordinator import HomapelCoordinator
from .entity import homapel_device_info
from .satellite import async_area_id_for

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: HomapelCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([HomapelConversationEntity(coordinator, entry)])


class HomapelConversationEntity(
    CoordinatorEntity[HomapelCoordinator], ConversationEntity
):
    """Conversation agent that proxies to the Homapel cloud."""

    _attr_has_entity_name = True
    _attr_translation_key = "homapel"
    _attr_supported_features = conversation.ConversationEntityFeature(0)

    def __init__(self, coordinator: HomapelCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._default_language = entry.data.get(CONF_DEFAULT_LANGUAGE, DEFAULT_LANGUAGE)
        self._attr_unique_id = f"{entry.entry_id}_conversation"
        self._attr_device_info = homapel_device_info(entry)

    @property
    def supported_languages(self) -> list[str]:
        return SUPPORTED_LANGUAGES

    async def _async_handle_message(
        self, user_input: ConversationInput, chat_log: Any
    ) -> ConversationResult:
        short_lang = self._short_language(user_input.language)
        wire_lang = user_input.language or self._default_language
        response = intent.IntentResponse(language=user_input.language or wire_lang)
        conversation_id = user_input.conversation_id or ulid_util.ulid_now()

        state = self.coordinator.data
        if state is None or not state.active:
            response.async_set_speech(DORMANT_PROMPT[short_lang])
            return ConversationResult(
                response=response,
                conversation_id=conversation_id,
                continue_conversation=False,
            )

        # Resolved through the same helper the STT stage uses: the cloud
        # compares both values when validating its speculative run.
        area_id = async_area_id_for(self.hass, user_input.device_id)

        sock_read = float(
            self._entry.options.get(
                CONF_CONVERSE_SOCK_READ, DEFAULT_CONVERSE_SOCK_READ
            )
        )
        started = time.monotonic()
        stream = self.coordinator.client.converse_stream(
            self.coordinator.api_key,
            text=user_input.text,
            conversation_id=conversation_id,
            language=wire_lang,
            sock_read=sock_read,
            device_id=user_input.device_id,
            area_id=area_id,
            # Set when this turn came from Homapel STT. Lets the cloud
            # pre-synthesize speech while the LLM streams; absent for typed
            # input, which simply runs without the feed.
            turn_id=self.coordinator.turns.take_transcript(user_input.text),
        )
        speech_parts: list[str] = []
        try:
            async for content in chat_log.async_add_delta_content_stream(
                user_input.agent_id, stream
            ):
                chunk = getattr(content, "content", None)
                if chunk:
                    speech_parts.append(chunk)
        except HomapelAuthError as err:
            return self._error_result(
                response, conversation_id, started,
                category="auth", short_lang=short_lang, err=err, level="error",
            )
        except HomapelUnitNotActiveError as err:
            # Cloud says dormant even though our cached state says active —
            # trust the cloud, speak the dormant prompt, and force a refresh.
            self._record_latency(started, error="dormant")
            _LOGGER.info("Cloud reports unit not active: %s", err)
            response.async_set_speech(DORMANT_PROMPT[short_lang])
            await self.coordinator.async_request_refresh()
            return ConversationResult(
                response=response,
                conversation_id=conversation_id,
                continue_conversation=False,
            )
        except HomapelCostCeilingError as err:
            return self._error_result(
                response, conversation_id, started,
                category="cost_ceiling", short_lang=short_lang, err=err, level="warning",
            )
        except HomapelRateLimitedError as err:
            return self._error_result(
                response, conversation_id, started,
                category="rate_limited", short_lang=short_lang, err=err, level="warning",
            )
        except (HomapelNetworkError, HomapelTimeoutError) as err:
            return self._error_result(
                response, conversation_id, started,
                category="network", short_lang=short_lang, err=err, level="warning",
            )
        except HomapelApiError as err:
            return self._error_result(
                response, conversation_id, started,
                category="unknown", short_lang=short_lang, err=err, level="error",
            )

        self._record_latency(started, error=None)
        self.coordinator.record_tokens(stream.tokens_in, stream.tokens_out)

        # Cloud can signal dormant per-request (§7.3.2) even if our cached
        # status is stale. Its speech already contains the activation prompt,
        # but we still want to refresh coordinator state.
        if stream.dormant:
            await self.coordinator.async_request_refresh()

        speech = "".join(speech_parts)

        # Only advertised when the cloud actually ran the TTS feed for this
        # request. Without it a Mode B attach is a guaranteed 410 round trip,
        # so the TTS entity goes straight to Mode A instead.
        if stream.turn_id and speech:
            self.coordinator.turns.remember_speech(speech, stream.turn_id)
        _LOGGER.debug(
            "Converse done in %sms (feed=%s tokens=%s/%s)",
            int((time.monotonic() - started) * 1000),
            "live" if stream.turn_id else "none",
            stream.tokens_in,
            stream.tokens_out,
        )

        response.async_set_speech(speech)
        return ConversationResult(
            response=response,
            conversation_id=stream.conversation_id or conversation_id,
            continue_conversation=stream.continue_conversation,
        )

    def _error_result(
        self,
        response: intent.IntentResponse,
        conversation_id: str,
        started: float,
        *,
        category: str,
        short_lang: str,
        err: Exception,
        level: str,
    ) -> ConversationResult:
        self._record_latency(started, error=category)
        log = _LOGGER.error if level == "error" else _LOGGER.warning
        log("Converse failed (%s): %s", category, err)
        response.async_set_speech(ERROR_SPEECH[category][short_lang])
        return ConversationResult(
            response=response,
            conversation_id=conversation_id,
            continue_conversation=False,
        )

    def _short_language(self, language: str | None) -> str:
        """Short code (tr/en) used for local dormant/error speech lookup only."""
        if not language:
            return self._default_language
        short = language.split("-", 1)[0].lower()
        if short in SUPPORTED_LANGUAGES:
            return short
        return self._default_language

    def _record_latency(self, started: float, *, error: str | None) -> None:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        self.coordinator.record_converse_latency(elapsed_ms, error)
