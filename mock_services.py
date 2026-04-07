"""
mock_services.py — 3 mock microservices for Pulse demo.

Services:
  checkout-svc  :8101  — depends on inventory-svc
  inventory-svc :8102  — depends on db-primary
  db-primary    :8103  — no dependencies

Scenarios:
  INCIDENT 1: db-primary OOM crash -> cascade up -> BOB traces + user approves restart
  INCIDENT 2: inventory-svc CPU spike on single pod -> BOB auto-restarts (rulebook)
  INCIDENT 3: inventory-svc overloaded (1 replica overwhelmed) -> BOB recommends scale to 5 -> user approves
"""

import sys
import threading
import time
import json
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler

# ── Shared state ──────────────────────────────────────────────────────────────
# inventory-svc starts with 3 pods — this is the steady-state baseline.
# Incident 3 simulates that 3 pods are being overwhelmed by traffic,
# so BOB recommends scaling UP to 5 to handle the load.
STATE = {
    "checkout-svc": {
        "healthy": True, "error_rate": 0, "latency_p99": 45,
        "memory_percent": 42, "cpu_percent": 18, "logs": [], "scenario": None,
        "pods": [
            {"name": "checkout-svc-7d4f9b-xk2p9", "status": "Running", "cpu": "45m", "memory": "128Mi", "restarts": 0}
        ]
    },
    "inventory-svc": {
        "healthy": True, "error_rate": 0, "latency_p99": 80,
        "memory_percent": 55, "cpu_percent": 22, "logs": [], "scenario": None,
        "pods": [
            {"name": "inventory-svc-5fb59b8fc6-4wpw9", "status": "Running", "cpu": "120m", "memory": "210Mi", "restarts": 0},
            {"name": "inventory-svc-5fb59b8fc6-7wtk5", "status": "Running", "cpu": "120m", "memory": "210Mi", "restarts": 0},
            {"name": "inventory-svc-5fb59b8fc6-qqflj", "status": "Running", "cpu": "120m", "memory": "210Mi", "restarts": 0},
        ]
    },
    "db-primary": {
        "healthy": True, "error_rate": 0, "latency_p99": 12,
        "memory_percent": 60, "cpu_percent": 25, "logs": [], "scenario": None,
        "pods": [
            {"name": "db-primary-5f9a2c-pl7r4", "status": "Running", "cpu": "200m", "memory": "512Mi", "restarts": 0}
        ]
    }
}

# Preserved incident logs (kept even after recovery so chat can reference them)
INCIDENT_LOGS = {}

DEPLOYMENTS = {
    "checkout-svc": [
        {"revision": "v1.4.2", "deployed_at": (datetime.utcnow() - timedelta(hours=3)).isoformat(), "status": "active"}
    ],
    "inventory-svc": [
        {"revision": "v2.1.0", "deployed_at": (datetime.utcnow() - timedelta(hours=8)).isoformat(), "status": "active"}
    ],
    "db-primary": [
        {"revision": "postgres-14.5", "deployed_at": (datetime.utcnow() - timedelta(days=30)).isoformat(), "status": "active"}
    ]
}

_lock = threading.Lock()


