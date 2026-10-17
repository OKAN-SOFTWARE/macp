"""Tests for the systemd unit generator (scripts/systemd/gen_units.py).

``scripts/systemd`` is not a package (no ``__init__.py``), so load the module by
path instead of relying on pytest's sys.path insertion — that keeps the test
working under ``uv run pytest scripts/systemd/test_gen_units.py`` and a bare
``uv run pytest`` alike.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_SPEC = importlib.util.spec_from_file_location(
    "gen_units", Path(__file__).with_name("gen_units.py"))
gen_units = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gen_units)


class RunnerOOMPolicyTest(unittest.TestCase):
    """A build daemon dying to the kernel OOM killer must not drop the runner
    unit (and the run it was executing) — the default ``OOMPolicy=stop`` kills
    every process in the cgroup. ``OOMPolicy=continue`` on the runner isolates
    that; it is deliberately NOT applied to the control-plane daemons, whose OOM
    should still trigger their normal restart."""

    def setUp(self):
        self.units = gen_units.build()

    def test_runner_unit_has_oom_policy_continue_in_service_section(self):
        runner = self.units["macp-runner-worker-default.service"]
        self.assertIn("OOMPolicy=continue", runner)
        # It must land in [Service], not leak into [Install].
        _, after_service = runner.split("[Service]", 1)
        service_section = after_service.split("[Install]", 1)[0]
        self.assertIn("OOMPolicy=continue", service_section)

    def test_control_plane_units_do_not_get_oom_policy(self):
        # A blanket OOMPolicy would silence real OOMs on these; isolating it to
        # the runner (the only unit that spawns builds) is the whole point.
        for name in ("macp-orchestrator.service", "macp-todo-sync.service",
                     "macp-dashboard.service"):
            self.assertNotIn("OOMPolicy", self.units[name], name)


class OneshotTimeoutStartSecTest(unittest.TestCase):
    """Type=oneshot's TimeoutStartSec defaults to `infinity`; a hung internal git
    fetch/clone could then occupy the unit forever and block the next timer tick.
    Every oneshot unit must carry a finite TimeoutStartSec in [Service]; simple
    (long-running) daemons must not, since a start timeout is meaningless there."""

    def setUp(self):
        self.units = gen_units.build()

    def test_oneshot_units_carry_timeout_start_sec(self):
        for name, text in self.units.items():
            if not name.endswith(".service") or "Type=oneshot" not in text:
                continue
            _, after_service = text.split("[Service]", 1)
            service_section = after_service.split("[Install]", 1)[0]
            self.assertIn("TimeoutStartSec=1800", service_section, name)

    def test_simple_units_do_not_get_timeout_start_sec(self):
        for name, text in self.units.items():
            if not name.endswith(".service") or "Type=simple" not in text:
                continue
            self.assertNotIn("TimeoutStartSec", text, name)


class RealUnitContentTest(unittest.TestCase):
    """The real-account trading loop must run from its OWN clone with its OWN venv
    and supply the four trading params INLINE. The clone's .secrets/kis.env carries
    KIS_ENV/creds but NOT BUY_AND_HOLD/KIS_MAX_DEPLOY_KRW, so without these inline
    the live account would trade on momentum at unbounded size. Credentials are
    never wired via EnvironmentFile. Lifecycle mirrors the paper unit (simple /
    on-failure / 60s, no OOMPolicy)."""

    def setUp(self):
        self.real = gen_units.build()["kis-trading-real.service"]

    def test_working_directory_is_the_real_clone(self):
        # Exact line (references the module constant) — a bare substring
        # "ai-stock-trading" would also match the PAPER clone's path prefix.
        self.assertIn(f"WorkingDirectory={gen_units.TRADING_REAL}\n", self.real)

    def test_runs_the_clones_dedicated_venv_interpreter(self):
        self.assertIn(
            f"ExecStart={gen_units.TRADING_REAL}/.venv/bin/python trading_loop.py\n",
            self.real)
        # NOT the paper loop's `uv run python` form.
        self.assertNotIn("uv run", self.real)

    def test_supplies_the_four_trading_params_inline_in_service_section(self):
        _, after_service = self.real.split("[Service]", 1)
        service_section = after_service.split("[Install]", 1)[0]
        for env in ("Environment=KIS_ENV=real", "Environment=BUY_AND_HOLD=1",
                    "Environment=KIS_UNIVERSE=etf",
                    "Environment=KIS_MAX_DEPLOY_KRW=3000000"):
            self.assertIn(env, service_section, env)

    def test_alert_script_points_at_the_vault(self):
        # config.py defaults to ~/Projects/second-brain (pre-rename) → rc=127.
        self.assertIn(
            f"Environment=KIS_ALERT_SCRIPT={gen_units.VAULT}/scripts/kakao_send.sh\n",
            self.real)

    def test_notify_channel_key_routes_slack_to_kis_channel(self):
        # slack_send._channel_key() reads NOTIFY_CHANNEL_KEY first; without it this
        # unit's Slack sibling-send would fall to the "default" channel. The real
        # account skips kis.env, so it can't inherit this line — it must be inline,
        # in [Service] alongside KIS_ALERT_SCRIPT.
        _, after_service = self.real.split("[Service]", 1)
        service_section = after_service.split("[Install]", 1)[0]
        self.assertIn("Environment=NOTIFY_CHANNEL_KEY=kis", service_section)

    def test_lifecycle_mirrors_paper_no_oom_policy(self):
        self.assertIn("Type=simple", self.real)
        self.assertIn("Restart=on-failure", self.real)
        self.assertIn("RestartSec=60", self.real)
        self.assertNotIn("OOMPolicy", self.real)

    def test_credentials_not_wired_via_environment_file(self):
        # Only the shared ~/.config/macp/env EnvironmentFile — never the clone's
        # kis.env. (The paper unit, by contrast, wires kis.env → count == 2.)
        self.assertNotIn("kis.env", self.real)
        self.assertEqual(self.real.count("EnvironmentFile="), 1)


class NotifyChannelScopingTest(unittest.TestCase):
    """NOTIFY_CHANNEL_KEY=kis routes the ai-stock-trading Slack sibling-send to the
    dedicated kis channel, and must stay scoped to the ast units only. The paper
    units (kis-trading / livesync / watchdog) inherit it via kis.env (rendered from
    KIS_ENV_TEMPLATE), so it lives there — NOT inline in each unit — while the real
    account, which skips kis.env, carries it inline. No other unit (todo-sync,
    worker, orchestrator, …) and not the shared ENV_TEMPLATE may carry it, or every
    daemon's Slack alerts would get rerouted."""

    def setUp(self):
        self.units = gen_units.build()

    def test_kis_env_template_covers_the_paper_units(self):
        # The three paper units share kis.env (EnvironmentFile=KIS_ENV_FILE), so one
        # line in KIS_ENV_TEMPLATE covers all of them — no per-unit duplication.
        self.assertIn("\nNOTIFY_CHANNEL_KEY=kis\n", gen_units.KIS_ENV_TEMPLATE)

    def test_only_the_real_unit_carries_it_inline(self):
        # The real account is the only unit whose rendered text holds the key inline;
        # the paper units inherit it from kis.env (so it is absent from their text).
        with_key = [n for n, t in self.units.items() if "NOTIFY_CHANNEL_KEY" in t]
        self.assertEqual(with_key, ["kis-trading-real.service"])

    def test_not_in_shared_env_template(self):
        # The shared env (every unit's EnvironmentFile) must not carry it, else all
        # daemons' Slack sends would reroute to the kis channel.
        self.assertNotIn("NOTIFY_CHANNEL_KEY", gen_units.ENV_TEMPLATE)


