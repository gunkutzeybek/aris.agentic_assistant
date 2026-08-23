"""Constants for the Homapel Conversation integration.

Config entry data written by the config flow (the customer's only setup path):

    api_key, unit_id, api_base, default_language   — cloud identity
    cloud_user_id, cloud_refresh_token_id          — the "Laris Cloud" HA user
                                                     whose long-lived token the
                                                     cloud presents to ha-mcp
    mcp_entry_id, mcp_webhook_id                   — the ha_mcp_tools server entry
                                                     the connector points at
    connector_source, connector_base_url           — how the cloud reaches HA
    pipeline_created                               — the "Laris" Assist pipeline
                                                     was created once

The access token itself is never persisted: it is minted from the refresh
token whenever the connector is (re-)registered with the cloud.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "homapel_conversation"

# Config entry keys — cloud identity.
CONF_API_KEY: Final = "api_key"
CONF_UNIT_ID: Final = "unit_id"
CONF_API_BASE: Final = "api_base"
CONF_DEFAULT_LANGUAGE: Final = "default_language"

# Config entry keys — home connector (see module docstring).
CONF_CLOUD_USER_ID: Final = "cloud_user_id"
CONF_CLOUD_REFRESH_TOKEN_ID: Final = "cloud_refresh_token_id"
CONF_MCP_ENTRY_ID: Final = "mcp_entry_id"
CONF_MCP_WEBHOOK_ID: Final = "mcp_webhook_id"
CONF_CONNECTOR_SOURCE: Final = "connector_source"
CONF_CONNECTOR_BASE_URL: Final = "connector_base_url"
CONF_PIPELINE_CREATED: Final = "pipeline_created"

# Options-only (set via OptionsFlow).
CONF_CONVERSE_SOCK_READ: Final = "converse_sock_read"
CONF_UNIFIED_PIPELINE: Final = "unified_pipeline"

# The Laris cloud API. Documented in the README; overridable per entry from the
# advanced section of the config flow (staging installs).
DEFAULT_API_BASE: Final = "https://api.homapel.com"
DEFAULT_LANGUAGE: Final = "tr"

# Where the customer manages the subscription and finds the API key.
DASHBOARD_URL: Final = "https://laris.homapel.com"

# --- Home connector (ha-mcp webhook + ha_auth) --------------------------------
# The ha-mcp in-process server ("HA-MCP Custom Component", HACS repo
# homeassistant-ai/ha-mcp-integration, HA >= 2026.6).
MCP_DOMAIN: Final = "ha_mcp_tools"
MCP_ENTRY_TYPE_KEY: Final = "entry_type"
MCP_ENTRY_TYPE_SERVER: Final = "server"
MCP_DATA_WEBHOOK_ID: Final = "webhook_id"
MCP_OPT_WEBHOOK_AUTH: Final = "webhook_auth"
MCP_OPT_ENABLE_WEBHOOK: Final = "enable_webhook"
MCP_WEBHOOK_AUTH_HA: Final = "ha_auth"
MCP_HACS_URL: Final = (
    "https://my.home-assistant.io/redirect/hacs_repository/"
    "?owner=homeassistant-ai&repository=ha-mcp-integration&category=integration"
)
MCP_MIN_HA_VERSION: Final = "2026.6.0"
# How long to wait for ha-mcp to bring its webhook up after we flip its options
# (the first bring-up pip-installs the server and can take minutes).
MCP_WEBHOOK_WAIT_TIMEOUT: Final = 300
# How long to wait for ha-mcp to *start* reloading after we change its options
# (its update listener runs as a task; normally it has begun within a tick).
MCP_RELOAD_GRACE: Final = 10
# At HA start the bring-up may still be installing; wait longer before the
# re-probe so the cloud does not record a spurious "unreachable".
MCP_WEBHOOK_STARTUP_WAIT_TIMEOUT: Final = 900

# The HA user + long-lived token the cloud authenticates with (ha_auth mode).
CLOUD_USER_NAME: Final = "Laris Cloud"
CLOUD_TOKEN_CLIENT_NAME: Final = "Laris Cloud"
CLOUD_ACCESS_TOKEN_DAYS: Final = 3650

CONNECTOR_SOURCE_NABU_CASA: Final = "nabu_casa"
CONNECTOR_SOURCE_EXTERNAL_URL: Final = "external_url"
CONNECTOR_SOURCE_TUNNEL: Final = "tunnel"
CONNECTOR_SOURCE_MANUAL: Final = "manual"

# Cloudflared add-on (brenner-tobias/ha-addons) used for the cloud-issued tunnel
# on Supervisor installs with no public URL.
CLOUDFLARED_ADDON_SLUG: Final = "9074a9fa_cloudflared"
CLOUDFLARED_ADDON_NAME: Final = "Cloudflared"
CLOUDFLARED_REPOSITORY_URL: Final = "https://github.com/brenner-tobias/ha-addons"
CLOUDFLARED_OPT_TUNNEL_TOKEN: Final = "tunnel_token"
CLOUDFLARED_START_TIMEOUT: Final = 120

# PUT /v1/units/connector probes HA synchronously (10 s on the cloud side).
CONNECTOR_TIMEOUT: Final = 30
# POST /v1/units/tunnel talks to Cloudflare.
TUNNEL_TIMEOUT: Final = 60

# Repair issues.
ISSUE_HOME_NOT_CONNECTED: Final = "home_not_connected"
ISSUE_HOME_UNREACHABLE: Final = "home_unreachable"
# Connector configured but not reachable for this long before we raise an issue.
UNREACHABLE_GRACE: Final = timedelta(minutes=15)

# The Assist pipeline created once after the first successful setup.
PIPELINE_NAME: Final = "Laris"

# Short-form codes used only for dormant prompt selection & local fallback.
# The cloud wire contract (§7.3.2) uses full BCP-47 tags.
SUPPORTED_LANGUAGES: Final = ["tr", "en"]

POLL_INTERVAL: Final = timedelta(minutes=5)
# Idle/inactivity timeout on the converse socket. Bounds the gap between
# bytes from the cloud (think-time before JSON, or gap between SSE deltas);
# the request as a whole is unbounded so long answers don't get truncated.
DEFAULT_CONVERSE_SOCK_READ: Final = 90
STATUS_TIMEOUT: Final = 5

# --- Voice (VOICE_API_AS_BUILT.md) -------------------------------------------
# `eager=true` is the consent flag that lets the cloud pre-synthesize TTS during
# the converse stream. Only sent when the pipeline is Homapel end-to-end; see
# stt.py::_eager_enabled. Users who deliberately mix engines can force it off.
DEFAULT_UNIFIED_PIPELINE: Final = True

STT_TIMEOUT: Final = 30
TTS_TIMEOUT: Final = 60

# Correlation window between pipeline stages. Matches the cloud's 30 s turn TTL —
# a stale entry can only cost one wasted Mode B attempt that falls back to Mode A.
TURN_TTL: Final = 30

# The cloud implements mp3 only (§4); wav/opus return 422.
TTS_AUDIO_FORMAT: Final = "mp3"

# Assist streams 16-bit mono PCM; used to bound the upload client-side (§3).
PCM_BYTES_PER_SAMPLE: Final = 2

# Spoken when the unit has no active subscription. Never reaches the cloud.
DORMANT_PROMPT: Final = {
    "tr": (
        "Merhaba! Laris'i kullanmaya başlamak için "
        "laris.homapel.com adresinden aboneliğinizi başlatın."
    ),
    "en": "Hello! To start using Laris, start your subscription at laris.homapel.com.",
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
        "tr": "Aboneliğiniz doğrulanamadı. Lütfen laris.homapel.com üzerinden kontrol edin.",
        "en": "Your subscription could not be verified. Please check laris.homapel.com.",
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
