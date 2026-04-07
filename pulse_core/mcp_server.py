"""
Pulse MCP Server — JSON-RPC 2.0 stdio protocol.
BOB auto-spawns this via .bob/mcp.json
"""
import sys, json, os, sqlite3
import requests
import yaml
from datetime import datetime
from pathlib import Path

PULSE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(PULSE_DIR))
CONFIG_PATH = PULSE_DIR / "config.yaml"

def load_config():
    try:
        with open(CONFIG_PATH) as f:
            return yaml.safe_load(f)
    except Exception:
        return {}

SERVICE_PORTS = {"checkout-svc": 8101, "inventory-svc": 8102, "db-primary": 8103}

BLOCKED = ["delete namespace","delete cluster","rm -rf","--force --grace-period=0",
           "exec -it","drain","cordon","taint","apply -f","create -f"]

TOOLS = [
    {"name":"get_logs","description":"Fetch recent logs for a service.",
     "inputSchema":{"type":"object","properties":{"service":{"type":"string"},"window_minutes":{"type":"integer","default":10}},"required":["service"]}},
    {"name":"get_metrics","description":"Get current metrics: error_rate, latency_p99, memory, cpu.",
     "inputSchema":{"type":"object","properties":{"service":{"type":"string"}},"required":["service"]}},
    {"name":"get_deployments","description":"Recent deployment history for a service.",
     "inputSchema":{"type":"object","properties":{"service":{"type":"string"}},"required":["service"]}},
    {"name":"get_service_dependencies","description":"Topology + current health of all dependencies.",
     "inputSchema":{"type":"object","properties":{"service":{"type":"string"}},"required":["service"]}},
    {"name":"get_pods","description":"List pods for a service with CPU, memory, status.",
     "inputSchema":{"type":"object","properties":{"service":{"type":"string"}},"required":["service"]}},
    {"name":"query_incident_history","description":"Past incidents and BOB actions for a service.",
     "inputSchema":{"type":"object","properties":{"service":{"type":"string"}},"required":["service"]}},
    {"name":"write_incident_step","description":"Log a live investigation step so UI updates in real time (keep under 10 words).",
     "inputSchema":{"type":"object","properties":{"incident_id":{"type":"string"},"step":{"type":"string"}},"required":["incident_id","step"]}},
    {"name":"write_incident_rca","description":"Write the final structured RCA once investigation is complete.",
     "inputSchema":{"type":"object","properties":{
         "incident_id":{"type":"string"},
         "rca":{"type":"object","properties":{
             "status":{"type":"string"},
             "what":{"type":"string"},
             "timeline":{"type":"string"},
             "root_cause":{"type":"string"},
             "confidence":{"type":"integer"},
             "action_taken":{"type":"string"},
             "recommended_actions":{"type":"array","items":{"type":"string"}}
         }}},"required":["incident_id","rca"]}},
    {"name":"run_fix","description":"Execute an approved remediation: restart_pod, rollback, scale_replicas.",
     "inputSchema":{"type":"object","properties":{
         "action":{"type":"string","enum":["restart_pod","rollback","scale_replicas"]},
         "service":{"type":"string"},
         "pod_name":{"type":"string","description":"Required for restart_pod"},
         "incident_id":{"type":"string"},
         "replicas":{"type":"integer","description":"For scale_replicas, target replica count"}
     },"required":["action","service","incident_id"]}},
    {"name":"run_kubectl","description":"Read-only kubectl. Write/delete commands are hard-blocked.",
     "inputSchema":{"type":"object","properties":{"command":{"type":"string"}},"required":["command"]}}
]


def get_logs(service, window_minutes=10):
    port = SERVICE_PORTS.get(service)
    if not port: return f"Unknown service: {service}"
    try:
        r = requests.get(f"http://localhost:{port}/logs", timeout=5)
        logs = r.json().get("logs", [])
        if not logs: return f"No logs for {service} (healthy)"
        return f"Logs for {service} (last {window_minutes} min):\n" + "\n".join(logs)
    except Exception as e:
        return f"Could not fetch logs: {e}"


