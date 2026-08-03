# Homapel Voice API — STT & TTS Gateway Specification

> **⚠️ Superseded as a wire contract.** This was the integration side's *proposal*.
> The backend answered it with `VOICE_GATEWAY_BACKEND.md` and then shipped
> `VOICE_API_AS_BUILT.md` — **code against the as-built document.** Known
> divergences: dormant units are served rather than getting `422 unit_not_active`;
> `enabled` is a feature flag, not tier entitlement (no `tier_not_allowed`);
> `mp3` is the only format; §7.3.7 eager continuation is *not* built (Phase 2b).
> Kept here for the reasoning — particularly §3, which is why the design streams.

**Audience:** the developer implementing the Homapel cloud API (`api.homapel.com`).
**Extends:** `ARCHITECTURE.md` Boundary A (§7.3). New sections proposed as §7.3.5 (STT),
§7.3.6 (TTS), §7.3.7 (turn continuity), plus additions to §7.3.3 (status).

This document specifies **only what the Home Assistant integration needs**. Everything
here is a contract the integration will code against.

---

## 1. Purpose

Users must configure exactly **one** credential — the existing Homapel `api_key` — and
get conversation, speech-to-text, and text-to-speech from it. Upstream providers
(GroqCloud Whisper for STT, Google Cloud TTS for TTS) are called with Homapel-owned
credentials, server-side. **No upstream provider key is ever sent to a Home Assistant
instance.** HA stores config entry data as plaintext in `.storage/core.config_entries`,
so any key delivered to a client must be treated as public.

## 2. Non-goals

- Per-user upstream provider keys ("merchant keys"). Neither Groq nor Google supports
  per-subkey spend or rate enforcement; a leaked key would be uncappable. Rejected.
- Wake word detection. Stays local in HA (`openWakeWord`/`microWakeWord`).
- Changing the existing `/v1/converse` contract. Additive only.
- New config flow steps, second API key, or new config entry fields.

## 3. The latency constraint — read this first

The naive implementation of this spec is **slower than what users have today** and must
not be built. Store-and-forward (receive whole audio → forward whole audio → receive
whole result → forward whole result) adds a full serial leg to every stage.

Three properties make the gateway *faster* than direct-to-provider. All three are
requirements, not optimizations:

1. **Terminate TCP close to the user.** Users are in Turkey; Groq is US-hosted. Uploading
   ~150 KB from a residential line over a ~150 ms path is dominated by TCP slow-start
   (several RTTs to open the congestion window). Terminating in Frankfurt or Istanbul and
   forwarding over a warm, pooled, already-handshaked datacenter connection makes the slow
   leg short and the long leg optimal.
2. **Stream, never buffer.** Audio must flow through the gateway as it arrives, in both
   directions. The gateway starts work on partial input.
3. **Overlap stages.** TTS synthesis begins on the first completed sentence, not after the
   full LLM response. (§7.3.7)

**If the gateway is hosted outside Europe, none of this works and the project should not
ship.** Region placement is the single decision this design depends on.

---

## 4. Common conventions

Unchanged from the existing Boundary A contract:

| Item | Value |
| --- | --- |
| Base URL | `https://api.homapel.com` (client-configurable, `api_base`) |
| Auth | `Authorization: Bearer {api_key}` |
| Error body | §7.2 envelope: `{"error": {"code": "...", "message": "..."}}` |
| Correlation | `X-Request-Id` on every response |
| Backoff | `Retry-After` (integer seconds) on every 429 |

The integration already parses this envelope in
[`api.py`](../custom_components/homapel_conversation/api.py) (`_raise_for_error`) and maps
codes to typed exceptions. **Reuse it exactly** — do not invent a second error shape for
voice endpoints.

---

## 5. §7.3.3 (extension) — capability discovery via `GET /v1/units/status`

The HA STT and TTS entities must declare `supported_languages` **at construction time**,
before any speech request. The integration builds them from the coordinator's first
refresh, which is already a `/v1/units/status` call. Add two blocks to the existing
response:

