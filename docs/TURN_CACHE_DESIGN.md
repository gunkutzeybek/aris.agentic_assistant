# Homapel Voice API — Turn Cache & Eager Continuation Design

> **⚠️ Superseded as a wire contract; partly unbuilt.** Code against
> `VOICE_API_AS_BUILT.md`. What shipped: the turn cache is Redis-backed (§9's
> instance-encoded ids and LB routing were dropped as a problem worth not
> having), and TTS is fed during the *real* converse stream — not speculatively.
> What did not: §6.1's LLM start at the STT boundary and §6.2's converse attach
> (Phase 2b, gated on measurement). Also note §10's "no failure mode produces a
> wrong answer" is **false as written** — it ignores side effects, since the
> agent executes tools; see the backend's write-barrier design. Kept for the
> replayable-buffer semantics in §5, which the Redis implementation preserves.

**Audience:** the developer implementing the Homapel cloud API.
**Companion to:** [`VOICE_API_SPEC.md`](VOICE_API_SPEC.md). This document specifies the
server-side state machine referenced there as §7.3.7.
**Depends on:** per-device STT entities on the integration side ("Option A"), which is what
makes device/area context available at the STT boundary.

---

## 1. What this solves

Home Assistant's Assist pipeline invokes three separate entities in sequence — `stt` →
`conversation` → `tts` — each a separate WAN round trip from the user's house. Those calls
cannot be merged; HA's architecture forbids it.

They *can* be overlapped. The gateway starts each stage before HA asks for it and holds the
result in a short-lived per-turn cache. Each subsequent HA call attaches to work already in
flight instead of starting it.

Target: the ~40 ms residential round trips get absorbed into provider work that was going to
happen anyway, and TTS synthesis overlaps LLM generation.

## 2. One correction to the flow

> "When the STT finishes, it can somehow ping back the HA so HA can continue with the
> conversation request."

**No ping-back is needed, and it would be slower.** HA's pipeline is synchronously blocked
inside `async_process_audio_stream` waiting for the `/v1/stt` HTTP response. Returning 200
with the transcript *is* the signal — HA proceeds to the conversation stage immediately, in
microseconds. A webhook push would require delivery to the home network plus correlation
logic, and would arrive strictly later than the response you are already sending.

The existing webhook stays what it is: activation and tier pushes only.

## 3. Timeline

```
  HA / device                          Gateway                     Providers
  ───────────                          ───────                     ─────────
  audio chunks ──────────────────────▶ forward as they arrive ───▶ Groq Whisper
                                                                        │
                                       transcript ◀────────────────────┘
                                       CREATE TURN
                                       start LLM  ─────────────────▶ LLM
  200 {text, turn_id} ◀──────────────── (before response is written)  │
        │                                                             │
        │ ~40ms WAN + µs local                    deltas buffered ◀───┤
        ▼                                         sentence 1 done ────┼──▶ Google TTS
  POST /v1/converse {turn_id} ───────▶ ATTACH to llm buffer           │        │
                                       replay buffered, then live     │  audio ◀┘
  SSE deltas ◀──────────────────────── ─────────────────────────      │  buffered
        │                                                             │
        ▼                                                             ▼
  POST /v1/tts {turn_id} ────────────▶ ATTACH to tts buffer (already producing)
  audio chunks ◀────────────────────── stream immediately
```

The two intermediate WAN round trips run concurrently with LLM generation and TTS
synthesis. If the LLM takes 800 ms and the round trip is 40 ms, the round trip is fully
hidden.

---

## 4. The turn record

```jsonc
TurnRecord {
  turn_id:      string,        // "turn_<instance>_<ulid>" — see §9
  unit_id:      string,
  device_id:    string | null, // from /v1/stt, thanks to per-device STT entities
  area_id:      string | null,
  language:     string,        // BCP-47
  eager:        bool,

  created_at:   timestamp,
  expires_at:   timestamp,     // created_at + TTL (§8)
  state:        TRANSCRIBING | LLM_RUNNING | LLM_DONE | FAILED,

  transcript:     string,
  audio_seconds:  float,       // billing

  llm:  StreamBuffer<SseEvent>,    // text deltas + meta
  tts:  StreamBuffer<bytes>,       // audio frames

  claimed: { converse: bool, tts: bool },

  meta: {                          // populated as the LLM stream completes
    conversation_id, continue_conversation, dormant,
    llm_used, tokens: {in, out}, tier
  },

  characters: int,             // TTS billing
  error: ErrorEnvelope | null  // §7.2 shape, if any stage failed
}
```