class EnableTargetsTest(unittest.TestCase):
    """`--enable` must NEVER start the real-account loop. The exclusion is
    unconditional and sits ABOVE the `--only` filter, so not even
    `--enable --only kis-trading-real` is a backdoor to start it (install to disk
    only; a human runs `systemctl --user enable --now kis-trading-real.service`)."""

    def setUp(self):
        self.units = gen_units.build()

    def test_real_unit_excluded_from_default_enable(self):
        targets = gen_units.enable_targets(self.units, None)
        self.assertNotIn("kis-trading-real.service", targets)
        # Surgical: the paper loop (a sibling simple daemon with no timer) is
        # still enabled — the exclusion touches only the install-only stem.
        self.assertIn("kis-trading.service", targets)

    def test_real_unit_cannot_be_enabled_even_via_only(self):
        targets = gen_units.enable_targets(self.units, ["kis-trading-real"])
        self.assertNotIn("kis-trading-real.service", targets)
        self.assertEqual(targets, [])

    def test_kakao_refresh_timer_excluded_from_default_enable(self):
        # Slack-only cutover: the Kakao token refresh is default-OFF. Its timer is
        # still written to disk (so a human rollback is one `enable --now`), but
        # `--enable` must not start it.
        targets = gen_units.enable_targets(self.units, None)
        self.assertNotIn("macp-kakao-refresh.timer", targets)
        self.assertNotIn("macp-kakao-refresh.service", targets)
        # Surgical: a sibling kakao timer (the once-a-day digest) is still enabled —
        # the exclusion touches only the refresh stem, not every kakao unit.
        self.assertIn("macp-kakao-digest.timer", targets)

    def test_kakao_refresh_cannot_be_enabled_even_via_only(self):
        targets = gen_units.enable_targets(self.units, ["macp-kakao-refresh"])
        self.assertNotIn("macp-kakao-refresh.timer", targets)
        self.assertEqual(targets, [])


