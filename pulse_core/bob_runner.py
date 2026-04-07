import sys, os, re, subprocess, json
from pathlib import Path

BOB_PATH = os.environ.get("BOB_PATH", "bob")
PULSE_DIR = Path(__file__).parent.parent


def run_bob(prompt: str, label: str = "BOB") -> str:
    print(f"\n{'='*60}\n[{label}] Invoking BOB\n{'='*60}")
    print(f"PROMPT (first 300 chars): {prompt[:300]}...\n")
    sys.stdout.flush()

    full_output = []
    try:
        proc = subprocess.Popen(
            [BOB_PATH, prompt],
            cwd=str(PULSE_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True, bufsize=1
        )
        for line in proc.stdout:
            print(f"[BOB] {line}", end="")
            sys.stdout.flush()
            full_output.append(line)
        proc.wait()
        result = "".join(full_output)
    except FileNotFoundError:
        print(f"[BOB RUNNER] WARNING: '{BOB_PATH}' not found -- SIMULATION MODE")
        result = _simulate(prompt, label)
        print(result)

    print(f"\n{'='*60}\n[{label}] Done\n{'='*60}\n")
    sys.stdout.flush()
    return result


def _incident_id_from_prompt(prompt):
    m = re.search(r'INCIDENT ID:\s*(\S+)', prompt)
    return m.group(1) if m else None


def _write_step(incident_id, step):
    if not incident_id: return
    try:
        from pulse_core.mcp_server import write_incident_step
        write_incident_step(incident_id, step)
    except Exception:
        pass


def _write_rca(incident_id, rca):
    if not incident_id: return
    try:
        from pulse_core.mcp_server import write_incident_rca
        write_incident_rca(incident_id, rca)
    except Exception:
        pass


def _do_fix(action, service, incident_id, pod_name=None, replicas=None):
    try:
        from pulse_core.mcp_server import run_fix
        return run_fix(action, service, incident_id, pod_name, replicas)
    except Exception as e:
        return f"Fix failed: {e}"


def append_replica_step_after_scale(incident_id: str, service: str) -> None:
    """
    After a scale fix, query mock_services for current pod names and append
    a step listing them so the engineer sees replicas without asking in chat.
    """
    import requests
    import yaml
    from pulse_core.db import get_conn

    try:
        with open(str(PULSE_DIR / "config.yaml")) as f:
            config = yaml.safe_load(f)

        port = config.get("services", {}).get(service, {}).get("port")
        if not port:
            return

        resp = requests.get(f"http://localhost:{port}/pods", timeout=5)
        pods = resp.json().get("pods", [])
        running = [p["name"] for p in pods if p.get("status") == "Running"]

        if not running:
            return

        step_text = f"{len(running)} replicas running -- {', '.join(running)}"

        conn = get_conn()
        row = conn.execute("SELECT steps FROM incidents WHERE id = ?", (incident_id,)).fetchone()
        from datetime import datetime
        steps = json.loads(row[0] or "[]") if row else []
        steps.append({"text": step_text, "ts": datetime.utcnow().isoformat()})
        conn.execute("UPDATE incidents SET steps = ? WHERE id = ?", (json.dumps(steps), incident_id))
        conn.commit()
        conn.close()
        print(f"[Pulse] Auto-appended replica step: {step_text}", flush=True)

    except Exception as e:
        print(f"[Pulse] append_replica_step failed: {e}", flush=True)


def _simulate(prompt: str, label: str) -> str:
    incident_id = _incident_id_from_prompt(prompt)

    # ── Incident 1: checkout-svc -> db-primary OOM (needs_action) ─────────
    if "checkout-svc" in prompt and ("502" in prompt or "error_rate=47" in prompt or "latency_p99=3200" in prompt):
        _write_step(incident_id, "checking checkout-svc dependencies")
        _write_step(incident_id, "inventory-svc degraded -- 35 errors/min")
        _write_step(incident_id, "tracing: inventory-svc to db-primary")
        _write_step(incident_id, "fetching logs -- last 15 min around alert")
        _write_step(incident_id, "db-primary CrashLoopBackOff -- OOM killed")
        _write_step(incident_id, "root cause confirmed: db-primary OOM crash")
        _write_rca(incident_id, {
            "status": "needs_action",
            "what": "checkout-svc 502 errors from cascading failure. db-primary crashed (OOM exit 137), inventory-svc lost DB connectivity, checkout-svc timed out.",
            "timeline": "6m ago: db-primary memory 78% -- 4m ago: 91% cache not evicting -- 3m ago: OOM kill exit 137, CrashLoopBackOff -- 2m ago: inventory-svc connection refused -- now: checkout-svc 47 errors/min",
            "root_cause": "db-primary pod exceeded memory limit (512Mi). Query cache not evicting caused unbounded memory growth. OOM killer terminated pod. All upstream services lost database access.",
            "confidence": 95,
            "action_taken": "",
            "recommended_actions": ["Restart db-primary pod to clear OOM crash"]
        })
        return "Simulation complete: db-primary OOM traced. Awaiting user approval to restart pod."

    # ── Incident 2: CPU spike -- rulebook match, auto-restart ─────────────
    elif "inventory-svc" in prompt and ("cpu" in prompt.lower() or "cpu_percent" in prompt.lower()) and "94" in prompt:
        _write_step(incident_id, "checking inventory-svc pods")
        _write_step(incident_id, "fetching logs -- last 15 min around alert")
        _write_step(incident_id, "single pod CPU spike 94% -- other 2 pods healthy")
        _write_step(incident_id, "db-primary healthy -- safe to restart")
        print(f"[SIMULATE] Running: kubectl delete pod inventory-svc-5fb59b8fc6-4wpw9 -n default", flush=True)
        _write_step(incident_id, "restarting offending pod")
        result = _do_fix("restart_pod", "inventory-svc", incident_id)
        _write_step(incident_id, "pod restarted -- service restored")
        _write_rca(incident_id, {
            "status": "resolved",
            "what": "inventory-svc single pod CPU spike at 94%. Runaway goroutine caused request queue buildup on that pod.",
            "timeline": "8m ago: CPU 71% on pod-4wpw9 -- 6m ago: 83% -- 4m ago: 94%, health checks failing -- now: pod restarted",
            "root_cause": "Single pod runaway goroutine -- not a code deploy issue, contained to one pod. Other 2 pods were healthy.",
            "confidence": 91,
            "action_taken": result,
            "recommended_actions": []
        })
        return f"Simulation complete: CPU spike fixed. {result}"

    # ── Incident 3: all 3 pods overloaded -- needs scale to 5 ─────────────
    elif "inventory-svc" in prompt and ("overload" in prompt.lower() or "latency_p99=920" in prompt or "error_rate=18" in prompt):
        _write_step(incident_id, "checking inventory-svc metrics")
        _write_step(incident_id, "fetching logs -- last 15 min around alert")
        _write_step(incident_id, "latency 920ms -- SLO breached")
        _write_step(incident_id, "all 3 replicas overwhelmed by traffic")
        _write_step(incident_id, "db-primary healthy -- not a dependency issue")
        _write_step(incident_id, "recommend: scale from 3 to 5 replicas")
        _write_rca(incident_id, {
            "status": "needs_action",
            "what": "inventory-svc latency at 920ms (SLO: 500ms). All 3 replicas overwhelmed by traffic. 18 timeouts/min, request queue depth 340.",
            "timeline": "10m ago: queue depth 120 -- 7m ago: latency 580ms -- 5m ago: SLO breached 920ms -- 3m ago: all 3 pods at 78% CPU -- now: sustained overload",
            "root_cause": "All 3 inventory-svc replicas running at 78% CPU and 72% memory. Traffic growth outpaced current capacity. Not a code bug -- need more replicas.",
            "confidence": 90,
            "action_taken": "",
            "recommended_actions": ["Scale inventory-svc from 3 to 5 replicas to handle traffic load"]
        })
        return "Simulation complete: overload diagnosed on all 3 pods, awaiting user approval to scale to 5."

    # ── Execute approved fix ───────────────────────────────────────────────
    elif "approved" in prompt.lower() and "execute" in prompt.lower():
        service = "inventory-svc"
        if "db-primary" in prompt:
            service = "db-primary"
        elif "checkout-svc" in prompt:
            service = "checkout-svc"

        _write_step(incident_id, "user approved -- executing fix")

        if "scale" in prompt.lower() or "replica" in prompt.lower():
            print(f"[SIMULATE] Running: kubectl scale deployment/{service} --replicas=5 -n default", flush=True)
            _write_step(incident_id, "scaling from 3 to 5 replicas")
            result = _do_fix("scale_replicas", service, incident_id, replicas=5)
            _write_step(incident_id, "scaled to 5 replicas -- verifying metrics")
            _write_step(incident_id, "latency normalizing -- SLO met")
            _write_rca(incident_id, {
                "status": "resolved",
                "what": "inventory-svc latency at 920ms (SLO: 500ms). All 3 replicas overwhelmed by traffic.",
                "timeline": "10m ago: queue depth 120 -- SLO breached -- now: scaled to 5 replicas, latency 80ms",
                "root_cause": "All 3 replicas at capacity. Traffic growth outpaced current replica count.",
                "confidence": 90,
                "action_taken": result,
                "recommended_actions": []
            })
            return f"Fix executed: scaled {service} to 5 replicas. {result}"

        elif "restart" in prompt.lower() or "oom" in prompt.lower() or "db-primary" in prompt:
            print(f"[SIMULATE] Running: kubectl delete pod db-primary-5f9a2c-pl7r4 -n default", flush=True)
            _write_step(incident_id, "restarting db-primary pod")
            result = _do_fix("restart_pod", "db-primary", incident_id)
            _write_step(incident_id, "db-primary restarted -- verifying")
            _write_step(incident_id, "cascade recovery -- all upstream services restored")
            _write_rca(incident_id, {
                "status": "resolved",
                "what": "checkout-svc 502 errors from cascading failure. db-primary crashed from OOM (exit 137).",
                "timeline": "6m ago: db-primary memory 78% -- OOM kill -- CrashLoopBackOff -- now: pod restarted, cascade recovered",
                "root_cause": "db-primary pod exceeded memory limit (512Mi). OOM killer terminated pod.",
                "confidence": 95,
                "action_taken": result,
                "recommended_actions": []
            })
            return f"Fix executed: restarted db-primary. {result}"

        else:
            _write_step(incident_id, "fix executed")
            return "Fix executed."

    # ── Chat / infra question ──────────────────────────────────────────────
    elif any(w in prompt.lower() for w in ["how many", "pods", "running", "cluster", "deployment", "status"]):
        try:
            from pulse_core.mcp_server import run_kubectl
            pods = run_kubectl("get pods")
            return f"Current Pulse service pods:\n{pods}"
        except Exception:
            return "3 services: checkout-svc, inventory-svc, db-primary."

    else:
        return "Analysis complete based on available context."


def answer_question_with_bob(question: str, incident_context: str = "", chat_history: str = "") -> str:
    ctx_section = f"\nCURRENT INCIDENT CONTEXT:\n{incident_context}\n" if incident_context else ""
    history_section = f"\nRECENT CHAT HISTORY:\n{chat_history}\n" if chat_history else ""

    live_keywords = ["current", "now", "right now", "latest", "live", "show me", "what are",
                     "logs", "metrics", "pods", "status", "errors", "latency", "cpu", "memory"]
    wants_live = any(w in question.lower() for w in live_keywords)
    live_instruction = ""
    if wants_live:
        live_instruction = """
- If asked for current metrics: call get_metrics and report exact numbers.
- If asked for logs or failure logs: call get_logs and quote actual log lines verbatim in a code block.
- If asked about pods: call get_pods and list them.
- Do NOT answer from memory for current-state questions -- always fetch live data.
"""
    voice_instruction = ""
    if "voice" in question.lower() or "speak" in question.lower() or "say it" in question.lower():
        voice_instruction = """
    - After answering, call elevenlabs_speak with your complete answer text so it is spoken aloud.
    """

    prompt = f"""You are Pulse, an AI on-call assistant.{ctx_section}{history_section}
QUESTION: {question}
{voice_instruction}
RESPONSE RULES:
- Answer directly in 1-3 sentences. No preamble, no narration.
- Do NOT restate the question. Just answer.
- If the answer is in the incident context or chat history, answer from it directly.
- If asked about all services, use get_metrics on all 3: checkout-svc, inventory-svc, db-primary.
- If asked how many pods, use get_pods -- count only Pulse service pods in default namespace.{live_instruction}
- Do NOT assume information. If unsure, say so.
- Never explain your reasoning. Just give the answer.
"""
    return run_bob(prompt, label="CHAT")


def execute_approved_fix(incident_id: str, action_description: str, service: str) -> str:
    """Called by API when user approves a recommended action. Invokes BOB to execute."""
    prompt = f"""You are Pulse. The engineer approved a fix. Execute it now.

INCIDENT ID: {incident_id}
Service: {service}
Approved action: {action_description}

Steps:
1. write_incident_step(incident_id, "user approved -- executing fix")
2. Execute the fix using run_fix with the correct action and replica count from the approved action
3. Verify by checking get_metrics and get_pods after execution
4. write_incident_step with the result
5. write_incident_rca updating status to "resolved" with action_taken set to the command you ran

Be concise. Log exactly what command you ran.
"""
    return run_bob(prompt, label=f"FIX:{incident_id[:8]}")