"""Every knob in one place. Edit here, not in the modules."""

from pathlib import Path

# --- paths -------------------------------------------------------------
PROJECT_ROOT = Path(__file__).parent

# The folder whose CLAUDE.md defines the assistant's identity. The voice
# session boots from here, so it is the same assistant as a terminal session
# launched in that folder.
#
# NOTE: this is the REAL iCloud path. Setup Guide.md documents a convenience
# symlink at ~/Documents/Obsidian-Vault, but that symlink does not currently
# exist on this machine, so we point at the real location.
SESSION_CWD = (
    Path.home()
    / "Library/Mobile Documents/iCloud~md~obsidian/Documents/Obsidian-Vault"
)

# --- services ----------------------------------------------------------
WHISPER_URL = "http://127.0.0.1:2022/v1/audio/transcriptions"
KOKORO_URL = "http://127.0.0.1:8880/v1/audio/speech"

# --- audio -------------------------------------------------------------
MIC_SAMPLE_RATE = 16000       # what whisper wants
MIC_BLOCK = 480               # 30ms at 16k — also the webrtcvad frame size
KOKORO_SAMPLE_RATE = 24000    # what Kokoro returns as raw pcm
ELEVEN_SAMPLE_RATE = 44100    # what we decode ElevenLabs mp3 down from

# --- hold-to-talk ------------------------------------------------------
# pynput key name. Key.cmd_r is Right Command. Swap to Key.alt_r for Right
# Option, Key.shift_r for Right Shift. This is the only line to change.
PTT_KEY_NAME = "cmd_r"

MIN_HOLD_SEC = 0.25           # taps shorter than this are ignored
RELEASE_TAIL_SEC = 0.18       # keep recording after release so the last word survives

# If any other key is pressed while the PTT key is held, treat it as a
# keyboard shortcut (Cmd+C, Cmd+Tab) and abort the utterance silently.
# This is what makes a modifier key safe to use as push-to-talk.
CHORD_CANCEL = True

# --- open-mic mode (legacy, behind --open-mic) -------------------------
VAD_AGGRESSIVENESS = 2        # 0-3, higher = more aggressive filtering
VAD_SILENCE_SEC = 0.8         # silence that ends an utterance
MIN_SPEECH_SEC = 0.24         # discard utterances with less real speech than this

# --- input normalisation -----------------------------------------------
# Whisper reads loud audio far better than quiet audio. Mic level swings
# hugely with distance from the laptop: the same sentence transcribed fine
# at peak 9797 and came back EMPTY at peak 665. We scale quiet captures up
# before sending, capped so near-silence is never amplified into
# hallucinated words.
TARGET_PEAK = 12000.0         # int16; ~37% of full scale, safely below clipping
MAX_GAIN = 12.0

# --- voice -------------------------------------------------------------
# "kokoro" = free local, $0 forever. "elevenlabs" = higher quality, needs
# ELEVENLABS_API_KEY in the environment. ElevenLabs falls back to Kokoro
# automatically on any failure, so the voice degrades instead of going mute.
TTS_ENGINE = "kokoro"

KOKORO_VOICE = "bm_daniel"

# Speaking rate. 1.0 is Kokoro's default and reads a touch slow for a desk
# assistant; 1.15 is noticeably brisker while still natural. Above ~1.35 it
# starts to sound clipped and hurried. Applies to BOTH engines — Kokoro takes
# it natively, ElevenLabs gets it via an ffmpeg atempo stage.
# Measured: "Paper trading is still in the measurement window..." runs 4.58s
# at 1.0, 4.26s at 1.15, 3.86s at 1.25, 3.13s at 1.4.
SPEECH_SPEED = 1.25

# Pick any voice id from https://elevenlabs.io/app/voice-library
# This default is "Brian". Changing the voice is this one line.
ELEVEN_VOICE_ID = "nPczCjzI2devNBz1zQrb"
ELEVEN_MODEL = "eleven_turbo_v2_5"   # turbo. NOT multilingual for English.
ELEVEN_STABILITY = 0.5
ELEVEN_SIMILARITY = 0.75
ELEVEN_STYLE = 0.0                   # above 0 makes delivery slow and dull

# Local mastering for ElevenLabs. Their website previews are mastered demo
# clips; raw API output never matches them. This chain closes most of the gap.
ELEVEN_MASTER_FILTER = (
    "equalizer=f=3200:t=q:w=1.2:g=3.5,"   # presence boost, brings it forward
    "bass=g=2.0:f=140:t=h,"                # gentle low shelf for body
    "acompressor=threshold=-18dB:ratio=3:attack=5:release=120,"
    "alimiter=limit=0.95"
)

# --- ducking -----------------------------------------------------------
DUCK_SPOTIFY = True
DUCK_FLOOR = 30               # never duck below this
DUCK_FACTOR = 0.6
DUCK_RESTORE_DEBOUNCE = 1.2   # so back-to-back sentences don't yo-yo volume

# --- signal bus --------------------------------------------------------
STATE_FILE = PROJECT_ROOT / ".voice_state"
WAVEFORM_FILE = PROJECT_ROOT / ".voice_waveform"
LOADING_PID_FILE = PROJECT_ROOT / ".voice_loading_pid"
# NOTE: .voice_alert is deliberately absent. That file belongs to any OTHER
# process that wants the visualizer's attention. We never write it.