def trigger_incident1():
    """Incident 1: db-primary OOM crash -> inventory-svc errors -> checkout-svc 502s."""
    with _lock:
        db_logs = [
            f"[{(datetime.utcnow()-timedelta(minutes=6)).isoformat()}] WARN  memory usage at 78% -- approaching limit",
            f"[{(datetime.utcnow()-timedelta(minutes=4)).isoformat()}] WARN  memory usage at 91% -- large query cache not evicting",
            f"[{(datetime.utcnow()-timedelta(minutes=3)).isoformat()}] ERROR memory usage at 99% -- OOM killer triggered",
            f"[{(datetime.utcnow()-timedelta(minutes=3)).isoformat()}] FATAL pod killed by OOM -- exit code 137",
            f"[{(datetime.utcnow()-timedelta(minutes=2)).isoformat()}] ERROR CrashLoopBackOff -- pod restarting but failing health check",
            f"[{datetime.utcnow().isoformat()}] ERROR pod not responding -- connection refused on port 5432",
        ]
        inv_logs = [
            f"[{(datetime.utcnow()-timedelta(minutes=3)).isoformat()}] ERROR connection refused: db-primary:5432 -- is the server running?",
            f"[{(datetime.utcnow()-timedelta(minutes=2)).isoformat()}] ERROR FATAL: could not connect to database -- connection refused",
            f"[{(datetime.utcnow()-timedelta(minutes=2)).isoformat()}] ERROR 35 failed db queries in last 60s",
            f"[{datetime.utcnow().isoformat()}] ERROR 35 errors/min, p99_latency=3000ms (timeout waiting for db)",
        ]
        co_logs = [
            f"[{(datetime.utcnow()-timedelta(minutes=2)).isoformat()}] WARN  upstream inventory-svc response time degraded (2800ms)",
            f"[{(datetime.utcnow()-timedelta(minutes=1)).isoformat()}] ERROR 502 Bad Gateway -- upstream inventory-svc timeout",
            f"[{datetime.utcnow().isoformat()}] ERROR 47 upstream timeouts in last 60s -- inventory-svc returning 500s",
        ]

        INCIDENT_LOGS["db-primary"] = list(db_logs)
        INCIDENT_LOGS["inventory-svc"] = list(inv_logs)
        INCIDENT_LOGS["checkout-svc"] = list(co_logs)

        STATE["db-primary"].update({
            "healthy": False, "error_rate": 0, "latency_p99": 0,
            "memory_percent": 99, "cpu_percent": 2,
            "scenario": "oom_crash", "logs": db_logs,
        })
        STATE["db-primary"]["pods"][0].update({
            "status": "CrashLoopBackOff", "cpu": "2m", "memory": "510Mi",
            "restarts": 3, "oom_killed": True
        })
        STATE["inventory-svc"].update({
            "healthy": False, "error_rate": 35, "latency_p99": 3000,
            "cpu_percent": 15, "scenario": "db_unreachable", "logs": inv_logs,
        })
        STATE["checkout-svc"].update({
            "healthy": False, "error_rate": 47, "latency_p99": 3200,
            "scenario": "upstream_timeout", "logs": co_logs,
        })
    print("[SCENARIO 1] Triggered: db-primary OOM crash -> cascade")


def trigger_incident2():
    """Incident 2: inventory-svc single pod CPU spike."""
    with _lock:
        bad_pod = "inventory-svc-5fb59b8fc6-4wpw9"
        logs = [
            f"[{(datetime.utcnow()-timedelta(minutes=8)).isoformat()}] WARN  CPU usage at 71% on pod {bad_pod}",
            f"[{(datetime.utcnow()-timedelta(minutes=6)).isoformat()}] WARN  CPU usage at 83% -- possible runaway goroutine",
            f"[{(datetime.utcnow()-timedelta(minutes=4)).isoformat()}] ERROR CPU usage at 94% -- request queue building up",
            f"[{(datetime.utcnow()-timedelta(minutes=2)).isoformat()}] ERROR health check failing -- pod not responding within 2s",
            f"[{datetime.utcnow().isoformat()}] ERROR 12 errors/min, latency p99=620ms",
        ]
        INCIDENT_LOGS["inventory-svc"] = list(logs)
        STATE["inventory-svc"].update({
            "healthy": False, "cpu_percent": 94, "error_rate": 12, "latency_p99": 620,
            "scenario": "cpu_spike", "logs": logs,
        })
        # Only mark the first pod as spiking — other 2 are fine
        STATE["inventory-svc"]["pods"][0].update({
            "cpu": "940m", "memory": "310Mi", "cpu_spike": True
        })
    print("[SCENARIO 2] Triggered: inventory-svc single pod CPU spike")


def trigger_incident3():
    """
    Incident 3: inventory-svc overloaded — all 3 pods overwhelmed by traffic.
    BOB recommends scaling UP to 5 replicas to handle the load.
    """
    with _lock:
        logs = [
            f"[{(datetime.utcnow()-timedelta(minutes=10)).isoformat()}] WARN  request queue depth at 120 -- above normal",
            f"[{(datetime.utcnow()-timedelta(minutes=7)).isoformat()}] WARN  p99 latency 580ms -- SLO breach imminent",
            f"[{(datetime.utcnow()-timedelta(minutes=5)).isoformat()}] ERROR p99 latency 920ms -- SLO breached (threshold: 500ms)",
            f"[{(datetime.utcnow()-timedelta(minutes=3)).isoformat()}] ERROR request queue depth at 340 -- all 3 replicas overwhelmed",
            f"[{(datetime.utcnow()-timedelta(minutes=1)).isoformat()}] ERROR 18 timeouts/min -- pods CPU at 78%, memory at 72%",
            f"[{datetime.utcnow().isoformat()}] ERROR sustained overload -- 3 replicas cannot handle current traffic volume",
        ]
        INCIDENT_LOGS["inventory-svc"] = list(logs)
        STATE["inventory-svc"].update({
            "healthy": False, "error_rate": 18, "latency_p99": 920,
            "memory_percent": 72, "cpu_percent": 78,
            "scenario": "overloaded", "logs": logs,
        })
        # All 3 pods are running but overwhelmed
        for pod in STATE["inventory-svc"]["pods"]:
            pod.update({"cpu": "780m", "memory": "290Mi"})
    print("[SCENARIO 3] Triggered: inventory-svc overloaded -- all 3 pods overwhelmed, needs scale to 5")


