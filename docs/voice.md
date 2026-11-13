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

Media URLs follow one policy shared with the Android and iOS apps (vectors in
`tests/fixtures/voice-url-vectors.json`, a copy of the app repository's
`shared/voice-url-vectors.json`): HTTPS (scheme case is normalized to
lowercase) or a root-relative Home Assistant path which the native app resolves
against its authenticated configured origin.  The bridge rejects other schemes,
HTTP LAN URLs, protocol-relative URLs, userinfo, whitespace, backslashes,
malformed hosts or ports, and values over 2,048 characters (never truncating).
A rejected media URL falls back to the spoken-text card.  Weather keeps a
legitimate `0` temperature.  It never reads
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
| `wake_engine`, `wake_engine_supported` | `micro_wake_word` / `open_wake_word` / `vs_wake_word`, bool | microWakeWord and openWakeWord are carried; only vsWakeWord (no native counterpart) is reported in `warnings`. |
| `wake_sensitivity`, `wake_sensitivity_label` | float 0–1, string | Wake word sensitivity select: Slightly 0.25, Moderately 0.5, Very 0.75, one scale for every engine (upstream's per-engine scales differ). `null` when unknown. |
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
**Conversation memory** select. An idle check runs before anything else, whichever items are carried, and
inputs are validated before any write, so a busy tablet (a run, media lease,
realtime session or configuration change) or an unknown pipeline changes
nothing and returns an error. The result echoes
`candidate_entity_id` and what was `applied`; the native client stores its own
choices only after this success. Review every reported reference yourself: the
replacement timer event is `tacktab_voice_timer`, but the bridge intentionally
does not rewrite an automation or script.

## Conversation memory

Each tablet has a **Conversation memory** select: how long Assist keeps context
between activations. Home Assistant's own Assist satellite keeps a conversation
for five minutes of inactivity, so `5_minutes` is the default and matches the
earlier behaviour. `isolated` starts a new conversation on every activation and
`1_minute` after a minute of quiet. A follow-up that Home Assistant authorized (the tablet listens again after a question) continues the same activation, so it keeps its context even when memory is `isolated`. `15_minutes` and `until_restart` go beyond
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

## Fleet voice profile

Voice settings that should be the same on every tablet live in one **fleet
voice profile** stored in Home Assistant (`.storage/tacktab.voice_profile`). A
tablet opts in, chooses which groups to follow, and either applies what Home
Assistant sends or pushes its own settings to the fleet. Per-device hardware
settings are never shared.

Requires protocol 2 and an app that declares the `voice_profile` capability.
Older apps, and any client on protocol 1, connect exactly as before: they
receive no profile field, no profile event, and the profile commands answer
`unsupported_capability`.

### Groups and keys

Home Assistant validates the keys below with the same ranges the apps enforce.
Keys it does not know are kept verbatim (see *Forward compatibility*).

| Group | Keys |
| --- | --- |
| `voice` | `wake_engine` (`micro_wake_word`\|`open_wake_word`), `wake_word_1`, `wake_word_2` (may be `""`), `wake_sensitivity` 0–1, `wake_fine_tune` −10…10, `stop_fine_tune` −10…10, `stop_word_enabled`, `skip_chime_after_wake`, `follow_up_enabled`, `follow_up_delay_ms` 0–3000, `follow_up_chime` |
| `appearance` | `appearance`, `skin_1`, `skin_2` (lowercase ids such as `glow_orb`), `theme` (`system`\|`light`\|`dark`), `background_dim` 0–0.85, `text_scale` 0.8–1.5, `reactive_activity_bar` |
| `conversation` | `show_transcript`, `show_response`, `show_tool_activity`, `hide_sentiment_tags`, `retain_answer_seconds` 0–300 |
| `timers` | `show_timers`, `show_timer_names`, `show_finished_timers`, `timer_scale` 0.75–1.5, `timer_alert_muted`, `speak_timer_finished` |
| `chimes` | `chimes_enabled`, `chime_volume` 0–1, and per event `wake`, `done`, `error`, `timer`, `announcement`: `<event>_chime_id`, `<event>_chime_enabled`, `<event>_chime_volume` 0–1. Sound ids only; custom sound files are not synced. |

Never synced (dropped, and listed in the update result's `dropped`):
`arbitration_offset_db`, `arbitration_offset`, `microphone_gain`, `mic_gain`,
`input_gain`, `device_name`, `name`, `area`, `area_id`, `device_id`,
`install_id`, `access_token`, `home_assistant_url`, `pipeline_id(_2)`,
`output_target(_2)`, `media_player_entity_id(_2)`, `enabled`, `muted`,
`hands_free`, `announcements_enabled`, `background_listening`, and any key whose
name contains `token`, `secret`, `password`, `credential`, `api_key` or
`private_key`. The full list is in every `voice_profile_get` result as
`excluded_keys`.

### Negotiation

The app adds `"voice_profile": true` to `capabilities` in `connect` (protocol 2
only; `voice_profile: true` on protocol 1 is rejected `unsupported_protocol`).
The field is optional, so apps that do not send it are unaffected. When it is
negotiated, the connect result gains:

```json
"voice_profile": {
  "revision": 12,
  "supported_groups": ["voice", "appearance", "conversation", "timers", "chimes"],
  "following": {"enabled": true, "groups": ["voice", "timers"]}
}
```

Compare `revision` with the last revision the tablet applied; call
`voice_profile_get` when it is newer.

### `tacktab/voice/voice_profile_get`

Request: `{"id", "type": "tacktab/voice/voice_profile_get", "epoch"}`. Result:

```json
{
  "schema_version": 1,
  "revision": 12,
  "updated_at": "2026-11-13T10:15:00+00:00",
  "updated_by": {"type": "tablet", "install_id": "…", "name": "Kitchen"},
  "group_revisions": {"voice": 12, "appearance": 3, "conversation": 0, "timers": 9, "chimes": 0},
  "groups": {"voice": {"wake_sensitivity": 0.4}, "appearance": {}, "conversation": {}, "timers": {}, "chimes": {}},
  "supported_groups": ["voice", "appearance", "conversation", "timers", "chimes"],
  "excluded_keys": ["area", "arbitration_offset_db", "…"],
  "following": {"enabled": true, "groups": ["voice", "timers"]}
}
```

`groups` holds every group; apply only the groups listed in `following.groups`.
`updated_by` is `{"type": "tablet", "install_id", "name"}`,
`{"type": "user", "user_id", "name"}` for an administrator, or `null`. A key
absent from a group simply has no fleet value; keep the local one.

### `tacktab/voice/voice_profile_update` ("push my settings to the fleet")

```json
{
  "id": 7, "type": "tacktab/voice/voice_profile_update", "epoch": "…",
  "request_id": "<uuid, new per user action, reused on retry>",
  "base_revision": 12,
  "groups": {"voice": {"wake_sensitivity": 0.4, "wake_fine_tune": 2}}
}
```

* `groups` is a **partial merge**: the keys sent replace those keys; every other
  key in the stored group, including keys this Home Assistant release or an
  older app does not know, is kept. Send the whole group to push everything.
* `base_revision` must equal the current revision, else the write is refused
  with error code `profile_conflict` (message says the current revision). Run
  `voice_profile_get`, show or reconcile, and retry with the new revision.
* A tablet may push only when it opted in and only groups it follows; otherwise
  `profile_not_following`. Opting in is a Home Assistant setting (below).
* A retry with the same `request_id` returns the original result and does not
  write twice, so a lost response is safe to retry.
* An update that changes nothing returns `changed: false` and does not bump the
  revision.

Result:

```json
{"revision": 13, "changed": true, "updated_at": "…", "group_revisions": {"voice": 13, "…": 0}, "dropped": ["voice.arbitration_offset_db"]}
```

Error codes: `invalid_profile` (bad group, key name or value; nothing is
stored), `profile_conflict`, `profile_not_following`, `unsupported_capability`,
`profile_read_only` (storage is from a newer release), `profile_unavailable`
(save failed), plus the standard `invalid_format`, `stale_epoch`,
`disconnected`.

### `voice_profile_changed` event

Sent on the tablet's subscription to followers (never to the writer, never to a
session that did not negotiate the capability):

```json
{
  "event": "voice_profile_changed", "epoch": "…", "seq": 41,
  "reason": "update",
  "schema_version": 1, "revision": 13, "updated_at": "…",
  "updated_by": {"type": "tablet", "install_id": "…", "name": "Kitchen"},
  "group_revisions": {"voice": 13, "appearance": 3, "conversation": 0, "timers": 9, "chimes": 0},
  "groups": {"voice": {"wake_sensitivity": 0.4, "wake_fine_tune": 2}},
  "following": {"enabled": true, "groups": ["voice", "timers"]}
}
```

`groups` carries the **full current content** of each changed group that the
tablet follows, so apply a group atomically. `reason` is `update` or
`follow_changed`. `follow_changed` is sent when a user changes the tablet's
opt-in in Home Assistant; it carries all followed groups (empty `groups` when
the tablet opted out) so the tablet catches up immediately.

### Following (Home Assistant side)

Each paired tablet has, under its device: **Follow fleet voice profile**
(switch, opt in, default off), **Follow fleet voice / appearance /
conversation / timers / chimes** (switches, default on) and a diagnostic
**Fleet voice profile revision** sensor (state is the revision; attributes
`updated_at`, `updated_by`, `following`, `followed_groups`, `app_supported`).
The same choices are in the tablet's config entry options (**Configure**), stored
as `fleet_profile_follow` and `fleet_profile_groups`.

### Administrator websocket API

Administrators only (a non-admin gets `unauthorized`):

* `tacktab/voice_profile/get` returns the `voice_profile_get` result plus
  `devices`: `[{"install_id", "name", "connected", "negotiated", "following"}]`.
* `tacktab/voice_profile/update` with `base_revision`, `groups`, optional
  `remove` (`{"voice": ["wake_future_knob"]}`, the only way to delete a key) and
  optional `request_id`. Same conflict rules; followers are notified.

### Forward compatibility

* Unknown **keys** inside a known group are stored and returned unchanged, as
  plain bounded JSON (null, boolean, number, string up to 512 characters, lists
  and objects up to 32 items and three levels deep, at most 1024 values in
  all; deeper or larger values are rejected `invalid_profile`, and a stored
  value that exceeds the limits is dropped on load while the rest is kept). A
  group holds up to 128 keys and 8 KiB.
* **Every key at every depth must already be `[a-z][a-z0-9_]{0,63}`.** Keys are
  never rewritten: a nested key with a hyphen, capital letter, space, fullwidth
  or look-alike (for example Cyrillic) letter is dropped and reported in
  `dropped` by path, such as `voice.future.api-key`. A top-level key that is
  malformed rejects the update, unless it is a recognised excluded name in
  another spelling (`deviceId`, `home-assistant-url`), which is dropped and
  reported. Excluded and credential-like names (see above) are removed at any
  depth, including inside lists; the generic names `name`, `enabled` and
  `muted` are excluded only at the top of a group.
* Unknown **groups** are rejected (`invalid_profile`); a new group needs an
  integration release so its follow switch exists.
* Because a push merges key by key, an older app cannot erase a newer app's
  keys. Known keys with out-of-range values reject the whole update.
* Storage is `.storage/tacktab.voice_profile`, version 1.1. A document written
  by an older layout is repaired on load; one from a newer major or minor version
  makes the profile read-only (writes answer `profile_read_only`) instead of being
  overwritten, so unknown groups and metadata survive a downgrade.
* A file that is not valid JSON is handled by Home Assistant's own storage
  layer: it renames the file to `tacktab.voice_profile.corrupt.<timestamp>`,
  raises a repair issue and starts with an empty, writable profile. The
  original bytes stay in the renamed file for manual recovery.
