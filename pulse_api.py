"""
pulse_api.py — Pulse API. Pure stdlib, no pip needed beyond requests+pyyaml.
Run: python3 pulse_api.py
"""
import sys, json, threading, re
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

PULSE_DIR = Path(__file__).parent
sys.path.insert(0, str(PULSE_DIR))

from pulse_core.db import (
    init_db, upsert_incident, get_incident, get_all_incidents,
    add_chat_message, get_chat_messages
)
from pulse_core.bob_runner import answer_question_with_bob, execute_approved_fix, append_replica_step_after_scale
from pulse_watcher import incident_queue, FIXING_SERVICES

init_db()


def clean_bob_response(raw: str) -> str:
    """Strip BOB shell artifacts. Use attempt_completion output as canonical answer."""
    text = re.sub(r"<thinking>.*?</thinking>", "", raw, flags=re.DOTALL)

    completion_match = re.search(
        r"\[using tool attempt_completion:.*?\]\s*---output---\s*(.*?)\s*---output---",
        text, flags=re.DOTALL
    )
    if completion_match:
        text = completion_match.group(1).strip()
        if text:
            return re.sub(r"\n{3,}", "\n\n", text)

    lines = text.split("\n")
    cleaned = []
    skip_until_output_end = False

    for line in lines:
        s = line.strip()
        if not s: continue
        if s.startswith("[BOB]"):
            s = s[5:].strip()
        if not s: continue
        if s.startswith("[using tool "):
            skip_until_output_end = True
            continue
        if s.startswith("---output---"):
            if skip_until_output_end:
                skip_until_output_end = False
            continue
        if skip_until_output_end: continue
        if s.startswith("---"): continue
        if any(s.startswith(p) for p in [
            "===", "PROMPT", "Cost:", "[CHAT]", "[INVESTIGATE", "[SIMULATE",
            "[BOB RUNNER]", "[RULEBOOK", "[FIX:", "[ERROR]",
            "<thinking>", "</thinking>", "Error parsing JSON",
        ]):
            continue
        if s.startswith('{"') or s.startswith('"incident_id"'): continue
        if re.match(r"^(The user |Looking at |I should |I need to |Let me check|This is a |However,? the kubectl|The incident)", s):
            continue
        cleaned.append(s)

    text = "\n".join(cleaned).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text if text else "Investigation complete."


STATIC_DIR = PULSE_DIR / "static"