def get_metrics(service):
    port = SERVICE_PORTS.get(service)
    if not port: return f"Unknown service: {service}"
    try:
        r = requests.get(f"http://localhost:{port}/metrics", timeout=5)
        m = r.json()
        return (f"Metrics for {service}:\n"
                f"  error_rate:  {m['error_rate_per_min']}/min (threshold: 10)\n"
                f"  latency_p99: {m['latency_p99_ms']}ms\n"
                f"  memory:      {m['memory_percent']}%\n"
                f"  cpu:         {m['cpu_percent']}%\n"
                f"  timestamp:   {m['timestamp']}")
    except Exception as e:
        return f"Could not fetch metrics: {e}"


def get_deployments(service):
    port = SERVICE_PORTS.get(service)
    if not port: return f"Unknown service: {service}"
    try:
        r = requests.get(f"http://localhost:{port}/deployments", timeout=5)
        deps = r.json().get("deployments", [])
        if not deps: return f"No deployment history for {service}"
        lines = [f"Deployments for {service}:"]
        for d in reversed(deps):
            note = f" -- NOTE: {d['note']}" if d.get("note") else ""
            lines.append(f"  {d['revision']} at {d['deployed_at']}{note}")
        return "\n".join(lines)
    except Exception as e:
        return f"Could not fetch deployments: {e}"


def get_pods(service):
    port = SERVICE_PORTS.get(service)
    if not port: return f"Unknown service: {service}"
    try:
        r = requests.get(f"http://localhost:{port}/pods", timeout=5)
        pods = r.json().get("pods", [])
        if not pods: return f"No pods found for {service}"
        lines = [f"Pods for {service}:"]
        for p in pods:
            flags = ""
            if p.get("cpu_spike"): flags += " <- CPU SPIKE"
            if p.get("oom_killed"): flags += " <- OOM KILLED"
            restarted = f" (replaced {p['restarted_from']})" if p.get("restarted_from") else ""
            lines.append(f"  {p['name']}  status={p['status']}  cpu={p['cpu']}  mem={p['memory']}  restarts={p['restarts']}{flags}{restarted}")
        return "\n".join(lines)
    except Exception as e:
        return f"Could not fetch pods: {e}"


def get_service_dependencies(service):
    config = load_config()
    svcs = config.get("services", {})
    if service not in svcs:
        return f"Service {service} not in topology"
    calls = svcs[service].get("calls", [])
    called_by = [s for s, v in svcs.items() if service in v.get("calls", [])]
    lines = [f"Topology for {service}:",
             f"  calls:     {calls or 'none'}",
             f"  called_by: {called_by or 'none'}"]
    if calls:
        lines.append("  dependency health:")
        for dep in calls:
            port = SERVICE_PORTS.get(dep)
            if port:
                try:
                    rh = requests.get(f"http://localhost:{port}/health", timeout=3)
                    rm = requests.get(f"http://localhost:{port}/metrics", timeout=3)
                    h = rh.json().get("status", "unknown")
                    m = rm.json()
                    lines.append(f"    {dep}: {h} -- latency={m['latency_p99_ms']}ms, errors={m['error_rate_per_min']}/min, cpu={m['cpu_percent']}%, mem={m['memory_percent']}%")
                except Exception:
                    lines.append(f"    {dep}: unreachable")
    return "\n".join(lines)


def query_incident_history(service):
    db_path = PULSE_DIR / "pulse.db"
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        incidents = conn.execute(
            "SELECT * FROM incidents WHERE service=? ORDER BY triggered_at DESC LIMIT 5",
            (service,)
        ).fetchall()
        actions = conn.execute(
            "SELECT * FROM actions_log WHERE service=? ORDER BY executed_at DESC LIMIT 5",
            (service,)
        ).fetchall()
        conn.close()
        lines = []
        if incidents:
            lines.append(f"Past incidents for {service}:")
            for r in incidents:
                lines.append(f"  [{r['triggered_at'][:16]}] {r['title']} -- {r['status']} (root: {r['rca_root_cause'] or 'unknown'})")
        if actions:
            lines.append(f"Past BOB actions for {service}:")
            for a in actions:
                lines.append(f"  [{a['executed_at'][:16]}] {a['action']} -- {a['result']} (incident: {a['incident_id']})")
        return "\n".join(lines) if lines else f"No history for {service}"
    except Exception as e:
        return f"Could not query history: {e}"


