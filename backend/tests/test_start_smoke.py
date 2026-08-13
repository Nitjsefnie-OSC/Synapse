"""Issue #3 — `./start.sh smoke` safety-refusal contract, plus the fix-loop regressions for the
independent adversary's findings (F1/F2/F3/F4 on candidate 855c2e709c62accce9915a05952d5b06e403c901).

The two live smokes (real Anthropic distill, real gpt-image-1 render) spend real money. This
suite proves the invariants that must hold no matter how the happy path is implemented:

  I1 keyless environment  -> actionable exit 1 (distinct from the unknown-command exit 2),
                              naming the missing key(s).
  I2 any CI environment   -> refuses unconditionally, even with (fake) keys present.
  F2 confirmation ordering-> keys present + closed stdin + no SYNAPSE_SMOKE_YES -> ZERO POSTs.
  F1 the "estimate" call  -> the confirmed run makes exactly ONE non-spending dry-run distill
                              call and exactly ONE paid distill call (never two paid calls).
  F3 the .env loader      -> an indented `# ...` comment and a non-identifier `KEY-WITH-DASH=`
                              line never crash the command (mirrors config.py's tolerant parse).
  F4 provider failures    -> a render failure after a successful distill records a FAILED
                              section (with the safe response body, no secrets) and exits a
                              documented, non-default code — never a bare curl exit status.

It never supplies real keys and never lets the refusal paths reach a network call: every
subprocess gets a from-scratch env dict (never `os.environ.copy()`), `SYNAPSE_ENV_FILE` is
pointed at a scratch path, and stdin is always closed (`subprocess.DEVNULL`) so a stray real TTY
can never flip a refusal test into a hang or an accidental confirmation.

The F1/F2/F4 tests talk to a SAFE, LOCAL, in-process stub HTTP server (127.0.0.1, ephemeral
port) standing in for the backend — no real network, no provider, no spend, ever.
"""

from __future__ import annotations

import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
START_SH = REPO_ROOT / "start.sh"

FAKE_ANTHROPIC_KEY = "sk-ant-test-not-a-real-key"
FAKE_OPENAI_KEY = "sk-test-not-a-real-key"
NODE_ID = "repo_a__docs__alpha.md"


def _run_smoke(tmp_path, extra_env=None):
    """Run `bash start.sh smoke` in a minimal, isolated environment: no inherited shell env
    (so no real keys, no ambient CI markers unless the caller adds them), an env-file override
    pointed at a scratch path that does not exist by default (backend/.env is never touched),
    and stdin always closed — a refusal test must never depend on (or block on) a real TTY."""
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "HOME": str(tmp_path),
        "SYNAPSE_ENV_FILE": str(tmp_path / "scratch.env"),
    }
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(START_SH), "smoke"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        stdin=subprocess.DEVNULL,  # L4: never let the child inherit pytest's own stdin
    )


def _keys_env_file(tmp_path, extra_lines=""):
    envfile = tmp_path / "keys.env"
    envfile.write_text(
        f"ANTHROPIC_API_KEY={FAKE_ANTHROPIC_KEY}\nOPENAI_API_KEY={FAKE_OPENAI_KEY}\n{extra_lines}",
        encoding="utf-8",
    )
    return envfile


# ── a safe, local, in-process stub backend ──────────────────────────────────────