class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        if args and str(args[1]) not in ("200", "304"):
            super().log_message(fmt, *args)

    def send_json(self, data, status=200):
        body = json.dumps(data, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, html):
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_body(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n)) if n else {}

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        parts = [p for p in path.split("/") if p]

        if path in ("/", "/index.html"):
            self.send_html((STATIC_DIR / "index.html").read_text())

        elif path == "/api/incidents":
            self.send_json(get_all_incidents())

        elif len(parts) == 3 and parts[0] == "api" and parts[1] == "incidents":
            inc = get_incident(parts[2])
            if not inc:
                self.send_json({"error": "not found"}, 404)
                return
            inc["steps"] = json.loads(inc.get("steps") or "[]")
            inc["recommended_actions"] = json.loads(inc.get("recommended_actions") or "[]")
            inc["chat"] = get_chat_messages(parts[2])
            self.send_json(inc)

        else:
            self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        parts = [p for p in path.split("/") if p]
        body = self.read_body()

        # ── PagerDuty V3 webhook ───────────────────────────────────────────
        if path == "/webhook/pagerduty":
            event = body.get("event", {})
            if event.get("event_type") != "incident.triggered":
                self.send_json({"status": "ignored", "event_type": event.get("event_type")})
                return
            data = event.get("data", {})
            svc = data.get("service", {}).get("summary", "unknown-svc")
            title = data.get("title", f"PagerDuty alert: {svc}")
            incident_queue.put({
                "service": svc, "title": title, "severity": "critical",
                "anomalies": [f"pagerduty: {title}"],
                "source": "pagerduty", "metrics": {}
            })
            self.send_json({"status": "queued", "service": svc, "title": title})

        # ── Demo trigger ───────────────────────────────────────────────────
        elif path == "/webhook/trigger":
            svc = body.get("service", "unknown")
            title = body.get("title", f"Incident on {svc}")
            incident_queue.put({
                "service": svc, "title": title,
                "severity": body.get("severity", "critical"),
                "anomalies": body.get("anomalies", [title]),
                "source": body.get("source", "manual"),
                "metrics": body.get("metrics", {})
            })
            self.send_json({"status": "queued", "service": svc})

        # ── Chat ───────────────────────────────────────────────────────────
        elif len(parts) == 4 and parts[1] == "incidents" and parts[3] == "chat":
            incident_id = parts[2]
            inc = get_incident(incident_id)
            if not inc:
                self.send_json({"error": "not found"}, 404)
                return
            msg = body.get("message", "").strip()
            if not msg:
                self.send_json({"error": "empty message"}, 400)
                return

            add_chat_message(incident_id, "user", msg)

            ctx_lines = [
                f"Incident: {inc['title']}",
                f"Service: {inc['service']}",
                f"Status: {inc['status']}",
            ]
            for f, label in [
                ("rca_what", "What happened"), ("rca_root_cause", "Root cause"),
                ("rca_timeline", "Timeline"), ("rca_confidence", "Confidence"),
                ("rca_action", "Action taken"), ("bob_executed_fix", "Fix executed"),
            ]:
                if inc.get(f):
                    ctx_lines.append(f"{label}: {inc[f]}")
            context = "\n".join(ctx_lines)

            all_msgs = get_chat_messages(incident_id)
            recent = all_msgs[-6:] if len(all_msgs) > 6 else all_msgs[:]
            history_lines = []
            for m in recent:
                if m["content"] == msg and m["role"] == "user":
                    continue
                role_label = "Engineer" if m["role"] == "user" else "Pulse"
                history_lines.append(f"{role_label}: {m['content'][:200]}")
            chat_history = "\n".join(history_lines)

            result = {"messages": []}

            def run_chat():
                answer = answer_question_with_bob(msg, context, chat_history)
                clean = clean_bob_response(answer)
                add_chat_message(incident_id, "bob", clean)
                result["messages"] = get_chat_messages(incident_id)

            t = threading.Thread(target=run_chat, daemon=True)
            t.start()
            t.join(timeout=90)
            self.send_json({"messages": result.get("messages") or get_chat_messages(incident_id)})

        # ── Approve action → BOB executes fix in watcher terminal ──────────
        elif len(parts) == 4 and parts[1] == "incidents" and parts[3] == "action":
            incident_id = parts[2]
            inc = get_incident(incident_id)
            if not inc:
                self.send_json({"error": "not found"}, 404)
                return
            action = body.get("action", "")
            approved = body.get("approved", False)

            if approved and action:
                service = inc.get("service", "unknown")
                is_scale = "scale" in action.lower() or "replica" in action.lower()

                def run_fix_async():
                    FIXING_SERVICES.add(service)
                    try:
                        # Pass action straight through — no normalization.
                        # run_fix uses whatever replica count BOB extracts from the prompt.
                        execute_approved_fix(incident_id, action, service)
                        if is_scale:
                            append_replica_step_after_scale(incident_id, service)
                    finally:
                        FIXING_SERVICES.discard(service)

                t = threading.Thread(target=run_fix_async, daemon=True)
                t.start()
                self.send_json({"status": "executing", "incident_id": incident_id})
            else:
                self.send_json({"status": "rejected"})

        else:
            self.send_json({"error": "not found"}, 404)


def main():
    server = HTTPServer(("0.0.0.0", 8000), Handler)
    print("\n[PULSE API] http://localhost:8000\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[PULSE API] Stopped")


if __name__ == "__main__":
    main()