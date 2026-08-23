# CLAUDE.md

Guidance for Claude (and humans) working in this repository.

---

## 1. What this is

**Homapel Conversation** (`custom_components/homapel_conversation/`, HA domain `homapel_conversation`) — the Home Assistant custom integration that makes a home a **Laris** home. It registers the three Assist pipeline stages — `stt.homapel`, `conversation.homapel`, `tts.homapel` — and proxies every utterance to the Laris cloud (`agentic_service`), which runs the agent, calls back into HA via ha-mcp, and returns the spoken reply. One **API key** authenticates all three stages; no speech-provider credentials ever live in HA.

**Laris** is Homapel's voice-first smart-home AI. Sibling repos under `D:\Homapel\Projects\Aris\`: `agentic_service` (cloud backend — **read its `CLAUDE.md` §1a for the business model**), `dashboard` (`laris.homapel.com`, where the customer gets the API key), `homapel_insights` + `laris_insights` (proactive layer; the edge integration reuses the same key).

Languages: Turkish (`tr`) and English (`en`). Wake-word detection stays local (openWakeWord / microWakeWord) and never reaches the cloud.

---

## 2. Business model & onboarding — B2C (decided 2026-08-23)

**The model changed.** This integration was written for B2B: Homapel's installer tool called `POST /v1/units/register`, seeded the resulting `api_key` into HA's `.storage/` over the REST API, and "the resident never opens Home Assistant". The config flow was documented as a *fallback for manual provisioning*. **That is retired.**

**Now Laris is sold B2C**, directly and through dealers. The customer buys a Laris Voice device (optionally a Homapel HA server with this integration pre-installed), subscribes on the dashboard, is shown their API key there, and **installs and configures this integration themselves through HACS**. The config flow is the primary — and only — provisioning path.

### Target customer path

1. Install HA (or plug in the Homapel HA server) → HACS → install the **HA-MCP Custom Component** (`homeassistant-ai/ha-mcp-integration`, domain `ha_mcp_tools`, HA ≥ 2026.6) and add its **HA-MCP Server** entry → add this repo as a custom repository (later: HACS default store) → install → restart.
2. *Settings → Devices & services → Add integration → Homapel Conversation* → paste the API key from `laris.homapel.com` → the flow validates it on `GET /v1/units/status` (a dormant unit validates), then **connects the home by itself** (see the connector design below) and stores the `entry.data` listed in `const.py`'s module docstring.
3. On the first setup with voice enabled the integration creates the **"Laris"** Assist pipeline (Homapel for STT, conversation, TTS) and makes it preferred — once, never touched again.
4. If the subscription isn't active yet, every utterance gets the dormant prompt (no cloud call, no cost) — it sends the customer to the dashboard to **subscribe**.
5. Key rotated from the dashboard → the coordinator raises `ConfigEntryAuthFailed` on the 401 → HA's **reauth** flow asks for the new key; the reload re-registers the connector with it.

### Status per area (built on branch `feature/b2c-connector`, 2026-08-23)