Keyed by `turn_id`. In-memory, per-instance, short-lived. **Do not persist it** — nothing
here survives a restart, and nothing needs to (§9).

## 5. The core primitive: replayable broadcast buffer

Both `llm` and `tts` are the same abstraction, and getting it right is most of the work.

A consumer may attach at three different times, and all three must behave identically from
the client's perspective:

- **Before production starts** → block, then stream live.
- **Mid-production** → replay what's buffered, then continue live seamlessly.
- **After completion** → replay everything, then close.

```python
class StreamBuffer:
    chunks:   list          # everything produced so far
    complete: bool = False
    error:    ErrorEnvelope | None = None
    _notify:  Condition

    def append(chunk):  chunks.append(chunk); notify_all()
    def finish():       complete = True;      notify_all()
    def fail(err):      error = err; complete = True; notify_all()

    async def iterate(from_index=0):
        i = from_index
        while True:
            while i < len(chunks):
                yield chunks[i]; i += 1
            if error:    raise error
            if complete: return
            await wait_for_notify()
```

Notes:

- The producer never blocks on the consumer. If HA is slow to attach, production continues
  into the buffer.
- `iterate()` starting at index 0 is the only mode the client needs — HA always attaches
  fresh, never resumes mid-stream.
- Errors propagate to whoever is attached *and* are recorded so a later attach sees the
  same failure rather than hanging until TTL.

## 6. Stage-by-stage behaviour

### 6.1 `POST /v1/stt`

Per `VOICE_API_SPEC.md` §7.3.5, plus these params — required for eager continuation to
produce a correct prompt:

| Param | Notes |
| --- | --- |
| `device_id` | HA device id of the satellite. From the per-device STT entity. |
| `area_id` | Resolved from HA's device registry by the integration. |
| `eager` | `true` to enable everything in this document. |

Sequence:

1. Stream audio to Groq as chunks arrive; do not wait for the complete body.
2. On transcript: create the `TurnRecord`, state `TRANSCRIBING` → `LLM_RUNNING`.
3. **If `eager=true` and the transcript is non-empty**, start the LLM call *before writing
   the HTTP response*, using `device_id` / `area_id` / `language` from the request. Wire
   its output into `turn.llm`.
4. Write `200 {text, turn_id, language, duration_ms, audio_seconds, timings}`.

If the transcript is empty (silence), return `text: ""` and **do not** start the LLM. Set
state `LLM_DONE` with an empty buffer so a stray converse attach doesn't hang.

If `eager=false`, still create the turn and return `turn_id`, but start nothing. Every
downstream call then behaves as a normal cold request.

### 6.2 `POST /v1/converse`

The body gains an optional `turn_id`. `text`, `device_id`, `area_id`, `conversation_id`
and `language` continue to be sent **always** — the client never assumes the turn exists.

| Condition | Behaviour |
| --- | --- |
| `turn_id` known, unclaimed, context matches | Attach to `turn.llm` via `iterate(0)`. Replay buffered SSE events in production order, then continue live. Mark `claimed.converse`. |
| `turn_id` known but already claimed | Ignore the turn, execute cold from `text`. Do **not** error — a retry must still work. |
| `turn_id` unknown or expired | Ignore silently, execute cold from `text`. |
| `turn_id` known but `device_id`/`area_id` disagree with the record | **Discard the speculative work**, cancel the in-flight LLM, execute cold. This catches pipeline misconfiguration where STT and conversation came from different devices. |

