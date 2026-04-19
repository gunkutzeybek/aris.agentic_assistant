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
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import intent
from homeassistant.helpers.device_registry import DeviceInfo
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
    CONF_DEFAULT_LANGUAGE,
    CONF_UNIT_ID,
    DEFAULT_LANGUAGE,
    DOMAIN,
    DORMANT_PROMPT,
    ERROR_SPEECH,
    SUPPORTED_LANGUAGES,
)
from .coordinator import HomapelCoordinator

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
    _attr_name = "Homapel"
    _attr_supported_features = conversation.ConversationEntityFeature(0)

    def __init__(self, coordinator: HomapelCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._default_language = entry.data.get(CONF_DEFAULT_LANGUAGE, DEFAULT_LANGUAGE)
        self._attr_unique_id = f"{entry.entry_id}_conversation"
        unit_id = entry.data[CONF_UNIT_ID]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, unit_id)},
            name="Homapel Aris",
            manufacturer="Homapel",
            model="Aris",
        )

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

        area_id = self._area_id_for(user_input.device_id) if user_input.device_id else None

        started = time.monotonic()
        try:
            result = await self.coordinator.client.converse(
                self.coordinator.api_key,
                text=user_input.text,
                conversation_id=conversation_id,
                language=wire_lang,
                device_id=user_input.device_id,
                area_id=area_id,
            )
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

        # Cloud can signal dormant per-request (§7.3.2) even if our cached
        # status is stale. Its speech already contains the activation prompt,
        # but we still want to refresh coordinator state.
        if result.dormant:
            await self.coordinator.async_request_refresh()

        response.async_set_speech(result.speech)
        return ConversationResult(
            response=response,
            conversation_id=result.conversation_id,
            continue_conversation=result.continue_conversation,
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

    def _area_id_for(self, device_id: str | None) -> str | None:
        if not device_id:
            return None
        registry = dr.async_get(self.hass)
        device = registry.async_get(device_id)
        return device.area_id if device else None

    def _record_latency(self, started: float, *, error: str | None) -> None:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        self.coordinator.record_converse_latency(elapsed_ms, error)