class ParameterizationTest(unittest.TestCase):
    """Host-identity knobs (MACP_HOST_ID / MACP_CLAUDE_RC_NAME / MACP_CALENDAR_ID)
    are env-overridable so a third party can render their own units. The host-id
    DEFAULTS are neutral: the live host's identity comes from its env file, and
    HOST_ID only feeds ENV_TEMPLATE (which write_env writes only when the file is
    missing), so rendered units are unaffected by the default (equivalence guard,
    Todo/items/macp-public-split-4-daemon-params.md if_then)."""

    @staticmethod
    def _reload(**env):
        # Load a fresh copy of the module under a patched environment; patch.dict
        # snapshots os.environ on enter and fully restores it on exit, so the
        # manual pops/sets below never leak into other tests. Keys mapped to None
        # are removed (exercising the hardcoded default), the rest are set.
        with mock.patch.dict(os.environ, {}, clear=False):
            for k, v in env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            spec = importlib.util.spec_from_file_location(
                "gen_units_reloaded", Path(__file__).with_name("gen_units.py"))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod

    def test_defaults_render_neutral_host_id_in_env_template(self):
        mod = self._reload(MACP_HOST_ID=None, MACP_CLAUDE_RC_NAME=None,
                           MACP_CALENDAR_ID=None)
        self.assertIn("\nMACP_HOST_ID=macp-host\n", mod.ENV_TEMPLATE)
        self.assertIn("\nCLAUDE_RC_NAME=macp-host\n", mod.ENV_TEMPLATE)

    def test_env_overrides_take_effect(self):
        # Host-identity ENV_TEMPLATE knobs still honour their env overrides; the
        # calendar id resolution has its own hermetic class (CalendarIdResolutionTest).
        mod = self._reload(MACP_HOST_ID="acme-hq",
                           MACP_CLAUDE_RC_NAME="acme-rc")
        self.assertIn("\nMACP_HOST_ID=acme-hq\n", mod.ENV_TEMPLATE)
        self.assertIn("\nCLAUDE_RC_NAME=acme-rc\n", mod.ENV_TEMPLATE)

    def test_claude_rc_name_defaults_to_host_id(self):
        # MACP_CLAUDE_RC_NAME unset → CLAUDE_RC_NAME mirrors MACP_HOST_ID, so a
        # third party setting only MACP_HOST_ID still gets a matching RC name.
        mod = self._reload(MACP_HOST_ID="acme-hq", MACP_CLAUDE_RC_NAME=None,
                           MACP_CALENDAR_ID=None)
        self.assertIn("\nMACP_HOST_ID=acme-hq\n", mod.ENV_TEMPLATE)
        self.assertIn("\nCLAUDE_RC_NAME=acme-hq\n", mod.ENV_TEMPLATE)


