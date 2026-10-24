---
summary: 'Optional realtime voice relay, credentials, tool permissions and session limits.'
read_when:
  - Configuring or changing realtime voice providers.
  - Reviewing voice privacy, credentials or session ownership.
---

# Optional realtime voice

The next Tacktab mobile update can use OpenAI Realtime, xAI Voice or Gemini
Live instead of an Assist pipeline for either voice slot. Home Assistant owns
the provider connection. The tablet sends PCM16 mono at 16 kHz to HA and plays
the returned PCM16 mono at 24 kHz. A regular Assist slot needs no provider key.

## Setup and privacy

1. Install this integration and add its free Voice satellite bridge setup mode.
2. Pair the tablet with an administrator HA login. Enable voice and grant
   microphone permission locally. Hands-free and background listening are
   separate local choices.
3. In the tablet's provider settings, save a provider API key, then choose a
   model and voice for a slot. Saving a key does not validate a paid voice
   session. Catalog discovery uses metadata endpoints and labels its bundled
   fallback when discovery fails.
4. Select Realtime for that slot. Talk or a granted wake starts a provider
   session. Provider charges and data handling follow your provider account.

Provider keys are shared by the integration's voice-bridge setup entry. They
are stored in normal HA config-entry storage, which is not claimed to be
encrypted. Keys are never returned to a tablet, included in public snapshots,
or retained in mobile preferences. Replace and clear are explicit operations.
Protect HA backups accordingly.

OpenAI Realtime is supported again in integration 1.3.2. Integration 1.3.1
removed saved OpenAI keys, and those keys cannot be recovered. After updating,
enter your OpenAI key again in the tablet's provider settings and explicitly
select OpenAI and Realtime for each slot that should use it. Slots that were
returned to Assist stay on Assist until you choose otherwise, and nothing
switches to a paid provider automatically. Unknown provider names are still
rejected.

Key changes require the actual HA WebSocket request to use TLS. Administrators
may explicitly allow trusted local HTTP in the Voice satellite bridge options;
it starts disabled and additionally requires a private or loopback peer address.
That option permits plaintext credentials on the trusted network.

Audio, conversation text, the configured instructions, tablet name/area and
authorized tool descriptions/results reach the selected provider. A local wake
word does not make a later cloud conversation local. No provider session opens
merely by loading settings or a catalog.

## Tools and ownership

The relay uses HA's real LLM APIs with the paired user, device and area context.
It respects entities exposed to Assist. Complete validated calls execute once;
streaming argument fragments never execute. HA timer tools use the tablet's
device ID and feed the same authoritative native timer mirror.

Only one Assist turn, local/remote output or realtime session owns a tablet's
media lease. Pending starts have a 15-second deadline. Cancellation, disconnect,
provider errors and unload release ownership. Tools that already changed the
home cannot be undone by stopping speech; queued calls are cancelled.

Native playback reports frames actually heard. Interruption retires the old
output and discards late chunks. Native microphone upload defaults to half
duplex during output; optional talk-over requires working device echo
cancellation. A session lasts at most nine minutes, with a configurable
1–60-second follow-up silence deadline and a 60-second utterance cap. Input
queues hold at most two seconds; aggregate unplayed output holds five seconds.

Conversation memory is optional, in RAM only, bounded to 64 entries/512 KiB and
at most 12 hours. It starts at 30 minutes and clears on the corresponding
identity/configuration changes or explicit forget. Completed text and tool
call/result history can seed the next session without re-executing tools.

## Verification

Provider wire fixtures, actual HA WebSocket/bootstrap tests, tool binding,
resampling, queue limits, cancellation, interruption, transport policy and
memory replay are covered by the test suite. Automated validation does not
spend provider credits or prove acoustic echo performance on mounted hardware.
See [realtime-events.schema.json](realtime-events.schema.json) for the strict
event contract and [voice.md](voice.md) for Assist and timer behavior.
