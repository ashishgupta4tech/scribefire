# Scribefire

**Push-to-talk dictation for macOS and Windows.** Hold a key, talk, release — the
text appears at your cursor.

Local Whisper, no subscription. **No audio ever leaves your machine.**

```
hold Right Alt / Right Option  →  talk  →  release  →  text at the cursor
```

---

## What's in this repo

| Path | What it is |
|---|---|
| `app/`, `components/`, `lib/`, `public/` | The marketing website (Next.js 16, React 19, Tailwind 4). |
| `scribefire/` | The desktop application (Python). |
| `tests/` | The application's test suites. |

---

## The desktop app

| | |
|---|---|
| **Hotkey** | Global, hold-to-talk. Default Right Alt (Windows) / Right Option (macOS). The key is not swallowed — other apps still receive it. |
| **HUD** | A floating overlay that draws your voice as an ink stroke. It never takes focus, so your cursor keeps blinking. |
| **Live preview** | Optional streaming preview (Vosk) shown on the HUD. Throwaway — Whisper still produces the text that gets pasted. |
| **Cleanup** | Three tiers with automatic failover: Groq → Ollama → rule-based. |
| **Dictionary** | Your own list of words the transcription gets wrong ("pie torch" → "PyTorch"). |
| **Tray + settings** | A real app, not a script. |

### Install

```bash
git clone https://github.com/ashishgupta4tech/scribefire.git
cd scribefire
python -m pip install -r requirements.txt
python -m scribefire
```

Python 3.11 or newer. The first dictation downloads the Whisper model
(~145 MB for `base`) into your user cache directory, with a progress bar.
Subsequent runs start instantly.

Useful flags:

```bash
python -m scribefire --no-tray     # headless: no tray icon (debugging)
python -m scribefire --check       # print the startup checks and exit
python -m scribefire --debug       # verbose logging
```

### Tests

```bash
python tests/scribefire_smoke.py
python tests/scribefire_hud_test.py
python tests/scribefire_inject_test.py
python tests/scribefire_pipeline_test.py
python tests/scribefire_ui_test.py
python tests/scribefire_models_test.py
```

### Configuration

The config lives in a single JSON file:

| OS | Path |
|---|---|
| Windows | `%LOCALAPPDATA%\Scribefire\config.json` |
| macOS | `~/Library/Application Support/Scribefire/config.json` |

```json
{
  "hotkey": "alt_r",
  "preroll_ms": 500,
  "hud_enabled": true,
  "live_preview": false,
  "whisper_model": "base",
  "cleanup": {
    "mode": "auto",
    "groq_model": "openai/gpt-oss-20b",
    "ollama_model": "llama3.2:3b"
  },
  "dictionary": [["pie torch", "PyTorch"], ["cloud", "Claude"]]
}
```

The **API key is never stored in this file** — it goes into the OS keychain
(Windows Credential Manager / macOS Keychain) via the `keyring` library.

### Cleanup tiers

| Tier | What | Timeout |
|---|---|---|
| 1 | Groq (`https://api.groq.com/openai/v1`) | 3 s |
| 2 | Ollama (`http://localhost:11434`) — works offline | 10 s |
| 3 | Rule-based, pure Python — always succeeds | — |

The tier that actually ran is printed and logged **every single time**, so a
silent fallback is never possible:

```
[cleanup] tier=groq elapsed=0.842s
```

### How the pieces fit

```
hotkey down  →  HUD shows, mic rolls (500 ms of pre-roll is kept in a ring buffer)
hotkey up    →  audio assembled → Whisper → cleanup tier → dictionary → paste
```

**The pre-roll matters.** The input stream is opened once at startup and stays
open. Bluetooth headsets need 300–800 ms to switch from music mode to call
mode, so the first word would be lost if the stream opened on key-press.
Instead a rolling ring buffer keeps the last 1.5 s; each recording starts with
the previous 500 ms already in it.

**The HUD never takes focus.** This is the single most fragile part of the
app: if the overlay becomes the key window, the text field you are typing into
loses the cursor and the dictated text pastes nowhere, with no error anywhere.
The macOS panel overrides `canBecomeKeyWindow`/`canBecomeMainWindow` and sets
`WS_EX_NOACTIVATE` on Windows.

**Two models, one paste.** Whisper cannot stream, so the on-screen preview uses
a second, faster model (Vosk). The preview is throwaway: the pasted text
always comes from Whisper.

---

## The website

Next.js 16, statically exported.

```bash
npm install
npm run dev     # local dev server
npm run build   # static export -> out/
npm run preview # serve the export locally
```

The build is a plain static export (`output: "export"` in `next.config.ts`), so
`out/` can be deployed to any static host.

---

## License

MIT.