```jsonc
{
  "unit_id": "unit_01H...",
  "active": true,
  "tier": "pro",
  "webhook_token": "...",
  "cost_ceiling_reached": false,
  "updated_at": "2026-08-03T10:00:00Z",

  "stt": {
    "enabled": true,
    "languages": ["tr-TR", "en-US", "en-GB"],
    "max_audio_seconds": 60
  },

  "tts": {
    "enabled": true,
    "default_language": "tr-TR",
    "default_voice": "tr-TR-Chirp3-HD-Aoede",
    "languages": ["tr-TR", "en-US", "en-GB"],
    "voices": {
      "tr-TR": [
        { "id": "tr-TR-Chirp3-HD-Aoede", "name": "Aoede (Kadın)" },
        { "id": "tr-TR-Chirp3-HD-Puck",  "name": "Puck (Erkek)" }
      ],
      "en-US": [
        { "id": "en-US-Chirp3-HD-Aoede", "name": "Aoede (Female)" }
      ]
    },
    "max_characters": 5000
  }
}
```

Requirements:

- `enabled` reflects **tier entitlement**. If a tier has no voice access, return
  `enabled: false` and the integration will not register the entity at all.
- `voices` is keyed by the exact language tags listed in `languages`. Voice `id` values
  are **Homapel-namespaced identifiers**, opaque to the client — do not leak raw Google
  voice names if you want freedom to remap them later.
- `name` is the user-facing label shown in the HA voice picker. Localize it if you like.
- Both blocks are **optional** in the response. If absent, the integration treats them as
  `enabled: false` and skips the platforms. This keeps older servers compatible.
- The existing ETag / `If-None-Match` behaviour must continue to work, and the ETag must
  change when voice capability changes (e.g. a tier upgrade adds voices).

---

## 6. §7.3.5 — `POST /v1/stt`

Transcribe a spoken utterance. Called by the HA `stt` entity from
`async_process_audio_stream`.

### Request

```
POST /v1/stt?language=tr-TR HTTP/1.1
Authorization: Bearer {api_key}
Content-Type: audio/L16; rate=16000; channels=1
Transfer-Encoding: chunked
Accept: application/json
```

The body is **raw headerless PCM, signed 16-bit little-endian**, streamed with
`Transfer-Encoding: chunked` as HA produces it. Chunks arrive roughly every 30–130 ms
(1024–4096 bytes) while the user is still speaking. End of body = end of speech.

The server **must not wait for the complete body before starting work.** Buffer into the
provider request as bytes arrive; open the Groq connection early so only the final tail
adds latency after end-of-speech.

| Query param | Required | Notes |
| --- | --- | --- |
| `language` | yes | BCP-47 tag, e.g. `tr-TR`. From HA's `SpeechMetadata.language`. |
| `device_id` | no | HA device id of the satellite. Available because the integration registers one STT entity per device — see Appendix A. |
| `area_id` | no | Area of that device, resolved from HA's device registry. |
| `eager` | no | `true` to enable speculative continuation (§7.3.7). Default `false`. |

`device_id` / `area_id` are what make eager continuation produce a *correct* prompt rather
than merely a fast one. Without them the gateway cannot disambiguate "turn on the lights"
when it starts the LLM at the STT boundary. They are optional on the wire so that
`eager=false` cold requests stay simple, but `eager=true` without them should be treated as
`eager=false` — do not speculate on an area-blind prompt.

Audio parameters come from the `Content-Type` media parameters (`rate`, `channels`), not
from query params. The integration will send exactly what HA's `SpeechMetadata` reports;
today that is always 16000 Hz / 1 channel / 16-bit, but do not hardcode it — read the
media parameters and reject unsupported combinations with `unsupported_audio_format`.

### Response — 200

```jsonc
{
  "text": "salondaki ışıkları aç",
  "language": "tr-TR",
  "turn_id": "turn_01H...",
  "duration_ms": 3120,
  "audio_seconds": 3.12,
  "timings": { "upload_ms": 48, "provider_ms": 190, "total_ms": 251 }
}
```

- `text` — the transcript. **Empty string is a valid 200 response** when the audio
  contained no speech. Do not return an error for silence; the integration maps empty text
  to a HA "didn't understand" result, which is the correct UX.
- `turn_id` — always returned, even when `eager=false`. Forward-compatible; the
  integration will echo it into `/v1/converse` from day one.
- `audio_seconds` — billable audio duration, surfaced as a HA usage sensor.
- `timings` — optional but strongly recommended. Drives the latency instrumentation we
  need to validate §3.

### Errors

| Status | `error.code` | When |
| --- | --- | --- |
| 401 | `unauthorized` / `invalid_token` | bad or missing key |
| 403 | `unit_suspended` / `tier_not_allowed` | STT not entitled for this tier |
| 413 | `audio_too_long` | exceeded `stt.max_audio_seconds` |
| 422 | `unit_not_active` | dormant unit |
| 422 | `unsupported_language` | language not in `stt.languages` |
| 422 | `unsupported_audio_format` | bad rate/channels/encoding |
| 429 | `rate_limited` (+ `Retry-After`) | per-unit throttle |
| 429 | `cost_ceiling_exceeded` | unit hit its spend ceiling |
| 5xx | — | upstream failure; integration surfaces a generic error |