class _StubHTTPServer(HTTPServer):
    """Typed home for the stub's mutable state — avoids monkey-patching attributes onto a
    plain `HTTPServer` instance."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.stub_requests: list[tuple[str, str, str]] = []
        self.mock = False
        self.render_status = 200
        self.render_body: dict = {"image": "media/x.png", "prompt": "a scene, no text"}


class _StubHandler(BaseHTTPRequestHandler):
    server: _StubHTTPServer

    def _record(self, method):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        body = raw.decode("utf-8", "replace")
        self.server.stub_requests.append((method, self.path, body))
        return body

    def do_GET(self):
        self._record("GET")
        code, payload = self._handle_get(self.path)
        self._send(code, payload)

    def do_POST(self):
        body = self._record("POST")
        parsed = {}
        try:
            parsed = json.loads(body) if body else {}
        except json.JSONDecodeError:
            pass
        code, payload = self._handle_post(self.path, parsed)
        self._send(code, payload)

    def _handle_get(self, path):
        if path == "/health":
            return 200, {"status": "ok"}
        if path == "/api/v1/models/status":
            return 200, {"mock": self.server.mock}
        if path == "/api/v1/graph":
            return 200, {"nodes": [{"id": NODE_ID}]}
        return 404, {"detail": "not found"}

    def _handle_post(self, path, body):
        if path == "/api/v1/distill":
            if body.get("dry_run"):
                return 200, {"tokens_est": 64, "threshold": 20000,
                              "requires_confirmation": False, "truncated": False,
                              "sources": [NODE_ID]}
            if body.get("confirm"):
                return 200, {"summary_note_id": "S — Alpha.md", "citations": 3,
                              "tokens_est": 64, "model": "claude-sonnet-5",
                              "truncated": False, "sources": [NODE_ID]}
            # neither dry_run nor confirm: the OLD (pre-fix) two-call shape — a real paid
            # summarize() on the actual backend; the stub answers like a completed run so a
            # test can tell this branch apart from the honest dry-run one.
            return 200, {"summary_note_id": "S — Alpha.md", "citations": 3,
                          "tokens_est": 64, "model": "claude-sonnet-5",
                          "truncated": False, "sources": [NODE_ID]}
        if path == "/api/v1/render":
            return self.server.render_status, self.server.render_body
        return 404, {"detail": "not found"}

    def _send(self, code, payload):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):  # noqa: A002 — quiet; pytest output stays readable
        pass


class StubBackend:
    """A minimal, faithful stand-in for the real /health, /api/v1/models/status,
    /api/v1/graph, /api/v1/distill and /api/v1/render endpoints — safe (127.0.0.1, ephemeral
    port, in-process), never touches a real provider. `render_status`/`render_body` let a test
    inject a provider failure (F4) without a real HTTP call anywhere."""

    def __init__(self):
        self._server = _StubHTTPServer(("127.0.0.1", 0), _StubHandler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def mock(self):
        return self._server.mock

    @mock.setter
    def mock(self, value):
        self._server.mock = value

    @property
    def render_status(self):
        return self._server.render_status

    @render_status.setter
    def render_status(self, value):
        self._server.render_status = value

    @property
    def render_body(self):
        return self._server.render_body

    @render_body.setter
    def render_body(self, value):
        self._server.render_body = value

    @property
    def requests(self):
        return list(self._server.stub_requests)

    def close(self):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


@pytest.fixture
def stub_backend():
    stub = StubBackend()
    try:
        yield stub
    finally:
        stub.close()


def _newest_transcript(reports_dir):
    files = sorted(Path(reports_dir).glob("live_smoke_*.md"), key=lambda p: p.stat().st_mtime)
    assert files, f"no live_smoke transcript was written under {reports_dir}"
    return files[-1]


# ── I1 / I2 — the original refusal contract ─────────────────────────────────────

class TestPaidSmokeCannotSpendAccidentally:
    def test_keyless_environment_refuses_actionably_with_exit_1(self, tmp_path):
        r = _run_smoke(tmp_path)
        combined = r.stdout + r.stderr
        assert r.returncode == 1, (
            f"expected an actionable exit 1 (not the unknown-command exit 2); "
            f"got returncode={r.returncode}\nstdout={r.stdout!r}\nstderr={r.stderr!r}"
        )
        assert "ANTHROPIC_API_KEY" in combined, combined
        assert "OPENAI_API_KEY" in combined, combined

    def test_ci_environment_refuses_unconditionally_even_with_keys(self, tmp_path):
        r = _run_smoke(tmp_path, extra_env={
            "CI": "true",
            # Deliberately fake, non-functional values: proves CI refusal does not depend on
            # whether keys are present — if this ever reached a provider, these would 401.
            "ANTHROPIC_API_KEY": FAKE_ANTHROPIC_KEY,
            "OPENAI_API_KEY": FAKE_OPENAI_KEY,
        })
        combined = r.stdout + r.stderr
        assert r.returncode == 1, (
            f"CI must refuse regardless of keys; got returncode={r.returncode}\n"
            f"stdout={r.stdout!r}\nstderr={r.stderr!r}"
        )
        assert "CI" in combined, combined


# ── F2 — confirmation-before-spend ordering ─────────────────────────────────────

class TestConfirmationBeforeSpend:
    def test_non_interactive_without_yes_makes_zero_backend_posts(self, tmp_path, stub_backend):
        """Keys present, backend reachable (a stub that WOULD happily answer every call), stdin
        closed, SYNAPSE_SMOKE_YES unset: the command must refuse before a single POST — no
        estimate call, no distill, no render. This is the invariant the adversary's M3 mutation
        (moving the confirmation block to AFTER the two distill POSTs) defeats while the OLD
        suite still reported 2 passed; see the fix report for the mutation-kill proof run
        against a scratch-mutated copy of start.sh (this file only asserts against the real,
        unmutated command)."""
        envfile = _keys_env_file(tmp_path)
        r = _run_smoke(tmp_path, extra_env={
            "SYNAPSE_ENV_FILE": str(envfile),
            "PORT": str(stub_backend.port),
        })
        combined = r.stdout + r.stderr
        assert r.returncode == 1, (
            f"non-interactive without SYNAPSE_SMOKE_YES must refuse (exit 1); "
            f"got returncode={r.returncode}\nstdout={r.stdout!r}\nstderr={r.stderr!r}"
        )
        posts = [req for req in stub_backend.requests if req[0] == "POST"]
        assert posts == [], f"expected ZERO POSTs before confirmation; backend saw: {posts}"
        assert "SYNAPSE_SMOKE_YES" in combined, combined


# ── F1 — one non-spending estimate, exactly one paid distill, never two ─────────

class TestNoDoubleSpend:
    def test_confirmed_run_makes_one_dry_run_and_exactly_one_paid_distill_and_one_render(
            self, tmp_path, stub_backend):
        envfile = _keys_env_file(tmp_path)
        r = _run_smoke(tmp_path, extra_env={
            "SYNAPSE_ENV_FILE": str(envfile),
            "PORT": str(stub_backend.port),
            "SYNAPSE_SMOKE_YES": "1",
            # Never let a test write a transcript into the real, tracked project tree.
            "SYNAPSE_SMOKE_REPORTS_DIR": str(tmp_path / "reports"),
        })
        combined = r.stdout + r.stderr
        assert r.returncode == 0, f"expected a clean run; got returncode={r.returncode}\n{combined}"

        distill_bodies = [
            json.loads(body) for (method, path, body) in stub_backend.requests
            if method == "POST" and path == "/api/v1/distill"
        ]
        render_calls = [
            1 for (method, path, _body) in stub_backend.requests
            if method == "POST" and path == "/api/v1/render"
        ]
        dry_runs = [b for b in distill_bodies if b.get("dry_run") is True]
        paid = [b for b in distill_bodies if b.get("confirm") is True and not b.get("dry_run")]

        assert len(dry_runs) == 1, (
            f"expected exactly ONE non-spending estimate call (dry_run: true); "
            f"distill bodies were: {distill_bodies}"
        )
        assert len(paid) == 1, (
            f"expected exactly ONE paid distill call (confirm: true); "
            f"distill bodies were: {distill_bodies}"
        )
        assert len(distill_bodies) == 2, (
            f"expected exactly two distill POSTs total (one free, one paid) — never a second "
            f"paid one; got {distill_bodies}"
        )
        assert len(render_calls) == 1, f"expected exactly one render call; got {render_calls}"


# ── F3 — the .env loader mirrors config.py and never crashes ───────────────────

class TestEnvLoaderToleratesCommentsAndOddKeys:
    def test_indented_comment_and_non_identifier_line_never_crash_the_command(
            self, tmp_path, unused_tcp_port):
        envfile = tmp_path / "odd.env"
        envfile.write_text(
            "  # local note: key=value\n"          # indented comment — must be skipped, not
                                                     # mistaken for a KEY=VALUE line
            "MY-VAR=1\n"                            # non-identifier assignment — config.py
                                                     # accepts it (any dict key); bash cannot
                                                     # export it, but must not crash on it
            f"ANTHROPIC_API_KEY={FAKE_ANTHROPIC_KEY}\n"
            f"OPENAI_API_KEY={FAKE_OPENAI_KEY}\n",
            encoding="utf-8",
        )
        r = _run_smoke(tmp_path, extra_env={
            "SYNAPSE_ENV_FILE": str(envfile),
            "PORT": str(unused_tcp_port),  # nothing listens here — expect the actionable
                                            # "no backend answering" refusal, never a crash
        })
        combined = r.stdout + r.stderr
        assert "invalid variable name" not in combined, (
            f"the .env loader crashed on an indented comment / non-identifier line "
            f"instead of tolerating it like config.py does:\n{combined}"
        )
        assert r.returncode == 1, (
            f"expected the actionable 'no backend answering' refusal (exit 1) once the real "
            f"keys in the file are recognized; got returncode={r.returncode}\n{combined}"
        )
        assert "no backend answering" in combined, combined


@pytest.fixture
def unused_tcp_port():
    """A real, momentarily-free TCP port on 127.0.0.1 — used only as a target nothing answers
    on, never bound by this fixture itself (freed immediately so start.sh's own curl can try
    and fail to connect, exactly like a genuinely-down backend)."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ── F4 — provider failures get a diagnostic, a transcript record, a real exit code ──

