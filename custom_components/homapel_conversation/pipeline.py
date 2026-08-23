"""Create the "Laris" Assist pipeline once — Homapel for all three stages.

``assist_pipeline.async_create_default_pipeline`` pins the conversation
agent to Home Assistant's built-in one, so the pipeline is written through
the pipeline store directly with the same field set HA's own default uses.

Rules:
* created only after the conversation, stt and tts entities all exist (voice
  must be enabled for the unit), and only once — ``pipeline_created`` in
  ``entry.data`` records it, so a deleted or edited pipeline is never
  recreated or touched again;
* set as the preferred pipeline at creation time, nothing else;
* an existing pipeline that already uses our conversation/stt/tts entities (or
  is already called "Laris") is adopted as-is — never duplicated, never edited,
  and it keeps whatever preferred flag it has.
"""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.util import language as language_util

from .const import (
    CONF_DEFAULT_LANGUAGE,
    CONF_PIPELINE_CREATED,
    DEFAULT_LANGUAGE,
    DOMAIN,
    PIPELINE_NAME,
)

_LOGGER = logging.getLogger(__name__)


@callback
def _own_entity_id(hass: HomeAssistant, entry: ConfigEntry, domain: str, suffix: str) -> str | None:
    registry = er.async_get(hass)
    return registry.async_get_entity_id(domain, DOMAIN, f"{entry.entry_id}_{suffix}")


def _pick_language(preferred: list[str], supported: list[str], country: str | None) -> str | None:
    for candidate in preferred:
        matches = language_util.matches(candidate, supported, country=country)
        if matches:
            return matches[0]
    return None


async def async_ensure_laris_pipeline(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Create the pipeline if it has never been created. Returns True if created."""
    if entry.data.get(CONF_PIPELINE_CREATED):
        return False

    conversation_id = _own_entity_id(hass, entry, "conversation", "conversation")
    stt_id = _own_entity_id(hass, entry, "stt", "stt")
    tts_id = _own_entity_id(hass, entry, "tts", "tts")
    if not (conversation_id and stt_id and tts_id):
        _LOGGER.debug("Voice entities not all present yet; not creating the pipeline")
        return False

    try:
        from homeassistant.components import stt, tts
        from homeassistant.components.assist_pipeline.pipeline import KEY_ASSIST_PIPELINE
        from homeassistant.components.conversation import async_get_conversation_languages
    except ImportError:  # pragma: no cover - HA without assist_pipeline
        return False

    pipeline_data = hass.data.get(KEY_ASSIST_PIPELINE)
    if pipeline_data is None:
        _LOGGER.debug("assist_pipeline not loaded; not creating the pipeline")
        return False
    store = pipeline_data.pipeline_store

    # Adopt any pipeline that already routes through this integration — by
    # engine, not by name. Installs that predate this feature have a working
    # hand-made pipeline (often called "Homapel"); creating a second one and
    # stealing "preferred" from it would be a regression, not an upgrade.
    existing = next(
        (
            p
            for p in store.async_items()
            if p.conversation_engine == conversation_id
            or p.name == PIPELINE_NAME
            or (p.stt_engine == stt_id and p.tts_engine == tts_id)
        ),
        None,
    )
    if existing is not None:
        _LOGGER.info(
            "Adopting the existing %r pipeline; not creating %r",
            existing.name,
            PIPELINE_NAME,
        )
        _mark_created(hass, entry)
        return False

    stt_engine = stt.async_get_speech_to_text_engine(hass, stt_id)
    tts_engine = tts.get_engine_instance(hass, tts_id)
    if stt_engine is None or tts_engine is None:
        return False

    country = hass.config.country
    preferred = [hass.config.language, entry.data.get(CONF_DEFAULT_LANGUAGE, DEFAULT_LANGUAGE)]
    try:
        agent_languages = async_get_conversation_languages(hass, conversation_id)
    except ValueError:
        _LOGGER.debug("Conversation agent %s not registered yet", conversation_id)
        return False
    if agent_languages == "*":
        conversation_language: str | None = preferred[0]
    else:
        conversation_language = _pick_language(preferred, list(agent_languages), country)
    stt_language = _pick_language(preferred, list(stt_engine.supported_languages), country)
    tts_language = _pick_language(preferred, list(tts_engine.supported_languages), country)
    if not (conversation_language and stt_language and tts_language):
        _LOGGER.warning(
            "No common language for the Laris pipeline (ha=%s, default=%s); skipping",
            hass.config.language,
            preferred[1],
        )
        return False

    tts_voice = None
    voices = tts_engine.async_get_supported_voices(tts_language)
    if voices:
        tts_voice = voices[0].voice_id

    pipeline = await store.async_create_item(
        {
            "conversation_engine": conversation_id,
            "conversation_language": conversation_language,
            "language": conversation_language,
            "name": PIPELINE_NAME,
            "stt_engine": stt_id,
            "stt_language": stt_language,
            "tts_engine": tts_id,
            "tts_language": tts_language,
            "tts_voice": tts_voice,
            "wake_word_entity": None,
            "wake_word_id": None,
            "prefer_local_intents": False,
        }
    )
    store.async_set_preferred_item(pipeline.id)
    _LOGGER.info("Created the %r Assist pipeline and made it preferred", PIPELINE_NAME)
    _mark_created(hass, entry)
    return True


@callback
def _mark_created(hass: HomeAssistant, entry: ConfigEntry) -> None:
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_PIPELINE_CREATED: True}
    )
