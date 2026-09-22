import importlib.util
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("pantheon", Path(__file__).parents[1] / "pimp_my_pantheon.py")
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)


class FakeMac:
    def __init__(self, folder):
        self.folder = folder
        self.events = []
        self.before = dict(version="1.2.3", sha="a" * 40)
        self.after = dict(version="1.2.4", sha="b" * 40)
        self.running = True
        self.path = "/Hermes.app"
        self.reopened = []
        self.changed = False
        self.exit_code = 0
        self.receipt_outcome = "success"
        self.receipt_pid = 123
        self.fail_quit = False
        self.fail_reopen = False
        self.interrupt = False
        self.dirty = False
        self.receipt_steps = []

    def validate_cli(self):
        self.events.append("validate")

    def preflight(self):
        self.events.append("preflight")
        if self.dirty:
            raise p.Deferred("Local source changes")

    def identity(self):
        return self.after if self.changed else self.before

    def selected(self):
        return [dict(pid=44, path=self.path)] if self.running else []

    def quit(self, apps):
        self.events.append("quit")
        if self.fail_quit:
            raise p.Deferred("Still busy")
        self.running = False

    def preserve_revision(self, run_id):
        self.events.append("preserve")
        return "refs/pimp-my-pantheon/pre-update/" + run_id

    def update(self, log, pid_callback):
        self.events.append("update")
        self.pending = p.read(self.folder / "state/pending.json")
        pid_callback(123)
        if self.interrupt:
            raise KeyboardInterrupt
        self.changed = True
        p.save(self.folder / "hermes/logs/update_receipts/latest.json", dict(
            pid=self.receipt_pid, started_at=p.now(), finished_at=p.now(),
            outcome=self.receipt_outcome, post_update=self.after, steps=self.receipt_steps))
        log.write(b"updater output\n")
        return self.exit_code

    def reopen(self, path):
        self.events.append("reopen")
        self.reopened.append(path)
        if self.fail_reopen:
            raise RuntimeError("not healthy")
        self.running = True
        return dict(status="ready")


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.state = self.folder / "state"
        self.config = dict(hermes_home=str(self.folder / "hermes"), app="/Hermes.app",
                           notifications=True, retain_runs=2, schedule="00:00")
        self.host = FakeMac(self.folder)

    def run_job(self, **kwargs):
        return p.execute(self.config, self.state, host=self.host, **kwargs)

    def test_update_restores_and_verifies(self):
        result = self.run_job()
        self.assertEqual(result["outcome"], "updated")
        self.assertEqual(self.host.events, ["validate", "preflight", "quit", "preflight", "preserve", "update", "reopen"])
        self.assertEqual(self.host.pending["reopen"], "/Hermes.app")
        self.assertEqual(self.host.reopened, ["/Hermes.app"])
        self.assertFalse((self.state / "pending.json").exists())
        self.assertEqual(p.read(self.state / "last-run.json"), result)

    def test_installed_copy_is_updated_and_reopened(self):
        self.host.path = "/Applications/Hermes.app"
        self.assertEqual(self.run_job()["outcome"], "updated")
        self.assertEqual(self.host.pending, dict(app="/Hermes.app", reopen="/Applications/Hermes.app",
                                                 run_id=self.host.pending["run_id"]))
        self.assertEqual(self.host.reopened, ["/Applications/Hermes.app"])

    def test_already_current_is_truthful(self):
        self.host.after = self.host.before
        result = self.run_job()
        self.assertEqual(result["outcome"], "current")
        self.assertIn("already current", p.notification_text(result)[1])

    def test_closed_app_stays_closed(self):
        self.host.running = False
        self.assertEqual(self.run_job()["outcome"], "updated")
        self.assertNotIn("quit", self.host.events)
        self.assertNotIn("reopen", self.host.events)

    def test_dirty_source_never_quits(self):
        self.host.dirty = True
        self.assertEqual(self.run_job()["outcome"], "skipped")
        self.assertEqual(self.host.events, ["validate", "preflight"])

    def test_busy_quit_never_updates(self):
        self.host.fail_quit = True
        self.assertEqual(self.run_job()["outcome"], "skipped")
        self.assertNotIn("update", self.host.events)

    def test_failure_still_reopens(self):
        self.host.exit_code = 1
        self.assertEqual(self.run_job()["outcome"], "error")
        self.assertEqual(self.host.events[-1], "reopen")

    def test_interruption_still_reopens(self):
        self.host.interrupt = True
        self.assertEqual(self.run_job()["outcome"], "error")
        self.assertEqual(self.host.events[-1], "reopen")

    def test_failed_reopen_keeps_recovery_intent(self):
        self.host.fail_reopen = True
        self.assertEqual(self.run_job()["outcome"], "error")
        self.assertTrue((self.state / "pending.json").exists())

    def test_recovery_precedes_daily_dedup(self):
        self.run_job()
        p.save(self.state / "pending.json", dict(app="/Hermes.app"))
        self.host.events.clear()
        self.assertEqual(self.run_job(scheduled=True)["outcome"], "already_ran_today")
        self.assertEqual(self.host.events, ["reopen"])
        self.assertEqual(self.host.reopened[-1], "/Hermes.app")

    def test_recovery_reopens_the_copy_that_was_closed(self):
        self.run_job()
        p.save(self.state / "pending.json", dict(app="/Hermes.app", reopen="/Applications/Hermes.app"))
        self.run_job(scheduled=True)
        self.assertEqual(self.host.reopened[-1], "/Applications/Hermes.app")
        self.assertFalse((self.state / "pending.json").exists())

    def test_no_recovery_in_dry_run(self):
        p.save(self.state / "pending.json", dict(app="/Hermes.app"))
        self.run_job(dry_run=True)
        self.assertNotIn("reopen", self.host.events)
        self.assertTrue((self.state / "pending.json").exists())

    def test_daily_dedup_including_failures(self):
        self.host.exit_code = 1
        self.run_job()
        self.host.events.clear()
        with patch.object(p, "notify") as notice:
            self.assertEqual(self.run_job(scheduled=True, send_notice=True)["outcome"], "already_ran_today")
            notice.assert_not_called()
        self.assertEqual(self.host.events, [])

    def test_login_before_schedule_waits(self):
        p.save(self.state / "last-run.json", dict(local_day="2000-01-01", finished_at="old"))
        self.config["schedule"] = "23:59"
        with patch.object(p, "datetime") as clock:
            clock.now.return_value.astimezone.return_value.strftime.return_value = "08:00"
            self.assertEqual(self.run_job(scheduled=True)["outcome"], "not_due_yet")
        self.assertEqual(self.host.events, [])

    def test_preview_is_silent_and_non_mutating(self):
        with patch.object(p, "notify") as notice:
            result = self.run_job(dry_run=True, send_notice=True)
            notice.assert_not_called()
        self.assertEqual(result["outcome"], "eligible")
        self.assertNotIn("quit", self.host.events)
        self.assertNotIn("update", self.host.events)
        self.assertFalse((self.state / "last-run.json").exists())

    def test_partial_receipt_not_success(self):
        self.host.receipt_outcome = "partial"
        self.assertEqual(self.run_job()["outcome"], "error")

    def test_unrelated_receipt_not_success(self):
        self.host.receipt_pid = 999
        self.assertEqual(self.run_job()["outcome"], "error")

    def test_receipt_failed_step_not_success(self):
        self.host.receipt_steps = [dict(name="desktop_rebuild", ok=False)]
        self.assertEqual(self.run_job()["outcome"], "error")

    def test_notification_failure_preserves_update(self):
        with patch.object(p, "notify", return_value=dict(status="failed")) as notice:
            result = self.run_job(send_notice=True)
        self.assertEqual(result["outcome"], "updated")
        notice.assert_called_once()

    def test_disabled_notification(self):
        self.config["notifications"] = False
        with patch.object(p, "notify") as notice:
            self.run_job(send_notice=True)
            notice.assert_not_called()

    def test_retention(self):
        for _ in range(3):
            self.run_job()
        self.assertEqual(len(list((self.state / "runs").glob("*.json"))), 2)
        self.assertEqual(len(list((self.state / "runs").glob("*.log"))), 2)