| Area | Today |
|---|---|
| Config-flow copy | Customer-facing in `strings.json` / `translations/{en,tr}.json`: "Paste the API key from your Laris dashboard"; URLs only through placeholders (`{dashboard_url}`, `{hacs_url}`, `{example_url}` — hassfest rejects literal URLs). No installer/provisioning wording anywhere |
| Dormant prompt | `const.py` `DORMANT_PROMPT` / `ERROR_SPEECH["auth"]` point at **laris.homapel.com** in TR and EN (exact strings from `../B2C_PROMPTS.md`) |
| README | Self-install guide: HA-MCP → this integration → key → automatic connection → Laris pipeline → troubleshooting; Speed/Options/Entities kept; entry.data keys documented |
| Code docstrings | `config_flow.py` documents the step graph; `const.py` documents the entry.data keys; no `/v1/units/register` / `ARCHITECTURE.md` references |
| Reauth | `async_step_reauth` / `reauth_confirm` (`unit_mismatch` abort when the key belongs to another unit); triggered by `ConfigEntryAuthFailed` from the coordinator |
| Reconfigure | `async_step_reconfigure` → confirmation form → re-runs only the connector steps (new URL, regenerated webhook id, HA-MCP installed later). Skipping there also `DELETE`s the cloud connector so both sides agree |
| Default `api_base` | `DEFAULT_API_BASE = "https://api.homapel.com"` (single constant, documented in the README); overridable in the *Advanced* section of the user step. Production host still to be confirmed — the live backend is `staging.api.homapel.com` |
| How the cloud reaches this HA | **Built.** ha-mcp webhook (`<base>/api/webhook/<webhook_id>`) in `ha_auth` mode with a long-lived token of the "Laris Cloud" admin user that `connector.py` provisions (`local_only=False`, 10-year access token; only `cloud_user_id`/`cloud_refresh_token_id` are stored). The flow flips the `ha_mcp_tools` server entry's options to `webhook_auth=ha_auth`, `enable_webhook=True`, waits for its reload + live webhook, picks the base URL (Nabu Casa remote UI → https `external_url` → on Supervisor a cloud-issued Cloudflare tunnel via the Cloudflared add-on `9074a9fa_cloudflared` from `brenner-tobias/ha-addons`, options `{tunnel_token}` → manual https form) and `PUT /v1/units/connector`. `ConnectorManager` re-PUTs (fresh bearer) after HA start and on a webhook-id change. No secret path |
| Tests / tooling | `pyproject.toml` (pytest + `pytest-homeassistant-custom-component` + ruff), `tests/` (config flow incl. tunnel, reauth/reconfigure, runtime, api — 116 tests), CI = hacs + hassfest + ruff/pytest |
| Manifest | `documentation`/`issue_tracker` → `github.com/gunkutzeybek/aris.agentic_assistant` (the repo customers install from); keys sorted for hassfest; `after_dependencies: assist_pipeline, cloud, hassio`; `hacs.json` requires HA 2026.6.0. `DeviceInfo` still says "Homapel Aris" / model "Aris" — product-level rename, don't change without being asked |

Dealers (planned, not built) have **no footprint here** — attribution happens at sign-up on the dashboard.

---

## 3. Wire contract with the cloud (`api.py`)

All calls: `Authorization: Bearer {api_key}`. The key alone identifies the unit; `unit_id` is never sent. Error bodies are `{"error": {"code", "message"}}`; `X-Request-Id` is captured into exceptions.

| Call | Purpose | Notes |
|---|---|---|
| `GET /v1/units/status` | validate key; poll every 5 min (`POLL_INTERVAL`) | `{unit_id, active, tier, webhook_token, cost_ceiling_reached, updated_at, stt{…}, tts{…}}`; ETag / `If-None-Match` → 304. `active` already folds in the cost ceiling. STT/TTS entities exist only when their block says `enabled` (evaluated at setup → restart after enabling) |
| `POST /v1/units/webhook` | register HA's webhook URL for instant status pushes | best-effort; failure only warns. Inbound body `{unit_id, active, tier, token, reason, timestamp}`, token checked with `hmac.compare_digest`; 410 before first refresh |
| `POST /v1/converse` | SSE stream of the turn | `{text, conversation_id, language, device_id?, turn_id?, context{area_id, speaker_id}?}`; `sock_read` timeout only — total is unbounded |
| `POST /v1/stt` | chunked PCM upload, `audio/L16` | `eager=true` = the cloud may start the turn from the transcript (unified-pipeline speed-up) |
| `POST /v1/tts` | mp3 for text, or for a `turn_id` already synthesized | reads `X-Homapel-Characters` |
| `PUT /v1/units/connector` | register how the cloud reaches ha-mcp | `{mcp_url, bearer, source, ha_version?, component_version?}` → `{reachable, checked_at, error?, tool_count?}`; the cloud probes `tools/list` synchronously (10 s) before answering → `CONNECTOR_TIMEOUT` 30 s. Called by the flow, by `ConnectorManager` after HA start / webhook-id change, after reauth (via the reload) |
| `GET /v1/units/connector` | stored connector state | `{configured, reachable, source, last_ok_at, last_error}`; not used at runtime today (status carries `connector{configured, reachable}`) |
| `DELETE /v1/units/connector` | forget the connector | on entry removal and when reconfigure skips the connector; best-effort |
| `POST /v1/units/tunnel` | cloud-issued Cloudflare tunnel | `{hostname, tunnel_token}`; 501 `tunnel_not_configured` → manual URL form. The token goes straight into the Cloudflared add-on options and is never stored |

