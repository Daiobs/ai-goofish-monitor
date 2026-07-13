# Xianyu Intelligent Monitor Bot

[中文](README.md) ｜ [English]

A Playwright and AI-powered multi-task real-time monitoring tool for Xianyu (闲鱼), featuring a complete web management interface.

## Core Features

- **Web Visual Management**: Task management, account management, AI criteria editing, run logs, results browsing
- **AI-Driven**: Natural language task creation, multimodal model for in-depth product analysis
- **Multi-Task Concurrency**: Independent configuration for keywords, prices, filters, and AI prompts
- **SQLite as Primary Storage**: Tasks, results, and price history are persisted in one embedded database instead of repeatedly scanning `jsonl`
- **Advanced Filtering**: Free shipping, new listing time range, province/city/district filtering
- **Instant Notifications**: Supports ntfy.sh, WeChat Work (企业微信), Bark, Telegram, Webhook
- **Scheduled Tasks**: Cron expression configuration for periodic tasks
- **Account & Proxy Rotation**: Multi-account management, task-account binding, proxy pool rotation with failure retry
- **Docker Deployment**: One-click containerized deployment

## Screenshots

![Monitoring Overview](static/img.png)
![Task Management](static/img_1.png)
![Result Viewer](static/img_2.png)
![Notification Settings](static/img_3.png)

## Quick Start

### Requirements

- Python 3.10+
- Node.js + npm (`Node v20.18.3` has been verified to complete the frontend build)
- Playwright CLI and Chromium. Before the first local run, install them with `python3 -m pip install playwright && python3 -m playwright install chromium`
- Chrome or Edge on desktop systems. On Linux, Chromium also works. `start.sh` checks this prerequisite before continuing

```bash
git clone https://github.com/Daiobs/ai-goofish-monitor
cd ai-goofish-monitor
cp .env.example .env
```

### Minimum Configuration

| Variable | Description | Required |
|----------|-------------|----------|
| `OPENAI_API_KEY` | AI model API key | Yes |
| `OPENAI_BASE_URL` | OpenAI-compatible API base URL | Yes |
| `OPENAI_MODEL_NAME` | Model name with image input support | Yes |
| `WEB_USERNAME` / `WEB_PASSWORD` | Web UI login credentials, default `admin/admin123` | No |
| `SESSION_SECRET` | Session signing secret; a strong random value is required in production | Production |

See "Configuration" below for the rest.

### Start Locally

```bash
chmod +x start.sh
./start.sh
```

`start.sh` first validates the Playwright CLI and browser prerequisites. Once they are available, it installs project dependencies, builds the frontend, copies the artifacts, and starts the backend.

### First-Time Setup