class NativeBoundaryTests(unittest.TestCase):
    def test_process_timeout_reaps_child_group(self):
        with tempfile.TemporaryDirectory() as folder:
            pidfile = Path(folder) / "pid"
            script = "import subprocess,time,pathlib; p=subprocess.Popen(['sleep','60']); pathlib.Path({!r}).write_text(str(p.pid)); time.sleep(60)".format(str(pidfile))
            with self.assertRaises(subprocess.TimeoutExpired):
                p.command([sys.executable, "-c", script], timeout=0.5)
            pid = int(pidfile.read_text())
            status = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
            self.assertTrue(status.returncode != 0 or status.stdout.strip().startswith("Z"))

    def test_arguments_are_literal(self):
        text = '$(touch /tmp/never) `nope` "quotes"\nnewline'
        self.assertEqual(p.command([sys.executable, "-c", "import sys; print(sys.argv[1],end='')", text])[1], text)

    def test_lock_rejects_overlap(self):
        with tempfile.TemporaryDirectory() as folder:
            with p.locked(Path(folder)):
                with self.assertRaises(RuntimeError):
                    with p.locked(Path(folder)):
                        self.fail("overlap")

    def test_plist_daily_local_and_no_keepalive(self):
        data = p.make_plist(dict(schedule="09:00", path="/usr/bin:/bin"), "/python3")
        data = plistlib.loads(plistlib.dumps(data))
        self.assertEqual(data["StartCalendarInterval"], dict(Hour=9, Minute=0))
        self.assertNotIn("KeepAlive", data)
        self.assertTrue(data["RunAtLoad"])
        self.assertEqual(data["ProgramArguments"][0], "/python3")

    def test_setup_launcher_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            app = Path(folder) / "Hermes.app"
            p.atomic_write(app / "Contents/Info.plist", plistlib.dumps(dict(CFBundleIdentifier="com.nousresearch.hermes.setup")))
            with self.assertRaises(ValueError):
                p.app_info(app)

    def test_environment_not_inherited_from_agent_profile(self):
        config = dict(path="/bin", hermes_home="/correct")
        with patch.dict(os.environ, dict(HERMES_HOME="/wrong", HERMES_PROFILE="wrong", GIT_DIR="/wrong", PYTHONPATH="wrong")):
            env = p.environment(config)
        self.assertEqual(env["HERMES_HOME"], "/correct")
        self.assertNotIn("HERMES_PROFILE", env)
        self.assertNotIn("GIT_DIR", env)
        self.assertNotIn("PYTHONPATH", env)

    def test_real_git_preflight_dirty_custom_and_recovery_ref(self):
        with tempfile.TemporaryDirectory() as folder:
            repo = Path(folder)
            git, host = git_fixture(folder)
            with patch.object(p, "app_info"), patch.object(host, "running", return_value=[]):
                host.preflight()
                ref = host.preserve_revision("fixture")
                self.assertEqual(git("rev-parse", ref), git("rev-parse", "HEAD"))
                (repo / "file").write_text("local work")
                with self.assertRaises(p.Deferred):
                    host.preflight()
                self.assertEqual((repo / "file").read_text(), "local work")
                (repo / "file").write_text("clean")
                git("switch", "-c", "custom")
                with self.assertRaises(p.Deferred):
                    host.preflight()

    def test_real_git_preflight_open_apps(self):
        with tempfile.TemporaryDirectory() as folder:
            _, host = git_fixture(folder)
            copy = dict(pid=50, path="/Applications/Hermes.app")
            with patch.object(p, "app_info"), patch.object(host, "running", return_value=[copy]):
                with patch.object(host, "processes", return_value=app_processes(copy, host.repo)):
                    host.preflight()
                with patch.object(host, "processes", return_value=app_processes(copy, "/elsewhere/hermes-agent")):
                    with self.assertRaises(p.Deferred):
                        host.preflight()

    def test_installed_copy_needs_this_checkouts_backend(self):
        host = fixture_mac()
        copy = dict(pid=50, path="/Applications/Hermes.app")
        elsewhere = dict(pid=50, path="/Users/fixture/Desktop/Hermes.app")
        with patch.object(host, "running", return_value=[copy]):
            with patch.object(host, "processes", return_value=app_processes(copy, "/fixture/hermes-agent")):
                self.assertEqual(host.selected(), [copy])
            # A sibling checkout whose path shares this one's prefix is another installation.
            with patch.object(host, "processes", return_value=app_processes(copy, "/fixture/hermes-agent-dev")):
                self.assertEqual(host.selected(), [])
        with patch.object(host, "running", return_value=[elsewhere]), \
                patch.object(host, "processes", return_value=app_processes(elsewhere, "/fixture/hermes-agent")):
            self.assertEqual(host.selected(), [])

    def test_quit_targets_and_waits_for_the_open_copy(self):
        host = fixture_mac()
        copy = dict(pid=50, path="/Applications/Hermes.app")
        calls = []
        def fake_command(argv, **kwargs):
            calls.append(argv)
            return 0, json.dumps(dict(accepted=True))
        # Its backend is already gone while the app is still closing.
        with patch.object(p, "command", fake_command), patch.object(p.time, "sleep"), \
                patch.object(host, "processes", return_value=[]), \
                patch.object(host, "running", side_effect=[[copy], []]) as running:
            host.quit([copy])
        self.assertEqual(calls[0][-3:], ["quit", "/Applications/Hermes.app", "50"])
        self.assertEqual(running.call_count, 2)

    def test_reopen_refuses_apps_outside_the_installation(self):
        host = fixture_mac()
        with patch.object(p, "command") as command, patch.object(p, "app_info"):
            with self.assertRaises(RuntimeError):
                host.reopen("/Users/fixture/Desktop/Hermes.app")
            command.assert_not_called()


