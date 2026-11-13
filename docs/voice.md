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

## Migrate from Voice Satellite

The authenticated `tacktab/voice/migration_plan` WebSocket command discovers
the installed `voice_satellite` integration's Assist satellites and returns a
read-only plan. Each candidate includes its assistant/pipeline, wake word and
detection settings, conversation/final-speech settings when exposed, timer
choices, and automation/script references to that satellite or
`voice_satellite_timer`. YAML is only parsed for this inventory; it is never
written, and the old Voice Satellite entry is never changed.

`tacktab/voice/migration_apply` is scoped to the connected Tacktab tablet and
only writes that tablet's primary Tacktab pipeline select. Native clients apply
their selected voice, conversation and timer choices locally. Review every
reported reference yourself: the replacement timer event is
`tacktab_voice_timer`, but the bridge intentionally does not rewrite an
automation or script. Voice Satellite skins currently have no native mapping.