def simulate_scale(service, replicas, pod_names=None):
    """
    Set service to exactly `replicas` pods. Never accumulates.
    Uses real k8s pod names if supplied so get_pods matches kubectl output.
    """
    import uuid
    with _lock:
        new_pods = []
        for i in range(replicas):
            if pod_names and i < len(pod_names):
                name = pod_names[i]
            else:
                name = f"{service}-{uuid.uuid4().hex[:9]}"
            new_pods.append({
                "name": name,
                "status": "Running", "cpu": "120m", "memory": "210Mi", "restarts": 0
            })
        STATE[service]["pods"] = new_pods
        STATE[service].update({
            "healthy": True, "error_rate": 0, "latency_p99": 80,
            "cpu_percent": 28, "memory_percent": 55, "scenario": None,
            "logs": [
                f"[{datetime.utcnow().isoformat()}] INFO  scaled to {replicas} replicas",
                f"[{datetime.utcnow().isoformat()}] INFO  request queue draining -- latency normalizing",
                f"[{datetime.utcnow().isoformat()}] INFO  p99 latency back to 80ms -- SLO met",
            ]
        })
    print(f"[SIMULATE] Scaled {service} to exactly {replicas} replicas")
    return {"replicas": replicas, "pods": [p["name"] for p in new_pods]}


def simulate_pod_restart(service: str, pod_name: str) -> dict:
    """Simulate BOB restarting a pod."""
    import uuid
    new_suffix = uuid.uuid4().hex[:9]
    new_pod_name = f"{service}-{new_suffix}"
    with _lock:
        svc = STATE[service]
        old_scenario = svc.get("scenario")
        svc["pods"] = [{
            "name": new_pod_name, "status": "Running",
            "cpu": "200m" if service == "db-primary" else "120m",
            "memory": "512Mi" if service == "db-primary" else "210Mi",
            "restarts": 0, "restarted_from": pod_name
        }]
        svc.update({
            "healthy": True,
            "cpu_percent": 25 if service == "db-primary" else 22,
            "error_rate": 0,
            "latency_p99": 12 if service == "db-primary" else 80,
            "memory_percent": 60 if service == "db-primary" else 55,
            "scenario": None,
            "logs": [
                f"[{datetime.utcnow().isoformat()}] INFO  pod {pod_name} deleted by Pulse",
                f"[{datetime.utcnow().isoformat()}] INFO  new pod {new_pod_name} started",
                f"[{datetime.utcnow().isoformat()}] INFO  health check passing -- service restored",
            ]
        })
        # Cascade recovery for db-primary
        if service == "db-primary" and old_scenario == "oom_crash":
            STATE["inventory-svc"].update({
                "healthy": True, "error_rate": 0, "latency_p99": 80,
                "cpu_percent": 22, "scenario": None,
                "logs": [
                    f"[{datetime.utcnow().isoformat()}] INFO  db-primary connection restored",
                    f"[{datetime.utcnow().isoformat()}] INFO  queries succeeding -- service recovered",
                ]
            })
            STATE["checkout-svc"].update({
                "healthy": True, "error_rate": 0, "latency_p99": 45,
                "cpu_percent": 18, "scenario": None,
                "logs": [
                    f"[{datetime.utcnow().isoformat()}] INFO  upstream inventory-svc recovered",
                    f"[{datetime.utcnow().isoformat()}] INFO  502 errors cleared",
                ]
            })
            print(f"[SIMULATE] Cascade recovery: db-primary healed all upstream")
    print(f"[SIMULATE] Pod restarted: {pod_name} -> {new_pod_name}")
    return {"old_pod": pod_name, "new_pod": new_pod_name}