1. Open the default Web UI at `http://127.0.0.1:8000` and sign in.
2. Go to "Xianyu Account Management" and use the [Chrome Extension](https://chromewebstore.google.com/detail/xianyu-login-state-extrac/eidlpfjiodpigmfcahkmlenhppfklcoa) to export and paste the Xianyu login-state JSON.
3. Login-state files are stored in `state/`, for example `state/acc_1.json`.
4. Go back to "Task Management", create a task, bind an account if needed, and run it.

### Create Your First Task

- `AI mode`: fill in the requirement description. Submission opens a separate progress dialog while the criteria are generated asynchronously.
- `Keyword mode`: provide keyword rules and the task is created immediately.
- `Region filter`: now uses a province / city / district selector backed by an embedded Xianyu page snapshot instead of manual text input.

## 🐳 Docker Deployment (Recommended)

```bash
git clone https://github.com/Daiobs/ai-goofish-monitor && cd ai-goofish-monitor
cp .env.example .env
vim .env # fill in the required values
docker compose up -d --build
docker compose logs -f app
docker compose down
```

- Default Web UI: `http://127.0.0.1:8000`
- The default Compose mapping binds only to host `127.0.0.1`, so other LAN devices cannot connect directly.
- Compose builds `ai-goofish-monitor:local` from the currently checked-out source by default, keeping the running code aligned with the current branch.
- `APP_IMAGE` can override the built image name and can reference a self-published image when using `--no-build`. This fork does not yet include an image publishing workflow.
- The source-built Docker image includes Chromium, so no extra browser install is required on the host.
- After updating the source, run `docker compose up -d --build` again to rebuild and start it.
- If you change `SERVER_PORT` in `.env`, update the `ports` mapping in `docker-compose.yaml` as well.
- `docker-compose.yaml` now mounts the primary SQLite database directory as `./data:/app/data`, with the default database file at `data/app.sqlite3`
- These paths are persisted by default:
  - `data/` for the SQLite primary store (tasks, results, price history)
  - `state/` for login-state cookie files
  - `prompts/` for task prompt files
  - `logs/` for runtime logs
  - `images/` for downloaded product images and per-task temporary image folders
  - `config.json`, `jsonl/`, and `price_history/` as legacy sources for the first SQLite migration

> Security: before exposing the service to a LAN or the internet, change `WEB_USERNAME` / `WEB_PASSWORD`, configure a strong `SESSION_SECRET` with `APP_ENV=production`, serve it behind an HTTPS reverse proxy, and set `SESSION_COOKIE_SECURE=true`. Do not publish the application port directly to the internet.

### Storage and Migration

- SQLite is now the online primary storage, with the default path `data/app.sqlite3`
- You can override the database path with `APP_DATABASE_FILE`; Docker sets it to `/app/data/app.sqlite3`
- On startup, the app initializes the schema and tries to import existing data once from legacy `config.json`, `jsonl/`, and `price_history/`
- `state/`, `prompts/`, `logs/`, and `images/` remain filesystem-based and are not stored in SQLite
- Product images are temporarily downloaded to `images/task_images_<task_name>/` and are normally cleaned up when the task finishes
- After the first upgrade and after verifying the database contents in `data/app.sqlite3`, you can decide whether to keep the legacy `config.json`, `jsonl/`, and `price_history/` mounts

## User Guide

<details>
<summary>Click to expand Web UI usage notes</summary>

### Task Management

- Supports AI creation, keyword rules, price range, new listing filters, region filters, account binding, and cron scheduling.
- AI task creation runs as a background job and shows a dedicated progress dialog after submission.
- Region filtering can greatly reduce results, so leaving it empty is the safer default.

### Account Management

- Import, update, and delete Xianyu login states.
- Each task can bind a specific account or leave account selection to the system.

### Results and Logs

- The results page and export endpoints now query SQLite instead of directly scanning `jsonl` files.
- The logs page is the first place to inspect login-state expiry, anti-bot issues, or AI call failures.

### System Settings

- View system status, edit prompts, and adjust proxy / rotation-related settings.

</details>

## Developer Guide

### Local Development

```bash
# backend
python -m src.app
# or
uvicorn src.app:app --host 0.0.0.0 --port 8000 --reload

# frontend
cd web-ui
npm install
npm run dev
```

- FastAPI initializes SQLite on startup and performs the one-time legacy import from `config.json/jsonl/price_history` when needed
- `spider_v2.py` now loads tasks from SQLite by default; JSON config is only used when `--config <path>` is passed explicitly
- The default local database path is `data/app.sqlite3`
- The Vite dev server proxies `/api`, `/auth`, and `/ws` to `http://127.0.0.1:8000`.
- `npm run build` writes `web-ui/dist/`, and `start.sh` copies it to the repository root `dist/`.
- FastAPI serves `dist/index.html` and `dist/assets/` from the repository root.
- `./start.sh` prints the default app URL `http://localhost:8000` and API docs URL `http://localhost:8000/docs`.

### Validation

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest
cd web-ui && npm run build
```

### Task Creation API

<details>
<summary>Click to expand API behavior</summary>

- `POST /api/tasks/generate`
  - `decision_mode=ai`: returns `202` with a `job`; the client should poll for progress.
  - `decision_mode=keyword`: returns the created task directly.
- `GET /api/tasks/generate-jobs/{job_id}`: fetch AI task-generation progress.
- `POST /auth/login`: validate credentials and set a signed session cookie.
- `POST /auth/logout`: clear the session cookie.
- `GET /auth/session`: inspect the current browser session.

</details>

## Configuration

<details>
<summary>Click to expand common configuration items</summary>

### AI and Runtime

- `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL_NAME`: required AI model settings.
- `PROXY_URL`: dedicated HTTP/SOCKS5 proxy for AI requests.
- `RUN_HEADLESS`: whether the scraper runs headless; keep it `true` in Docker.
- `SERVER_PORT`: backend port, default `8000`.
- `APP_ENV`: runtime mode, default `development`; `production` refuses to start with a missing or weak session secret or with `SESSION_COOKIE_SECURE=false`.
- `SESSION_SECRET`: session signing secret; use a strong random value of at least 32 bytes in production.
- `SESSION_MAX_AGE_SECONDS`: session lifetime, default `86400` seconds.
- `SESSION_COOKIE_SECURE`: restricts the session cookie to HTTPS; keep `false` for local HTTP and set `true` behind HTTPS.
- `LOGIN_IS_EDGE`: use Edge instead of Chrome locally; Docker images do not bundle Edge and always run with Chromium.
- `PCURL_TO_MOBILE`: convert desktop item URLs to mobile URLs.

### Notifications

- `NTFY_TOPIC_URL`
- `GOTIFY_URL` / `GOTIFY_TOKEN`
- `BARK_URL`
- `WX_BOT_URL`
- `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` / `TELEGRAM_API_BASE_URL`
- `WEBHOOK_*`

### Proxy Rotation and Failure Guard

- `PROXY_ROTATION_ENABLED`
- `PROXY_ROTATION_MODE`
- `PROXY_POOL`
- `PROXY_ROTATION_RETRY_LIMIT`
- `PROXY_BLACKLIST_TTL`
- `TASK_FAILURE_THRESHOLD`
- `TASK_FAILURE_PAUSE_SECONDS`
- `TASK_FAILURE_GUARD_PATH`

See `.env.example` for the full list.

</details>

## Web Authentication

<details>
<summary>Click to expand authentication notes</summary>

- The Web UI signs in through `POST /auth/login`. The backend sets a signed `HttpOnly`, `SameSite=Strict`, `Path=/` cookie; `SESSION_COOKIE_SECURE` controls its `Secure` attribute.
- On startup the frontend calls `GET /auth/session`, and logout calls `POST /auth/logout`. Passwords, session tokens, and trusted login flags are never stored in `localStorage` or `sessionStorage`.
- `/health` remains anonymous. All task, account, result, log, prompt, and settings routes under `/api/*`, plus `/ws`, require a valid session.
- Development generates a temporary secret with a warning when `SESSION_SECRET` is absent or weak, invalidating sessions after restart. Production requires a fixed strong secret and `SESSION_COOKIE_SECURE=true`, otherwise startup is refused.
- Default credentials are for first-time localhost use only. LAN or public exposure requires real credentials, a fixed strong secret, an HTTPS reverse proxy, and a Secure cookie.

</details>

## 🚀 Workflow

The diagram below shows the core processing flow of a monitoring task. The main service runs in `src.app` and launches one or more task processes based on user actions or schedule triggers.

```mermaid
graph TD
    A[Start Monitoring Task] --> B[Select Account/Proxy Configuration];
    B --> C[Task: Search Products];
    C --> D{Found New Products?};
    D -- Yes --> E[Scrape Product Details & Seller Info];
    E --> F[Download Product Images];
    F --> G[Call AI for Analysis];
    G --> H{AI Recommended?};
    H -- Yes --> I[Send Notification];
    H -- No --> J[Save Record to SQLite];
    I --> J;
    D -- No --> K[Next Page/Wait];
    K --> C;
    J --> C;
    C --> L{Risk Control/Exception?};
    L -- Yes --> M[Account/Proxy Rotation and Retry];
    M --> C;
```

## FAQ

<details>
<summary>Click to expand FAQ</summary>

### Why does AI task creation take time?

In AI mode, the system generates analysis criteria before the task itself is created. This now runs as a background job with a separate progress dialog instead of blocking the task form.

### Why is the region filter optional by default?

Region filtering can sharply reduce result volume. Leave it empty if you want a broader market scan first.

### Why does the app say the frontend build artifacts are missing?

It means the repository root `dist/` directory is missing. Run `./start.sh`, or build the frontend in `web-ui/` and make sure the artifacts are copied to the root `dist/`.

### Why does `./start.sh` complain about missing Playwright or a browser?

The script performs a prerequisite check before installing project dependencies. Install the Playwright CLI and Chromium first, then make sure Chrome, Edge, or Chromium is available on the system and rerun `./start.sh`.

</details>

## Acknowledgments

<details>
<summary>Click to expand acknowledgments</summary>

This project referenced the following excellent projects during development. Special thanks to:

- [superboyyy/xianyu_spider](https://github.com/superboyyy/xianyu_spider)

Also thanks to LinuxDo contributors for script contributions:

- [@jooooody](https://linux.do/u/jooooody/summary)

And thanks to the [LinuxDo](https://linux.do/) community.

Also thanks to ClaudeCode/Gemini/Codex and other model tools for freeing our hands and experiencing the joy of Vibe Coding.

</details>


## Notices

<details>
<summary>Click to expand notice details</summary>

- Please comply with Xianyu's user agreement and robots.txt rules. Do not make frequent requests to avoid burdening the server or having your account restricted.
- This project is for learning and technical research purposes only. Do not use it for illegal purposes.
- This project is released under the [MIT License](LICENSE), provided "as is", without any form of warranty.
- The project authors and contributors are not responsible for any direct, indirect, incidental, or special damages or losses caused by the use of this software.
- For more details, please refer to the [Disclaimer](DISCLAIMER.md) file.

</details>

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=Usagi-org/ai-goofish-monitor&type=Date)](https://www.star-history.com/#Usagi-org/ai-goofish-monitor&Date)