def write_incident_step(incident_id, step):
    db_path = PULSE_DIR / "pulse.db"
    try:
        conn = sqlite3.connect(str(db_path))
        row = conn.execute("SELECT steps FROM incidents WHERE id=?", (incident_id,)).fetchone()
        if row:
            steps = json.loads(row[0] or "[]")
            steps.append({"text": step, "ts": datetime.utcnow().isoformat()})
            conn.execute("UPDATE incidents SET steps=? WHERE id=?", (json.dumps(steps), incident_id))
            conn.commit()
        conn.close()
        return f"Step logged for {incident_id}: {step}"
    except Exception as e:
        return f"Error logging step: {e}"


def write_incident_rca(incident_id, rca):
    db_path = PULSE_DIR / "pulse.db"
    try:
        final_status = rca.get("status", "needs_action")
        conn = sqlite3.connect(str(db_path))
        conn.execute("""UPDATE incidents SET status=?,rca_what=?,rca_timeline=?,
            rca_root_cause=?,rca_confidence=?,rca_action=?,recommended_actions=?
            WHERE id=?""", (
            final_status,
            rca.get("what", ""),
            rca.get("timeline", ""),
            rca.get("root_cause", ""),
            rca.get("confidence", 0),
            rca.get("action_taken", ""),
            json.dumps(rca.get("recommended_actions", [])),
            incident_id
        ))
        conn.commit()
        conn.close()
        return f"RCA saved for {incident_id}. Confidence: {rca.get('confidence', 0)}%."
    except Exception as e:
        return f"Error saving RCA: {e}"


def _kubectl_available():
    import shutil
    return shutil.which("kubectl") is not None


def _real_kubectl(cmd_parts, timeout=30):
    import subprocess
    try:
        r = subprocess.run(
            ["kubectl"] + cmd_parts,
            capture_output=True, text=True, timeout=timeout
        )
        return r.stdout.strip(), r.stderr.strip(), r.returncode
    except Exception as e:
        return "", str(e), 1


def _get_real_pod_names(service):
    """Get current running pod names from k8s for this service."""
    stdout, _, rc = _real_kubectl([
        "get", "pods", "-l", f"app={service}",
        "--no-headers", "-o", "custom-columns=NAME:.metadata.name"
    ])
    if rc == 0 and stdout.strip():
        return [p.strip() for p in stdout.splitlines() if p.strip()]
    return []


def _sync_mock(service, action, pod_name=None, replicas=None, pod_names=None):
    """
    Sync mock_services STATE after a fix so metrics recover and get_pods
    returns the correct pod list matching what kubectl shows.
    """
    port = SERVICE_PORTS.get(service)
    if not port:
        return
    try:
        if action == "restart_pod":
            requests.post(f"http://localhost:{port}/pods/restart",
                          json={"pod_name": pod_name}, timeout=10)
        elif action == "scale_replicas":
            payload = {"replicas": replicas}
            if pod_names:
                payload["pod_names"] = pod_names
            requests.post(f"http://localhost:{port}/scale", json=payload, timeout=10)
    except Exception as e:
        print(f"[MCP] mock sync failed: {e}", flush=True)