---

## 7. §7.3.6 — `POST /v1/tts`

Synthesize speech. Called by the HA `tts` entity. Two modes on one endpoint.

### Mode A — standalone (required, Phase 1)

Used for `tts.speak` service calls, automations, and any case with no live turn.

```
POST /v1/tts HTTP/1.1
Authorization: Bearer {api_key}
Content-Type: application/json
Accept: audio/mpeg
```

```jsonc
{
  "text": "Salondaki ışıkları açtım.",
  "language": "tr-TR",
  "voice": "tr-TR-Chirp3-HD-Aoede",   // optional; server falls back to default_voice
  "format": "mp3"                      // optional; default "mp3"
}
```

### Mode B — attach to an in-flight turn (Phase 2, §7.3.7)

```jsonc
{ "turn_id": "turn_01H..." }
```

The gateway already holds (or is actively producing) the synthesized audio for that turn.
It streams what it has and continues streaming as synthesis completes. `text` is not sent
— the gateway generated the text itself.

If the turn is unknown or expired, return **410 `turn_not_found`**. The integration will
transparently retry as Mode A with the full text. This fallback must work; do not treat it
as an exceptional path.

### Response — 200

```
HTTP/1.1 200 OK
Content-Type: audio/mpeg
Transfer-Encoding: chunked
X-Homapel-Characters: 26
X-Request-Id: req_...
```

Audio bytes, **streamed as produced**. The first byte must leave the gateway as soon as
the first audio frame is available from the provider — not after full synthesis.

Format requirements:

- Default and required format: **MP3**. It is frame-concatenable, so progressive playback
  works and HA can cache it directly. `Content-Type: audio/mpeg`, integration reports
  extension `mp3`.
- Optional: `wav` (`audio/wav`), `opus` (`audio/ogg`). Only implement if cheap.
- **Text longer than Google's 5000-byte per-request limit must be split at sentence
  boundaries by the gateway and the resulting audio concatenated.** The client will not do
  this. MP3 frame concatenation is safe; if you emit WAV, you must rewrite the header.
- `X-Homapel-Characters` → billable character count, for the usage sensor. (Header, not
  body, since the body is audio.)

### Errors

Same table as §7.3.5, plus:

| Status | `error.code` | When |
| --- | --- | --- |
| 410 | `turn_not_found` | `turn_id` expired or unknown (Mode B) |
| 422 | `unsupported_voice` | voice not in this tier's `tts.voices` |
| 413 | `text_too_long` | exceeded `tts.max_characters` |

Errors must be returned **before** the first audio byte, with a JSON content type. Once
audio has started streaming there is no way to signal failure in-band — if the upstream
dies mid-stream, close the connection and the integration will treat truncated audio as a
transport failure.

---

## 8. §7.3.7 — Turn continuity and eager continuation (Phase 2)

> **Full design: [`TURN_CACHE_DESIGN.md`](TURN_CACHE_DESIGN.md).** That document specifies
> the turn record, the replayable broadcast buffer both streams are built on, claim
> semantics, TTL and resource bounds, multi-instance routing, and the failure matrix. This
> section is the summary; build from that one.

This is where the latency win comes from. **Ship Phase 1 first and measure; build this
second.** The protocol above already carries `turn_id`, so this is purely additive.

### The problem

HA's Assist pipeline invokes three separate entities in sequence: `stt` → `conversation`
→ `tts`. Each is a separate HTTP round trip from the user's house. Those round trips
cannot be merged — HA's architecture forbids it. But they *can* be hidden.

### The mechanism

Server-side, start the next stage before the client asks for it:

1. `POST /v1/stt?eager=true` completes transcription. **Before writing the HTTP response**,
   the gateway starts the LLM call using that transcript, keyed by `turn_id`. Then it
   returns `{text, turn_id}`.
2. HA receives the transcript, and its conversation entity calls `POST /v1/converse` with
   `turn_id` in the body. The gateway **attaches to the already-running LLM stream** rather
   than starting a new one, and replays any deltas produced so far. The residential round
   trip overlapped with real work.
3. As the LLM stream produces complete sentences, the gateway feeds them to TTS
   incrementally and buffers the audio under `turn_id`.
4. HA's TTS entity calls `POST /v1/tts` with `turn_id` and immediately receives buffered
   audio.

