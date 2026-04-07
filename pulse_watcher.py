"""
pulse_watcher.py — Always-on watcher. Zero BOB tokens unless needed.

Run: python3 pulse_watcher.py
This terminal = BOB Console. All reasoning streams here.
"""
import time, json, uuid, sys, os
import requests, yaml
from datetime import datetime
from pathlib import Path
from queue import Queue, Empty

PULSE_DIR = Path(__file__).parent
sys.path.insert(0, str(PULSE_DIR))

from pulse_core.db import init_db, upsert_incident, is_active_incident, get_all_incidents
from pulse_core.bob_runner import run_bob

CONFIG_PATH = PULSE_DIR / "config.yaml"
RULEBOOK_PATH = PULSE_DIR / "rulebook.json"

incident_queue = Queue()

# Services currently having a BOB-approved fix executed.
# Watcher skips signals for these to prevent re-triggering during fix execution.
FIXING_SERVICES: set = set()


def load_config():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def load_rulebook():
    if not RULEBOOK_PATH.exists():
        RULEBOOK_PATH.write_text("[]")
        return []
    txt = RULEBOOK_PATH.read_text().strip()
    return json.loads(txt) if txt else []


def get_dependency_chain(service, config):
    """Get all services connected to this service (upstream + downstream)."""
    services = config.get("services", {})
    related = set()

    def walk_down(svc):
        if svc in related: return
        related.add(svc)
        for dep in services.get(svc, {}).get("calls", []):
            walk_down(dep)

    def walk_up(svc):
        if svc in related: return
        related.add(svc)
        for s, cfg in services.items():
            if svc in cfg.get("calls", []):
                walk_up(s)

    walk_down(service)
    walk_up(service)
    return related


def poll_services(config):
    signals = []
    thresholds = config.get("thresholds", {})
    for svc_name, svc_cfg in config.get("services", {}).items():
        port = svc_cfg.get("port")
        if not port: continue
        try:
            r = requests.get(f"http://localhost:{port}/metrics", timeout=3)
            m = r.json()
            anomalies = []
            if m["error_rate_per_min"] >= thresholds.get("error_rate_per_min", 10):
                anomalies.append(f"error_rate={m['error_rate_per_min']}/min")
            if m["latency_p99_ms"] >= thresholds.get("latency_p99_ms", 500):
                anomalies.append(f"latency_p99={m['latency_p99_ms']}ms")
            if m["memory_percent"] >= thresholds.get("memory_percent", 85):
                anomalies.append(f"memory={m['memory_percent']}%")
            if m["cpu_percent"] >= thresholds.get("cpu_percent", 80):
                anomalies.append(f"cpu_percent={m['cpu_percent']}%")
            if anomalies:
                signals.append({
                    "service": svc_name, "anomalies": anomalies, "metrics": m,
                    "source": "watcher",
                    "title": f"{svc_name} anomaly: {', '.join(anomalies)}"
                })
        except Exception:
            pass
    return signals


def match_rulebook(signal, rulebook):
    for entry in rulebook:
        anomaly_type = entry.get("pattern", {}).get("anomaly_type", "")
        if any(anomaly_type in a for a in signal["anomalies"]):
            return entry
    return None


def build_brief(signal, config):
    svc = signal["service"]
    calls = config.get("services", {}).get(svc, {}).get("calls", [])
    parts = [
        f"Service: {svc}",
        f"Anomalies: {', '.join(signal['anomalies'])}",
        f"Source: {signal.get('source','watcher')}",
    ]
    if signal.get("metrics"):
        m = signal["metrics"]
        parts.append(f"Metrics: error_rate={m['error_rate_per_min']}/min, latency={m['latency_p99_ms']}ms, cpu={m['cpu_percent']}%, mem={m['memory_percent']}%")
    if calls:
        parts.append(f"Dependencies: {calls}")
    return "\n".join(parts)


def investigate(incident_id, signal, config):
    brief = build_brief(signal, config)
    prompt = f"""You are Pulse, an AI on-call assistant. Investigate this production incident.

INCIDENT ID: {incident_id}
{brief}

INVESTIGATION PROTOCOL:
1. write_incident_step(incident_id, "starting investigation")
2. get_service_dependencies — understand topology and check dependency health
3. For EACH failing or degraded service in the dependency chain: call get_logs with window_minutes=15
   — this scopes log fetch to the 15 minutes around the alert, just like production triage
   — write_incident_step summarising what the logs show (keep under 10 words)
4. get_metrics on the root cause service to confirm anomaly
5. get_pods on the root cause service to check pod state
6. write_incident_rca with complete findings

RULES:
- Fetch logs for failing services AFTER checking dependencies — not before
- Never auto-execute run_fix for non-rulebook incidents — use status "needs_action"
- action_taken must be a string (use "" if nothing executed)
- confidence is an integer 0-100
- recommended_actions should list the specific fix for the engineer to approve
- Steps must be under 10 words each
"""
    return run_bob(prompt, label=f"INVESTIGATE:{incident_id[:8]}")