def recover_all():
    with _lock:
        for svc in STATE:
            STATE[svc].update({"healthy": True, "error_rate": 0, "scenario": None, "logs": []})
        STATE["checkout-svc"].update({"latency_p99": 45, "cpu_percent": 18, "memory_percent": 42})
        STATE["checkout-svc"]["pods"] = [
            {"name": "checkout-svc-7d4f9b-xk2p9", "status": "Running", "cpu": "45m", "memory": "128Mi", "restarts": 0}
        ]
        STATE["inventory-svc"].update({"latency_p99": 80, "cpu_percent": 22, "memory_percent": 55})
        # Reset back to 3 pods — clean baseline
        STATE["inventory-svc"]["pods"] = [
            {"name": "inventory-svc-5fb59b8fc6-4wpw9", "status": "Running", "cpu": "120m", "memory": "210Mi", "restarts": 0},
            {"name": "inventory-svc-5fb59b8fc6-7wtk5", "status": "Running", "cpu": "120m", "memory": "210Mi", "restarts": 0},
            {"name": "inventory-svc-5fb59b8fc6-qqflj", "status": "Running", "cpu": "120m", "memory": "210Mi", "restarts": 0},
        ]
        STATE["db-primary"].update({"latency_p99": 12, "cpu_percent": 25, "memory_percent": 60})
        STATE["db-primary"]["pods"] = [
            {"name": "db-primary-5f9a2c-pl7r4", "status": "Running", "cpu": "200m", "memory": "512Mi", "restarts": 0}
        ]
        INCIDENT_LOGS.clear()
    print("[RECOVER] All services restored to baseline (inventory-svc: 3 pods)")


# ── HTTP Handler ──────────────────────────────────────────────────────────────
SERVICE_PORTS = {"checkout-svc": 8101, "inventory-svc": 8102, "db-primary": 8103}


class ServiceHandler(BaseHTTPRequestHandler):
    service_name = None

    def log_message(self, fmt, *args):
        pass

    def send_json(self, data, status=200):
        body = json.dumps(data, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        svc = self.service_name
        with _lock:
            s = dict(STATE[svc])

        if self.path == "/health":
            self.send_json({
                "service": svc,
                "status": "healthy" if s["healthy"] else "degraded",
                "timestamp": datetime.utcnow().isoformat()
            }, 200 if s["healthy"] else 503)

        elif self.path == "/metrics":
            self.send_json({
                "service": svc,
                "error_rate_per_min": s["error_rate"],
                "latency_p99_ms": s["latency_p99"],
                "memory_percent": s["memory_percent"],
                "cpu_percent": s["cpu_percent"],
                "timestamp": datetime.utcnow().isoformat()
            })

        elif self.path == "/logs":
            logs = list(s["logs"][-20:])
            preserved = INCIDENT_LOGS.get(svc, [])
            if preserved:
                all_logs = list(preserved) + [l for l in logs if l not in preserved]
                logs = all_logs[-30:]
            self.send_json({"service": svc, "logs": logs})

        elif self.path == "/deployments":
            self.send_json({"service": svc, "deployments": DEPLOYMENTS.get(svc, [])})

        elif self.path == "/pods":
            pods = list(s["pods"])
            self.send_json({"service": svc, "pods": pods})

        else:
            self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        if self.path == "/scenario/incident1":
            trigger_incident1()
            self.send_json({"triggered": "incident1"})
        elif self.path == "/scenario/incident2":
            trigger_incident2()
            self.send_json({"triggered": "incident2"})
        elif self.path == "/scenario/incident3":
            trigger_incident3()
            self.send_json({"triggered": "incident3"})
        elif self.path == "/scenario/recover":
            recover_all()
            self.send_json({"status": "recovered"})
        elif self.path == "/pods/restart":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            pod = body.get("pod_name", STATE[self.service_name]["pods"][0]["name"])
            result = simulate_pod_restart(self.service_name, pod)
            self.send_json(result)
        elif self.path == "/scale":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            replicas = body.get("replicas", 5)
            pod_names = body.get("pod_names")  # real k8s names from mcp_server
            result = simulate_scale(self.service_name, replicas, pod_names)
            self.send_json(result)
        else:
            self.send_json({"error": "not found"}, 404)


def make_handler(name):
    class H(ServiceHandler):
        service_name = name
    return H


def run_servers():
    services = [("checkout-svc", 8101), ("inventory-svc", 8102), ("db-primary", 8103)]
    for name, port in services:
        srv = HTTPServer(("0.0.0.0", port), make_handler(name))
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        print(f"  {name}  ->  http://localhost:{port}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "incident1":
        trigger_incident1(); print("Done.")
    elif cmd == "incident2":
        trigger_incident2(); print("Done.")
    elif cmd == "incident3":
        trigger_incident3(); print("Done.")
    elif cmd == "recover":
        recover_all(); print("Done.")
    elif cmd == "status":
        for svc, s in STATE.items():
            print(f"{svc}: healthy={s['healthy']} pods={len(s['pods'])} cpu={s['cpu_percent']}% errors={s['error_rate']}/min")
    else:
        print("\n[MOCK SERVICES] Starting 3 microservices:")
        run_servers()
        print("\nBaseline: inventory-svc starts with 3 pods")
        print("Incident 3: all 3 pods overwhelmed -> BOB scales to 5")
        print()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n[MOCK SERVICES] Stopped")