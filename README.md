# Homapel Conversation

The Home Assistant integration that makes your home a **Laris** home. It provides the complete Assist voice pipeline — speech-to-text, the conversation agent and text-to-speech — and proxies every utterance to the Laris cloud, which runs the agent, controls your home through Home Assistant and returns the spoken reply.

One API key covers everything. Speech providers are called with Laris's own credentials, server-side — **no provider key is ever stored on your Home Assistant instance.**

## What it does

- Registers a conversation agent, a speech-to-text engine and a text-to-speech engine for Assist
- Connects your home to the cloud automatically: the cloud reaches Home Assistant through the [HA-MCP](https://github.com/homeassistant-ai/ha-mcp) server's webhook, signed in with its own Home Assistant account
- Creates a ready-to-use **Laris** voice pipeline the first time it starts
- Without an active subscription, answers every utterance with a spoken prompt to subscribe — **no cloud call, no cost** — so you can install now and subscribe later
- Keeps subscription and connection state fresh via a 5-minute poll **and** a cloud webhook for instant updates
- Exposes diagnostics: subscription active, home connected, tier, cloud and speech latency

## Requirements

- Home Assistant **2026.6.0** or newer (Home Assistant OS, Supervised, Container or Core)
- [HACS](https://hacs.xyz/)
- The **HA-MCP Custom Component** (HACS repository `homeassistant-ai/ha-mcp-integration`) — this is how Laris controls your home
- A Laris account at **laris.homapel.com** (the API key is shown there; the subscription can be started later)

## Installation

### 1. Install HA-MCP

1. In HACS, search for **HA-MCP Custom Component** (or add `https://github.com/homeassistant-ai/ha-mcp-integration` as a custom repository, category *Integration*), install it and restart Home Assistant.
2. Go to **Settings → Devices & services → Add integration → HA-MCP** and add the **HA-MCP Server** entry. The first start downloads the server and can take a few minutes.

### 2. Install Homapel Conversation

1. In HACS, add `https://github.com/gunkutzeybek/aris.agentic_assistant` as a custom repository (category *Integration*) and install **Homapel Conversation**.
2. Restart Home Assistant.

### 3. Connect your home

1. Open **laris.homapel.com → Connect your home** and copy the API key.
2. Go to **Settings → Devices & services → Add integration → Homapel Conversation** and paste the key.

The integration validates the key against the cloud and then connects your home by itself:

| Step | What happens |
|---|---|
| HA-MCP check | Finds the HA-MCP Server entry. If it is missing or not started yet, you get instructions and a *Check again* button; you can also *Skip for now* and finish the connection later from **Reconfigure**. |
| Webhook auth | Switches the HA-MCP Server to **Home Assistant auth** on its webhook (asks first). HA-MCP restarts itself. |
| Credential | Creates a Home Assistant user named **Laris Cloud** (administrator) with a long-lived access token. The cloud uses this token to sign in to HA-MCP. You will see the user under **Settings → People** — do not delete it. The token itself is never written to disk by this integration. |
| Public address | Picks how the cloud reaches Home Assistant, in this order: **Home Assistant Cloud** remote UI → your https **external URL** → on Home Assistant OS/Supervised a **Laris-issued Cloudflare tunnel** (installs and starts the *Cloudflared* add-on with a token from the cloud; the tunnel only exposes the HA-MCP webhook path, nothing else) → otherwise a form asking for the public https address. |
| Registration | Registers the endpoint with the cloud, which immediately checks it can reach your home. If it cannot, you can retry, change the address, or finish anyway — a repair will remind you until the home is reachable. |

### 4. Voice

The first time the integration starts with voice enabled, it creates an Assist pipeline named **Laris** with Homapel for all three stages and makes it the preferred pipeline. It is created once and never modified afterwards, so any edits you make are kept.

Assign it to your voice satellite under **Settings → Voice assistants**. Wake-word detection stays local (`openWakeWord` / `microWakeWord`) — it never reaches the cloud.

To build a pipeline by hand: **Settings → Voice assistants → Add assistant**, then select Homapel for speech-to-text, conversation agent and text-to-speech. Voices are offered per language in the text-to-speech settings.

## Subscription

- **Dormant** — the key is linked but the subscription is not active. Every utterance gets a spoken prompt to start the subscription at laris.homapel.com. Speech works, so the prompt is heard on the satellite. Nothing is sent to the cloud.
- **Active** — normal operation. `binary_sensor.homapel_aris_active` is `on`.
- **Key regenerated** on the dashboard — within five minutes the integration notices the old key is rejected and Home Assistant shows a *Reauthenticate* prompt on the integration card (**Settings → Devices & services → Homapel Conversation**). Paste the new key there; the home connection is re-registered with it automatically.

## Home connection

- `binary_sensor.homapel_aris_home_connected` is `on` while the cloud reports it can reach your home. Its attributes show the source (`nabu_casa`, `external_url`, `tunnel`, `manual`) and the address used.
- The connection is re-registered automatically after every Home Assistant start and whenever HA-MCP regenerates its webhook id.
- **Reconfigure** (**Settings → Devices & services → Homapel Conversation → Reconfigure**) re-runs the connection steps — use it after changing your public address or HA-MCP settings, or to connect a home that was skipped during setup.
- Repairs: **"Laris cannot control your home yet"** means there is no connection (skipped, or HA-MCP removed); **"Laris cannot reach your home"** means the registered connection has been unreachable for more than 15 minutes.

## Removing the integration

Deleting the config entry tells the cloud to forget the connection, revokes the long-lived token and deletes the **Laris Cloud** user. The Laris pipeline, the HA-MCP component and the Cloudflared add-on are left in place.

## Speed

The three Assist stages are separate calls, each paying a round trip from the home. The integration lets the cloud overlap them: transcription starts while you are still speaking, and the reply is synthesized as the model writes it, so audio begins playing before the answer is finished.

This only applies to pipelines that use Homapel for **all three** stages. Mixed pipelines work normally, just without the overlap. See [Options](#options) if you want it off.

## Configuration

The config entry stores:

| Key | Meaning |
|---|---|
| `api_key`, `unit_id`, `api_base`, `default_language` | Cloud identity. `api_base` defaults to `https://api.homapel.com` and can be overridden in the *Advanced* section of the setup form (staging installs). `default_language` (`tr`/`en`) is used for the spoken subscription prompt when the pipeline does not specify a language. |
| `cloud_user_id`, `cloud_refresh_token_id` | The **Laris Cloud** Home Assistant user and its long-lived refresh token. The access token is minted from it on demand and never stored. |
| `mcp_entry_id`, `mcp_webhook_id` | The HA-MCP Server entry the connection points at and the webhook id registered with the cloud. |
| `connector_source`, `connector_base_url` | How the cloud reaches this Home Assistant. Absent when the connection was skipped. |
| `pipeline_created` | Set once the Laris pipeline has been created (or an existing one adopted). |

## Entities

| Entity | Purpose |
|---|---|
| `conversation.homapel_aris_homapel` | The conversation agent Assist talks to |
| `stt.homapel_aris_homapel` | Speech-to-text engine |
| `tts.homapel_aris_homapel` | Text-to-speech engine |
| `binary_sensor.homapel_aris_active` | `on` when the subscription is active |
| `binary_sensor.homapel_aris_home_connected` | `on` when the cloud can reach this Home Assistant |
| `sensor.homapel_aris_subscription_tier` | `dormant` / `basic` / `pro` |
| `sensor.homapel_aris_last_cloud_latency` | Last `/v1/converse` round-trip in ms |
| `sensor.homapel_aris_last_speech_to_text_latency` | Cloud-side transcription time in ms, excluding upload — attributes carry the last utterance length and reply size |

The speech entities appear only when voice is enabled for the home server-side. Entity names are translated (English and Turkish).

Entity ids are generated once, when each entity first appears, and Home Assistant never renames them afterwards. On installations from before 0.5.0 the speech entities may carry an area prefix (for example `stt.oturma_odasi_homapel_aris_homapel`) because they registered before the device had a name. That is cosmetic — the integration always resolves its own entities through the registry, never by id.

## Options

**Settings → Devices & services → Homapel Conversation → Configure**

| Option | Default | Notes |
|---|---|---|
| Cloud idle timeout | 90 s | How long the cloud may go silent mid-request before the call is aborted. Bounds model think-time; the request as a whole is unbounded so long answers aren't truncated |
| Speed up spoken replies | on | Lets the cloud start generating speech while the answer is still being written. Applies only to pipelines that are Homapel end-to-end; turn off to make each step wait for the previous one |

## Languages

Turkish and English. The pipeline's language is passed through to the cloud per utterance; the **Default language** option only controls the spoken subscription prompt.

Speech-to-text and text-to-speech advertise their supported languages separately — a language may be available for transcription but not synthesis.

## Troubleshooting

- **"The API key was rejected by the Laris cloud"** — copy the key again from laris.homapel.com → Connect your home. If the key was regenerated there, wait for the *Reauthenticate* prompt on the integration card (or restart Home Assistant) and paste the new key.
- **"Could not reach the Laris cloud"** — check outbound internet; the integration needs HTTPS access to `api.homapel.com`.
- **Every utterance is the subscription prompt** — the subscription is not active, or the daily usage limit was reached. Check laris.homapel.com.
- **"HA-MCP Server not found"** — install the HA-MCP Custom Component from HACS, restart, add the **HA-MCP Server** entry, then choose *Check again* (or run **Reconfigure** later). Home Assistant 2026.6.0 or newer is required.
- **"Laris could not reach your home"** — HA-MCP may still be starting (wait a minute and retry); check that the HA-MCP Server entry is loaded and that remote access (Home Assistant Cloud, your external URL or the Cloudflared add-on) is up; confirm the public address is https. Then **Reconfigure**.
- **Laris answers but does not control anything** — the home is not connected. Look for the repair *"Laris cannot control your home yet"* and run **Reconfigure**.
- **No Homapel option under speech-to-text or text-to-speech** — voice is not enabled for this home server-side. The entities are only created when the cloud advertises them; restart Home Assistant after it is enabled.
- **Assist says it didn't understand, but the microphone works** — silence and background noise are filtered in the cloud and return an empty transcript by design, which is what prevents a noisy room from inventing commands.
- **Replies are slower than expected** — check `sensor.homapel_aris_last_speech_to_text_latency`. It measures only the cloud's own transcription time, so a high value points at the cloud, and a low value with slow replies points at the network between the home and Laris.

## Development

Tests use [pytest-homeassistant-custom-component](https://github.com/MatthewFlamm/pytest-homeassistant-custom-component), which pins a Home Assistant version (currently 2026.8.3, Python 3.14). Home Assistant does not install on Windows — use Linux, WSL or a container.

```sh
pip install -e ".[test]"        # or: uv pip install -e ".[test]"
ruff check custom_components tests
pytest tests -q
```

CI (`.github/workflows/validate.yaml`) runs HACS validation, hassfest, ruff and the test suite. A release is a `manifest.json` version bump plus a `vX.Y.Z` tag.

## License

See [LICENSE](LICENSE).
