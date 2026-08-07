# voice-line

Hold a key, talk, hear the reply. A local-first push-to-talk voice interface to a
Claude Code session, plus a fullscreen Matrix-rain visualiser that reacts to it.

Speech-to-text and text-to-speech both run **on the machine** — audio never leaves
it. Only the transcript goes to the model API.

```
mic ─► ears (sounddevice) ─► whisper.cpp :2022 ─► warm Claude Agent SDK session
                                                        │
    speakers ◄── mouth (sentence-chunked TTS) ◄─────────┘
                     │
                     └─► signal bus (files) ─► visualiser :8777
```

Built and tested on macOS (Apple Silicon). The architecture is portable; the
launcher, permissions and audio plumbing are macOS-flavoured.

---

## Design

**Half-duplex.** The microphone is open *only* while the key is held, and fully
closed otherwise. The system cannot hear its own speakers, so there is no
echo-cancellation problem and no barge-in on open speakers.

**One warm session.** A single `ClaudeSDKClient` is created at launch and kept
warm. The startup sequence (reading an index, yesterday's notes, the open task
list) is forced to happen during a warm-up query behind the spoken greeting, so
the first real turn is as fast as every other one.

**Sentence chunking is the latency trick.** Partial messages are streamed and
each completed sentence is handed to the mouth immediately, so audio begins while
the rest of the reply is still being written.

**Read-only by design.** The session gets `Read`, `WebFetch` and `WebSearch` and
nothing else. Commands arrive by speech, and speech recognition mishears.

---

## Layout

| File | Role |
|---|---|
| `main.py` | Turn loop, hold-to-talk wiring, raw-terminal typed input |
| `ears.py` | Mic capture, input normalisation, transcription |
| `brain.py` | The warm SDK session, streaming, sentence chunking |
| `mouth.py` | TTS queue and cancellable playback (Kokoro / ElevenLabs) |
| `ptt.py` | Global hold-to-talk listener |
| `gate.py` | Permission allowlist — the last thing before a tool call |
| `signals.py` | Visualiser signal bus (plain files) |
| `ducking.py` | Optional Spotify ducking |
| `config.py` | Every knob, in one place |
| `verify.py` | End-to-end suite (silent by default) |
| `audit_permissions.py` | Enumerates what the live session can actually do |
| `voices.py` | Browse, audition and set the voice |
| `visualizer/` | Matrix-rain scene + read-only bus server |

---

## Requirements

- Python 3.12, [uv](https://docs.astral.sh/uv/)
- [whisper.cpp](https://github.com/ggml-org/whisper.cpp) server on `:2022`
- [Kokoro-FastAPI](https://github.com/remsky/Kokoro-FastAPI) on `:8880`
- `ffmpeg`, `espeak-ng`

Pin `setuptools<81`: `webrtcvad` still imports `pkg_resources`.

```bash
uv sync
./run-voice-line.sh
```

macOS needs **Accessibility** permission for the hosting terminal — not Input
Monitoring. Microphone is granted on first use. Do not run it under launchd;
nobody wants a 24/7 open mic.

---

## Gotchas worth knowing

Each of these cost real debugging time.

**Key repeat.** The OS fires `on_press` continuously while a key is held. Without
a held-state flag every repeat looks like a fresh press and kills the reply
before it speaks. `ptt.py` filters on `self._held`.

**Chord cancellation.** Using a modifier as push-to-talk means `Cmd+C` would open
the mic. If any other key goes down while the PTT key is held, the utterance is
discarded.

**CoreAudio cold open.** The first input-stream open in a process can take over
three seconds. Called synchronously on key press, that stalls the event loop, the
release event queues behind it, and you capture only the tail of silence — a
completely silent failure. `Ears.warm()` pays that cost at launch instead.

**One query at a time.** A `ClaudeSDKClient` has a single response stream. Issue a
second `query()` while another coroutine is still draining `receive_response()`
and both get garbage — the second caller sees a handful of fragment messages, no
text at all, and speaks nothing. Every query+receive cycle holds a lock.

**Flush on content-block stop.** Flushing the sentence buffer only on
sentence-ending punctuation leaves pre-tool filler ("checking now") silent for the
entire tool run, then glued to the answer. Flush when a content block closes.

**Cancel flags latch.** An interrupt flag cleared only inside the playback
function will never be cleared if the worker `continue`s before reaching it — the
mouth goes mute forever while the terminal keeps printing sentences. Use a
generation counter, which cannot get stuck.

**`sd.stop()` does not stop your stream.** It only affects streams started by
`sd.play()`. Aborting a self-managed `OutputStream` requires holding a reference
and calling `.abort()`; otherwise audio drains for ~400ms after an interrupt.

**`allowed_tools` is not a restriction.** It controls what runs *without* being
prompted. The base set of tools that exist is `tools`. Neither governs MCP —
account-level connectors load into every session and never appear in
`~/.claude.json`, so a config check reports "no MCP servers" while the session
holds dozens. Enumerate the live session, not the config. `gate.py` is an
allowlist for exactly this reason: a denylist fails open the day a connector
gains a new tool.

**Headless Chrome fires a late resize** that clears the canvas after a
synchronous render, so screenshots come back black unless you re-render on
resize.

---

## Visualiser

```bash
cd visualizer && python3 server.py        # :8777, reads the live bus
python3 server.py --mock                  # :8778, scripted state loop
```

The server is strictly read-only on the bus. A fresh waveform means "speaking"
regardless of what the state file says, so a stray process cannot break the show
mid-sentence.

`?mockstate=speaking` simulates a state locally; `?shot=speaking&t=2000` renders
one deterministic frame and freezes for screenshotting.

---

## Licence

MIT.
