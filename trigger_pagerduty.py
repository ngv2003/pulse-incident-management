#!/usr/bin/env python3
"""
trigger_pagerduty.py — Demo script. Sends real PagerDuty V3 webhook format to Pulse.

Usage:
  python3 trigger_pagerduty.py incident1    # db-primary OOM crash → cascade
  python3 trigger_pagerduty.py incident2    # inventory-svc CPU spike (auto-fix)
  python3 trigger_pagerduty.py incident3    # inventory-svc overload (needs approval)
  python3 trigger_pagerduty.py recover      # recover all services
  python3 trigger_pagerduty.py status       # show service health
"""
import sys, json, uuid, requests
from datetime import datetime

PULSE_API = "http://localhost:8000"

INCIDENTS = {
    "incident1": {
        "service": "checkout-svc",
        "title": "checkout-svc: 502 errors spiking — 47/min, upstream dependency failing",
    },
    "incident2": {
        "service": "inventory-svc",
        "title": "inventory-svc: CPU spike — single pod at 94%, health checks failing",
    },
    "incident3": {
        "service": "inventory-svc",
        "title": "inventory-svc: SLO breach — p99 latency 920ms, single replica overwhelmed",
    },
}


def send_pagerduty_webhook(incident_key: str):
    details = INCIDENTS[incident_key]
    service = details["service"]

    try:
        r = requests.post(f"http://localhost:8101/scenario/{incident_key}", timeout=5)
        print(f"[MOCK] Triggered {incident_key}: {r.json()}")
    except Exception as e:
        print(f"[MOCK] Warning: could not trigger scenario (is mock_services.py running?): {e}")

    payload = {
        "event": {
            "id": uuid.uuid4().hex[:22].upper(),
            "event_type": "incident.triggered",
            "resource_type": "incident",
            "occurred_at": datetime.utcnow().isoformat() + "Z",
            "agent": None, "client": None,
            "data": {
                "id": uuid.uuid4().hex[:14].upper(),
                "type": "incident", "status": "triggered",
                "title": details["title"],
                "created_at": datetime.utcnow().isoformat() + "Z",
                "incident_key": f"pulse-{incident_key}-{uuid.uuid4().hex[:6]}",
                "service": {
                    "id": "P" + uuid.uuid4().hex[:6].upper(),
                    "summary": service,
                    "type": "service_reference",
                    "html_url": f"https://your-company.pagerduty.com/services/{service}"
                },
                "assignees": [{"id": "U" + uuid.uuid4().hex[:6].upper(),
                               "summary": "On-Call Engineer", "type": "user_reference"}],
                "urgency": "high", "priority": {"name": "P1"}
            }
        }
    }

    print(f"\n[PAGERDUTY] Sending V3 webhook to Pulse...")
    print(f"  service: {service}")
    print(f"  title:   {details['title'][:70]}")

    try:
        r = requests.post(f"{PULSE_API}/webhook/pagerduty", json=payload,
                          headers={"Content-Type": "application/json",
                                   "User-Agent": "PagerDuty-Webhook/V3.0"}, timeout=10)
        print(f"\n[PULSE] {r.json()}")
        print(f"\n✓ Incident queued. Watch Terminal 2 (BOB Console).")
        print(f"  Open http://localhost:8000\n")
    except Exception as e:
        print(f"\n[ERROR] Could not reach Pulse API — is pulse_api.py running?\n")


def show_status():
    print("\n[SERVICE STATUS]")
    for svc, port in [("checkout-svc", 8101), ("inventory-svc", 8102), ("db-primary", 8103)]:
        try:
            h = requests.get(f"http://localhost:{port}/health", timeout=3).json()
            m = requests.get(f"http://localhost:{port}/metrics", timeout=3).json()
            s = "✓ healthy" if h["status"] == "healthy" else "✗ DEGRADED"
            print(f"  {svc:<20} {s}  cpu={m['cpu_percent']}%  errors={m['error_rate_per_min']}/min  latency={m['latency_p99_ms']}ms  mem={m['memory_percent']}%")
        except Exception:
            print(f"  {svc:<20} ✗ unreachable")
    print()


def recover():
    try:
        r = requests.post("http://localhost:8101/scenario/recover", timeout=5)
        print(f"[RECOVER] {r.json()}")
        print("Run: bash reset.sh")
    except Exception as e:
        print(f"[RECOVER] Failed: {e}")


def check_services_running():
    ok = True
    try: requests.get("http://localhost:8000/api/incidents", timeout=2)
    except Exception:
        print("[!] pulse_api.py NOT running"); ok = False
    try: requests.get("http://localhost:8101/health", timeout=2)
    except Exception:
        print("[!] mock_services.py NOT running"); ok = False
    return ok


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    if cmd in ("incident1", "incident2", "incident3"):
        if not check_services_running():
            print("\nFix the above and retry."); sys.exit(1)
        send_pagerduty_webhook(cmd)
    elif cmd == "recover": recover()
    elif cmd == "status": show_status()
    else: print(__doc__)