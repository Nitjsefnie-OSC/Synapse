"""Issue #3 — `./start.sh smoke` safety-refusal contract.

The two live smokes (real Anthropic distill, real gpt-image-1 render) spend real money. This
suite proves the ONE invariant that must hold no matter how the happy path is implemented: the
command can never spend accidentally. It exercises ONLY the refusal paths —

  I1 keyless environment  -> actionable exit 1 (distinct from the unknown-command exit 2),
                              naming the missing key(s).
  I2 any CI environment   -> refuses unconditionally, even with (fake) keys present.

It never supplies real keys and never lets the command reach a network call: every subprocess
gets a from-scratch env dict (never `os.environ.copy()`), and `SYNAPSE_ENV_FILE` is pointed at a
scratch path that does not exist, so a developer's real `backend/.env` can never be read or used
by the command under test.
"""

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
START_SH = REPO_ROOT / "start.sh"


def _run_smoke(tmp_path, extra_env=None):
    """Run `bash start.sh smoke` in a minimal, isolated environment: no inherited shell env
    (so no real keys, no ambient CI markers unless the caller adds them), and an env-file
    override pointed at a scratch path that does not exist — backend/.env is never touched."""
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
    )


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
            "ANTHROPIC_API_KEY": "sk-ant-test-not-a-real-key",
            "OPENAI_API_KEY": "sk-test-not-a-real-key",
        })
        combined = r.stdout + r.stderr
        assert r.returncode == 1, (
            f"CI must refuse regardless of keys; got returncode={r.returncode}\n"
            f"stdout={r.stdout!r}\nstderr={r.stderr!r}"
        )
        assert "CI" in combined, combined
