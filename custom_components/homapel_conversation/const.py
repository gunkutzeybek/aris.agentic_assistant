"""Constants for the Homapel Conversation integration.

Config entry data shape (per ARCHITECTURE.md §7.7.3) is exactly:
    { api_key, unit_id, api_base }
plus one local extension set by the config flow but optional when the
installer provisioning script writes the entry directly:
    { default_language }
"""
from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "homapel_conversation"

# Config entry keys (canonical, match §7.7.3)
CONF_API_KEY: Final = "api_key"
CONF_UNIT_ID: Final = "unit_id"
CONF_API_BASE: Final = "api_base"
CONF_DEFAULT_LANGUAGE: Final = "default_language"

DEFAULT_API_BASE: Final = "https://api.homapel.com"
DEFAULT_LANGUAGE: Final = "tr"

# Short-form codes used only for dormant prompt selection & local fallback.
# The cloud wire contract (§7.3.2) uses full BCP-47 tags.
SUPPORTED_LANGUAGES: Final = ["tr", "en"]

POLL_INTERVAL: Final = timedelta(minutes=5)
CONVERSE_TIMEOUT: Final = 15
STATUS_TIMEOUT: Final = 5

DORMANT_PROMPT: Final = {
    "tr": (
        "Merhaba! Akıllı ev asistanınızı aktifleştirmek için "
        "homapel.com adresini ziyaret edin ve aktivasyon kodunuzu girin."
    ),
    "en": (
        "Hello! To activate your smart home assistant, "
        "visit homapel.com and enter your activation code."
    ),
}

# Categorized user-facing error speech. Keys map to internal error categories,
# not directly to §7.2 error.code values — several error.code values share one
# speech message (e.g. cost ceiling vs. rate limited both say "try again later").
ERROR_SPEECH: Final = {
    "network": {
        "tr": "Üzgünüm, şu anda ev asistanınıza ulaşamıyorum. Lütfen daha sonra tekrar deneyin.",
        "en": "Sorry, I can't reach your home assistant right now. Please try again later.",
    },
    "auth": {
        "tr": "Aboneliğiniz doğrulanamadı. Lütfen homapel.com üzerinden tekrar aktifleştirin.",
        "en": "Your subscription could not be verified. Please reactivate at homapel.com.",
    },
    "rate_limited": {
        "tr": "Şu anda çok fazla istek var. Lütfen bir dakika sonra tekrar deneyin.",
        "en": "Too many requests right now. Please try again in a minute.",
    },
    "cost_ceiling": {
        "tr": "Günlük kullanım sınırına ulaşıldı. Lütfen yarın tekrar deneyin.",
        "en": "Daily usage limit reached. Please try again tomorrow.",
    },
    "unknown": {
        "tr": "Bir sorun oluştu. Lütfen tekrar deneyin.",
        "en": "Something went wrong. Please try again.",
    },
}
