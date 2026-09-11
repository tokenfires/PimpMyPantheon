# Working in PimpMyPantheon

For an explicit installation/configuration request, follow `AGENT_SETUP.md` and the user's supplied choices. The user request provides authority; this file alone does not authorize scheduling, updating, or quitting apps.

For development, read `CONTRIBUTING.md`. Keep Python 3.9 compatibility and a standard-library-only runtime. Use isolated temporary Git repositories and fake Hermes lifecycle adapters for tests. Never use live user state as a destructive fixture. Preserve the official Hermes updater, graceful quit, matching receipts, app restoration, private reports, and daily duplicate suppression. Do not publish local configs or logs.