The SSE contract is unchanged — same `delta` / `meta` / `error` events the client already
parses in [`api.py`](../custom_components/homapel_conversation/api.py). Replayed events are
indistinguishable from live ones.

Two additions:

- The `meta` event should carry `turn_id`, so the TTS stage can find the turn.
- Emit SSE keepalive comment lines (`: ping\n\n`) during long gaps. The client's default
  idle timeout is 90 s (`DEFAULT_CONVERSE_SOCK_READ`) and it already skips comment lines,
  so this is free insurance.

### 6.3 Incremental TTS feed

As `turn.llm` produces text, the gateway segments it and feeds completed segments to
Google TTS, appending audio to `turn.tts`. This is what makes the TTS stage instant.

Segmentation rules:

- **First segment: the first complete sentence, as early as possible.** This sets
  time-to-first-audio and is the single most user-visible number in the system.
- **Subsequent segments: batch larger** — 2–3 sentences, or a ~300 character target.
  Google synthesizes each request independently, so prosody resets at every join. Small
  chunks sound choppy. Big first-chunk latency vs. smooth audio is the trade; front-load
  the small chunk and smooth out afterwards.
- **Flush on length:** if no sentence terminator appears within ~200 characters, flush at
  the nearest clause boundary (comma, conjunction) so a rambling response doesn't stall.
- **Turkish-aware splitting:** `.` `!` `?` plus abbreviation handling. Do not split on the
  decimal separator or on ordinals (`1.`, `2.`), which are common in Turkish.

Concatenation must be format-safe: MP3 frames concatenate cleanly, WAV does not without
rewriting the header. This is one more reason MP3 is the required default format.

### 6.4 `POST /v1/tts`

| Condition | Behaviour |
| --- | --- |
| `turn_id` known, unclaimed | Attach to `turn.tts`. Stream buffered audio, then live as synthesis continues. Mark `claimed.tts`. |
| `turn_id` unknown, expired, or already claimed | **410 `turn_not_found`.** The integration retries as Mode A with full text. |
| No `turn_id` | Mode A — standalone synthesis, per spec §7.3.6. |

**Defer the `200` and `Content-Type` until the first audio byte is available.** Once you
have committed to a `200 audio/mpeg`, there is no in-band way to report a failure. Any
error that occurs before first byte must come back as a JSON `4xx`/`5xx`; after first byte,
the only signal is closing the connection, and the client will treat truncated audio as a
transport failure.

Set `X-Homapel-Characters` from `turn.characters` before writing headers.

---

## 7. Claim semantics

Each buffer is claimed exactly once:

- Prevents double billing.
- Prevents an audio stream being played twice if HA retries.
- Makes leaked/guessed `turn_id` values near-useless.

`turn_id` must be unguessable (ULID or 128-bit random, not sequential) and is scoped to its
`unit_id` — a turn created under one unit can never be claimed by another. Reject
cross-unit claims as `turn_not_found`, not `forbidden`; do not confirm existence.

## 8. Lifetime and resource bounds

| Bound | Value | Rationale |
| --- | --- | --- |
| Turn TTL | 30 s from creation | Voice turns are short. Anything longer is an abandoned pipeline. |
| Speculative turns per unit | 2 concurrent | A home has few simultaneous speakers. |
| Audio buffer cap per turn | ~256 KB | ~60 s of 32 kbps MP3. Beyond that, stop synthesis and finish the buffer. |
| Converse attach wait | bounded by client `sock_read` (90 s default) | Keepalives prevent the client giving up. |

On TTL expiry: **cancel in-flight LLM and TTS work**, release buffers, drop the record. An
expired turn that keeps generating tokens is pure cost with no consumer.

Memory is dominated by audio: ~150 KB per live turn, so a thousand concurrent turns is
~150 MB. Not a concern at expected scale, but cap it explicitly rather than trusting that.

### Billing policy for unclaimed work

Speculative LLM and TTS calls cost real money whether or not HA claims them. Recommendation:
**bill them to the unit** — the user did speak, and unclaimed turns should be rare. Track
the unclaimed rate as an operational metric; if it climbs above a few percent, something is
wrong with the `eager` gating, not with billing.