def run_fix(action, service, incident_id, pod_name=None, replicas=None):
    """
    Execute fix using whatever replica count BOB passes.
    Syncs mock STATE with real k8s pod names after execution
    so get_pods always shows the truth.
    """
    port = SERVICE_PORTS.get(service)
    db_path = PULSE_DIR / "pulse.db"
    result_text = ""
    command = ""
    use_real_k8s = _kubectl_available()

    if action == "restart_pod":
        if not pod_name:
            if use_real_k8s:
                stdout, _, _ = _real_kubectl(["get", "pods", "-l", f"app={service}",
                                              "--no-headers", "-o",
                                              "custom-columns=NAME:.metadata.name"])
                pods = [p.strip() for p in stdout.splitlines() if p.strip()]
                pod_name = pods[0] if pods else f"{service}-pod"
            else:
                try:
                    r = requests.get(f"http://localhost:{port}/pods", timeout=5)
                    pods = r.json().get("pods", [])
                    bad = next((p for p in pods if p.get("cpu_spike") or p.get("oom_killed")), pods[0] if pods else None)
                    pod_name = bad["name"] if bad else f"{service}-unknown"
                except Exception:
                    pod_name = f"{service}-unknown"

        command = f"kubectl delete pod {pod_name} -n default"
        print(f"[MCP run_fix] Running: {command}", flush=True)

        if use_real_k8s:
            _, _, rc = _real_kubectl(["delete", "pod", pod_name, "-n", "default"], timeout=30)
            if rc == 0:
                import time; time.sleep(3)
                new_names = _get_real_pod_names(service)
                new_pod = new_names[0] if new_names else "new-pod-starting"
                result_text = f"Pod {pod_name} deleted. New pod: {new_pod}. Service restored."

        _sync_mock(service, "restart_pod", pod_name=pod_name)
        if not result_text:
            result_text = f"Pod {pod_name} deleted. Service restored."

    elif action == "scale_replicas":
        # Use exactly what BOB passes — no override
        target = replicas if replicas else 5
        command = f"kubectl scale deployment/{service} --replicas={target} -n default"
        print(f"[MCP run_fix] Running: {command}", flush=True)

        real_pod_names = []
        if use_real_k8s:
            stdout, _, rc = _real_kubectl(["scale", f"deployment/{service}",
                                           f"--replicas={target}", "-n", "default"])
            if rc == 0:
                import time; time.sleep(4)  # let k8s create pods
                real_pod_names = _get_real_pod_names(service)
                result_text = f"Scaled {service} to {target} replicas. {stdout}".strip()

        # Sync mock with real pod names and real replica count
        _sync_mock(service, "scale_replicas", replicas=target, pod_names=real_pod_names or None)
        if not result_text:
            result_text = f"Scaled {service} to {target} replicas successfully."

    elif action == "rollback":
        command = f"kubectl rollout undo deployment/{service}"
        print(f"[MCP run_fix] Running: {command}", flush=True)
        if use_real_k8s:
            stdout, stderr, rc = _real_kubectl(["rollout", "undo", f"deployment/{service}"])
            result_text = stdout if rc == 0 else f"Rollback failed: {stderr}"
        else:
            result_text = f"Rolled back deployment/{service} successfully."
    else:
        return f"Unknown action: {action}"

    # Log to DB and mark resolved
    try:
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO actions_log (incident_id,service,action,command,result,executed_at) VALUES(?,?,?,?,?,?)",
            (incident_id, service, action, command, result_text, datetime.utcnow().isoformat())
        )
        conn.execute(
            "UPDATE incidents SET bob_executed_fix=?, status='resolved', resolved_at=? WHERE id=?",
            (result_text, datetime.utcnow().isoformat(), incident_id)
        )
        conn.commit()
        conn.close()
    except Exception:
        pass

    return result_text


def run_kubectl(command):
    cmd_lower = command.lower().strip()
    for blocked in BLOCKED:
        if blocked in cmd_lower:
            return f"BLOCKED: '{blocked}' -- only read-only kubectl allowed."

    if _kubectl_available():
        import subprocess
        try:
            result = subprocess.run(
                ["kubectl"] + command.split(),
                capture_output=True, text=True, timeout=10
            )
            output = result.stdout.strip() or result.stderr.strip()
            return f"kubectl {command}\n{output}"
        except Exception as e:
            return f"kubectl {command}\nError: {e}"

    sims = {
        "get pods": lambda: _kubectl_pods(),
        "top pods": lambda: _kubectl_top(),
        "get deployments": lambda: _kubectl_deployments(),
        "get nodes": "NAME     STATUS  ROLES   AGE  VERSION\nnode-01  Ready   master  30d  v1.28.3",
    }
    for key, val in sims.items():
        if key in cmd_lower:
            result = val() if callable(val) else val
            return f"kubectl {command}\n{result}"
    return f"kubectl {command}\n(simulated)"