Net effect: two of the three residential round trips are overlapped with provider work
instead of serialized after it, and TTS synthesis overlaps LLM generation.

### `POST /v1/converse` additions

Accept an optional `turn_id` in the request body. Behaviour:

- **Known, in-flight turn** → attach; replay buffered deltas from the start, then continue
  live. The SSE contract (`delta` / `meta` / `error` events) is unchanged.
- **Unknown or expired turn** → ignore `turn_id` silently and execute normally using
  `text`. Do **not** error. The integration always sends `text` as well, so a cold start is
  always possible.

The existing `meta` event should gain `turn_id` so the TTS stage can find it.

### Rules

- **TTL:** discard unclaimed turn state after 30 seconds. Voice turns are short-lived.
- **Idempotency:** a `turn_id` may be claimed by `/v1/converse` exactly once and by
  `/v1/tts` exactly once. Second claims get `turn_not_found`.
- **Wasted work:** if the pipeline aborts, speculative LLM/TTS work is billed to nobody
  and costs you real money. Bound it: only speculate when `eager=true`, cap concurrent
  speculative turns per unit, and stop synthesis the moment the TTL expires.
- **Consent:** the integration only sends `eager=true` when its config option
  `unified_pipeline` is enabled (default on). Users who mix Homapel STT with a third-party
  conversation agent will have it off, and the gateway must behave correctly either way.
- **Sticky routing:** turn state is per-instance. If the gateway runs behind a load
  balancer, either encode the instance identity in the `turn_id` and route on it, or hold
  turn state in shared Redis. **Getting this wrong produces intermittent `turn_not_found`
  that only appears under production load.** Decide explicitly.

---

## 9. Infrastructure requirements

These are correctness requirements for §3, not deployment preferences.

- **Region.** Deploy in `europe-west3` (Frankfurt) or an Istanbul-local provider. Measure
  RTT from a Turkish residential connection before committing. A US-hosted gateway makes
  the product worse than the status quo.
- **Disable response buffering end to end.** nginx `proxy_buffering off`, and bypass or
  configure any CDN in front of the API. A buffering reverse proxy will silently collapse
  every streaming guarantee in this document into store-and-forward — this is the most
  likely way to ship something slower than today and not notice.
- **Warm upstream connection pools.** Persistent keep-alive connections to Groq and Google
  with HTTP/2 where available. A cold TLS handshake to a US endpoint costs more than the
  entire Whisper inference.
- **Cache Google OAuth tokens.** Mint the service-account access token in the background
  and refresh before expiry. Never call `oauth2.googleapis.com` on the request path.
- **Least-privilege service account.** The Google service account should hold only the
  Text-to-Speech role, in a project used for nothing else, so blast radius is bounded.
- **Per-unit quotas at the gateway.** This is the entire justification for the proxy
  architecture — enforce per-unit rate limits and spend ceilings here, reusing the existing
  `rate_limited` / `cost_ceiling_exceeded` codes.

## 10. Provider notes to verify

Flagging these because they change the design and I have not verified them against your
current plans:

- **Groq region.** Groq has been expanding beyond US datacenters. If an EU endpoint is
  available on your plan, use it — it removes the transatlantic leg entirely and is worth
  more than everything else in §9.
- **Google regional endpoint.** If you are currently calling the global TTS endpoint from
  Turkey, switching to `eu-texttospeech.googleapis.com` is a free latency win independent
  of this project.
- **Google streaming synthesis.** `StreamingSynthesize` (bidi gRPC) is what makes §7.3.6
  first-byte latency good, but voice-model support is limited — Chirp 3 HD voices support
  it; older Standard/WaveNet/Neural2 voices may not, and encoding options are narrower
  (may return PCM only, requiring transcode to MP3 at the gateway). **Confirm which voices
  support streaming before finalizing the voice catalogue in §5**, because the answer
  determines which voices you can offer at low latency.
- **Whisper model choice.** `whisper-large-v3-turbo` on Groq is substantially faster than
  `whisper-large-v3` at minor accuracy cost. For short home-automation utterances, turbo is
  almost certainly the right trade.

## 11. Metering

Report per-request so the integration can expose usage sensors alongside the existing
token sensors:

| Stage | Unit | Where reported |
| --- | --- | --- |
| STT | `audio_seconds` | `/v1/stt` JSON response |
| TTS | `characters` | `X-Homapel-Characters` response header |
| LLM | `tokens.in` / `tokens.out` | existing `meta` SSE event |

