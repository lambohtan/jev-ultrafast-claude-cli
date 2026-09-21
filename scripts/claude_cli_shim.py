"""OpenAI-compatible /chat/completions endpoint backed by the local `claude` CLI.

Point TEXT_MODEL_BASE_URL at this server to use a Claude Code subscription as the
text helper instead of a paid API key. Only the fields jev-ultrafast sends are
honoured: `model`, `messages` (one system + one user), and the JSON-object
response contract. Sampling, reasoning, and streaming parameters are ignored.

The CLI costs ~20s to boot, so one session is kept warm and reused. Each request
is prefixed with an isolation instruction and the session is recycled after
MAX_TURNS turns to keep earlier requests from bleeding into later ones.
"""

import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("CLAUDE_SHIM_PORT", "8899"))
MODEL = os.environ.get("CLAUDE_SHIM_MODEL", "haiku")
MAX_TURNS = int(os.environ.get("CLAUDE_SHIM_MAX_TURNS", "8"))
TIMEOUT = float(os.environ.get("CLAUDE_SHIM_TIMEOUT", "90"))

ISOLATION = (
    "New independent request. Ignore every earlier message in this conversation; "
    "they were unrelated requests. Answer only from the JSON below.\n\n"
)


class Session:
    """One warm `claude` process, guarded by a lock and recycled periodically."""

    def __init__(self, system_prompt, model=MODEL):
        self.system_prompt = system_prompt
        self.model = model
        self.lock = threading.Lock()
        self.process = None
        self.turns = 0
        self.workdir = tempfile.mkdtemp(prefix="claude-shim-")

    def spawn(self):
        self.kill()
        command = [
            "claude", "-p",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",
            "--model", self.model,
            "--no-session-persistence",
            "--strict-mcp-config",
            "--allowed-tools", "",
            "--system-prompt", self.system_prompt,
        ]
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            cwd=self.workdir,
        )
        self.turns = 0

    def kill(self):
        if self.process and self.process.poll() is None:
            try:
                self.process.stdin.close()
            except OSError:
                pass
            self.process.terminate()
        self.process = None

    def ask(self, prompt):
        with self.lock:
            if self.process is None or self.process.poll() is not None or self.turns >= MAX_TURNS:
                self.spawn()
            try:
                return self._round_trip(prompt)
            except (BrokenPipeError, OSError):
                self.spawn()
                return self._round_trip(prompt)

    def _round_trip(self, prompt):
        message = {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": prompt}]}}
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()
        self.turns += 1
        deadline = time.monotonic() + TIMEOUT
        for line in self.process.stdout:
            event = json.loads(line) if line.strip().startswith("{") else {}
            if event.get("type") == "result":
                if event.get("is_error"):
                    raise RuntimeError(event.get("result", "claude CLI reported an error"))
                return event.get("result", "")
            if time.monotonic() > deadline:
                raise TimeoutError("claude CLI did not answer in time")
        raise RuntimeError("claude CLI closed the stream without a result")


def extract_json_object(text):
    """The CLI answers in prose-friendly markdown; recover the JSON object from it."""
    fenced = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.S)
    candidate = fenced.group(1) if fenced else text
    brace = re.search(r"\{.*\}", candidate, re.S)
    if not brace:
        raise ValueError(f"no JSON object in CLI output: {text[:200]!r}")
    return json.loads(brace.group(0))


SESSION = None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("  shim: " + fmt % args + "\n")

    def _reply(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if not self.path.rstrip("/").endswith("/chat/completions"):
            return self._reply(404, {"error": {"message": f"no route for {self.path}"}})
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length) or "{}")
        messages = request.get("messages", [])
        system = next((m["content"] for m in messages if m.get("role") == "system"), "")
        user = next((m["content"] for m in messages if m.get("role") == "user"), "")

        model = request.get("model") or MODEL
        global SESSION
        if SESSION is None or (SESSION.system_prompt, SESSION.model) != (system, model):
            if SESSION is not None:
                SESSION.kill()
            SESSION = Session(system, model)

        started = time.perf_counter()
        try:
            raw = SESSION.ask(ISOLATION + user)
            value = extract_json_object(raw)
        except Exception as error:  # surfaced to jev as a failed text request
            self.log_message("failed after %.1fs: %s", time.perf_counter() - started, error)
            return self._reply(502, {"error": {"message": str(error)}})
        self.log_message("%.1fs -> %s", time.perf_counter() - started, json.dumps(value)[:80])
        self._reply(200, {
            "id": "claude-cli-shim",
            "object": "chat.completion",
            "model": SESSION.model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": json.dumps(value)},
                         "finish_reason": "stop"}],
            "usage": {},
        })


def prewarm():
    """Pay the ~20s CLI boot before the agent needs it, not during the first TYPE_TEXT."""
    global SESSION
    questions = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "jev_ultrafast", "questions.py")
    spec = importlib.util.spec_from_file_location("jev_questions", questions)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # questions.py imports nothing, so this stays cheap
    SESSION = Session(module.TEXT_VALUE)
    started = time.perf_counter()
    SESSION.ask(ISOLATION + json.dumps({"goal": "warm up", "field": {}, "page": {}, "recent_actions": []}))
    print(f"warm after {time.perf_counter() - started:.1f}s", flush=True)


def main():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"claude CLI shim on http://127.0.0.1:{PORT}/v1  (model: {MODEL})", flush=True)
    print("Set TEXT_MODEL_BASE_URL to that URL.", flush=True)
    try:
        prewarm()
    except Exception as error:
        print(f"prewarm skipped ({error}); the first request pays the boot cost instead", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        if SESSION:
            SESSION.kill()


if __name__ == "__main__":
    main()
