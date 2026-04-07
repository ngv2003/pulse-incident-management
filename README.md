# Pulse — Run Guide (macOS)

## Prerequisites

- macOS (Apple Silicon or Intel)
- Python 3.11+ — check with `python3 --version`
- IBM  BOB
- Rancher Desktop — **instead of Docker Desktop** (not allowed on IBM machines)
- 4 terminal windows 

---

## One-time installs (if not already done)

```bash

# Install Rancher Desktop (replaces Docker Desktop entirely)
brew install --cask rancher

# Open Rancher Desktop from Applications and let it finish setup
# Then verify kubectl and k8s are available
kubectl get nodes   # should show a Ready node
```

---

## Setup (once per project)

```bash
cd /Users/ranjanav/p-ulse/

# Install Python dependencies
pip3 install requests pyyaml

# One-time k8s setup (creates real k8s deployments)
bash setup_k8s.sh

# Set up ElevenLabs MCP venv (for voice feature)
cd elevenlabs_mcp
python3 -m venv venv
./venv/bin/pip install mcp requests python-dotenv
cd ..

# Initialise the database
python3 pulse_core/db.py
# Expected: [DB] Initialised pulse.db
```

---

## Running (4 terminals)

### Terminal 1 — Mock Services
```bash
cd /Users/ranjanav/p-ulse/
python3 mock_services.py
```
Expected:
```
[checkout-svc]  listening on :8101
[inventory-svc] listening on :8102
[db-primary]    listening on :8103
```

### Terminal 2 — Pulse Watcher (BOB Console)
```bash
cd /Users/ranjanav/p-ulse/
python3 pulse_watcher.py
```
Expected:
```
============================================================
  PULSE WATCHER — BOB Console
============================================================
  Polling every 15s
  Services: [checkout-svc, inventory-svc, db-primary]
  BOB path: bob
============================================================
```
**This is your BOB Console. Every investigation, tool call, and reasoning step
streams here in real time when an incident fires.**

### Terminal 3 — Pulse API
```bash
cd /Users/ranjanav/p-ulse/
python3 pulse_api.py
```
Expected:
```
[PULSE API] Listening on http://localhost:8000
```

### Terminal 4 — Trigger terminal
Used for firing incidents and verifying service state during the demo.

---

## Open the UI

```
http://localhost:8000
```

You should see the dark Pulse UI with: *"Watching for incidents... All services nominal."*

---

## Verify everything is up (before triggering anything)

```bash
# All 3 services should return "status": "healthy"
curl -s http://localhost:8101/health | python3 -m json.tool
curl -s http://localhost:8102/health | python3 -m json.tool
curl -s http://localhost:8103/health | python3 -m json.tool

# inventory-svc should show 3 pods running
curl -s http://localhost:8102/pods | python3 -m json.tool
```

---

## INCIDENT 1 — db-primary OOM → checkout-svc cascade

### What happens:
- db-primary crashes with OOM (exit 137, CrashLoopBackOff)
- inventory-svc loses DB connectivity
- checkout-svc times out → 47 errors/min
- Alert fires on **checkout-svc** but BOB traces root cause to **db-primary**
- Requires user approval to restart db-primary pod

### Trigger (Terminal 4):
```bash
python3 trigger_pagerduty.py incident1
```

### What to watch:

**Terminal 2 (BOB Console):**
```
[WATCHER] Signal detected: checkout-svc anomaly
[WATCHER] No rulebook match. Creating incident and invoking BOB...

[BOB] [tool: get_service_dependencies] checkout-svc calls: [inventory-svc]
[BOB] [tool: get_metrics] checkout-svc — error_rate: 47/min ← ANOMALOUS
[BOB] [tool: get_metrics] inventory-svc — degraded, connection refused
[BOB] [tool: get_metrics] db-primary — CrashLoopBackOff ← ROOT CAUSE
[BOB] [tool: get_logs] fetching last 15 min around alert...
[BOB] [tool: get_logs] db-primary OOM killed, exit 137
[BOB] Root cause confirmed. Writing RCA...
```

**UI:**
- Orange incident card appears for checkout-svc
- Investigation steps fill in live
- RCA: db-primary OOM → cascade upstream
- Confidence: 95%
- Recommended action: `Restart db-primary pod to clear OOM crash`

### Approve the fix:
Click **✓ Restart db-primary pod to clear OOM crash** in the UI.
BOB restarts the pod, cascade recovers, incident card turns green.

### Clean up:
```bash
python3 trigger_pagerduty.py recover
bash reset.sh
```

---

## INCIDENT 2 — inventory-svc CPU spike (auto-fix, no approval needed)

### What happens:
- Single inventory-svc pod hits 94% CPU (runaway goroutine)
- Other 2 pods are healthy
- Rulebook match → BOB **auto-restarts** the offending pod, no user approval needed
- Resolves itself — watch Terminal 2 for the auto-fix

### Trigger (Terminal 4):
```bash
python3 trigger_pagerduty.py incident2
```

### What to watch:

**Terminal 2 (BOB Console):**
```
[BOB] [tool: get_pods] inventory-svc — 1 pod CPU 94% ← CPU SPIKE
[BOB] [tool: get_logs] fetching last 15 min around alert...
[BOB] [tool: get_logs] single pod spike, other 2 pods healthy
[WATCHER] Rulebook match: cpu-spike — auto-restarting pod
[BOB] [tool: run_fix] restart_pod inventory-svc
```

