# PimpMyPantheon

**Daily divine maintenance. No mortal intervention required.**

A small macOS utility that closes Hermes Desktop, runs `hermes update --yes --keep-stash`, and brings the app back. A notification tells you which Hermes version and code revision you have—or whether something needs your attention.

<p><img src="docs/images/macos-notification.png" width="560" alt="PimpMyPantheon macOS notification: Hermes updated to v0.21.2 (31d0a242)"></p>

*A real notification after a completed update and desktop readiness check.*

The gods may take weekends off. Your updater doesn't.

## Quick start

Requires macOS, Python 3.9+, Git, and an existing **local Git installation of [Hermes Agent](https://github.com/NousResearch/hermes-agent)** with its built Desktop app. Hermes must support `update --yes`, `--keep-stash`, `--plan`, and structured update receipts. This is for a normal `main` installation, not a development checkout, container, package-managed install, or a desktop connected only to a remote server.

```sh
git clone https://github.com/tokenfires/PimpMyPantheon.git
cd PimpMyPantheon
python3 pimp_my_pantheon.py install --at 09:00
```

Installation starts the first update immediately. Thereafter, macOS runs it daily at **9:00 a.m. local time**. Choose a time when restarting Hermes is convenient.

No cloud account, API key, subscription, running AI agent, or extra Python packages are needed for this utility. Hermes retains its own dependencies and existing authentication.

Prefer to hand setup to an agent? Give **any assistant with local terminal access** this repository and ask:

> Set up PimpMyPantheon on this Mac. Follow AGENT_SETUP.md, discover my local Hermes installation, and schedule it for 9:00 a.m. local time. Verify the first update and that Hermes reopens if it was running.

Repeat on each Mac. Each machine owns its schedule; there is no central controller and nothing to send to another agent.

## What happens each day

1. Check the configured Hermes command and installation. Skip modified source files, custom branches, a detached checkout that isn't a Hermes stable release, unfinished Git operations, hidden-change index flags, another active Hermes updater, or a Desktop app from a different Hermes installation.
2. If Hermes Desktop is open, whether from the configured build or the copy Hermes installs in `/Applications`, request a **normal quit** and wait for it and its owned backend to exit. Hermes's active-work confirmation is respected. If it stays open, skip the update; never click “Quit Anyway” or force-kill the app.
3. Preserve the starting Git revision under `refs/pimp-my-pantheon/pre-update/…`, then run Hermes's own unattended updater. Hermes manages dependencies, configuration migrations, its configured state backups, desktop rebuilding, and gateway restarts.
4. Require a successful exit and a fresh Hermes receipt belonging to this exact updater process, matching the resulting code revision. Failed or partial update steps are reported as needing attention.
5. If the app was open, reopen the same copy—even after an update error—and check its local backend's `/api/health` response and version. If it was closed, leave it closed.
6. Show one macOS notification with the result. A version such as `v0.21.1` identifies the release; the short commit ID identifies the particular code revision, since several daily updates can share one version number.

The job makes **one scheduled attempt per local calendar day**, including skipped or failed attempts. Login can catch up after the scheduled time. Logging in before that time waits for the calendar event. Sleep can delay a run until wake; the utility does not wake or power on your Mac. Manual `run` explicitly retries, even on the same day. There is no retry loop or `KeepAlive` job.

## Commands

```sh
# Inspect schedule, configuration, latest result, and interrupted-run recovery
python3 pimp_my_pantheon.py status

# Preview eligibility: no quit, update, recovery, or notification
python3 pimp_my_pantheon.py run --dry-run

# Update now and display the outcome
python3 pimp_my_pantheon.py run --notify

# Replay the previous real notification without another update
python3 pimp_my_pantheon.py notify

# Change the time; preserves existing installation choices
python3 pimp_my_pantheon.py install --replace --at 08:30

# Disable automatic notifications
python3 pimp_my_pantheon.py install --replace --no-notifications

# Remove only this utility's schedule
python3 pimp_my_pantheon.py uninstall
```

Notifications use the built-in macOS script sender, which may appear as **Script Editor** in System Settings → Notifications. Focus and notification preferences determine whether a banner appears. A successful submission does not prove you saw it. A notification failure does not turn a successful Hermes update into a failed one. To re-enable automatic notices, set `notifications` to `true` in the installed `config.json`.

## Installation paths

Default discovery looks for `hermes` on PATH, `~/.hermes/hermes-agent`, and a single built `Hermes.app` under its `apps/desktop/release/mac*` folder. `HERMES_HOME` is honored during initial installation. For another layout:

```sh
python3 pimp_my_pantheon.py install --at 09:00 \
  --hermes /absolute/path/to/hermes \
  --hermes-home /absolute/path/to/hermes-home \
  --checkout /absolute/path/to/hermes-agent \
  --app /absolute/path/to/built/Hermes.app
```

Configure the built desktop app inside the checkout's `apps/desktop/release/` folder. Setup verifies that the command reports the configured installation and that the app has the Hermes Desktop bundle identifier. Multiple candidate builds require an explicit `--app`.

`/Applications/Hermes.app` can be Hermes's **setup launcher**, which has a different bundle identifier and is ignored. Current Hermes updates also copy the rebuilt app over an existing `/Applications/Hermes.app` or `~/Applications/Hermes.app`, so Finder, the Dock and Spotlight open that copy. PimpMyPantheon treats that copy as part of the configured installation only when its backend runs from the configured checkout. Hermes refreshes the copy while it is closed for the update, and the copy is reopened afterwards. A Hermes Desktop app open from any other location, or backed by another checkout, still skips the day's update.

This utility runs as your normal macOS user. **Do not use sudo.** It uses a per-user LaunchAgent, with a stable Python executable path and an explicit command search path. It does not modify your shell startup files or create an AI automation. It supports one Hermes installation per macOS user. Hermes's updater can refresh gateways across profiles sharing that installation; profile-specific configuration remains under Hermes's control.

| Item | Location |
| --- | --- |
| Installed code and configuration | `~/Library/Application Support/PimpMyPantheon/` |
| Last result | `~/Library/Application Support/PimpMyPantheon/last-run.json` |
| Update logs and reports, last 30 runs | `~/Library/Application Support/PimpMyPantheon/runs/` |
| Launcher errors | `~/Library/Logs/PimpMyPantheon/launcher-error.log` |
| Schedule | `~/Library/LaunchAgents/com.tokenfires.pimpmypantheon.plist` |
| Hermes's own update receipts | `<hermes-home>/logs/update_receipts/` |

Logs and configuration are private local files. **Do not publish them:** Hermes's own output can include machine paths, profile names, or sensitive diagnostics. The normal success notification contains only the version and commit ID.

## Hermes channels and versions

Official Hermes checkouts follow the **stable** channel by default: `hermes update` checks out the exact commit of the latest `vX.Y.Z` release, so Git shows a detached HEAD. That is Hermes working as designed, and PimpMyPantheon updates it when HEAD sits exactly on a stable release tag and `hermes --version` names the stable channel. A checkout on `main` (`hermes update --set-channel main`) is updated too. Any other branch or detached commit is left for you to review.

PimpMyPantheon reads the Hermes version from the `install-stamp.json` that Hermes keeps beside its code (since September 2026), the same version its `/api/health` endpoint reports. Older checkouts fall back to `hermes_cli/__init__.py`. `hermes --version` also runs Hermes's own update check, so the utility allows it two minutes.

## Limits and recovery

- This wraps the official update procedure; it is not an independent package manager or transactional rollback system. Installing new upstream code also runs Hermes's dependency/build/migration routines. Use it on an installation you already trust to update unattended.
- Source files must be clean. Committed changes on `main` are subject to Hermes's normal branch reconciliation, which can replace divergent commits. The utility preserves the old revision in a separate recovery ref before every attempt. Those refs are retained until you deliberately remove them; they are **code recovery anchors, not backups of sessions, credentials, dependencies, or model files**. Existing Hermes backup settings remain in effect.
- “Ready” means a newly opened app has a local backend answering the health check with the installed version. It does not prove every model provider, channel, remote connection, tool, or conversation works. Gateway restart outcomes come from Hermes's update receipt.
- If Hermes asks whether to quit active work, the job waits up to 45 seconds, then skips. You may still see the normal Hermes confirmation dialog. Choose “Keep Running” and retry later. It does not poll or inspect your conversations to guess whether they are busy.
- Update commands have a 30-minute limit; reopening has a two-minute readiness budget. These and report retention are configurable in `config.json`. A timed-out update is stopped and reported as needing attention; review the update log before retrying. The wrapper never force-kills the desktop app.
- If the wrapper is interrupted after requesting a quit, `pending.json` records that the app needs restoring. The next real run attempts that restoration before the daily duplicate check. An abrupt shutdown, force-kill, damaged install, or an unlaunchable app can still require manual recovery. Nothing can reopen while the Mac is off.
- A failed update can leave Hermes partially upgraded. Review its receipt and logs; follow [Hermes's update and recovery documentation](https://hermes-agent.nousresearch.com/docs/getting-started/updating). This utility does not reset your checkout or restore session databases automatically.

Uninstall preserves Hermes, reports, configuration, the runtime copy, and recovery refs. Pulling a newer copy of **this repository** does not silently change the installed job: inspect the new code, then use `install --replace`. Replacement refuses while a run is active or interrupted recovery is pending. A reinstall starts a run only if today's scheduled attempt has not already completed and the run is due.

## Development

One Python file, standard library only. Run:

```sh
python3 -m unittest discover -s tests -v
```

Tests cover update/reopen sequencing, installed-copy ownership, failures and interruption, receipt matching, notification behavior, once-per-day scheduling, retention, literal argument handling, process-group cleanup, and real temporary Git repositories. Native Desktop health and notification visibility require a real macOS smoke test. CI runs on macOS with Python 3.9 and 3.14.

See [CONTRIBUTING.md](CONTRIBUTING.md). MIT licensed. Independent community utility; not affiliated with Nous Research.
