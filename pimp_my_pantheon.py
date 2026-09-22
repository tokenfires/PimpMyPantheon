#!/usr/bin/env python3
"""A daily tune-up for Hermes Desktop on macOS. No agent on duty required."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid

VERSION = "1.0.1"
LABEL = "com.tokenfires.pimpmypantheon"
STATE = Path.home() / "Library/Application Support/PimpMyPantheon"
CONFIG = STATE / "config.json"
PLIST = Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")
LOGS = Path.home() / "Library/Logs/PimpMyPantheon"
BUNDLE_ID = "com.nousresearch.hermes"
# `hermes update` copies its rebuilt app over these, so Finder, the Dock and Spotlight launch them.
INSTALLED_COPIES = ("/Applications/Hermes.app", str(Path.home() / "Applications/Hermes.app"))
OPERATIONS = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "BISECT_LOG",
              "rebase-merge", "rebase-apply", "sequencer", "index.lock", "HEAD.lock",
              "config.lock", "shallow.lock", "packed-refs.lock")


def now():
    return datetime.now(timezone.utc).isoformat()


def local_day():
    return datetime.now().astimezone().date().isoformat()


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name, dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save(path, value):
    atomic_write(path, (json.dumps(value, indent=2) + "\n").encode())


def read(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return default


@contextmanager
def locked(state):
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state / "run.lock").open("a+") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another PimpMyPantheon run is active")
        yield


class Deferred(RuntimeError):
    """Leave this installation alone until the user reviews it."""


def command(argv, timeout=30, env=None, check=True, output=None, pid_callback=None):
    """No shell; bounded subprocess and its children; raw updater output stays local."""
    with tempfile.TemporaryFile() as capture:
        destination = output if output is not None else capture
        process = subprocess.Popen([str(a) for a in argv], stdin=subprocess.DEVNULL,
                                   stdout=destination, stderr=subprocess.STDOUT,
                                   env=env, start_new_session=True)
        try:
            if pid_callback:
                pid_callback(process.pid)
            code = process.wait(timeout=timeout)
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            except ProcessLookupError:
                pass
            finally:
                # A child can ignore TERM after its parent exits.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            raise
        capture.seek(0)
        text = capture.read().decode("utf-8", "replace")
        if check and code:
            # Keep command diagnostics out of banners and reports (may contain secrets).
            raise RuntimeError("{} failed (exit {}); see the local log".format(Path(argv[0]).name, code))
        return code, text


def environment(config):
    env = dict(os.environ)
    for key in list(env):
        if key.startswith(("GIT_", "HERMES_")) or key in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
            del env[key]
    env.update(PATH=config["path"], HERMES_HOME=config["hermes_home"],
               PYTHONUNBUFFERED="1", PYTHONUTF8="1", GIT_TERMINAL_PROMPT="0",
               GIT_ASKPASS="/usr/bin/false", SSH_ASKPASS="/usr/bin/false",
               GCM_INTERACTIVE="never", GIT_MERGE_AUTOEDIT="no",
               GIT_SSH_COMMAND="ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=15")
    return env


JXA = r'''
ObjC.import('AppKit');
function run(argv) {
  var apps=$.NSWorkspace.sharedWorkspace.runningApplications, result=[];
  for(var i=0; i<apps.count; i++) {
    var a=apps.objectAtIndex(i);
    if(ObjC.unwrap(a.bundleIdentifier)!=='com.nousresearch.hermes') continue;
    var path=ObjC.unwrap(a.bundleURL.path), pid=Number(a.processIdentifier);
    if(argv[0]==='quit' && path===argv[1] && pid===Number(argv[2])) {
      return JSON.stringify({accepted:Boolean(a.terminate)});
    }
    result.push({pid:pid,path:path});
  }
  if(argv[0]==='quit') return JSON.stringify({accepted:false});
  return JSON.stringify(result);
}
'''


def app_info(path):
    with (Path(path) / "Contents/Info.plist").open("rb") as stream:
        info = plistlib.load(stream)
    if info.get("CFBundleIdentifier") != BUNDLE_ID:
        raise ValueError("Select the built Hermes Desktop app, not the Hermes setup launcher")
    executable = Path(path) / "Contents/MacOS" / info["CFBundleExecutable"]
    if not executable.is_file():
        raise ValueError("Hermes Desktop executable is missing")
    return info


class Mac:
    def __init__(self, config):
        self.config = config
        self.env = environment(config)
        self.repo = Path(config["checkout"]).resolve()
        self.app = str(Path(config["app"]).resolve())

    def git(self, *args):
        return command([self.config["git"], "-c", "core.fsmonitor=false", "-C", self.repo, *args],
                       env=self.env)[1].strip()

    def identity(self):
        text = (self.repo / "hermes_cli/__init__.py").read_text()
        match = re.search(r'^__version__\s*=\s*[\"\']([^\"\']+)', text, re.M)
        if not match:
            raise RuntimeError("Cannot read the Hermes version")
        return dict(version=match[1], sha=self.git("rev-parse", "HEAD"))

    def running(self):
        return json.loads(command(["/usr/bin/osascript", "-l", "JavaScript", "-e", JXA, "list"])[1])

    def owns(self, app):
        """The configured build, or an installed copy whose backend runs from this checkout."""
        return app["path"] == self.app or (app["path"] in INSTALLED_COPIES and
                                           bool(self.backends([app["pid"]])))

    def selected(self):
        # Hermes Desktop holds a single-instance lock, so at most one copy is open.
        return [a for a in self.running() if self.owns(a)]

    def preflight(self):
        app_info(self.app)
        if not Path(self.app).is_relative_to(self.repo / "apps/desktop/release"):
            raise Deferred("The selected app is outside this Hermes checkout's desktop release folder")
        if self.git("rev-parse", "--show-toplevel") != str(self.repo):
            raise Deferred("The configured Hermes checkout is not a repository root")
        if self.git("branch", "--show-current") != "main":
            raise Deferred("Hermes is on a custom or detached branch; review manually")
        if self.git("status", "--porcelain=v1", "--untracked-files=normal", "--ignore-submodules=none"):
            raise Deferred("Hermes source has local changes; review manually")
        for name in OPERATIONS:
            path = Path(self.git("rev-parse", "--git-path", name))
            if not path.is_absolute():
                path = self.repo / path
            if path.exists():
                raise Deferred("Hermes has an unfinished Git operation or lock")
        flags = self.git("ls-files", "-v")
        if any(line and (line[0].islower() or line[0] == "S") for line in flags.splitlines()):
            raise Deferred("Hermes has Git index flags that can hide local changes")
        marker = Path(self.config["hermes_home"]) / ".hermes-update-in-progress"
        if marker.exists():
            try:
                pid = int(marker.read_text().splitlines()[0])
                if pid <= 0:
                    raise ValueError("invalid PID")
                os.kill(pid, 0)
            except ProcessLookupError:
                pass  # Hermes owns stale-marker cleanup, not this utility.
            except (ValueError, IndexError, PermissionError):
                raise Deferred("Hermes update marker needs review")
            else:
                raise Deferred("Another Hermes updater is running")
        if not all(self.owns(a) for a in self.running()):
            raise Deferred("Another Hermes Desktop installation is open")

    def validate_cli(self):
        help_text = command([self.config["hermes"], "update", "--help"], env=self.env)[1]
        for flag in ("--yes", "--plan", "--keep-stash"):
            if flag not in help_text:
                raise ValueError("Upgrade Hermes manually first: required update options are missing")
        version = command([self.config["hermes"], "--version"], env=self.env)[1]
        match = re.search(r"^Install directory:\s*(.+)$", version, re.M)
        if not match or Path(match[1].strip()).resolve() != self.repo:
            raise ValueError("The Hermes command does not report the configured installation")

    def processes(self):
        rows = command(["/bin/ps", "-axo", "pid=,ppid=,args="])[1]
        result = []
        for row in rows.splitlines():
            parts = row.strip().split(None, 2)
            if len(parts) == 3:
                result.append((int(parts[0]), int(parts[1]), parts[2]))
        return result

    def backends(self, app_pids):
        rows = self.processes()
        descendants = set(app_pids)
        for _ in range(10):
            more = {pid for pid, parent, _ in rows if parent in descendants}
            if more <= descendants:
                break
            descendants |= more
        return [pid for pid, _, argv in rows if pid in descendants and
                str(self.repo) + os.sep in argv and "hermes_cli" in argv and " serve " in argv]

    def quit(self, apps):
        paths = {a["path"] for a in apps}
        children = set(self.backends([a["pid"] for a in apps]))
        for app in apps:
            result = json.loads(command(["/usr/bin/osascript", "-l", "JavaScript", "-e", JXA,
                                         "quit", app["path"], str(app["pid"])])[1])
            if not result["accepted"]:
                raise Deferred("Hermes declined the quit request")
        deadline = time.monotonic() + self.config["quit_timeout_seconds"]
        while time.monotonic() < deadline:
            remaining = {pid for pid, _, _ in self.processes()}
            if not any(a["path"] in paths for a in self.running()) and not (children & remaining):
                return
            time.sleep(1)
        raise Deferred("Hermes is still open or busy; update skipped without force-quitting")

    def preserve_revision(self, run_id):
        reference = "refs/pimp-my-pantheon/pre-update/" + run_id
        self.git("update-ref", reference, "HEAD")
        return reference

    def update(self, log, pid_callback):
        return command([self.config["hermes"], "update", "--yes", "--keep-stash"],
                       timeout=self.config["update_timeout_seconds"], env=self.env,
                       check=False, output=log, pid_callback=pid_callback)[0]

    def reopen(self, path):
        if path != self.app and path not in INSTALLED_COPIES:
            raise RuntimeError("Refusing to open an app outside this Hermes installation")
        app_info(path)
        command(["/usr/bin/open", "-g", "-a", path])
        deadline = time.monotonic() + self.config["startup_timeout_seconds"]
        # Never send ambient proxy credentials to a local health probe.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        while time.monotonic() < deadline:
            apps = [a for a in self.running() if a["path"] == path]
            for pid in self.backends([a["pid"] for a in apps]):
                _, ports = command(["/usr/sbin/lsof", "-nP", "-a", "-p", str(pid),
                                    "-iTCP", "-sTCP:LISTEN", "-Fn"], check=False)
                for port in re.findall(r"^n127\.0\.0\.1:(\d+)$", ports, re.M):
                    try:
                        with opener.open("http://127.0.0.1:" + port + "/api/health", timeout=3) as reply:
                            health = json.load(reply)
                        if health.get("ok") is True and health.get("version") == self.identity()["version"]:
                            return dict(status="ready", app=path, pid=apps[0]["pid"], backend_pid=pid,
                                        version=health["version"])
                    except (OSError, ValueError):
                        pass
            time.sleep(2)
        raise RuntimeError("Hermes was reopened but its local backend did not pass the readiness check")


def receipt_result(receipt, pid, started, identity):
    if not isinstance(receipt, dict) or receipt.get("pid") != pid:
        raise RuntimeError("No matching Hermes update receipt; review the local update log")
    if not receipt.get("finished_at") or receipt.get("started_at", "") < started:
        raise RuntimeError("Hermes update receipt is incomplete or stale")
    if receipt.get("outcome") != "success":
        raise RuntimeError("Hermes reported an incomplete or unsuccessful update")
    if receipt.get("post_update", {}).get("sha") != identity["sha"]:
        raise RuntimeError("Hermes receipt does not match the installed revision")
    failed = [s.get("name", "unknown") for s in receipt.get("steps", [])
              if s.get("ok") is False and s.get("name") != "pre_update_backup"]
    restart = receipt.get("gateway_restart", {})
    if failed or restart.get("incomplete") or restart.get("failed_units") or restart.get("phase_error"):
        raise RuntimeError("Hermes reported unfinished update steps; review its receipt")
    return dict(outcome="success", finished_at=receipt["finished_at"],
                receipt_pid=pid, failed_steps=failed)


def notification_text(report):
    outcome = report["outcome"]
    identity = report.get("after") or report.get("before") or {}
    build = "v{} ({})".format(identity.get("version", "?"), identity.get("sha", "unknown")[:8])
    title = "PimpMyPantheon"
    if outcome == "updated":
        body = "Hermes updated to " + build
    elif outcome == "current":
        body = "Hermes already current: " + build
    elif outcome == "skipped":
        body = "Hermes update skipped. " + report["reason"]
    else:
        body = "Hermes update needs attention. See the local report."
    return title, body


def notify(report):
    title, body = notification_text(report)
    script = "on run argv\n display notification (item 2 of argv) with title (item 1 of argv)\nend run"
    try:
        command(["/usr/bin/osascript", "-e", script, title, body], timeout=10)
        return dict(status="submitted", title=title, body=body, at=now())
    except (OSError, subprocess.SubprocessError, RuntimeError):
        return dict(status="failed", reason="macOS did not accept the notification")


def execute(config, state=STATE, scheduled=False, dry_run=False, send_notice=False, host=None):
    host = host or Mac(config)
    state = Path(state)
    with locked(state):
        pending = read(state / "pending.json")
        if pending and not dry_run:
            # Interrupted runs may have closed the app. Restore it before a new update.
            if pending.get("app") != config["app"]:
                raise RuntimeError("An interrupted run belongs to a different app; review pending.json")
            host.reopen(pending.get("reopen", config["app"]))
            (state / "pending.json").unlink()
        previous = read(state / "last-run.json", {})
        if scheduled and previous.get("local_day") == local_day() and previous.get("finished_at"):
            return dict(outcome="already_ran_today")
        if scheduled and previous and datetime.now().astimezone().strftime("%H:%M") < config["schedule"]:
            return dict(outcome="not_due_yet")
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        report = dict(schema=1, run_id=run_id, started_at=now(), local_day=local_day(),
                      outcome="running", dry_run=dry_run)
        reopen_path = None
        log_path = state / "runs" / (run_id + ".log")
        try:
            host.validate_cli()
            host.preflight()
            report["before"] = host.identity()
            apps = host.selected()
            report["app_was_running"] = bool(apps)
            if dry_run:
                report["outcome"] = "eligible"
            else:
                if apps:
                    # Write intent before quitting so an abrupt interruption is recoverable.
                    # Reopen the copy the user was running, which may be an installed copy.
                    save(state / "pending.json", dict(app=config["app"], reopen=apps[0]["path"], run_id=run_id))
                    reopen_path = apps[0]["path"]
                    host.quit(apps)
                host.preflight()
                report["recovery_ref"] = host.preserve_revision(run_id)
                log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                report["log"] = str(log_path)
                report["update_started_at"] = now()
                with log_path.open("wb") as log:
                    def remember_pid(pid):
                        report["update_pid"] = pid
                        save(state / "progress.json", report)
                    report["exit_code"] = host.update(log, remember_pid)
                report["after"] = host.identity()
                if report["exit_code"]:
                    raise RuntimeError("hermes update exited {}; see the local log".format(report["exit_code"]))
                receipt = read(Path(config["hermes_home"]) / "logs/update_receipts/latest.json")
                report["hermes_receipt"] = receipt_result(receipt, report["update_pid"],
                                                          report["update_started_at"], report["after"])
                report["outcome"] = "updated" if report["before"] != report["after"] else "current"
        except Deferred as error:
            report.update(outcome="skipped", reason=str(error))
        except KeyboardInterrupt:
            report.update(outcome="error", reason="Update interrupted; review the local log")
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            report.update(outcome="error", reason=str(error))
        finally:
            if reopen_path:
                try:
                    report["reopen"] = host.reopen(reopen_path)
                    (state / "pending.json").unlink(missing_ok=True)
                except Exception:
                    report.update(outcome="error", reason="Hermes could not be verified after reopening; review the local log")
                    report["reopen"] = dict(status="unverified")
        report["finished_at"] = now()
        if not dry_run and send_notice and config["notifications"]:
            report["notification"] = notify(report)
        if dry_run:
            save(state / "last-preview.json", report)
        else:
            save(state / "runs" / (run_id + ".json"), report)
            save(state / "last-run.json", report)
            save(state / "progress.json", report)
            for old in sorted((state / "runs").glob("*.json"), reverse=True)[config["retain_runs"]:]:
                old.with_suffix(".log").unlink(missing_ok=True)
                old.unlink()
        return report


def load_config(path):
    config = read(path)
    if not isinstance(config, dict):
        raise ValueError("No configuration found; run install first")
    for key in ("checkout", "hermes_home", "app", "hermes", "git", "path", "schedule"):
        if not isinstance(config.get(key), str) or not config[key]:
            raise ValueError("Missing configuration: " + key)
    for key in ("checkout", "hermes_home", "app", "hermes", "git"):
        if not Path(config[key]).is_absolute():
            raise ValueError(key + " must be an absolute path")
    for key, default in (("quit_timeout_seconds", 45), ("update_timeout_seconds", 1800),
                         ("startup_timeout_seconds", 120), ("retain_runs", 30)):
        value = config.setdefault(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 7200:
            raise ValueError("Invalid configuration: " + key)
    if not isinstance(config.setdefault("notifications", True), bool):
        raise ValueError("notifications must be true or false")
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", config["schedule"]):
        raise ValueError("Use a 24-hour schedule such as 09:00")
    return config


def launchctl(*args, check=True):
    return command(["/bin/launchctl", *args], check=check)


def make_plist(config, interpreter, state=STATE, logs=LOGS):
    hour, minute = map(int, config["schedule"].split(":"))
    return dict(Label=LABEL, ProgramArguments=[interpreter, str(state / "pimp_my_pantheon.py"), "scheduled-run"],
                StartCalendarInterval=dict(Hour=hour, Minute=minute), RunAtLoad=True,
                ProcessType="Background", LowPriorityIO=True, Nice=10, ExitTimeOut=180,
                EnvironmentVariables=dict(PATH=config["path"]), StandardOutPath="/dev/null",
                StandardErrorPath=str(logs / "launcher-error.log"))


def install(args):
    if sys.platform != "darwin" or os.geteuid() == 0:
        raise ValueError("Install on macOS as your normal user, without sudo")
    existing = read(CONFIG, {})
    if (existing or PLIST.exists()) and not args.replace:
        raise ValueError("Already installed; inspect status, then use install --replace")
    config = dict(existing)
    for key in ("hermes", "checkout", "hermes_home", "app"):
        value = getattr(args, key)
        if value:
            config[key] = str(Path(value).expanduser().absolute())
    config.setdefault("hermes_home", str(Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser().resolve()))
    config.setdefault("checkout", str(Path(config["hermes_home"]) / "hermes-agent"))
    config["checkout"] = str(Path(config["checkout"]).resolve())
    config.setdefault("hermes", shutil.which("hermes") or str(Path.home() / ".local/bin/hermes"))
    config.setdefault("git", shutil.which("git") or "/usr/bin/git")
    if "app" not in config:
        choices = list((Path(config["checkout"]) / "apps/desktop/release").glob("mac*/Hermes.app"))
        if len(choices) != 1:
            raise ValueError("Cannot choose the built app; supply --app /absolute/path/Hermes.app")
        config["app"] = str(choices[0].resolve())
    config["schedule"] = args.at or config.get("schedule", "09:00")
    config["app"] = str(Path(config["app"]).resolve())
    config.setdefault("notifications", True)
    if args.no_notifications:
        config["notifications"] = False
    config.setdefault("path", ":".join(dict.fromkeys([str(Path(config["hermes"]).parent),
        str(Path(sys.executable).parent), str(Path(config["hermes_home"]) / "node/bin"),
        str(Path.home() / ".local/bin"), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"])))
    with tempfile.TemporaryDirectory() as folder:
        temporary = Path(folder) / "config.json"
        save(temporary, config)
        config = load_config(temporary)
    host = Mac(config)
    host.validate_cli()
    host.preflight()
    with locked(STATE):
        if read(STATE / "pending.json"):
            raise RuntimeError("Resolve the interrupted run before replacing the installation")
        if PLIST.exists():
            launchctl("bootout", "gui/{}/{}".format(os.getuid(), LABEL), check=False)
        LOGS.mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_write(STATE / "pimp_my_pantheon.py", Path(__file__).read_bytes())
        save(CONFIG, config)
        atomic_write(PLIST, plistlib.dumps(make_plist(config, os.path.abspath(sys.executable))))
    launchctl("bootstrap", "gui/" + str(os.getuid()), str(PLIST))
    print("Installed PimpMyPantheon: daily at " + config["schedule"] + "; one attempt per local day.")
    print("The first run starts now. Reports: " + str(STATE))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=VERSION)
    sub = parser.add_subparsers(dest="action", required=True)
    setup = sub.add_parser("install", help="Install the daily local macOS job; starts the first run now")
    for option in ("hermes", "checkout", "hermes-home", "app", "at"):
        setup.add_argument("--" + option)
    setup.add_argument("--replace", action="store_true")
    setup.add_argument("--no-notifications", action="store_true")
    run = sub.add_parser("run", help="Run now, even if today's scheduled attempt is complete")
    run.add_argument("--config", type=Path, default=CONFIG)
    run.add_argument("--state-dir", type=Path, default=STATE)
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--notify", action="store_true")
    sub.add_parser("scheduled-run", help="Internal daily scheduler entry point")
    sub.add_parser("status", help="Show schedule and last result")
    sub.add_parser("notify", help="Replay the last result notification; no update")
    sub.add_parser("uninstall", help="Remove this schedule; keep Hermes and reports")
    args = parser.parse_args(argv)
    os.umask(0o077)
    def interrupted(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    try:
        if args.action == "install":
            install(args)
        elif args.action in ("run", "scheduled-run"):
            scheduled = args.action == "scheduled-run"
            config = load_config(CONFIG if scheduled else args.config)
            result = execute(config, STATE if scheduled else args.state_dir, scheduled,
                             False if scheduled else args.dry_run, scheduled or args.notify)
            print(json.dumps(result, indent=2))
            return 1 if result["outcome"] == "error" else 0
        elif args.action == "status":
            print(json.dumps(dict(config=read(CONFIG), last_run=read(STATE / "last-run.json"),
                                  pending=read(STATE / "pending.json")), indent=2))
            if sys.platform == "darwin":
                print(launchctl("print", "gui/{}/{}".format(os.getuid(), LABEL), check=False)[1])
        elif args.action == "notify":
            report = read(STATE / "last-run.json")
            if not report or not report.get("finished_at"):
                raise ValueError("No completed run to display")
            result = notify(report)
            print(json.dumps(result))
            return 0 if result["status"] == "submitted" else 1
        elif args.action == "uninstall":
            with locked(STATE):
                if read(STATE / "pending.json"):
                    raise RuntimeError("Resolve the interrupted run before uninstalling")
                launchctl("bootout", "gui/{}/{}".format(os.getuid(), LABEL), check=False)
                PLIST.unlink(missing_ok=True)
            print("Schedule removed. Hermes, configuration, reports, and recovery refs retained.")
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
