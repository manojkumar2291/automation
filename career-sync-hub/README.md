# Career Sync Hub (`career-sync-hub`)

A standalone, decoupled Python orchestration hub that watches independent software repositories on `git push`, evaluates engineering impact locally using **LangChain + `ChatOllama`** (`langchain-ollama`, with optional Google Gemini support), surgically commits rich project updates directly into your React portfolio's ES module (`export const projects = [...]` in `projects.js`), and provides a zero-bot-risk local desktop CLI (`desktop_agent.py`) to paste updates into **FlowCV** and **LinkedIn** via native OS keyboard macros.

---

## System Architecture

```text
  [Watched Project Repo] ──(git push)──▶ [.github/workflows/notify-hub.yml]
                                                      │
                                                      ▼  POST /analyze (Bearer / HMAC)
                                        ┌──────────────────────────────────┐
                                        │  server.py (FastAPI Hub)         │
                                        │  1. Clone target repo (PAT)      │
                                        │  2. Extract tree, manifests      │
                                        │     & filtered git diff          │
                                        │  3. LangChain ChatOllama         │
                                        │     .with_structured_output()    │
                                        └──────┬───────────────────┬───────┘
                                               │                   │
                     (if is_high_impact=true)  │                   │  Surgical ES-Module Upsert
                                               ▼                   ▼
                              ┌────────────────────────┐   ┌───────────────────────────────┐
                              │ data/latest_generation │   │ React Portfolio Repository    │
                              │ + GET /latest API      │   │ Preserves imports, `image: x` │
                              └───────────┬────────────┘   │ & pushes -> Vercel auto-build │
                                          │                └───────────────────────────────┘
                                          ▼  Local file or HTTPS pull
                              ┌────────────────────────┐
                              │ desktop_agent.py (CLI) │
                              │ Native Browser + 5s    │
                              │ Countdown + Ctrl/Cmd+V │
                              └──────┬──────────┬──────┘
                                     ▼          ▼
                                 [FlowCV]   [LinkedIn]
```

### Key Engineering Highlights
- **LangChain `ChatOllama` + Native JSON Schema Grammar (`server.py`, `models.py`)**: Binds `LLMEvaluationSchema` directly via `ChatOllama.with_structured_output(..., method="json_schema")` and expands `num_ctx=16384` so local models never truncate prompts or hallucinate malformed JSON keys.
- **Lossless React `projects.js` Surgical Upserter (`portfolio_updater.py`)**: Parses ES module `export const projects = [...]` character spans with a comment/string-aware lexer. Top-level asset imports (`import lab from "./assets/..."`), bare identifier references (`image: lab`), inline comments (`// featured: false`), and bottom filter helpers (`publishedProjects`, `featuredProjects`, `getProjectFilters`) remain **100% untouched byte-for-byte**.
- **Privacy-First Source Links**: Automatically checks GitHub repository visibility (`private` flag) so private/client repositories never publish `source_code_link` to your public portfolio.
- **Windows-Safe Workspace Cleanup**: Explicitly closes `GitPython` file handles and strips Windows read-only `.git/objects/pack` flags before `shutil.rmtree` inside `finally` blocks.

---

## Quick Start

### 1. Pull a Local Coding Model in Ollama & Install Dependencies
```bash
# Ensure Ollama is running with a coding/instruct model pulled
ollama pull qwen2.5-coder:7b

cd career-sync-hub
python -m venv .venv

# Windows (PowerShell)
.\.venv\Scripts\Activate.ps1
# macOS / Linux: source .venv/bin/activate

pip install -r requirements.txt
```

### 2. Configure Environment Variables
Copy `.env.example` to `.env` and fill in your GitHub values:

```bash
cp .env.example .env
```

