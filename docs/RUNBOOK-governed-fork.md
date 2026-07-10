# Governed Fork Runbook

This runbook documents the fork, upstream, deploy, and override-protection
model for `asset-011-hermes-agent`, the estate's governed fork of
[NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent). It
was established by the N2WM satellite work (wo-20260709-N2WM, PRJ-2026-035)
on 2026-07-09 and is the canonical reference for all fork operations.

The session-close code fix deployed in the same N2WM window
(`db.end_session(session_id, end_reason)` in `_run_and_close()` finally,
`gateway/platforms/api_server.py` lines 4108-4119) is referenced here as
context only; it is not re-documented by this runbook.

---

## 1. Fork topology

The estate runs a **governed fork**, not a mirror or a clone. The fork
diverges from upstream only where estate overrides require it, and tracks
upstream deliberately (see section 2).

**Canonical remote set** (verified on the live host at
`~/.hermes/hermes-agent`):

| Remote | URL | Purpose |
|--------|-----|---------|
| `origin` | `https://github.com/drizzyenterprises/asset-011-hermes-agent.git` | The estate's fork. All pushes, PRs, and bot-authored commits go here. |
| `upstream` | `https://github.com/NousResearch/hermes-agent.git` | The original project. Fetch-only; used for governed fast-forwards. |
| `drizzy` | _(dropped)_ | A stale legacy remote that pointed at an older mirror. Removed during N2WM to eliminate confusion about which remote is authoritative. |

This is the canonical remote set. No additional remotes should be added
without updating this runbook. The `drizzy` remote was dropped because it
pointed at a stale mirror that could not serve as either an authoritative
source (that is `origin`) or an upstream reference (that is `upstream`).
Keeping it risked accidental pushes to the wrong remote or confusion during
sync operations.

[Source: N2WM satellite record, PRJ-2026-035 handoff, 2026-07-09]

---

## 2. Upstream-sync policy

Updates from `upstream` (`NousResearch/hermes-agent`) are **deliberate,
governed fast-forwards**. There is no auto-updater.

**What "governed" means in practice:**

1. **Backup-first.** Before any upstream sync, the live host checkout is
   backed up to `/home/ophelia/backups/` with a timestamped directory name.
   This ensures a clean rollback path if the fast-forward introduces
   incompatibility with estate overrides.

2. **Andre-gated.** The decision to sync upstream is made by Andre. No agent
   or automated process initiates an upstream sync on its own. The sync is
   scheduled as a work order, executed by an agent (typically Forge), and the
   gateway restart that finalizes the deploy is performed by Andre.

3. **Fast-forward only.** The fork's `main` branch fast-forwards to the
   upstream commit. If the fork has diverged (estate overrides on conflicting
   lines), the conflict is resolved manually, preserving the estate override.
   See section 4 for the override-protection model.

4. **No auto-updater.** There is no cron job, systemd timer, or background
   process that fetches or merges from `upstream` automatically. The absence
   of an auto-updater is intentional: it prevents unreviewed upstream changes
   from clobbering estate overrides or introducing behavioral regressions
   without explicit approval.

**Who runs the sync:** An agent (typically Forge) executes the git operations
under a work order. Andre reviews the diff, approves, and restarts the
gateway. The sync is never a silent or background operation.

[Source: N2WM satellite record, PRJ-2026-035 handoff, 2026-07-09]

---

## 3. Deploy

The running Hermes gateway is deployed from the `asset-011-hermes-agent` fork
on the live host at `~/.hermes/hermes-agent/`. Deployment is a manual,
Andre-gated process:

1. **Backup-first.** The current live checkout is copied to
   `/home/ophelia/backups/` with a timestamped directory name before any
   changes are applied.

2. **Host-apply.** The target commit is applied to the live checkout. For a
   clean fast-forward, this is `git fetch origin && git checkout main && git
   merge --ff-only origin/main`. For a feature-branch deploy (e.g., a fix
   that has not yet been merged to `main`), the branch is checked out
   directly.

3. **Andre restarts.** After the host checkout is updated, Andre restarts the
   gateway. The restart is never performed by an agent without explicit
   approval. The gateway process PID is recorded as proof of restart.

### Worked example: N2WM fast-forward (2026-07-09)

The N2WM satellite work is the reference example of a clean governed deploy:

- **Operation:** Fast-forward from `7beafbe3b` to `ef5adf957` (2 files
  changed, +295 lines).
- **Content:** The session-close fix (`db.end_session` stamp in
  `_run_and_close()` finally block, `gateway/platforms/api_server.py` +15)
  and its accompanying test suite (`tests/gateway/test_run_end_session_stamp.py`,
  +280, newly added). The fork-override protection (remote re-point) was
  performed as a host-level git-remote operation and produced no committed
  file; it is documented in sections 1 and 4, not here.
- **HEAD after deploy:** `ef5adf957`.
- **Gateway restart:** Andre restarted the gateway. New PID: `2142025`.
- **Backup:** A timestamped backup was taken before the fast-forward.

This deploy was a clean fast-forward: no conflicts, no override clobbering.
The two files changed were the fix and its test; no estate override files
were touched.

[Source: N2WM satellite record, PRJ-2026-035 handoff, 2026-07-09. Acceptance
(c) confirmed: re-point holds, HEAD `ef5adf957`, gateway restarted PID
2142025.]

---

## 4. Override-protection

The fork carries **local estate overrides** that must survive upstream syncs.
These overrides are the reason the fork exists and the reason auto-update is
disabled.

**What the overrides are:**

Estate overrides are modifications to the hermes-agent codebase that adapt it
to the estate's infrastructure, agent profiles, governance model, and
operational requirements. They include things like:

- Agent profile configurations and SOUL files.
- Gateway API server customizations (e.g., the session-close fix deployed in
  N2WM).
- Estate-specific platform integrations and connector configurations.

**Why auto-update stays off:**

If an auto-updater were enabled, an upstream commit that touches the same
lines as an estate override would silently clobber the override during an
unattended fast-forward. The result would be a running gateway that appears
to be on the latest upstream version but has lost estate-specific behavior,
potentially breaking agent profiles, governance gates, or integrations
without any visible error.

The `drizzy` remote was dropped for the same reason: a stale remote that
points at an outdated mirror introduces ambiguity about which remote is
authoritative, increasing the risk of an accidental push or pull from the
wrong source.

**How to reconcile an upstream change without clobbering overrides:**

1. **Fetch upstream** (`git fetch upstream`) without merging.
2. **Diff against HEAD** (`git diff HEAD...upstream/main`) to identify which
   files and lines upstream has changed.
3. **Cross-reference with estate overrides.** If upstream touches a file that
   carries estate overrides, the conflict must be resolved manually:
   - Apply the upstream change.
   - Re-apply the estate override on top.
   - Verify the override is intact (`git diff` to confirm the override lines
     are present).
4. **Test before deploy.** Run the test suite against the reconciled tree.
5. **Backup, host-apply, Andre restarts** (per section 3).

This manual reconciliation is the core of the governed fork model: upstream
changes are welcome, but they are reviewed and reconciled against estate
overrides before they reach the running gateway. The model trades automation
for safety, and that trade is intentional.

[Source: N2WM satellite record, PRJ-2026-035 handoff, 2026-07-09. The N2WM
deploy is the worked example: clean fast-forward `7beafbe3b` to `ef5adf957`
with no override conflicts, precisely because no auto-updater had introduced
unreconciled upstream changes.]