class CalendarIdResolutionTest(unittest.TestCase):
    """CALENDAR_ID / LABEL_PREFIX resolve env → <vault>/.macp/config.toml → neutral
    (empty). An empty CALENDAR_ID makes the todo-sync unit OMIT --calendar-id (the
    CLI's own default then applies) rather than syncing to a blank target, and both
    values are injected into ENV_TEMPLATE so the daemon resolves them regardless of
    its WorkingDirectory. Every case uses a throwaway MAESTRO_VAULT_DIR + neutral
    values, so no personal identifier is referenced (public-split leaf 7)."""

    @staticmethod
    def _reload_with_vault(vault, **env):
        # patch.dict snapshots os.environ on enter and restores it on exit, so the
        # MAESTRO_VAULT_DIR + knob writes below never leak into other tests. Keys
        # mapped to None are removed (exercising env-absent), the rest are set.
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ["MAESTRO_VAULT_DIR"] = str(vault)
            for k, v in env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            spec = importlib.util.spec_from_file_location(
                "gen_units_cal", Path(__file__).with_name("gen_units.py"))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod

    @staticmethod
    def _write_config(vault, body):
        (vault / ".macp").mkdir(parents=True, exist_ok=True)
        (vault / ".macp" / "config.toml").write_text(body, encoding="utf-8")

    def test_calendar_id_from_config_when_no_env(self):
        with tempfile.TemporaryDirectory() as d:
            vault = Path(d)
            self._write_config(vault, "calendar_id = 'cal@example.com'\n")
            mod = self._reload_with_vault(vault, MACP_CALENDAR_ID=None)
            self.assertIn("--calendar-id cal@example.com\n",
                          mod.build()["macp-todo-sync.service"])

    def test_calendar_id_env_overrides_config(self):
        with tempfile.TemporaryDirectory() as d:
            vault = Path(d)
            self._write_config(vault, "calendar_id = 'cal@example.com'\n")
            mod = self._reload_with_vault(vault, MACP_CALENDAR_ID="ops@acme.example")
            todo_sync = mod.build()["macp-todo-sync.service"]
            self.assertIn("--calendar-id ops@acme.example\n", todo_sync)
            self.assertNotIn("cal@example.com", todo_sync)

    def test_calendar_id_omitted_when_config_and_env_absent(self):
        with tempfile.TemporaryDirectory() as d:
            vault = Path(d)
            self._write_config(vault, "profile = 'test-host'\n")  # no calendar_id key
            mod = self._reload_with_vault(vault, MACP_CALENDAR_ID=None)
            todo_sync = mod.build()["macp-todo-sync.service"]
            self.assertIn("--calendar-sync", todo_sync)
            self.assertNotIn("--calendar-id", todo_sync)

    def test_env_template_injects_label_and_calendar_from_config(self):
        with tempfile.TemporaryDirectory() as d:
            vault = Path(d)
            self._write_config(
                vault, "label_prefix = 'com.acme.'\ncalendar_id = 'cal@example.com'\n")
            mod = self._reload_with_vault(vault, MACP_LABEL_PREFIX=None,
                                          MACP_CALENDAR_ID=None)
            self.assertIn("\nMACP_LABEL_PREFIX=com.acme.\n", mod.ENV_TEMPLATE)
            self.assertIn("\nMACP_CALENDAR_ID=cal@example.com\n", mod.ENV_TEMPLATE)


if __name__ == "__main__":
    unittest.main()


class EnvBackfillTest(unittest.TestCase):
    """write_env() keeps an existing env file (it holds hand-filled secrets), so
    identity keys added after install must be appended, not skipped — a host
    without MACP_LABEL_PREFIX resolved com.macp. from $HOME and daemonctl could
    no longer restart remote-control (2026-09-26)."""

    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "env"

    def _backfill(self, text):
        self.path.write_text(text)
        with mock.patch.object(gen_units, "LABEL_PREFIX", "com.example."), \
             mock.patch.object(gen_units, "CALENDAR_ID", "cal@example.com"):
            gen_units._backfill_identity(self.path)
        return self.path.read_text()

    def test_appends_missing_keys_and_keeps_existing_lines(self):
        out = self._backfill("SECRET=keep\n")
        self.assertIn("SECRET=keep\n", out)
        self.assertIn("MACP_LABEL_PREFIX=com.example.\n", out)
        self.assertIn("MACP_CALENDAR_ID=cal@example.com\n", out)

    def test_never_overrides_a_key_already_present(self):
        out = self._backfill("MACP_LABEL_PREFIX=com.pinned.\n")
        self.assertEqual(out.count("MACP_LABEL_PREFIX="), 1)
        self.assertIn("MACP_LABEL_PREFIX=com.pinned.", out)

    def test_commented_key_counts_as_missing(self):
        out = self._backfill("# MACP_LABEL_PREFIX=com.old.\n")
        self.assertIn("\nMACP_LABEL_PREFIX=com.example.\n", out)