## 9. Multi-instance routing

Turn state is in-memory and per-instance. All three calls for a turn **must** land on the
same instance.

Recommended: encode instance identity in the id — `turn_<instance>_<ulid>` — and configure
the load balancer to route on that prefix. This is simpler than sharing streaming buffers
and matches the 30-second lifetime.

The alternative (Redis Streams via `XADD`/`XREAD BLOCK`) works but adds a hop to every
chunk on the latency-critical path. Not worth it for state that lives 30 seconds.

**Instance restarts orphan turns.** That is fine and must be: the client falls back to cold
execution on `turn_not_found`, and `/v1/converse` always carries `text`. Deploys cause a
brief window of slightly-slower-but-correct turns.

> Getting this wrong produces intermittent `turn_not_found` that appears only under
> production load with more than one instance, and never in testing. Decide it explicitly
> before writing the cache.

## 10. Failure and fallback matrix

| Failure | Gateway behaviour | Client result |
| --- | --- | --- |
| Groq fails during STT | 5xx, no turn created | HA reports STT error |
| LLM fails after eager start | `turn.llm.fail(envelope)`; attach raises it | SSE `error` event, mapped to localized `ERROR_SPEECH` |
| TTS fails after eager start | `turn.tts.fail(envelope)` | If pre-first-byte, JSON error; else truncated stream |
| Turn expired before converse | Ignore, execute cold | Slower but correct |
| Turn expired before TTS | 410 `turn_not_found` | Client retries Mode A with full text |
| Instance restart mid-turn | Turn gone | Cold fallback on both stages |
| Device/area mismatch | Discard speculation, execute cold | Correct answer, no latency gain |

Every row degrades to "correct but slower." **There is no failure mode in this design that
produces a wrong answer**, and that property should be preserved by anything added later.

---

## 11. What the integration sends

For contract-checking against the client.

**Per-device STT entities (Option A).** The integration registers one `stt` entity per
discovered satellite rather than one per config entry. Each entity knows its own
`device_id`, resolves `area_id` from HA's device registry — the same lookup
[`conversation.py:208-213`](../custom_components/homapel_conversation/conversation.py#L208-L213)
already does — and sends both on every `/v1/stt` call. Pipelines are auto-provisioned via
`assist_pipeline.async_create_default_pipeline` so users configure nothing.

**`eager` gating.** The integration sends `eager=true` only when the satellite's pipeline
uses Homapel for all three stages. If a user pairs Homapel STT with a third-party
conversation agent, it sends `eager=false` and the gateway must not speculate.

**The client never depends on a turn existing.** `/v1/converse` always carries full `text`,
`device_id`, `area_id`, `conversation_id`, `language`. `/v1/tts` falls back to Mode A on
410. Build and test the cold path first; eager continuation is an optimization layered on a
working system, not a prerequisite for one.

## 12. Acceptance criteria

1. `/v1/stt?eager=true` starts the LLM before writing its HTTP response. Verifiable: the
   LLM provider's request timestamp precedes the STT response timestamp.
2. `/v1/converse` with a known `turn_id` emits its first SSE delta measurably sooner than
   the same request without one.
3. A converse attach arriving *after* the LLM has already finished replays the full
   response correctly and closes — no hang, no truncation.
4. A converse attach arriving *before* any delta exists blocks, then streams live.
5. `/v1/tts` with a known `turn_id` returns first audio byte in under ~50 ms server-side.
6. `turn_id` claimed twice: second claim gets `turn_not_found` on TTS, cold execution on
   converse.
7. Device/area mismatch between STT and converse discards speculation and answers correctly
   from the converse-supplied context.
8. TTL expiry cancels in-flight provider calls — verifiable via provider-side request
   duration, not just gateway logs.
9. With two gateway instances behind the LB, 1000 sequential turns produce **zero**
   unexpected `turn_not_found`.
10. Kill an instance mid-turn: the client completes the turn correctly via cold fallback.
11. Silent audio produces `text: ""`, no LLM call, and no hang if converse attaches anyway.
