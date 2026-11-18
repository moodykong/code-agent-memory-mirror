# This script stays the same when the project settings change: what to install (companion repos, python,
# setup_commands) is read from projects/<project>/env.json, and the agent-mirror version from store.json,
# on the store's default branch. A later change there takes effect at the next session start, which pulls
# the store, moves agent-mirror to the pinned ref and runs the changed setup steps again (drift repair);
# it does that at every session start until the vendor rebuilds its cached machine. To make the vendor
# rebuild it, raise the revision number above and save.