`GET /v1/units/status` also returns `connector{configured, reachable}` (absent on an older cloud → `HomapelState.connector_*` are `None`).

Error mapping: 401 → auth (coordinator: `ConfigEntryAuthFailed` → reauth), 403 → forbidden (suspended), 422 `unit_not_active` → dormant prompt + refresh, other 422 (e.g. `invalid_mcp_url`) → `HomapelInvalidRequestError`, 429 `cost_ceiling_exceeded` → cost-ceiling speech, other 429 → rate-limited (`Retry-After`), 501 → `HomapelTunnelNotConfiguredError`, other 5xx → network. Each spoken one has localized `ERROR_SPEECH`.

**This contract is owned by `agentic_service`.** Read that repo to understand it; change it there and here together, never silently.

---

## 4. Repository map

```
custom_components/homapel_conversation/
  __init__.py        setup: client → coordinator → first refresh → webhook registration → platforms
                     → ConnectorManager → Laris pipeline; async_remove_entry revokes the credential + DELETEs the connector
  config_flow.py     steps: user (key + advanced section) → mcp_check → [mcp_missing | mcp_not_loaded menus]
                     → mcp_enable_auth (confirm) → mcp_wait (progress) → connector_url → [tunnel_check →
                     tunnel_replace | tunnel_install (progress) | tunnel_failed] | manual_url → connector_register
                     (progress) → [connector_unreachable | connector_error menus] → finish / skip_connector /
                     finish_anyway; reauth → reauth_confirm; reconfigure (confirm form) → mcp_check…; options flow
  connector.py       ha-mcp entry lookup, ha_auth flip + McpReloadWatcher, "Laris Cloud" credential provisioning /
                     revocation, base-URL detection (Nabu Casa / external_url), async_register_connector,
                     ConnectorManager (re-PUT on HA start + webhook-id change)
  tunnel.py          Cloudflared add-on via hassio AddonManager + Supervisor store (repository, install, options, start)
  pipeline.py        one-time "Laris" Assist pipeline through the pipeline store (+ preferred)
  coordinator.py     DataUpdateCoordinator: 5-min poll with ETag, webhook merge (active/tier only), connector state,
                     repair issues home_not_connected / home_unreachable (15-min grace), ConfigEntryAuthFailed on 401
  api.py             HomapelCloudClient — every cloud call + error mapping
  conversation.py    ConversationEntity: dormant short-circuit, SSE turn, continue_conversation
  stt.py / tts.py    speech entities, created only when the cloud advertises them
  binary_sensor.py   Active · Home connected (both CONNECTIVITY) · sensor.py  tier / cloud latency / STT latency
  entity.py          the shared DeviceInfo ("Homapel Aris") every platform uses
  webhook.py         inbound status push handler
  const.py           DOMAIN, DEFAULT_API_BASE, entry.data keys, ha-mcp / Cloudflared constants, POLL_INTERVAL,
                     DORMANT_PROMPT, ERROR_SPEECH, option defaults
  strings.json, translations/{en,tr}.json, manifest.json
tests/               conftest.py (CloudMock over aioclient_mock, fake ha_mcp_tools server entry), test_config_flow*.py,
                     test_reauth_reconfigure.py, test_runtime.py, test_api.py, test_smoke.py
pyproject.toml       pytest config + `[test]` extras + ruff (HA-style isort sections)
docs/                VOICE_API_SPEC.md, TURN_CACHE_DESIGN.md — both self-marked superseded; history only
.github/workflows/   validate.yaml (hacs + hassfest + ruff/pytest), release.yaml (tag v* must equal manifest version → zip release)
hacs.json            HA ≥ 2026.6.0 (ha-mcp's in-process server needs it)
```

---

## 5. Conventions & hard gates