# --- 13c: default-mode golden + installed mode -------------------------------------

GOLDEN = Path(__file__).with_name("testdata") / "default_units.golden"
# Neutral host: no real home or vault path ends up in the golden (it sits inside the
# identifier gate's scanned tree). The vault has no .macp/config.toml, so the
# calendar id and label prefix resolve to "". None = removed from the env.
NEUTRAL_ENV = {
    "HOME": "/srv/neutral-home",
    "MAESTRO_VAULT_DIR": "/srv/neutral-vault",
    "MACP_HOST_ID": None,
    "MACP_CLAUDE_RC_NAME": None,
    "MACP_CALENDAR_ID": None,
    "MACP_LABEL_PREFIX": None,
    "MACP_ENGINE_PYTHON": None,
    "MACP_BROWSER_DIR": None,
}


@contextlib.contextmanager
def _env(**env):
    """Same patch.dict pattern as ParameterizationTest._reload: keys mapped to None
    are removed, the rest set, and os.environ is fully restored on exit."""
    with mock.patch.dict(os.environ, {}, clear=False):
        for k, v in env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield


def _neutral_gen_units(**extra):
    """A fresh gen_units loaded under NEUTRAL_ENV (plus ``extra``); ``Path.home()``
    reads the patched HOME at load. MACP_ENGINE_PYTHON is read by build() at call
    time, so callers patch it around build() (see _default_build)."""
    with _env(**(NEUTRAL_ENV | extra)):
        spec = importlib.util.spec_from_file_location(
            "gen_units_neutral", Path(__file__).with_name("gen_units.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod


def _default_build(mod):
    """build() with the knob absent from the env, whatever the host has set."""
    with _env(MACP_ENGINE_PYTHON=None):
        return mod.build()


def _render(mod, units=None):
    """Every unit (``--print`` framing), then both env templates, as one string."""
    units = _default_build(mod) if units is None else units
    return ("".join(f"##### {n}\n{t}" for n, t in units.items())
            + "##### ENV_TEMPLATE\n" + mod.ENV_TEMPLATE
            + "##### KIS_ENV_TEMPLATE\n" + mod.KIS_ENV_TEMPLATE)


class DefaultModeGoldenTest(unittest.TestCase):
    """Invariance pin, not a RED→GREEN test: the golden was rendered from the
    generator BEFORE installed mode existed (13c). With no engine-python knob every
    unit, timer and env template must stay byte-identical, so a host that reruns
    gen_units.py without opting in gets exactly the units it already runs."""

    def test_default_mode_matches_golden_byte_for_byte(self):
        self.assertEqual(_render(_neutral_gen_units()),
                         GOLDEN.read_text(encoding="utf-8"))


REPO = Path(__file__).resolve().parents[2]
ENGINE_PY = "/opt/macp-engine/bin/python"
# Units whose script ships with the engine (unit stem → script name).
CONVERTED = {
    "macp-orchestrator": "orchestrator_tick.sh",
    "macp-planner": "planner_tick.sh",
    "macp-brief": "brief.sh",
    "macp-kakao-refresh": "kakao_token_refresh.sh",
    "macp-health": "health_report.py",
    "macp-kakao-digest": "kakao_digest_send.py",
    "claude-auth-watch": "claude_auth_watch.py",
}
# Units that ran ``uv run python -m ...`` in the vault (public split 14): installed
# mode runs the same module/arguments from the installed interpreter with -P.
MODULE_UNITS = {
    "macp-runner-worker-default": "-m todo_system.orchestrator.runner --agent worker-default "
                                  "--mode live-staged --uri file://Worker/agents --daemon",
    "macp-maintenance": "-m todo_system.worker maintenance",
    "macp-todo-sync": "-u -m todo_system.cli daemon --interval 3 --calendar-sync",
    "macp-dashboard": "-u -m todo_system.engine_script macp_dashboard.py "
                      "--host 127.0.0.1 --port 8787 --no-open",
}
# Units whose file stays in the vault: their path survives installed mode.
VAULT_ONLY = ("claude-upgrade", "claude-remote-control", "claude-remote-control-watchdog",
              "macp-monitor")


def _service_section(text):
    return text.split("[Service]", 1)[1].split("[Install]", 1)[0]


def _exec_start(text):
    (line,) = [ln for ln in text.splitlines() if ln.startswith("ExecStart=")]
    return line[len("ExecStart="):]


def _export_excluded():
    lines = (REPO / "scripts" / "public_export_exclude.txt").read_text(encoding="utf-8").splitlines()
    return {ln.split("#", 1)[0].strip() for ln in lines} - {""}


class InstalledModeTest(unittest.TestCase):
    """--engine-python / MACP_ENGINE_PYTHON (13c): engine-script units run through
    ``todo_system.engine_script`` from the installed interpreter with -P (the vault
    cwd may still hold a checkout's todo_system/) and MACP_VAULT_ROOT; nothing else
    in any unit moves."""

    def setUp(self):
        self.mod = _neutral_gen_units()
        self.default = _default_build(self.mod)
        self.inst = self.mod.build(ENGINE_PY)

    def test_engine_script_units_exec_through_engine_script(self):
        for stem, name in CONVERTED.items():
            self.assertEqual(_exec_start(self.inst[f"{stem}.service"]),
                             f"{ENGINE_PY} -P -m todo_system.engine_script {name}", stem)

    def test_engine_script_units_pass_vault_root_in_service_section(self):
        env_line = f"Environment=MACP_VAULT_ROOT={self.mod.VAULT}"
        for stem in CONVERTED:
            text = self.inst[f"{stem}.service"]
            self.assertIn(env_line, _service_section(text), stem)
            self.assertEqual(text.count(env_line), 1, stem)
            # engine_script setdefaults it to its own interpreter.
            self.assertNotIn("MACP_PYTHON", text, stem)

    def test_converted_units_keep_working_directory_and_lifecycle(self):
        env_line = f"Environment=MACP_VAULT_ROOT={self.mod.VAULT}"

        def strip(text):
            return [ln for ln in text.splitlines()
                    if not ln.startswith("ExecStart=") and ln != env_line]

        for stem in CONVERTED:
            name = f"{stem}.service"
            self.assertEqual(strip(self.inst[name]), strip(self.default[name]), stem)
        self.assertIn("WorkingDirectory=/srv/neutral-home\n", self.inst["claude-auth-watch.service"])

    def test_vault_only_units_keep_vault_paths(self):
        for stem in VAULT_ONLY:
            name = f"{stem}.service"
            self.assertEqual(self.inst[name], self.default[name], stem)
            self.assertIn(f"{self.mod.VAULT}/", _exec_start(self.inst[name]), stem)
            self.assertNotIn("engine_script", self.inst[name], stem)

    def test_module_units_run_from_installed_interpreter(self):
        env_line = f"Environment=MACP_VAULT_ROOT={self.mod.VAULT}"
        for stem, args in MODULE_UNITS.items():
            text = self.inst[f"{stem}.service"]
            self.assertEqual(_exec_start(text), f"{ENGINE_PY} -P {args}", stem)
            self.assertNotIn(str(self.mod.UV), text, stem)
            self.assertEqual(_service_section(text).count(env_line), 1, stem)

    def test_module_units_keep_everything_but_exec_and_vault_root(self):
        env_line = f"Environment=MACP_VAULT_ROOT={self.mod.VAULT}"

        def strip(text):
            return [ln for ln in text.splitlines()
                    if not ln.startswith("ExecStart=") and ln != env_line]

        for stem in MODULE_UNITS:
            name = f"{stem}.service"
            self.assertEqual(strip(self.inst[name]), strip(self.default[name]), stem)
        # The runner keeps its OOM policy (order inside [Service] is irrelevant).
        self.assertIn("OOMPolicy=continue", self.inst["macp-runner-worker-default.service"])

    def test_no_vault_engine_reference_left_in_macp_units(self):
        # After the switch nothing under macp-* may still import the vault's own
        # todo_system/ (uv run in the vault) or exec an engine script by vault path.
        for name, text in self.inst.items():
            if not name.startswith("macp-") or not name.endswith(".service"):
                continue
            if name == "macp-monitor.service":
                continue  # vault-only Worker/_state script
            exec_start = _exec_start(text)
            self.assertNotIn(f"{self.mod.UV} run", exec_start, name)
            self.assertNotIn(f"{self.mod.VAULT}/scripts/", exec_start, name)

    def test_everything_else_is_untouched(self):
        self.assertEqual(list(self.inst), list(self.default))
        converted = {f"{stem}.service" for stem in [*CONVERTED, *MODULE_UNITS]}
        for name in self.default:
            if name not in converted:
                self.assertEqual(self.inst[name], self.default[name], name)

    def test_env_knob_equals_cli_arg(self):
        with _env(MACP_ENGINE_PYTHON=ENGINE_PY):
            via_env = self.mod.build()
        self.assertEqual(via_env, self.inst)

    def test_empty_knob_is_default_mode(self):
        self.assertEqual(self.mod.build(""), self.default)
        with _env(MACP_ENGINE_PYTHON=""):
            via_empty_env = self.mod.build()
        self.assertEqual(_render(self.mod, via_empty_env), GOLDEN.read_text(encoding="utf-8"))

    def test_relative_engine_python_rejected(self):
        with self.assertRaises(ValueError):
            self.mod.build("venv/bin/python")
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            self.mod.main(["--print", "--engine-python", "python"])
        self.assertEqual(cm.exception.code, 2)

    def test_print_cli_renders_installed_mode(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = self.mod.main(["--print", "--engine-python", ENGINE_PY])
        self.assertEqual(rc, 0)
        self.assertIn(f"ExecStart={ENGINE_PY} -P -m todo_system.engine_script orchestrator_tick.sh\n",
                      out.getvalue())

    def test_macp_worker_unit_not_invented(self):
        # The AC names a macp-worker unit, but systemd replaced worker_run_next.sh
        # with the runner + maintenance units; inventing one would break the
        # default-mode golden.
        self.assertNotIn("macp-worker.service", self.inst)
        self.assertNotIn("macp-worker.service", self.default)


class ClassificationMatchesExportExcludeTest(unittest.TestCase):
    """scripts/public_export_exclude.txt decides what is vault-only: those files
    leave the engine tree, everything else ships in the wheel and leaves the vault.
    A unit left on a vault path for an exported script would break once the vault
    copy is gone; one routed through engine_script for a vault-only file would not
    find it in the wheel's export set."""

    def setUp(self):
        self.mod = _neutral_gen_units()
        self.default = _default_build(self.mod)
        self.excluded = _export_excluded()

    def test_converted_scripts_are_exported_engine_files(self):
        for stem, name in CONVERTED.items():
            self.assertNotIn(f"scripts/{name}", self.excluded, stem)
            self.assertTrue((REPO / "scripts" / name).is_file(), name)
            self.assertIn(f"{self.mod.VAULT}/scripts/{name}",
                          _exec_start(self.default[f"{stem}.service"]), stem)

    def test_vault_only_units_point_at_excluded_or_non_engine_files(self):
        for stem in VAULT_ONLY:
            exec_start = _exec_start(self.default[f"{stem}.service"])
            prefix = f"{self.mod.VAULT}/scripts/"
            if prefix in exec_start:
                name = exec_start.split(prefix, 1)[1].split()[0]
                self.assertIn(f"scripts/{name}", self.excluded, stem)
            else:
                self.assertIn(f"{self.mod.VAULT}/Worker/", exec_start, stem)

    def test_every_vault_script_unit_is_classified(self):
        classified = set(CONVERTED) | set(VAULT_ONLY)
        for name, text in self.default.items():
            if name.endswith(".service") and f"{self.mod.VAULT}/scripts/" in _exec_start(text):
                self.assertIn(name.rsplit(".", 1)[0], classified, name)


class BrowserUnitTest(unittest.TestCase):
    """macp-browser.service starts the logged-in Chrome (Xvfb + CDP) at boot, and
    systemd must never be the thing that kills it: `browser.sh up` backgrounds
    Chrome with setsid, which does not leave the unit's cgroup, so the default
    KillMode=control-group would SIGKILL the profile on any stop/restart. Chrome
    is stopped only through ExecStop = `browser.sh stop` (SIGTERM)."""

    def setUp(self):
        self.mod = _neutral_gen_units()
        self.units = _default_build(self.mod)
        self.unit = self.units["macp-browser.service"]
        self.dir = self.mod.BROWSER_DIR

    def test_browser_dir_defaults_under_home(self):
        self.assertEqual(self.dir, Path("/srv/neutral-home/.config/macp-browser"))

    def test_exec_start_runs_browser_sh_up(self):
        self.assertEqual(_exec_start(self.unit), f"/bin/bash {self.dir}/browser.sh up")
        self.assertIn(f"WorkingDirectory={self.dir}\n", self.unit)

    def test_stop_goes_through_browser_sh_stop_only(self):
        section = _service_section(self.unit)
        self.assertIn(f"ExecStop=/bin/bash {self.dir}/browser.sh stop\n", section)
        lines = [ln for ln in self.unit.splitlines() if not ln.startswith("#")]
        self.assertEqual(sum(ln.startswith("ExecStop=") for ln in lines), 1)
        for key in ("Restart=", "ExecStartPre=", "ExecStartPost=", "ExecStopPost=", "ExecReload="):
            self.assertFalse(any(ln.startswith(key) for ln in lines), key)
        self.assertFalse(any("pkill" in ln or "kill " in ln for ln in lines))

    def test_oneshot_remains_after_exit_with_killmode_process(self):
        section = _service_section(self.unit)
        for line in ("Type=oneshot", "RemainAfterExit=yes", "KillMode=process",
                     "TimeoutStartSec=1800"):
            self.assertIn(f"{line}\n", section, line)
        directives = [ln for ln in section.splitlines() if not ln.startswith("#")]
        self.assertEqual(sum(ln.startswith("KillMode=") for ln in directives), 1)
        self.assertNotIn("KillMode=none", self.unit)
        self.assertNotIn("KillMode=control-group", self.unit)

    def test_unit_section_after_network_and_condition(self):
        head = self.unit.split("[Service]", 1)[0]
        self.assertIn("After=network-online.target\n", head)
        self.assertIn(f"ConditionPathExists={self.dir}/browser.sh\n", head)

    def test_installed_at_boot(self):
        self.assertTrue(self.unit.endswith("[Install]\nWantedBy=default.target\n"))
        self.assertNotIn("macp-browser.timer", self.units)
        self.assertIn("macp-browser.service", self.mod.enable_targets(self.units, None))
        self.assertEqual(self.mod.enable_targets(self.units, ["macp-browser"]),
                         ["macp-browser.service"])

    def test_unit_carries_design_rationale_comment(self):
        comments = [ln for ln in self.unit.splitlines() if ln.startswith("#")]
        self.assertTrue(any("browser.sh stop" in ln for ln in comments), comments)
        # Comments live inside [Service], never on a directive line.
        self.assertTrue(all(c in _service_section(self.unit) for c in comments))

    def test_env_knob_overrides_dir(self):
        alt = _neutral_gen_units(MACP_BROWSER_DIR="/srv/alt-browser")
        unit = _default_build(alt)["macp-browser.service"]
        self.assertEqual(_exec_start(unit), "/bin/bash /srv/alt-browser/browser.sh up")
        self.assertIn("ExecStop=/bin/bash /srv/alt-browser/browser.sh stop\n", unit)
        self.assertIn("ConditionPathExists=/srv/alt-browser/browser.sh\n", unit)
        # Empty knob falls back to the home default (same as browser_watch).
        empty = _neutral_gen_units(MACP_BROWSER_DIR="")
        self.assertEqual(empty.BROWSER_DIR, self.dir)

    def test_same_in_installed_mode(self):
        self.assertEqual(self.mod.build(ENGINE_PY)["macp-browser.service"], self.unit)

    def test_not_restarted_on_code_change(self):
        # Maintenance restarts LONG_LIVED_LABELS units when code changes; listing
        # this unit there (or giving it a daemonctl label) would bounce Chrome.
        from todo_system import daemonctl, worker

        self.assertNotIn("macp-browser", set(daemonctl.UNITS.values()))
        restarted = {daemonctl.unit_name(label) for label in worker.LONG_LIVED_LABELS}
        self.assertNotIn("macp-browser", restarted)
        self.assertFalse(any("browser" in label for label in worker.LONG_LIVED_LABELS))