class TestProviderFailureIsRecordedNotSwallowed:
    def test_render_429_after_successful_distill_is_diagnosed_and_recorded(
            self, tmp_path, stub_backend):
        stub_backend.render_status = 429
        stub_backend.render_body = {"detail": "rate limited — retry later"}
        envfile = _keys_env_file(tmp_path)
        reports_dir = tmp_path / "reports"
        r = _run_smoke(tmp_path, extra_env={
            "SYNAPSE_ENV_FILE": str(envfile),
            "PORT": str(stub_backend.port),
            "SYNAPSE_SMOKE_YES": "1",
            # Never let a test write a transcript into the real, tracked project tree.
            "SYNAPSE_SMOKE_REPORTS_DIR": str(reports_dir),
        })
        combined = r.stdout + r.stderr

        assert r.returncode == 3, (
            f"expected the documented provider-call-failed exit code (3), not a bare curl "
            f"exit status; got returncode={r.returncode}\n{combined}"
        )
        assert "429" in combined, f"no diagnostic mentioning the HTTP status:\n{combined}"
        assert "render" in combined.lower(), f"diagnostic doesn't name which call failed:\n{combined}"

        transcript = _newest_transcript(reports_dir)
        text = transcript.read_text(encoding="utf-8")
        assert "## Distill — result" in text, (
            f"the successful distill must still be recorded before the render failure:\n{text}"
        )
        assert "FAILED" in text and "Render" in text, (
            f"no failure section for the render call in the transcript:\n{text}"
        )
        assert "429" in text and "rate limited" in text, (
            f"the transcript failure section must carry the safe response context:\n{text}"
        )
        assert FAKE_ANTHROPIC_KEY not in text and FAKE_OPENAI_KEY not in text, (
            "a secret leaked into the transcript"
        )
