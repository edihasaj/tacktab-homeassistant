# Native Assist voice

`tacktab/voice/*` is an optional, free Home Assistant Core 2026.10+ bridge. It
creates a separate voice-only Tacktab config entry and one stable HA device per
tablet. Pairing requires an administrator WebSocket session; it neither enables
the Tacktab HTTP remote-control API nor reads its Pro password.

Each entry has `primary` and optional `secondary` Assist satellites. Each slot
has a real HA pipeline select and an independent Assist conversation context.
Only one slot may own the tablet microphone or local output at a time.

## Wire summary

Clients begin with `tacktab/voice/connect`, then send `configure`, `status`,
`snapshot`, `claim`, `run`, PCM binary frames, `audio_end`, `cancel`,
`cancel_output` (protocol 2), `ack`, or `timer_action`. All JSON commands reject
unknown fields. The bridge returns
standard HA result/error envelopes and sends subscription events: `snapshot`,
`state`, `pipeline`, `play`, `cancel_action`, `timer`, `claim_result`, and
`session_closed`.

## Conversation results (protocol 3)

Protocol 3 adds bounded `conversation_result` events after an Assist
`intent-end` and after a completed realtime tool.  The payload is versioned
(`version: 1`) and contains native display data only: text, tool use, weather,
an entity state, image, video, or numeric/stock value and change.  Core intent
responses may opt into a typed card through their response data; otherwise the
client receives its normal speech text.  Tool output is reduced to a safe
plain-text line such as “Ran script Goodnight”.

Media URLs must be HTTPS, or a root-relative Home Assistant URL which the
native app resolves against its authenticated configured origin.  The bridge
rejects file URLs, HTTP LAN URLs and protocol-relative URLs.  It never reads
Home Assistant conversation history with a tablet token; where an
administrator supplies richer context through a normal pipeline/tool result,
the same bounded sanitization applies.  Protocol 1 and 2 sessions do not
receive these events, so older native clients retain their existing sequence
handling and text conversation.

`connect` returns a new epoch, binary handler id, and atomic snapshot. Every
later event includes strictly increasing `seq`; reconnecting replaces the
epoch. Wake claims are scored in one server-timed 400 ms SNR window, with a
single-use grant valid for two seconds. Audio is PCM16 LE, 16 kHz mono, with a
32 KiB frame cap, a two-second queued-audio cap, and a 60-second utterance cap.

Native playback is acknowledged with `ack`; Home Assistant announcement,
conversation, and question services wait for that physical completion. Protocol
2 can instead route a slot to an HA `media_player`; it reports the separate
`remote_playback` lifecycle only after observing the speaker start and finish.
See [remote-output.md](remote-output.md) for its strict event contract and
completion requirements. Protocol 1 remains local-output only.

Timers use one HA `async_register_timer_handler` per tablet device. Timer
changes are authoritative Core events. Finished timer records remain in the
bounded mirror until native dismisses them, so reconnect preserves an active
alarm. Assist Core timers are memory-only: a Home Assistant restart returns an
empty timer snapshot and does not recreate timers.
