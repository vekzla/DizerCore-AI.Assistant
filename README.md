<div align="center">

<img src="web-ui/static/DizerCoreYNoBGAI.png" alt="DizerCore AI Assistant" width="220">

# DizerCore AI Assistant

**Turn a Raspberry Pi 5 + NVMe into an offline prompt-engineering workstation for World of Warcraft emulation.**

Describe a problem → get a structured WoW prompt → paste it into any AI coding agent.

*No API keys. No cloud. No telemetry.*

[![Platform](https://img.shields.io/badge/platform-Raspberry%20Pi%205-c51a4a?style=flat-square)](https://www.raspberrypi.com/)
[![Model](https://img.shields.io/badge/model-Qwen2.5-6236ff?style=flat-square)](https://huggingface.co/Qwen)
[![Runtime](https://img.shields.io/badge/runtime-llama.cpp-8a2be2?style=flat-square)](https://github.com/ggerganov/llama.cpp)
[![Web](https://img.shields.io/badge/interface-Flask-000000?style=flat-square)](https://flask.palletsprojects.com/)

</div>

---

<div align="center">

📖 **[Install](#-install)** · **[Daily Use](#-daily-use)** · **[Training](#-custom-model-training)** · **[Threat Model](#-threat-model)** · **[Troubleshooting](#-troubleshooting)** · **[Cheat Sheet](#-cheat-sheet)**

</div>

---

## 🧠 What It Is

DizerCore turns a Pi 5 into a **self-contained AI prompt refinery** for TrinityCore WoW server development. You describe a problem in plain language; the Pi searches your reference codebase, feeds the matched files to a local LLM, and returns a structured investigation prompt you can paste into a coding agent.

Everything runs on the Pi. No data leaves the device.


---

## 🛡️ Threat Model

> **Read this before exposing DizerCore to any network.**

DizerCore is designed for a **single-user Raspberry Pi on a trusted LAN**.

### Authentication

- Every `/api/*` request requires a shared-secret token sent as the `X-DizerCore-Token` header.
- The token is generated at install time and stored in `/data/web-ui/.api-token` (mode `600`).
- The browser fetches it from `/api/token`, which is readable **same-origin only** — this is what stops CSRF.

### What the token protects against — and what it doesn't

| ✅ Protects against | ❌ Does NOT protect against |
|---|---|
| Cross-site request forgery | Anyone who can read `/data/web-ui/.api-token` |
| Casual LAN probing | Anyone with shell access to the Pi |

> **With the token**, an attacker has full API access — model deploy, index rebuild, and a root-privileged `install.sh` re-run via `/api/update`.

### Network exposure

| ✅ Do | ❌ Don't |
|---|---|
| Keep it on a trusted LAN | Port-forward port `5000` |
| Use SSH port-forwarding for remote access | Put the Pi on an untrusted network |
| Use Tailscale for cross-network access | Assume the token is secret |

**SSH port-forward example:**
```bash
ssh -L 5000:127.0.0.1:5000 pi@<pi-ip>
# Then open http://localhost:5000 in your browser
```

    raffic is plain HTTP — no TLS. Fine on a trusted LAN; don't send anything sensitive over a network you don't control.

Sudo surface

NOPASSWD rules live in /etc/sudoers.d/dizercore-update and cover exactly two targets:
Target	Purpose	Owner	Risk
/usr/local/sbin/dizercore-training-deploy	Model swap	root	Safe
install.sh	Update path	user-owned source dir	Residual — see below

The install.sh target is the residual risk: the Web UI user owns the source directory, so any code execution reaching the Web UI can overwrite install.sh and get root on the next /api/update. This is by design for an unauthenticated LAN appliance, but it means the Web UI must be trusted.
📋 Requirements
Component	Spec
Board	Raspberry Pi 5 8 GB
Storage	NVMe SSD via M.2 HAT
OS	Raspberry Pi OS 64-bit Lite
Network	SSH + internet during install
Cooling	Active cooler recommended if overclocking
📦 Install

One command from a fresh Pi OS:
```bash
curl -fsSL https://raw.githubusercontent.com/vekzla/DizerCore-AI.Assistant/main/bootstrap.sh -o /tmp/dca.sh && sudo bash /tmp/dca.sh
```
Duration: ~15–30 minutes (most of it compiling llama.cpp)

The installer runs inside a tmux session so it survives SSH disconnects. You'll be attached automatically.

    💡 Why -o /tmp/dca.sh instead of | sudo bash?

    tmux needs a real terminal. Piping to bash makes stdin a pipe, which tmux can't use. Downloading first keeps stdin as your terminal.

tmux controls
Action	Keys
Detach (leave install running)	Ctrl+B then D
Reattach	tmux attach -t dizercore-install
Kill the session	tmux kill-session -t dizercore-install
What the Install Looks Like
text

━━━ 1/10: NVMe setup ━━━
[+] NVMe mounted at /data
[+] Persisting source repo to /data/dizercore-src ...

━━━ 2/10: Docker ━━━
[+] Docker data-root → /data/docker

━━━ 3/10: Gitea ━━━
[+] Gitea starting at http://192.168.1.33:3000

━━━ 4/10: llama.cpp + model ━━━
Selected model: Qwen2.5-3B-Instruct (1.9 GB, ~3 tok/s)
[+] llama.cpp built successfully
[+] Model downloaded (1.0G)

━━━ 5/10: llama-server (persistent model in RAM) ━━━
[+] llama-server ready — model resident in RAM (~1.9GB loaded)

━━━ 6/10: Web UI ━━━
[+] Web UI running at http://192.168.1.<PI:IP>:5000

━━━ 7/10: System optimization + overclock ━━━
[+] ZRAM configured (50% of RAM, zstd compression)
Overclock the Pi 5? [y/N]: y
Profile [1]: 1
[+] Applied conservative overclock — 2.6 GHz CPU, 850 MHz GPU
[+] Note: the `dtparam=fan_temp*` curve in `config.txt` only drives
    the official GPIO Active Cooler.

━━━ 8/10: Reference repository ━━━
Git repository URL (leave blank to skip): https://github.com/vekzla/DizerCore-WoW.git
Branch to checkout [main]: Feature/Housing
[+] Reference repo ready: /data/reference/DizerCore-WoW

━━━ 9/10: Training infrastructure ━━━
[+] Training assets copied to /data/web-ui/training
[+] Deploy helper ready
[+] Sudoers rules updated

━━━ 10/10: README ━━━
[+] Runtime README written to /data/README.md

Install Report
text

═══════════════════════════════════════════════════════════════════
  Install Report
═══════════════════════════════════════════════════════════════════
  Installed:   22
  Skipped:     4
  Warnings:    0
  Errors:      0

  Full log: /var/log/dizercore-install.log
  Exit code: 0
═══════════════════════════════════════════════════════════════════

🚀 After Install
bash

# 1. Log out and back in (activates docker group)
exit

# 2. Reconnect, then verify
groups          # should include "docker"

# 3. Reboot if you chose an overclock profile
sudo reboot

# 4. Open the Web UI
#    http://<pi-ip>:5000

What You Get
Service	URL	Purpose
🌐 Web UI	http://<pi-ip>:5000	Prompt refinement, stats, history, logs, index, training
📦 Gitea	http://<pi-ip>:3000	Local Git server for your repos
🤖 llama-server	http://127.0.0.1:8080	Persistent LLM endpoint (localhost only)
👁️ index-watcher	http://127.0.0.1:8091	Keeps the reference index fresh

Everything else lives on the NVMe at /data.
☀️ Daily Use

    Open http://<pi-ip>:5000

    Type a rough problem — "feathering the nest quest credit not firing"

    Click Refine Prompt

    Wait ~3–5 seconds

    Output shows a mode badge (CPP / SQL / SMART / OPCODE / DBC) and a source tag (FTS5 / RIPGREP)

    Click Copy

    Paste into your AI coding agent

Every refinement is saved to /data/prompt-history/history.json.
🎛️ Web UI Panels
Live Stats Bar

Updated every 3 seconds below the header:
text

CPU   ▓▓▓░░░░░░░░░░░  23%    RAM   ▓▓▓▓░░░░░░░░  26%    DISK  ▓░░░░░░░░░░░  10%
TEMP  ▓▓▓▓▓░░░░░░░░  52°C    GPU   800 MHz

Widget	🟡 Warn	🔴 Critical
CPU	60%	85%
RAM	75%	90%
DISK	75%	90%
TEMP	65°C	80°C
GPU	—	⚠ when throttled

    Hover any widget for detail. The bar pauses when the browser tab is hidden.

Version Badge
Badge	Meaning
🟢 ✓ a3f9c12	Up to date with main
🔴 ✗ b8e2d45	Out of date — click Update
⚪ ⚠ offline	Cannot reach GitHub
System Info

Grouped snapshot of every component:

    DizerCore commit, Docker, Gitea, PostgreSQL

    llama.cpp, active model + RSS

    Reference repo, index stats

    Training state, history count

Logs

Filterable installer log with live counts. Five filters:

    All · Installed · Skipped · Warnings · Errors

Colour-coded lines. Download and Clear buttons.
Index

FTS5 index state:

    File count, size, last build, watcher status

    Rebuild Now button

    Option to index all SQL versions (larger, slower)

Training

Four states:
State	Actions
No dataset	Build Dataset
Dataset ready	Download Dataset + Rebuild
Awaiting upload	File picker + Upload & Deploy
Deployed	Revert to Base Model + Upload Different Model

Training button colour indicates state:
Colour	Meaning
🟡 Yellow	Dataset exists, no trained model
🟣 Purple	Trained model active
🔴 Red	Error state
🗺️ WoW Core Domain Routing

Auto-detects the domain of each prompt and applies a matching system prompt.
Domain	Trigger Keywords	Focus
CPP (default)	none matched	src/server/game/
SQL	sql, database, table, quest_template, smart_scripts	World DB tables
SMART	smart, script, phase, boss script	SMART_ACTION_*, SMART_EVENT_*
OPCODE	opcode, packet, cmsg_, smsg_, handler	Packet handlers
DBC	dbc, db2, client data, visual	Client data files
Repo Search

Before calling the model, the Web UI searches the reference repo for keywords from your input and injects matched file paths and snippets.
Method	Latency	When used
FTS5 index	~50 ms	Primary
ripgrep	~4.5 s	Fallback

The index-watcher.py service keeps the index fresh, checking every 5 minutes. It pauses while you're refining a prompt.
Mode and Source Badges

Every refined prompt shows two coloured badges:

    Mode — CPP (🟢 green), SQL (🔵 blue), SMART (🟡 yellow), OPCODE (🩷 pink), DBC (🟣 purple)

    Source — FTS5 (🟢 green), RIPGREP (🟡 yellow)

🏗️ Architecture
text

Browser → Web UI (Flask, port 5000)
              │
              │  1. Classify prompt → mode
              │  2. Search index → matched files + snippets
              │  3. Build system prompt
              │
              ├── POST to llama-server (port 8080)   ← fast path
              │
              └── fallback: llama-cli subprocess     ← slow path

Background:
  index-watcher.py (port 8091)
    ├── Checks reference repo every 5 min
    ├── Rebuilds FTS5 index on change
    ├── Rebuilds training dataset on change
    └── Pauses while Web UI is running an AI task

🎓 Custom Model Training

Fine-tune whichever Qwen2.5 model is installed on the Pi. The dataset carries a meta row naming the base model — e.g. 3b-base → Qwen/Qwen2.5-3B-Instruct.

Runs once on Kaggle's free T4 GPU; produces a ~1–2 GB GGUF that replaces the base model.

📖 Full walkthrough: training/README.md
The Four Phases
#	Phase	Where	Time
1	Build dataset	Pi (Web UI)	2–5 min
2	Train + convert	Kaggle notebook	~1–3 hours
3	Upload GGUF	Pi (Web UI)	~1 min
4	Test	Pi (Web UI)	instant

    ⚠️ If you switch models on the Pi, rebuild the dataset before retraining. The meta row must match the installed model, or the wrong base gets trained.

What Training Does

The LoRA adapter teaches the model WoW Core's patterns:

    Naming conventions (SMART_ACTION_ADD_QUEST_CREDIT, spell_area, quest_poi)

    File layout (src/server/game/, sql/old/12.x/world/)

    Common APIs and signatures

    SQL row structure

Specific facts (like quest ID 94210) still come from FTS5 retrieval at inference time:

    RAG = facts · LoRA = style

Reverting

Training tab → Revert to Base Model. Switches back in ~15 seconds. Trained file stays on disk.
When to Retrain

Only when:

    You switch the installed base model (rebuild the dataset too)

    Your WoW Core fork diverges significantly

    You've added substantial new SQL content

Files in the Repo
File	Purpose
training/dataset-builder.py	Walks /data/reference/, produces JSONL
training/dizercore-colab.ipynb	Kaggle training notebook (multi-cell)
training/README.md	Step-by-step guide
web-ui/training-deploy.sh	Privileged helper that swaps the active model
🔄 Self-Update

Updates run in a detached systemd-run --unit=dizercore-update unit so they survive the prompt-gateway restart mid-update. Status is read from systemctl is-active dizercore-update plus /var/log/dizercore-update.log.

Update sequence:

    git fetch origin main in /data/dizercore-src

    git reset --hard origin/main

    Runs install.sh with DIZERCORE_NON_INTERACTIVE=1

What the Update Button Covers
Component	Updated?
Installer, Web UI, system prompts, watcher, training scripts	✅
Python dependencies	✅
llama.cpp binary	✅ if missing
Model file	✅ if missing
Trained LoRA model	❌ preserved
Gitea / PostgreSQL containers	❌ manual
Docker Engine	❌ manual
Overclock config	❌ set once
📝 Logging

Every installer action is logged to /var/log/dizercore-install.log on the SD card (survives NVMe wipes).
Tag	Meaning
[STEP]	Section header
[INSTALL]	Action taken
[SKIP]	Already present
[WARN]	Non-fatal issue
[ERROR]	Fatal error

Useful greps:
bash

# Everything set up
grep '\[INSTALL\]' /var/log/dizercore-install.log

# Every failure
grep '\[ERROR\]' /var/log/dizercore-install.log

Log Rotation (Optional)
bash

sudo tee /etc/logrotate.d/dizercore > /dev/null <<'EOF'
/var/log/dizercore-install.log {
    weekly
    rotate 8
    compress
    missingok
    notifempty
}
EOF

🔍 System Audit

Read-only diagnostic. Checks every component and reports what's present, missing, or misconfigured.
Run It
bash

curl -fsSL https://raw.githubusercontent.com/vekzla/DizerCore-AI.Assistant/main/audit.sh -o /tmp/audit.sh
sudo bash /tmp/audit.sh

What It Checks
#	Section	Verifies
1	Operating System	Kernel, architecture, uptime
2	Hardware	Pi model, RAM, CPU clock, temperature, throttle
3	Storage	/data mount, NVMe device, fstab
4	Base packages	curl, git, python3, jq, ripgrep, cmake, etc.
5	Docker	5 packages, version, data-root, apt repo, containerd
6	Systemd services	llama-server, prompt-gateway, index-watcher
7	llama-server	Active model path, health endpoint
8	llama.cpp build	llama-cli, llama-server, llama-quantize
9	Web UI files	Python, HTML, assets
10	Python venv	flask, flask-cors, psutil versions
11	Gitea + PostgreSQL	Container status, image tags, branding
12	Reference / index / training	Repos, FTS5 index, dataset, trained GGUF
13	Config files	Sudoers, daemon.json, ZRAM, logrotate, overclock
14	Logs	Install + uninstall sizes, error/warning counts
Output Legend
Marker	Meaning
🟢 ✓	Present and correct
🔴 ✗	Missing or broken
🟡 !	Present but non-standard
Report File

Saved to /tmp/dizercore-audit-YYYYMMDD-HHMMSS.txt.
bash

scp <your-user>@<pi-ip>:/tmp/dizercore-audit-*.txt .

    The script is read-only. Safe to run any time.

🔁 Reference Repo Auto-Sync

The index-watcher service already checks every 5 minutes whether any file under /data/reference/ is newer than the index DB — and rebuilds the index + dataset automatically when it is.

So the only scheduled job needed is a git pull; the watcher does the reindexing on its own. No manual trigger, no extra daemons.
1. Make the log writeable
bash

sudo touch /var/log/dizercore-repo-sync.log && sudo chown $USER:$USER /var/log/dizercore-repo-sync.log

2. Create the cron job
bash

crontab -e

Add:
cron

0 */6 * * * cd /data/reference/DizerCore-WoW && git checkout master && git pull --ff-only origin master >> /tmp/dizercore-repo-sync.log 2>&1

🔒 Automatic Security Updates

Security-only updates run via unattended-upgrades (Debian's standard tool — no cron needed, self-scheduled daily).
bash

sudo apt update
sudo apt install -y unattended-upgrades apt-listchanges
sudo dpkg-reconfigure -plow unattended-upgrades   # select "Yes"

🔧 Troubleshooting
Training button stays orange after upload
bash

sudo systemctl cat llama-server | grep -- "-m "
curl -s http://127.0.0.1:8080/v1/models | python3 -m json.tool

If both show /data/models/dizercore-q4_k_m.gguf, the deploy worked — force-refresh with Ctrl+Shift+R.

If it still shows orange, check the sudoers rule:
bash

sudo cat /etc/sudoers.d/dizercore-update
# Should include:
# <your-user> ALL=(ALL) NOPASSWD: /usr/local/sbin/dizercore-training-deploy

Prompt output references made-up schema

    Check the index is built — click Index

    Confirm mode and source badges appear in the output

    Add entries to /data/web-ui/game-data.txt for whatever it got wrong

Dataset build log doesn't stream live

The current version streams. If you're on an older install, the log buffers until the process exits.
llama-server won't start after model deploy
bash

sudo systemctl status llama-server -n 30

Revert from the Training tab, or manually:
bash

sudo cp /etc/systemd/system/llama-server.service.pre-training /etc/systemd/system/llama-server.service
sudo systemctl daemon-reload && sudo systemctl restart llama-server

Index watcher keeps rebuilding
bash

journalctl -u index-watcher -f

If firing too often, increase the interval:
bash

sudo systemctl edit index-watcher

Add:
ini

[Service]
Environment="WATCH_INTERVAL=1800"

GPU shows ⚠ throttle warning
bash

vcgencmd get_throttled

Value	Meaning
0x0	Clean
0x50000	Throttled previously, not now
0x5	Actively throttled
🗑️ Uninstall
bash

curl -fsSL https://raw.githubusercontent.com/vekzla/DizerCore-AI.Assistant/main/uninstall.sh -o /tmp/dca-uninstall.sh
sudo bash /tmp/dca-uninstall.sh

Flag	Effect
--yes / -y	Non-interactive
--keep-nvme	Preserve /data
--keep-docker	Leave Docker installed

Or answer y at the wipe prompt when re-running the installer.
📋 Cheat Sheet
bash

# ── Web UI ──────────────────────────────────────────────
sudo systemctl restart prompt-gateway
sudo journalctl -u prompt-gateway -f

# ── llama-server ────────────────────────────────────────
sudo systemctl status llama-server
sudo systemctl restart llama-server
curl -s http://127.0.0.1:8080/health

# ── Index watcher ───────────────────────────────────────
sudo systemctl status index-watcher
sudo journalctl -u index-watcher -f

# ── Game data ───────────────────────────────────────────
nano /data/web-ui/game-data.txt

# ── Gitea ───────────────────────────────────────────────
cd /data/gitea && docker compose ps

# ── History ─────────────────────────────────────────────
cat /data/prompt-history/history.json | jq '.[] | {mode, original}'

# ── Training ────────────────────────────────────────────
ls -lh /data/training/
ls -lh /data/models/dizercore-q4_k_m.gguf
curl -s http://127.0.0.1:5000/api/training/status | python3 -m json.tool

# ── Logs ────────────────────────────────────────────────
tail -f /var/log/dizercore-install.log
grep '\[ERROR\]' /var/log/dizercore-install.log

# ── Live readings ───────────────────────────────────────
vcgencmd measure_temp
vcgencmd measure_clock arm
vcgencmd measure_clock v3d
vcgencmd get_throttled
free -h
df -h /data

# ── Versions ────────────────────────────────────────────
docker --version
/data/llama.cpp/build/bin/llama-cli --version

📚 Reference
Directory Map
Path	Contents
/data/gitea	Gitea config and repos
/data/postgres	PostgreSQL data
/data/models	Base GGUF + trained dizercore-q4_k_m.gguf
/data/training	dizercore-dataset.jsonl
/data/prompt-history	history.json
/data/reference	Cloned reference repos
/data/web-ui	Flask app + logo + game-data.txt + training/
/data/llama.cpp	llama.cpp source and build
/data/.dizercore-model	Saved model choice
/data/dizercore-src	Persistent source clone
/var/log/dizercore-install.log	Install log (SD card)
/etc/systemd/system/llama-server.service	Model server unit
/etc/systemd/system/prompt-gateway.service	Web UI unit
/etc/systemd/system/index-watcher.service	Watcher unit
Repo Layout
text

DizerCore-AI.Assistant/
├── bootstrap.sh                 Entry point — installs tmux, runs install.sh
├── install.sh                   Installer (10 steps)
├── uninstall.sh                 Full removal
├── audit.sh                     Read-only diagnostic
├── lib/                         One file per install step
│   ├── common.sh
│   ├── 01-nvme.sh
│   ├── 02-docker.sh
│   ├── 03-gitea.sh
│   ├── 04-llama.sh
│   ├── 05-webui.sh
│   ├── 06-optimize.sh
│   ├── 07-readme.sh
│   ├── 08-reference-repo.sh
│   ├── 09-llama-server.sh
│   └── 10-training.sh
├── training/
│   ├── dataset-builder.py
│   ├── dizercore-colab.ipynb
│   └── README.md
└── web-ui/
    ├── app.py
    ├── training-deploy.sh
    ├── indexer.py
    ├── index-watcher.py
    ├── game-data.txt
    ├── requirements.txt
    ├── static/DizerCoreYNoBGAI.png
    └── templates/index.html

Model Reference

Change the model by re-running the installer or editing /data/.dizercore-model.
Key	Model	Size	Speed	Notes
0.5b	Qwen2.5-Coder-0.5B	380 MB	~25 tok/s	
0.5b-base	Qwen2.5-0.5B-Instruct	0.4 GB	~45 tok/s	
1.5b	Qwen2.5-Coder-1.5B	1.0 GB	~8 tok/s	
1.5b-base	Qwen2.5-1.5B-Instruct	1.1 GB	~25 tok/s	default
3b	Qwen2.5-Coder-3B	1.9 GB	~3 tok/s	
3b-base	Qwen2.5-3B-Instruct	1.9 GB	~18 tok/s	

Interactive:
bash

cd /data/dizercore-src
sudo bash install.sh

Non-interactive:
bash

echo 1.5b-base | sudo tee /data/.dizercore-model
sudo bash /data/dizercore-src/install.sh

<div align="center"><img src="web-ui/static/DizerCoreYNoBGAI.png" alt="DizerCore" width="80">

Built by DizerCore AI Assistant

Powered by llama.cpp · Qwen · Gitea · Flask
</div> ```
