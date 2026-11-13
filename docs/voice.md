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