def shutil_which_git():
    import shutil
    return shutil.which("git")


def git_fixture(folder):
    def git(*args):
        return subprocess.run(["git", "-C", folder, *args], check=True, capture_output=True, text=True).stdout.strip()
    git("init", "-b", "main")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.invalid")
    (Path(folder) / "file").write_text("clean")
    git("add", ".")
    git("commit", "-m", "fixture")
    host = p.Mac(dict(checkout=folder, app=str(Path(folder) / "apps/desktop/release/mac-arm64/Hermes.app"),
                      path=os.environ["PATH"], hermes_home=folder, git=shutil_which_git()))
    return git, host


def fixture_mac():
    return p.Mac(dict(checkout="/fixture/hermes-agent", app="/fixture/hermes-agent/apps/desktop/release/mac-arm64/Hermes.app",
                      path="/bin", hermes_home="/fixture", git="/usr/bin/git", quit_timeout_seconds=5))


def app_processes(app, checkout):
    """ps rows for a desktop app and the backend it started from `checkout`."""
    return [(app["pid"], 1, app["path"] + "/Contents/MacOS/Hermes"),
            (app["pid"] + 1, app["pid"], "{}/venv/bin/python -m hermes_cli.main serve --host 127.0.0.1 --port 0".format(checkout))]


if __name__ == "__main__":
    unittest.main()
