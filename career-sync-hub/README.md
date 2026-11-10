# Career Sync Hub (`career-sync-hub` v2 — README Release Pipeline)

Whenever you finish building a project and write its **`README.md`**, pass that `README.md` to `career-sync-hub` once and the automation handles all four career channels end-to-end:

1. **React Portfolio (`projects.js`)** — Surgically inserts/updates your rich project entry in `export const projects = [...]` and pushes to GitHub (triggering automatic Vercel deployment).
2. **LinkedIn Public Launch Post (Hands-Free API)** — Automatically publishes a launch post (with link preview card) to your LinkedIn feed via `LINKEDIN_ACCESS_TOKEN`.
3. **FlowCV Resume** — Prepares clean ATS action-verb bullets ready for 5-second safe native OS paste (`desktop_agent.py --flowcv --step`).
4. **LinkedIn Profile -> Projects Section** — Prepares a 1st-person systems engineering narrative ready for 5-second paste (`desktop_agent.py --linkedin --step`).
5. **Executive Brief** — Returns a complete Markdown summary of everything done + all generated copy (`data/latest_brief.md` & terminal output).

---

## Architecture Flow

```text
  [Completed Project README.md]
        │
        ├──▶ python publish.py --readme path\to\README.md
        └──▶ OR POST http://localhost:8000/analyze
                    │
                    ▼
        ┌────────────────────────────────────────────┐
        │ 1. LangChain ChatOllama (num_ctx=4096)     │
        │    Extracts structured Portfolio, ATS CV,  │
        │    LinkedIn Profile & LinkedIn Launch Post │
        └───────┬────────────────────┬───────────────┘
                │                    │
                ▼                    ▼
  ┌──────────────────────────┐  ┌─────────────────────────────────┐
  │ 2. GitHub Portfolio Push │  │ 3. LinkedIn Official API        │
  │    Updates `projects.js` │  │    POST /v2/ugcPosts (PUBLIC)   │
  │    -> Vercel Auto-Deploy │  │    -> Returns live Post URL     │
  └─────────────┬────────────┘  └────────────────┬────────────────┘
                │                                │
                └───────────────┬────────────────┘
                                ▼
              ┌─────────────────────────────────────┐
              │ 4. Executive Brief + Desktop Agent  │
              │    Saves `data/latest_brief.md` +   │
              │    5s Ctrl+V paste for FlowCV &     │
              │    LinkedIn Profile Projects        │
              └─────────────────────────────────────┘
```

---

## Setup (`career-sync-hub/.env`)

Copy `.env.example` to `.env` and configure:

| Variable | Purpose |
| :--- | :--- |
| `OLLAMA_MODEL` | Local Ollama model (e.g. `qwen2.5-coder:7b` or `qwen2.5-coder:3b`). |
| `OLLAMA_NUM_CTX` | `4096` (fast local inference for `README.md` inputs). |
| `GITHUB_PAT` | GitHub PAT with `Contents: Read & Write` on your React portfolio repo. |
| `PORTFOLIO_REPO_URL` | HTTPS URL of your React portfolio repository. |
| `LINKEDIN_ACCESS_TOKEN` | Official LinkedIn OAuth token for automatic feed post publishing. |
| `WEBHOOK_SECRET` | Optional Bearer token protecting `/analyze`. |

### How to Get `LINKEDIN_ACCESS_TOKEN` (2 Minutes, Free)
1. Go to **[LinkedIn Developers — Create App](https://www.linkedin.com/developers/apps/new)** (create a personal app/company page if needed).
2. Open the **Products** tab and click **Request access** on:
   - **Share on LinkedIn** (grants `w_member_social`)
   - **Sign In with LinkedIn using OpenID Connect** (grants `openid` & `profile`)
3. Open **Auth -> OAuth 2.0 tools -> Create token**, check **`openid`**, **`profile`**, and **`w_member_social`**, authorize in browser, and paste the token into `.env` as `LINKEDIN_ACCESS_TOKEN=...` (`LINKEDIN_PERSON_URN` is resolved automatically!).

---

## Daily Usage (Once You Finish a Project)

### Option A: One-Command CLI (`publish.py`) — Simplest!
Works whether `uvicorn` is running or not (auto-detects your project's GitHub remote URL from `.git/config`):

```powershell
cd career-sync-hub

# 1. Analyze local README.md -> Push Portfolio -> Publish LinkedIn Post -> Print Brief
.\.venv\Scripts\python.exe publish.py --readme "C:\path\to\your-project\README.md"

# 2. Or add `--paste` to immediately step through FlowCV & LinkedIn Profile paste right after!
.\.venv\Scripts\python.exe publish.py --readme "C:\path\to\your-project\README.md" --paste

# 3. Or analyze directly from a GitHub repo URL (fetches README.md via GitHub API in 0.2s, zero clone!)
.\.venv\Scripts\python.exe publish.py --repo-url "https://github.com/manojkumar2291/vibe"

# 4. Dry-run preview first (generates brief & copy WITHOUT pushing to GitHub or posting to LinkedIn)
.\.venv\Scripts\python.exe publish.py --readme "C:\path\to\your-project\README.md" --dry-run
```

### Option B: Direct HTTP Call to FastAPI (`POST /analyze`)
Start FastAPI (`.\.venv\Scripts\uvicorn.exe server:app --port 8000`), then send any of the 3 formats:

```powershell
# 1. Pass local README path in JSON
Invoke-RestMethod -Method Post -Uri "http://localhost:8000/analyze" `
  -Headers @{ Authorization = "Bearer YOUR_WEBHOOK_SECRET" } `
  -ContentType "application/json" `
  -Body '{"readme_path": "C:/path/to/project/README.md", "live_demo_link": "https://my-app.vercel.app"}'

# 2. Upload raw README.md file bytes directly to /analyze/raw
Invoke-RestMethod -Method Post `
  -Uri "http://localhost:8000/analyze/raw?repo_url=https://github.com/manojkumar2291/vibe" `
  -Headers @{ Authorization = "Bearer YOUR_WEBHOOK_SECRET" } `
  -ContentType "text/markdown" `
  -InFile "C:\path\to\project\README.md"
```

---

## Safe Paste into FlowCV Resume & LinkedIn Profile (`desktop_agent.py`)

```powershell
# Guided field-by-field paste into FlowCV Resume (Title -> Stack -> URL -> ATS Bullets)
.\.venv\Scripts\python.exe desktop_agent.py --flowcv --step

# Guided field-by-field paste into LinkedIn Profile -> Projects section
.\.venv\Scripts\python.exe desktop_agent.py --linkedin --step

# Optional fallback: paste the LinkedIn Feed Post manually if LINKEDIN_ACCESS_TOKEN is not set
.\.venv\Scripts\python.exe desktop_agent.py --linkedin-post
```