| Variable | Required | Description |
| :--- | :---: | :--- |
| `LLM_PROVIDER` | No | `ollama` (default, 100% local via `ChatOllama`) or `gemini` (`ChatGoogleGenerativeAI`). |
| `OLLAMA_MODEL` | **Yes** (Ollama) | Local model tag from `ollama list` (e.g., `qwen2.5-coder:7b`, `llama3.1:8b`, `gemma3`). |
| `OLLAMA_BASE_URL` | No | Ollama server URL (default `http://localhost:11434`). |
| `OLLAMA_NUM_CTX` | No | Context window tokens passed to `ChatOllama` (default `16384` to fit full git diffs). |
| `GEMINI_API_KEY` | Only if `gemini` | Google AI Studio API key (Note: consumer Gemini Pro web subscriptions don't grant API keys directly, so local `ChatOllama` is zero-cost and default; free-tier Studio keys also work here). |
| `GITHUB_PAT` | **Yes** | GitHub PAT (`Contents: Read` on watched repos; `Contents: Read & Write` on portfolio repo). |
| `PORTFOLIO_REPO_URL` | **Yes** | HTTPS clone URL of your React portfolio repository. |
| `PORTFOLIO_PROJECTS_PATH` | No | Path inside portfolio repo (default `src/data/projects.js`; auto-discovers `src/projects.js`, `.ts`, or `.json`). |
| `WEBHOOK_SECRET` | Recommended | Shared secret authenticating `POST /analyze`, `GET /latest`, and `GET /history`. |
| `HUB_SERVER_URL` | Hybrid/Cloud | Base URL of `server.py` (default `http://localhost:8000`). |

### 3. Run the Central Webhook Server & Expose Webhook Tunnel
Since `ChatOllama` runs locally on your machine (`localhost:11434`), run `server.py` locally and expose port `8000` with Cloudflare Tunnel or ngrok so GitHub Actions can ping `/analyze`:

```bash
uvicorn server:app --host 0.0.0.0 --port 8000

# In a second terminal (free public HTTPS webhook URL):
cloudflared tunnel --url http://localhost:8000
# or: ngrok http 8000
```

---

## Per-Project GitHub Actions Hook (`.github/workflows/notify-hub.yml`)

Drop `templates/notify-hub.yml` into any watched repository at `.github/workflows/notify-hub.yml`, and set two Actions secrets (`CAREER_HUB_URL` and `CAREER_HUB_SECRET`):

```yaml
name: Notify Career Sync Hub

on:
  push:
    branches:
      - main
      - master

jobs:
  ping-career-hub:
    runs-on: ubuntu-latest
    if: "!contains(github.event.head_commit.message, 'docs(portfolio):')"
    steps:
      - name: Trigger career-sync-hub /analyze
        env:
          HUB_URL: ${{ secrets.CAREER_HUB_URL }}
          HUB_SECRET: ${{ secrets.CAREER_HUB_SECRET }}
          REPO_URL: ${{ github.server_url }}/${{ github.repository }}
          COMMIT_SHA: ${{ github.sha }}
          BRANCH_NAME: ${{ github.ref_name }}
          IS_PRIVATE: ${{ github.event.repository.private }}
          HOMEPAGE_URL: ${{ github.event.repository.homepage }}
        run: |
          jq -n \
            --arg repo_url "$REPO_URL" \
            --arg commit_sha "$COMMIT_SHA" \
            --arg branch "$BRANCH_NAME" \
            --arg live_demo_link "$HOMEPAGE_URL" \
            --argjson is_private "$IS_PRIVATE" \
            '{
              repo_url: $repo_url,
              commit_sha: $commit_sha,
              branch: $branch,
              is_private: $is_private,
              live_demo_link: (if $live_demo_link == "" then null else $live_demo_link end)
            }' > payload.json

          curl --fail-with-body --silent --show-error \
            -X POST "${HUB_URL%/}/analyze" \
            -H "Content-Type: application/json" \
            -H "Authorization: Bearer ${HUB_SECRET}" \
            -d @payload.json
```

---

## Using the Safe Local Desktop Macro CLI (`desktop_agent.py`)

`desktop_agent.py` opens your real logged-in desktop browser, copies ATS-clean or narrative text via `pyperclip`, runs a 5-second countdown so you can click the target input field, and fires native OS paste (`Ctrl+V` / `Cmd+V`).

```bash
# 1. Single-shot paste ATS bullets into FlowCV (opens browser, 5s countdown, pastes)
python desktop_agent.py --flowcv

# 2. Single-shot paste 1st-person narrative + tech stack into LinkedIn
python desktop_agent.py --linkedin

# 3. Interactive field-by-field mode (Title -> Subtitle/Stack -> URL -> Bullets)
python desktop_agent.py --flowcv --step
python desktop_agent.py --linkedin --step

# 4. Browse evaluation history and paste a specific earlier project
python desktop_agent.py --list
python desktop_agent.py --select multi-agent-rag --linkedin --no-browser

# 5. Preview formatted output safely without touching clipboard or keyboard
python desktop_agent.py --flowcv --linkedin --preview
```

> **Emergency Stop**: `pyautogui.FAILSAFE = True` is active at all times. Move your mouse cursor into the **top-left corner `(0, 0)`** of your primary screen during the countdown to abort immediately.

---

## Cross-Platform OS Access Configuration for `PyAutoGUI` & `pyperclip`

### Windows (10 / 11)
1. **No special accessibility toggle required** for standard windows.
2. **Privilege Parity (UIPI)**: Run PowerShell / Windows Terminal as the **same normal user** as your browser (an unprivileged terminal cannot send `Ctrl+V` into an elevated "Run as Administrator" browser window).

### macOS (Sonoma / Sequoia / Ventura)
1. Open **System Settings -> Privacy & Security -> Accessibility**.
2. Click **`+`** and add your terminal application (**Terminal**, **iTerm2**, **Warp**, or **VS Code / Antigravity**), toggle it **ON**, and restart the terminal once.

### Linux (Ubuntu / Debian / Fedora)
1. Install clipboard & X11 packages:
   ```bash
   sudo apt update && sudo apt install -y python3-tk python3-dev xclip xsel scrot
   ```
2. Run under an **X11** session (or XWayland browser window) so `PyAutoGUI` can deliver synthetic `Ctrl+V` events across windows.

---

## Running Automated Tests
```bash
pytest -v
```