WAVEFORM_HZ = 15              # cap waveform writes at this rate
WAVEFORM_POINTS = 64

# --- what the voice session is allowed to do ---------------------------
# Read-only, deliberately. The voice line can read anything on disk and
# reach the web, and can do nothing else: no shell, no writing, no editing.
#
# `tools` in brain.py sets the base set that EXISTS. Anything not listed
# here is absent from the session rather than merely gated, which is the
# distinction that actually matters for a mic-triggered agent.
VOICE_TOOLS = ["Read", "WebFetch", "WebSearch"]

# Named explicitly so widening the set is a deliberate act, never a drift.
BLOCKED_TOOLS = [
    "Bash", "Write", "Edit", "NotebookEdit", "Task", "Agent",
    "KillShell", "BashOutput", "SlashCommand",
    "mcp__claude_ai_Spotify__*",     # not asked for; ducking uses AppleScript
]

# --- Robinhood: read yes, trade never ----------------------------------
# `tools` above governs BUILT-IN tools only and does nothing to MCP. Your
# claude.ai account connectors load into every CLI session and do NOT appear
# in ~/.claude.json — a check there reports "no MCP servers" while the
# session actually holds 60 of them, including place_equity_order.
#
# This is an ALLOWLIST, not a denylist, and that choice is deliberate: a
# denylist silently fails open the day the connector gains a new mutating
# tool. Anything not named here is denied, including tools that do not exist
# yet. Trading by voice is not a feature — whisper mishears.
ROBINHOOD_READ_TOOLS = {
    "get_accounts", "get_portfolio", "get_equity_positions", "get_option_positions",
    "get_equity_orders", "get_option_orders", "get_equity_tax_lots",
    "get_realized_pnl", "get_pnl_trade_history",
    "get_equity_quotes", "get_option_quotes", "get_index_quotes", "get_indexes",
    "get_equity_historicals", "get_option_historicals", "get_index_historicals",
    "get_equity_price_book", "get_equity_fundamentals", "get_financials",
    "get_equity_technical_indicators", "get_equity_tradability",
    "get_option_chains", "get_option_instruments", "get_option_level_upgrade_info",
    "get_earnings_calendar", "get_earnings_results",
    "get_watchlists", "get_watchlist_items", "get_option_watchlist",
    "get_popular_watchlists", "get_scans", "get_scanner_filter_specs",
    "search",
}

# Everything below mutates money, positions or saved state. Never allowed by
# voice. review_* is included because it is part of the order-placement flow,
# not a quote lookup.
ROBINHOOD_WRITE_TOOLS = {
    "place_equity_order", "place_option_order",
    "review_equity_order", "review_option_order",
    "cancel_equity_order", "cancel_option_order", "cancel_option_exercise",
    "exercise_option",
    "add_to_watchlist", "add_option_to_watchlist", "create_watchlist",
    "remove_from_watchlist", "remove_option_from_watchlist", "update_watchlist",
    "follow_watchlist", "unfollow_watchlist",
    "create_scan", "update_scan_config", "update_scan_filters", "run_scan",
}

RH_PREFIX = "mcp__claude_ai_Robinhood__"

# --- conversation ------------------------------------------------------
QUIT_PHRASES = ("goodbye", "end voice mode", "hang up")

GREETING = "Voice line is up. Hold right command and talk."

# If the model needs tools before it says anything, the line goes silent and
# the person assumes the app is broken. Speak a short holding line instead.
FILLER_AFTER_SEC = 0.9
FILLER_LINE = "One sec."

# How many sentences to batch into one spoken chunk after the first.
# 2 sounds smoother — lone short sentences can land flat — but it makes you
# WAIT for a second sentence before hearing anything more, which is the
# single biggest source of dead air mid-reply. 1 = ship every sentence the
# moment it is complete. Set back to 2 if it sounds too clipped.
BREATH_SENTENCES = 1

SPOKEN_DISCIPLINE = """
<spoken-output>
Your replies are being READ ALOUD by a text-to-speech engine. The person
hears them; they never see them. Write for the ear.

- Short, conversational sentences. Contractions. Plain words.
- NEVER use markdown. No asterisks, no headers, no bullet points, no
  numbered lists, no tables, no code blocks. They get read out as literal
  garbage or silence.
- If you must convey a list, say it as prose: "three things — first X,
  then Y, and finally Z."
- Never read out file paths, URLs, or code verbatim unless explicitly
  asked. Describe them instead. Say "the config file in voice line"
  rather than spelling out a path.
- The TTS performs your punctuation, so flat text becomes flat audio.
  Vary sentence length. Use commas and full stops deliberately.
- Keep answers brief. Two or three sentences for anything simple. The
  person can always ask for more. A long spoken monologue is unbearable.
- Before a tool call that will take a moment, say a short filler line so
  the silence is not dead air. "On it, checking now." Then do the work.
- Never announce that you are about to speak, and never describe your own
  formatting. Just talk.
</spoken-output>
"""
