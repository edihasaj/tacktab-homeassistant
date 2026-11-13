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

Two WebSocket commands serve the native migration wizard. Both require an
**administrator** user and a socket that has completed Tacktab pairing for the
tablet (its `epoch` must match); a non-admin or unpaired socket gets
`unauthorized` or `disconnected` and no data.

`tacktab/voice/migration_plan` reads the installed `voice_satellite`
integration's Assist satellites and returns a read-only plan. Each candidate
has one typed `settings` object; a value is `null` when the source did not
say, and the clients then leave their own preference alone.

| Key | Type | Source |
| --- | --- | --- |
| `assistant`, `assistant_preferred`, `assistant_pipeline_id` | string, bool, string | Pipeline select (`preferred` or a name); the id is the resolved stable pipeline. |
| `wake_detection` | `local` / `server` / `disabled` | Wake word detection select. |
| `wake_engine`, `wake_engine_supported` | `microwakeword` / `openwakeword` / `vswakeword`, bool | Only microWakeWord is carried; other engines are reported in `warnings`. |
| `wake_word`, `wake_word_source` | string | Wake word model select. Upstream ids are mapped to Tacktab's (`ok_nabu` becomes `okay_nabu`); `wake_word_source` keeps the original. |
| `finished_speaking` | `default` / `relaxed` / `aggressive` | Finished-speaking (VAD) select. |
| `conversation`, `conversation_seconds`, `conversation_memory`, `conversation_memory_rounded` | `persistent` / `timed` / `isolated`, int, option, bool | Session duration select (`5_minutes`, `isolated`, ...). `conversation_memory` names the Tacktab option that keeps context at least as long; `conversation_memory_rounded` is true when that is longer than the original. |
| `timer_muted` | bool | Mute timers switch. |

Skins, the second wake word/assistant and card-only timer display options have
no mapping and are listed in `warnings`.

References come from the automations and scripts Home Assistant has loaded
(including `!include`, packages and UI-created items). Only when a component is
not loaded is its YAML file read, keeping `!secret`/`!include` nodes. If any
configuration could not be inspected, `references_complete` is `false` and
`reference_warnings` says why, so an empty list is never presented as
"nothing found". Nothing is written, and the old Voice Satellite entry is never
changed.

`tacktab/voice/migration_apply` writes only this tablet's Tacktab selects:
`assistant` sets the primary pipeline to the resolved stable id (`Preferred`
and `preferred` both mean HA's preferred pipeline), and `conversation` sets the
new **Finished speaking** select (how much silence ends a command) and the new
**Conversation memory** select. Inputs are validated first, so a busy tablet or an
unknown pipeline changes nothing and returns an error. The result echoes
`candidate_entity_id` and what was `applied`; the native client stores its own
choices only after this success. Review every reported reference yourself: the
replacement timer event is `tacktab_voice_timer`, but the bridge intentionally
does not rewrite an automation or script.

## Conversation memory

Each tablet has a **Conversation memory** select: how long Assist keeps context
between activations. Home Assistant's own Assist satellite keeps a conversation
for five minutes of inactivity, so `5_minutes` is the default and matches the
earlier behaviour. `isolated` starts a new conversation on every activation and
`1_minute` after a minute of quiet. `15_minutes` and `until_restart` go beyond
Home Assistant's five minutes: Tacktab refreshes the chat session through Home
Assistant's own helper until the retention has passed, and `until_restart` never
lets go until Home Assistant restarts. This is separate from follow-up
listening after a question, which the native apps control. Realtime
conversations keep their own bounded memory.

## Timer events

Every timer on a paired tablet also fires one Home Assistant bus event,
`tacktab_voice_timer`, from the same authoritative adapter that feeds the
tablet. It covers timers started by voice, by a realtime conversation or by
`intent`/`assist_satellite` actions:

| Field | Type | Meaning |
| --- | --- | --- |
| `event_type` | string | `started`, `updated`, `paused`, `resumed`, `cancelled`, `finished` or `dismissed`. |
| `timer_id` | string | Home Assistant's timer ID, stable across the timer's events. |
| `name` | string | The timer's name, empty when it has none. |
| `total_seconds` | integer | The duration the timer was started with. Added time does not change it. |
| `seconds_left` | integer | Seconds left when the event fired. |
| `is_active` | boolean | False while paused. |
| `device_id` | string | The tablet's Home Assistant device ID. |
| `device_name` | string | The tablet's Home Assistant device name. |

`paused` and `resumed` are reported separately because Home Assistant Core
reports both through its generic `updated` handler. Reconnects, snapshots and a
replayed handler callback never repeat an event: the adapter remembers the last
published state per timer and publishes only a new transition. An explicit
repeat of the same `pause`, `resume`, add or subtract that Core ignores also
produces no duplicate event.

An integration reload adopts the timers that already exist, so a pause made
outside the tablet is still labelled `paused` and the duration the timer
started with is preserved. Core keeps the original `start_hours`,
`start_minutes` and `start_seconds` after time is added (only `created_seconds`
grows), so a reload after an add still reports the original duration — for
example, a 120-second timer extended by 30 seconds reloads and still reports
`total_seconds: 120`. An unnamed timer publishes `name: ""`, never `null`.

```yaml
automation:
  - alias: Flash the lamps when any tablet timer ends
    triggers:
      - trigger: event
        event_type: tacktab_voice_timer
        event_data:
          event_type: finished
    actions:
      - action: light.turn_on
        target:
          entity_id: light.living_room
        data:
          flash: long
      - action: notify.send_message
        target:
          entity_id: notify.mobile_app_phone
        data:
          message: >-
            The {{ trigger.event.data.name or 'timer' }} on the
            {{ trigger.event.data.device_name }} is done.
```

## Which tablet is talking

Each paired tablet has a **Voice turn** sensor. Its state is `idle` when the
tablet is quiet, `realtime` during a realtime conversation and `assist` during
an Assist pipeline turn. Attributes add `device_id`, `device_name`, `area`,
`slot_id` and `active`. This exists because an `assist_satellite` entity stays
idle during a realtime conversation, so it cannot tell automations which
tablet is talking.

```yaml
automation:
  - alias: Pause the TV while the kitchen tablet is talking
    triggers:
      - trigger: state
        entity_id: sensor.kitchen_tablet_voice_turn
        from: idle
    actions:
      - action: media_player.media_pause
        target:
          entity_id: media_player.kitchen_tv
```
