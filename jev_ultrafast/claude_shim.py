"""OpenAI-compatible /chat/completions endpoint backed by the local `claude` CLI.

The text helper is a runtime dependency of the agent loop: `TYPE_TEXT` cannot run
without one. So this fork owns the helper's lifecycle rather than leaving it to
whoever happens to call the agent — `ensure()` runs at the start of every `Agent`,
starts this server if `TEXT_MODEL_BASE_URL` points at a local port nobody is
listening on, and does nothing at all when the helper is a remote API.

Only the fields jev-ultrafast sends are honoured: `model`, `messages` (one system +
one user), and the JSON-object response contract. Sampling, reasoning, and streaming
parameters are ignored.

The CLI costs ~20s to boot, so one session is kept warm and reused. Each request is
prefixed with an isolation instruction and the session is recycled after MAX_TURNS
turns to keep earlier requests from bleeding into later ones. The server exits by
itself once idle, so an autostarted helper never outlives its usefulness.
"""

import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .questions import TEXT_VALUE

PORT = int(os.environ.get("CLAUDE_SHIM_PORT", "8899"))
MODEL = os.environ.get("CLAUDE_SHIM_MODEL", "haiku")
MAX_TURNS = int(os.environ.get("CLAUDE_SHIM_MAX_TURNS", "8"))
TIMEOUT = float(os.environ.get("CLAUDE_SHIM_TIMEOUT", "90"))
IDLE_EXIT = float(os.environ.get("CLAUDE_SHIM_IDLE", "900"))  # leave no process behind after 15 idle minutes
BOOT_BUDGET = float(os.environ.get("CLAUDE_SHIM_BOOT", "120"))
LOG = Path(os.environ.get("TMPDIR", "/tmp")) / "claude-cli-shim.log"

LAST_REQUEST = time.monotonic()

ISOLATION = (
    "New independent request. Ignore every earlier message in this conversation; "
    "they were unrelated requests. Answer only from the JSON below.\n\n"
)


class ShimUnavailable(RuntimeError):
    """The text helper never came up. Nothing ran, so this says nothing about the task."""


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
        global LAST_REQUEST
        LAST_REQUEST = time.monotonic()
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
    SESSION = Session(TEXT_VALUE)
    started = time.perf_counter()
    SESSION.ask(ISOLATION + json.dumps({"goal": "warm up", "field": {}, "page": {}, "recent_actions": []}))
    print(f"warm after {time.perf_counter() - started:.1f}s", flush=True)


def watch_idle():
    while True:
        time.sleep(30)
        if time.monotonic() - LAST_REQUEST > IDLE_EXIT:
            print(f"idle for {IDLE_EXIT:.0f}s — shutting down", flush=True)
            shutdown()


def shutdown(*_signal):
    if SESSION:
        SESSION.kill()
    os._exit(0)


def serve():
    """Run the server in the foreground until it is stopped or goes idle."""
    signal.signal(signal.SIGTERM, shutdown)
    # Prewarm BEFORE binding: callers treat an open port as "ready", so the port must not
    # open while the CLI is still booting.
    try:
        prewarm()
    except Exception as error:
        print(f"prewarm skipped ({error}); the first request pays the boot cost instead", flush=True)
    threading.Thread(target=watch_idle, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"READY: claude CLI shim on http://127.0.0.1:{PORT}/v1  (model: {MODEL}, "
          f"exits after {IDLE_EXIT:.0f}s idle)", flush=True)
    print("Set TEXT_MODEL_BASE_URL to that URL.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        shutdown()


# --- lifecycle, used by Agent and by the `jev-shim` command ---------------------------------


def local_target():
    """The host:port this shim would serve, or None when the text helper is somewhere else.

    An unset TEXT_MODEL_BASE_URL is a remote default (see model.field_text), not this shim,
    so it must not autostart anything.
    """
    parsed = urlparse(os.environ.get("TEXT_MODEL_BASE_URL", ""))
    if parsed.hostname not in {"127.0.0.1", "localhost"}:
        return None
    return parsed.hostname, parsed.port or 80


def listening(host, port):
    with socket.socket() as probe:
        probe.settimeout(1)
        return probe.connect_ex((host, port)) == 0


def start_background(host, port):
    """Boot the shim detached and wait for the port. It binds only after prewarming."""
    print(f"text helper not running on {host}:{port} — starting it (first boot takes ~20s)...", flush=True)
    with open(LOG, "a") as log:
        subprocess.Popen(
            [sys.executable, "-m", "jev_ultrafast.claude_shim"],
            stdout=log, stderr=log, stdin=subprocess.DEVNULL,
            start_new_session=True,  # outlives this run, so later runs skip the boot
        )
    deadline = time.monotonic() + BOOT_BUDGET
    while time.monotonic() < deadline:
        if listening(host, port):
            print("text helper ready (it exits on its own after idling)", flush=True)
            return
        time.sleep(0.5)
    raise ShimUnavailable(f"the claude CLI shim did not come up within {BOOT_BUDGET:.0f}s; see {LOG}")


def ensure():
    """Make the local text helper available, or explain why it is not. No-op for remote helpers."""
    if os.environ.get("JEV_SHIM_AUTOSTART", "1").lower() in {"0", "false", "no"}:
        return None
    target = local_target()
    if target is None or listening(*target):
        return target
    start_background(*target)
    return target


def stop():
    """Stop a running shim. Returns how many processes were signalled."""
    found = subprocess.run(["pgrep", "-f", "jev_ultrafast.claude_shim|jev-shim"],
                           capture_output=True, text=True).stdout.split()
    stopped = 0
    for pid in found:
        if int(pid) == os.getpid():  # `jev-shim stop` matches its own command line
            continue
        os.kill(int(pid), signal.SIGTERM)
        stopped += 1
    return stopped


def main(argv=None):
    command = (argv if argv is not None else sys.argv[1:])[:1] or ["start"]
    if command[0] == "stop":
        print(f"stopped {stop()} shim process(es)")
        return 0
    if command[0] == "status":
        host, port = local_target() or ("127.0.0.1", PORT)
        print(f"claude CLI shim on {host}:{port}: " + ("up" if listening(host, port) else "down"))
        return 0
    if command[0] != "start":
        print(f"usage: jev-shim [start|status|stop]  (got {command[0]!r})")
        return 2
    serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