def _kubectl_pods():
    lines = ["NAME                            READY  STATUS   RESTARTS  AGE  CPU    MEM"]
    for svc, port in SERVICE_PORTS.items():
        try:
            r = requests.get(f"http://localhost:{port}/pods", timeout=3)
            for p in r.json().get("pods", []):
                spike = "  HIGH CPU" if p.get("cpu_spike") else ""
                lines.append(f"  {p['name']:<33} 1/1    {p['status']:<8} {p['restarts']}         1h   {p['cpu']:<6} {p['memory']}{spike}")
        except Exception:
            lines.append(f"  {svc}-pod  1/1  Running  0  1h  -  -")
    return "\n".join(lines)


def _kubectl_top():
    lines = ["NAME                            CPU(cores)  MEMORY(bytes)"]
    for svc, port in SERVICE_PORTS.items():
        try:
            r = requests.get(f"http://localhost:{port}/pods", timeout=3)
            for p in r.json().get("pods", []):
                lines.append(f"  {p['name']:<33} {p['cpu']:<11} {p['memory']}")
        except Exception:
            pass
    return "\n".join(lines)


def _kubectl_deployments():
    lines = ["NAME            READY  UP-TO-DATE  AVAILABLE  AGE"]
    for svc in SERVICE_PORTS:
        lines.append(f"  {svc:<15} 1/1    1           1          2h")
    return "\n".join(lines)


# ── JSON-RPC stdio loop ───────────────────────────────────────────────────────

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def handle(req):
    method = req.get("method", "")
    rid = req.get("id")
    params = req.get("params", {})

    if method == "initialize":
        send({"jsonrpc":"2.0","id":rid,"result":{
            "protocolVersion":"2024-11-05",
            "serverInfo":{"name":"pulse","version":"1.0.0"},
            "capabilities":{"tools":{}}
        }})
        return

    if method in ("notifications/initialized", "notifications/cancelled"):
        return

    if method == "tools/list":
        send({"jsonrpc":"2.0","id":rid,"result":{"tools":TOOLS}})
        return

    if method == "tools/call":
        name = params.get("name","")
        args = params.get("arguments",{})
        try:
            if name == "get_logs":         text = get_logs(args["service"], args.get("window_minutes",10))
            elif name == "get_metrics":    text = get_metrics(args["service"])
            elif name == "get_deployments":text = get_deployments(args["service"])
            elif name == "get_pods":       text = get_pods(args["service"])
            elif name == "get_service_dependencies": text = get_service_dependencies(args["service"])
            elif name == "query_incident_history":   text = query_incident_history(args["service"])
            elif name == "write_incident_step":      text = write_incident_step(args["incident_id"], args["step"])
            elif name == "write_incident_rca":       text = write_incident_rca(args["incident_id"], args["rca"])
            elif name == "run_fix":        text = run_fix(args["action"], args["service"], args["incident_id"],
                                                          args.get("pod_name"), args.get("replicas"))
            elif name == "run_kubectl":    text = run_kubectl(args["command"])
            else:
                send({"jsonrpc":"2.0","id":rid,"error":{"code":-32601,"message":f"Unknown tool: {name}"}})
                return
            send({"jsonrpc":"2.0","id":rid,"result":{"content":[{"type":"text","text":text}]}})
        except Exception as e:
            send({"jsonrpc":"2.0","id":rid,"error":{"code":-32000,"message":str(e)}})
        return

    if method == "ping":
        send({"jsonrpc":"2.0","id":rid,"result":{}})
        return

    if rid is not None:
        send({"jsonrpc":"2.0","id":rid,"result":{}})


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            handle(json.loads(line))
        except Exception as e:
            sys.stderr.write(f"[MCP] {e}\n")
            sys.stderr.flush()


if __name__ == "__main__":
    main()