**UI:** Incident card appears, steps fill in, resolves automatically (no approve button).

### Clean up:
```bash
python3 trigger_pagerduty.py recover
bash reset.sh
```

---

## INCIDENT 3 — inventory-svc overloaded, scale to 5 replicas

### What happens:
- All 3 inventory-svc pods overwhelmed: CPU 78%, latency 920ms (SLO: 500ms)
- 18 timeouts/min, request queue depth 340
- db-primary is healthy — this is a capacity issue
- Requires user approval to scale from 3 → 5 replicas

### Trigger (Terminal 4):
```bash
python3 trigger_pagerduty.py incident3
```

### What to watch:

**Terminal 2 (BOB Console):**
```
[BOB] [tool: get_metrics] inventory-svc — latency 920ms, 18 errors/min ← ANOMALOUS
[BOB] [tool: get_pods] inventory-svc — all 3 pods at 78% CPU, 72% memory
[BOB] [tool: get_metrics] db-primary — healthy. Not a dependency issue.
[BOB] [tool: get_logs] fetching last 15 min around alert...
[BOB] Traffic spike, insufficient replica count. Recommend scaling to 5.
[BOB] Writing RCA...
```

**UI:**
- Orange card for inventory-svc
- RCA: all replicas at capacity, traffic growth outpaced replica count
- Confidence: 90%
- Recommended action: `Scale inventory-svc from 3 to 5 replicas to handle traffic load`

### Approve the fix:
Click **✓ Scale inventory-svc from 3 to 5 replicas to handle traffic load** in the UI.
BOB scales the deployment, syncs mock state with real k8s pod names, and appends
a step showing all 5 running pods.

### Verify (Terminal 4):
```bash
kubectl get pods -l app=inventory-svc
# Should show 5 Running pods

curl -s http://localhost:8102/pods | python3 -m json.tool
# Should also show 5 pods (mock state synced)
```

### Clean up:
```bash
python3 trigger_pagerduty.py recover
bash reset.sh
```

---

## Chat (per-incident)

Click any incident card → type in the chat box. BOB fetches live data, does not answer from memory.

Examples:
```
how many replicas are running now?
what do the logs show?
what caused this incident, give it in voice
```

The last example triggers ElevenLabs voice — BOB speaks the answer aloud via `afplay`.

---

## Reset between demo runs

```bash
# Full reset — run this between every incident
python3 trigger_pagerduty.py recover
bash reset.sh
```

`reset.sh` restores mock services to healthy state (inventory-svc back to 3 pods)
and wipes + reinitialises pulse.db. Always run it before triggering the next incident.

---

## File structure

```
/Users/ranjanav/p-ulse/
├── config.yaml              ← service topology + alert thresholds
├── rulebook.json            ← cpu-spike rulebook entry (auto-fix)
├── pulse.db                 ← SQLite, auto-created by db.py
├── reset.sh                 ← resets mock state + wipes DB
├── setup_k8s.sh             ← one-time k8s deployment setup
├── mock_services.py         ← Terminal 1: fake microservices (ports 8101-8103)
├── pulse_watcher.py         ← Terminal 2: BOB Console, polls every 15s
├── pulse_api.py             ← Terminal 3: API + serves the UI
├── trigger_pagerduty.py     ← Terminal 4: trigger incidents
├── .bobrules                ← BOB investigation guardrails
├── .bob/
│   └── mcp.json             ← two MCP servers: pulse + elevenlabs
├── pulse_core/
│   ├── db.py                ← SQLite helpers
│   ├── mcp_server.py        ← MCP tools (get_logs, get_metrics, run_fix, etc.)
│   └── bob_runner.py        ← invokes BOB, simulation mode fallback
├── static/
│   └── index.html           ← Pulse UI (served at localhost:8000)
└── elevenlabs_mcp/
    ├── server.py            ← elevenlabs_speak MCP tool
    └── venv/                ← isolated venv for elevenlabs deps
```

---

## BOB not in PATH?

Pulse auto-detects this and runs in **simulation mode**. The simulation writes real
steps and RCA to the database — the UI fills in identically to real BOB. All 3
incidents are fully demoable without BOB installed.

Terminal 2 will show (expected):
```
[BOB RUNNER] WARNING: 'bob' not found — running in SIMULATION MODE
```

---

## Common issues

**Port already in use**
```bash
lsof -ti:8000 | xargs kill -9
lsof -ti:8101 | xargs kill -9
lsof -ti:8102 | xargs kill -9
lsof -ti:8103 | xargs kill -9
```

**"Active incident already exists — skipping"**

A previous incident is still open. Run the full reset:
```bash
python3 trigger_pagerduty.py recover
bash reset.sh
```

**`ModuleNotFoundError: No module named 'yaml'`**
```bash
pip3 install pyyaml
```

**`ModuleNotFoundError: No module named 'requests'`**
```bash
pip3 install requests
```

**Rancher Desktop k8s not ready**
```bash
kubectl get nodes   # wait until STATUS is Ready
# If stuck, restart Rancher Desktop from the menu bar icon
```