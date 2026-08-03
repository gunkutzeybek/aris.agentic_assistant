"""Correlates a single voice turn across HA's three pipeline entities.

HA's Assist pipeline invokes ``stt`` → ``conversation`` → ``tts`` as three
independent entities with no shared context object. The cloud, however, ties
them together with a ``turn_id`` (VOICE_API_AS_BUILT.md §6) so it can
pre-synthesize speech during the converse stream.

The only thing that flows naturally between the stages is *text*: the STT
transcript becomes ``ConversationInput.text``, and the conversation's speech
becomes the TTS ``message``. So we key on exactly that.

Entries are single-use and expire with the cloud's own 30 s turn TTL. Both
failure modes are harmless: a miss means the next stage runs cold (correct,
marginally slower), and a stale hit means one Mode B call returns 410 and the
caller retries as Mode A.
"""
from __future__ import annotations

import time

from .const import TURN_TTL


class TurnRegistry:
    """Single-use, TTL-bounded text → turn_id maps for one config entry."""

    def __init__(self, ttl: float = TURN_TTL) -> None:
        self._ttl = ttl
        # Transcript → turn_id, written by the STT entity, read by conversation.
        self._transcripts: dict[str, tuple[str, float]] = {}
        # Speech → turn_id, written by conversation, read by the TTS entity.
        self._speech: dict[str, tuple[str, float]] = {}

    def remember_transcript(self, text: str, turn_id: str) -> None:
        self._put(self._transcripts, text, turn_id)

    def take_transcript(self, text: str) -> str | None:
        return self._take(self._transcripts, text)

    def remember_speech(self, text: str, turn_id: str) -> None:
        self._put(self._speech, text, turn_id)

    def take_speech(self, text: str) -> str | None:
        return self._take(self._speech, text)

    def _put(self, store: dict[str, tuple[str, float]], text: str, turn_id: str) -> None:
        key = self._key(text)
        if not key:
            return
        self._prune(store)
        store[key] = (turn_id, time.monotonic())

    def _take(self, store: dict[str, tuple[str, float]], text: str) -> str | None:
        key = self._key(text)
        if not key:
            return None
        self._prune(store)
        entry = store.pop(key, None)
        return entry[0] if entry else None

    def _prune(self, store: dict[str, tuple[str, float]]) -> None:
        cutoff = time.monotonic() - self._ttl
        for key in [k for k, (_, ts) in store.items() if ts < cutoff]:
            del store[key]

    @staticmethod
    def _key(text: str) -> str:
        """Normalize so incidental whitespace differences still correlate.

        Keyed on text alone rather than (text, language): the language tag HA
        hands the TTS entity is not guaranteed to be byte-identical to the one
        the conversation entity sent on the wire, and a collision between two
        identical utterances only costs a Mode A fallback.
        """
        return " ".join(text.split()) if text else ""
