# Remote HA speaker output

Protocol 2 adds an HA-owned output mode for slots whose `output` is
`ha_media_player`. A protocol 1 connection rejects that slot type and receives
only the frozen local `play`/native-`ack` flow. The `connect` result echoes the
requested supported protocol version.

```text
media source or TTS URL
  -> resolve/sign with Home Assistant helpers
  -> media_player.play_media using paired-user context
  -> observe matching playing/buffering state
  -> remote_playback.started
  -> observe matching idle/off state
  -> finish Assist response + release output lease
  -> remote_playback.finished
```

The server accepts a remote speaker only when it is an available
`media_player` with `PLAY_MEDIA`. It resolves `media-source://` identifiers with
`media_source.async_resolve_media` for that player, and processes URLs with
`media_player.async_process_play_media_url`; the bridge never sends an HA bearer
credential to a speaker. Service completion alone is never playback completion.

The observer must see a fresh post-request transition to the exact resolved
media, then go idle or off while still reporting that media. An already-playing
matching URL is not evidence that this request started it. Service dispatch and
that fresh start share the start timeout. A replacement item, removed or
unavailable player, missing start, excessive playback duration, cancellation,
disconnection, or unload fails the action. Cleanup only removes the observer:
it never stops a speaker because subsequent audio may belong to another HA
action.

`remote_playback` shares the session's global `epoch` and strictly increasing
`seq`. Its complete schema is [remote-output.schema.json](remote-output.schema.json).
The `finished` event is emitted only after HA has finished the satellite
response and released the shared output lease. Native clients must never ACK
this event and may begin a consented follow-up only after `finished` has
`listen_after: true`.

To interrupt an active remote action, a protocol-2 client sends
`tacktab/voice/cancel_output` with exactly `{id, type, epoch, action_id}`. The
paired session may cancel only its current action. The bridge cancels the
observer and invokes `media_stop` only when the player still reports the exact
media URL owned by that action. A later replacement item is never stopped. The
command has a normal result/error envelope; the interrupted action fails and
cannot emit `remote_playback.finished`.