Note that HA caches TTS output locally by message hash, so repeated phrases
("Işıkları açtım") will not re-hit the API. Expect real TTS character volume to be well
below naive per-utterance estimates.

---

## 12. Phasing

**Phase 0 — measure (do this first).**
Instrument the current direct-to-provider path and get the split: handshake vs. upload vs.
inference vs. download. If inference dominates, the gateway will not help much and the
scope should be reconsidered. Everything below assumes upload + handshake dominate, which
the geography strongly suggests.

**Phase 1 — streaming proxy.**
§5 capability discovery, §7.3.5 STT with chunked upload, §7.3.6 TTS Mode A with chunked
response. No turn continuity. Already faster than direct if §9 is respected. This is a
complete, shippable product.

**Phase 2 — eager continuation.**
§7.3.7. `eager=true`, `turn_id` on `/v1/converse`, TTS Mode B. Biggest remaining win, and
it compounds with the LLM delta streaming the integration already does.

## 13. Acceptance criteria

Phase 1 is done when:

1. `GET /v1/units/status` returns `stt` and `tts` blocks gated by tier entitlement, and the
   ETag changes when entitlement changes.
2. `POST /v1/stt` accepts a chunked PCM body and begins forwarding to Groq **before** the
   request body is complete. Verifiable: time from last-audio-byte to response is
   materially less than total audio duration.
3. `POST /v1/stt` returns 200 with `text: ""` for silent audio.
4. `POST /v1/tts` emits its first audio byte before synthesis completes. Verifiable:
   time-to-first-byte is materially less than time-to-last-byte for a long utterance.
5. `POST /v1/tts` correctly splits and concatenates text over 5000 bytes.
6. Every error path returns the §7.2 envelope with the codes in §7.3.5 / §7.3.6, and 429s
   carry `Retry-After`.
7. A tier without voice entitlement gets 403 `tier_not_allowed`, and the status blocks say
   `enabled: false`.
8. Measured end-to-end from a Turkish residential connection: time-to-first-audio is **not
   worse** than the current direct-to-provider path. This is the gate — if it fails, §9 is
   the place to look.

---

## Appendix A — what the integration will do

For context, so the API contract can be sanity-checked against the client.

**New files:** `stt.py`, `tts.py`. **Modified:** `__init__.py` (add `Platform.STT`,
`Platform.TTS`), `manifest.json` (add `stt`, `tts` dependencies), `api.py` (new client
methods), `const.py`, `coordinator.py` (parse capability blocks).

**No config flow changes. No new credential. No new config entry fields**, other than one
option toggle for `unified_pipeline`.

**STT entities — one per satellite device ("Option A").** `SpeechMetadata` carries no
device identity, so a single shared STT entity cannot tell which room the audio came from.
The integration therefore registers **one STT entity per discovered satellite**, and
auto-provisions a matching pipeline for each via
`assist_pipeline.async_create_default_pipeline` so users configure nothing. Each entity
then knows its own `device_id` inherently and resolves `area_id` from HA's device registry
— the same lookup `conversation.py` already performs — and sends both on every `/v1/stt`
call.

Each entity declares `supported_languages` from `status.stt.languages`;
`supported_formats=[WAV]`, `supported_codecs=[PCM]`, `supported_bit_rates=[16]`,
`supported_sample_rates=[16000]`, `supported_channels=[MONO]`. Its
`async_process_audio_stream(metadata, stream)` pipes the incoming `AsyncIterable[bytes]`
directly into an aiohttp chunked request body — no local buffering — and maps the response
to `SpeechResult(text, SUCCESS)`.

The integration sends `eager=true` only when that satellite's pipeline uses Homapel for all
three stages. A user pairing Homapel STT with a third-party conversation agent gets
`eager=false`, and the gateway must not speculate.

**TTS entity** — declares `supported_languages` and `default_language` from
`status.tts`; `supported_options=["voice"]`; `async_get_supported_voices(language)` returns
`Voice(id, name)` built from `status.tts.voices[language]`. Implements
`async_get_tts_audio` returning `("mp3", bytes)` and, where the HA version supports it,
`async_stream_tts_audio` returning a chunk generator.

**Failure behaviour** — the integration will map `HomapelAuthError`,
`HomapelRateLimitedError`, `HomapelCostCeilingError` and friends to the existing localized
`ERROR_SPEECH` messages in `const.py`. It will not retry automatically on 429; it respects
`Retry-After` and surfaces the error. `turn_not_found` is the one code it retries, by
falling back to the standalone path.