1. **Don't break the Assist contract or the cloud contract.** `ConversationResult` semantics (`continue_conversation`), entity ids, and `entry.data` keys are user-visible; changing them needs a migration (`VERSION`/`async_migrate_entry`) and a note in the README.
2. **Every user-visible string in both `en` and `tr`** (`strings.json` = `translations/en.json`, plus `tr.json`). Spoken prompts live in `const.py` and must be natural TR/EN.
3. **The dormant path must never call the cloud** — it is what makes an unpaid home cost zero.
4. **No third-party requirements** in `manifest.json` — use HA's bundled `aiohttp`/`voluptuous`.
5. **Release = bump `manifest.json` version + tag `vX.Y.Z`**; the release workflow fails on mismatch.
6. **Product is Laris; identifiers still say Aris/Homapel** (`DeviceInfo`, entity ids `homapel_aris_*`, repo name). Cosmetic — don't mass-rename without being asked.
7. Feature branch → PR → `main`. **Add tests with new behaviour** — the harness is `pytest-homeassistant-custom-component` (HA does not install on Windows; run the suite in WSL/Linux, see the README's Development section). `ruff check`, `pytest tests` and hassfest must be green before a PR.
8. **Secrets never touch `entry.data` or logs**: the "Laris Cloud" access token and the Cloudflare tunnel token are minted/received, sent, and dropped. Only the user id and refresh-token id are stored.
9. Config-flow steps reached through `async_show_progress_done` must ignore foreign `user_input` (HA re-enters the next step with the previous step's input when the task finishes eagerly), and the first step of any flow source must be a form/menu, never a progress step.

---

## 6. Gotchas

- A **dormant unit validates fine** in the config flow (`/units/status` returns 200 with `active: false`) — dormancy is a runtime state, not a setup error. Keep it that way; the customer may install before paying.
- The STT/TTS entities and the STT-latency sensor are created only if the capability was enabled at setup time → the cloud enabling voice later needs an HA restart (and the Laris pipeline is only created once those entities exist).
- Entity ids are `<device name>_<entity name>`: `conversation.homapel_aris_homapel`, `binary_sensor.homapel_aris_home_connected`, … All platforms share `entity.homapel_device_info()` so the device name exists before any entity registers. **Verified on the real dev box (2026-08-23):** before that fix, `stt`/`tts`/`stt_latency` registered against a nameless device and HA fell back to the *area* name — that install has `stt.oturma_odasi_homapel_aris_homapel` for good (ids are never regenerated). Never resolve our own entities by id; go through the registry by `unique_id`.
- **A pre-0.5 install already has a working Assist pipeline** — on the dev box it is named "Homapel", not "Laris", is preferred and has `prefer_local_intents: true`. `pipeline.py` therefore adopts any pipeline whose conversation engine (or stt+tts pair) is ours, whatever its name; matching on the name alone would create a duplicate and steal the preferred flag on every upgrade.
- **ha-mcp registers its webhook only at the end of its background bring-up** (first start pip-installs the server, minutes). "Entry loaded" ≠ "endpoint live" — `connector.async_is_webhook_live` polls `hass.data["webhook"]`, and `ConnectorManager` waits up to 15 min at HA start before re-probing so the cloud doesn't record a spurious "unreachable".
- Flipping ha-mcp's options makes it reload itself *asynchronously*; `McpReloadWatcher` watches the entry's state transitions (`SIGNAL_CONFIG_ENTRY_CHANGED`) so the flow doesn't probe the pre-reload server.
- `ConnectorManager` re-PUTs on every setup while HA is running, so "config flow → entry created" produces two PUTs in a row (the cloud treats them as idempotent; tests assert `== 2`).
- Progress tasks can finish eagerly (HA eager task start): the flow manager then runs straight through `progress_done` into the next step, passing the *previous* step's `user_input` along — hence the `CONF_BASE_URL in user_input` guard in `manual_url` and the confirmation form in front of `reconfigure`.
- A Cloudflared add-on that already holds a customer's own `tunnel_token` is never overwritten silently — the `tunnel_replace` menu asks first.
- The Laris pipeline is created once (`pipeline_created` flag); a deleted or edited pipeline is never recreated or touched. HA refuses to delete the *preferred* pipeline, so the customer must prefer another one first.
- The webhook URL sent to the cloud is whatever `webhook.async_generate_url` produces — i.e. HA's configured external/internal URL. If the home isn't reachable from the internet the push silently never arrives and the 5-minute poll is the only sync.
- `docs/` cite `ARCHITECTURE.md`, `VOICE_API_AS_BUILT.md`, `VOICE_GATEWAY_BACKEND.md` — none live in this repo (they're in `agentic_service` / the project root), and `ARCHITECTURE.md`'s business-model sections are historical.
