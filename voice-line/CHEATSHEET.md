# voice-line — cheat sheet

## Launch

```bash
~/voice-line/run-voice-line.sh
```

Starts whisper (:2022) and Kokoro (:8880) if they aren't already up, then runs
in the foreground. **Run it from Terminal or iTerm, never as a daemon.**

## Controls

| Action | How |
|---|---|
| **Talk** | Hold **Right Command**, speak, release |
| **Interrupt it** | Tap Right Command while it's speaking |
| **Type instead** | Just type and hit Enter — reply is still spoken |
| **Interrupt by typing** | Start typing while it talks |
| **Hang up** | Say or type *"goodbye"*, *"end voice mode"*, *"hang up"* |
| **Force quit** | Ctrl-C |

Holds under **250ms are ignored** (accidental taps).
Pressing any other key while Right Command is held **cancels** the utterance —
so `Cmd+C`, `Cmd+Tab`, `Cmd+S` never open the mic.
Recording continues **0.18s after release** so your last word survives.

## Flags

```bash
run-voice-line.sh --voice elevenlabs   # needs ELEVENLABS_API_KEY
run-voice-line.sh --open-mic           # legacy hands-free VAD mode
run-voice-line.sh --no-duck            # don't duck Spotify
run-voice-line.sh --cwd /some/project  # different identity/context
```

## Switching to ElevenLabs

1. Create a key at <https://elevenlabs.io/app/settings/api-keys>
2. Pick any voice at <https://elevenlabs.io/app/voice-library>, copy its voice ID
3. Put the ID in `ELEVEN_VOICE_ID` in `config.py`
4. Export the key (add to `~/.zshrc` to make it stick):

```bash
export ELEVENLABS_API_KEY="your_key_here"
```

5. Run with `--voice elevenlabs`, or set `TTS_ENGINE = "elevenlabs"` in `config.py`

If ElevenLabs is down, unauthorized, or out of credits, it **falls back to
Kokoro automatically** — the voice degrades, it never goes mute.

## Common knobs (`config.py`)

| Setting | Default | What it does |
|---|---|---|
| `PTT_KEY_NAME` | `cmd_r` | Talk key. Try `alt_r`, `shift_r`, `ctrl_r` |
| `KOKORO_VOICE` | `bm_lewis` | 68 voices available on the server |
| `SESSION_CWD` | Obsidian vault | Folder whose CLAUDE.md is the identity |
| `MIN_HOLD_SEC` | `0.25` | Tap-ignore threshold |
| `RELEASE_TAIL_SEC` | `0.18` | Recording tail after release |
| `CHORD_CANCEL` | `True` | Abort on Cmd+<key> shortcuts |
| `DUCK_SPOTIFY` | `True` | Duck music while speaking |

## macOS permissions (one time)

**System Settings → Privacy & Security → Accessibility** → enable your
terminal app (Terminal, iTerm, Ghostty — whichever you launch from).

This is the one that matters. pynput's key listener uses a CGEventTap, which
macOS gates behind **Accessibility**, not Input Monitoring. If it isn't
granted you'll see this on startup and the key will silently do nothing:

```
This process is not trusted! Input event monitoring will not be possible
until it is added to accessibility clients.
```

Grant it to the *terminal app*, not to python or uv — the permission attaches
to the hosting application. After granting, **fully quit and reopen the
terminal**; it won't pick up the change in an existing window.

Input Monitoring is worth enabling too, but on its own it is not sufficient.

**Microphone** is granted on first use — a prompt appears the first time you
hold the key. It's also granted to the *terminal app*, which is why this must
not run under launchd.

## Signal bus (for a visualizer)

Files written in `~/voice-line/`:

- `.voice_state` — `idle` | `listening` | `thinking` | `speaking`
- `.voice_waveform` — `{"ts": float, "samples": [64 floats]}`, ≤15/sec
- `.voice_loading_pid` — exists while a thinking sound plays

`.voice_alert` is **never written by voice-line** — that one belongs to any
other process that wants the visualizer's attention.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Key does nothing | Input Monitoring not granted to your terminal |
| No audio, no error | Check `~/Kokoro-FastAPI/server.log` |
| Transcribes silence as words | Normal whisper hallucination; the junk filter catches most |
| Slow first reply | Prompt-cache toll; the greeting is meant to cover it |
| Mic picks up speakers | Shouldn't happen — mic is closed unless the key is held |

Servers log to `~/whisper.cpp/server.log` and `~/Kokoro-FastAPI/server.log`.

```bash
pkill -f whisper-server; pkill -f "uvicorn api.src.main"   # stop both
```