def handle_rulebook_match(incident_id, signal, entry, config):
    svc = signal["service"]
    brief = build_brief(signal, config)
    verify_prompt = entry.get("bob_verify_prompt", "").format(service=svc)
    auto_threshold = entry.get("auto_resolve_confidence", 80)

    prompt = f"""You are Pulse. A rulebook pattern matched for this incident.

INCIDENT ID: {incident_id}
RULEBOOK ENTRY: {entry.get('description', '')}
{brief}

TASK: {verify_prompt}

Steps:
1. write_incident_step(incident_id, "rulebook match: {entry.get('id','')}")
2. get_metrics and get_pods to verify current state
3. get_logs with window_minutes=15 to confirm the anomaly in logs
4. If single pod issue and confidence >= {auto_threshold}: call run_fix(action="restart_pod", service="{svc}", incident_id="{incident_id}")
5. Log the kubectl command in write_incident_step before executing
6. write_incident_rca with findings and action_taken (string, not null)

auto_resolve_confidence threshold: {auto_threshold}
"""
    return run_bob(prompt, label=f"RULEBOOK:{incident_id[:8]}")


def process_signal(signal, config, rulebook, seen):
    svc = signal["service"]
    print(f"\n[WATCHER] Signal: {signal['title']}")

    # Block if BOB is executing a fix for this service right now
    if svc in FIXING_SERVICES:
        print(f"[WATCHER] Dedup: BOB executing fix for {svc} — skipping")
        return set()

    if is_active_incident(svc):
        print(f"[WATCHER] Dedup: open incident for {svc} — skipping")
        return set()

    if svc in seen:
        print(f"[WATCHER] Dedup: {svc} in dependency chain of active incident — skipping")
        return set()

    incident_id = f"INC-{uuid.uuid4().hex[:8].upper()}"
    upsert_incident(
        incident_id, service=svc, title=signal["title"],
        status="investigating", source=signal.get("source", "watcher"),
        severity="critical", triggered_at=datetime.utcnow().isoformat()
    )
    print(f"[WATCHER] Created {incident_id}")

    chain = get_dependency_chain(svc, config)
    print(f"[WATCHER] Suppressing duplicates for chain: {chain}")

    match = match_rulebook(signal, rulebook)
    if match:
        print(f"[WATCHER] Rulebook match: {match['id']} — BOB verifying...")
        handle_rulebook_match(incident_id, signal, match, config)
    else:
        print(f"[WATCHER] No rulebook match — full BOB investigation...")
        investigate(incident_id, signal, config)

    print(f"[WATCHER] Done: {incident_id}")
    return chain


def get_all_chains_for_open(open_svcs, config):
    all_related = set()
    for svc in open_svcs:
        all_related |= get_dependency_chain(svc, config)
    return all_related


def watcher_loop():
    config = load_config()
    poll_interval = config.get("pulse", {}).get("poll_interval_seconds", 15)
    print(f"\n{'='*60}")
    print("  PULSE WATCHER — BOB Console")
    print(f"{'='*60}")
    print(f"  Poll interval: {poll_interval}s")
    print(f"  Services: {list(config.get('services', {}).keys())}")
    print(f"{'='*60}\n")

    seen = set()

    while True:
        try:
            config = load_config()
            rulebook = load_rulebook()

            # Process webhook signals first
            try:
                while True:
                    signal = incident_queue.get_nowait()
                    new_covered = process_signal(signal, config, rulebook, seen)
                    seen |= new_covered
            except Empty:
                pass

            # Poll services
            for signal in poll_services(config):
                new_covered = process_signal(signal, config, rulebook, seen)
                seen |= new_covered

            # Clear seen for resolved incidents only
            open_svcs = {i["service"] for i in get_all_incidents()
                         if i["status"] not in ("resolved", "auto_resolved")}
            seen = seen & (open_svcs | get_all_chains_for_open(open_svcs, config))

        except Exception as e:
            print(f"[WATCHER ERROR] {e}")

        time.sleep(poll_interval)


if __name__ == "__main__":
    init_db()
    watcher_loop()