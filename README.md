# Git Sync Tool

The tool always excludes `.github/workflows/` from imported changes and preserves existing workflows on B.

Minimal Windows Tkinter app. Requires Python 3.10+ with Tkinter and Git on PATH; no third-party Python packages.

## Run

Double-click `run.bat`, or run:

```powershell
cd C:\laragon\www\git-sync-tool
python sync_tool.py
```

## Basic workflow

The **Source A — From** and **Source B — To** groups each contain Folder / Browse, URL and Branch. Changes flow from A's branch into B's branch. **Switch A ↔ B** swaps both sources, branches and folder paths, and clears the loaded list.

The **New commit message** field is optional:

- **Blank:** click Sync directly. The tool finds new A commits using the saved source history or the common A/B history, and applies them as separate commits on B. No selection or list load is required. Any highlighted rows are ignored in this case. Original messages, authors and author dates are retained; the committer is your configured Git identity. Applying commits on B can produce new hashes. Existing B commits and A's original commits are unchanged. Already-present changes and workflow-only commits do not create empty commits. Excluding workflows changes imported commit hashes/content; source commits are not modified.
- **Entered:** click **Load commits from A**, use Ctrl/Shift-click to select commits, enter the custom subject/body and click Sync. Only selected changes are combined into one new commit using your configured identity. Full source hashes are appended for traceability; this identifies the person doing the sync, not the original code writer. A custom message requires at least one selected commit.

There is no mode selector: the message field determines the behavior. When the selected B branch already exists, the first automatic sync needs common history, an existing sync record, or a previously saved baseline. If histories are unrelated and the initial range is unknown, explicitly select the initial commits and enter a message for the first sync. Automatic sync then handles newer commits separately. Older unselected commits before an already-synced source point are not automatically replayed; they can still be selected manually with a custom message.

B's branch is created and pushed automatically when absent, even if A has no new commits. It is based on B's server default branch when available; if that default is missing, the tool uses B's existing `main`, `master`, or only branch. For a new B branch with unrelated history, the first Sync imports a full source-code snapshot on top of B's base while preserving B's workflows; later Syncs copy new commits separately. Existing B branches are not overwritten. If a previously synced branch was deleted, its old state is backed up as `state-before-branch-recreation-<id>.json` and the requested branch is rebuilt. If B has several branches and none of these can identify a base safely, choose an existing B branch or set the server default.

If B has no branches yet, no manual initialization is required. With a blank message, the requested B branch is built by replaying the source first-parent history with workflows excluded. Separate nonempty code commits retain their original messages/authors/author dates, with your Git identity as committer. Original source commits containing workflows are never pushed directly. With a custom message, code before the oldest selected commit is imported as a workflow-free baseline snapshot, then selected code changes are combined into one new commit. If the source root itself is selected, the result is a new root commit without an internal helper parent. Changes outside the selection after the chosen base are not silently included. The destination repository must already exist and allow your account to push; this creates Git branches, not hosting-service repositories.

Both servers are fetched before syncing. Automatic mode uses first-parent deltas for merges; explicit selection of a merge commit still stops for a mainline decision. Rewritten histories and conflicts stop for review. If one commit in a batch conflicts, the whole batch is discarded in the private checkout and nothing is pushed. Pushes never force-update B, so a concurrent B update is rejected safely. A pending push is saved and recovered or stopped conservatively on retry; check the log before starting a new selection after a retry.

## Optional folders and latest server code

Only the exact folder root is checked; no child or parent directory search is performed. Source A remains a read-only convenience cache: if it has no Git metadata, its URL is used through the private cache. For Source B, a missing or empty folder is created/initialized automatically on Sync and connected to the entered B server URL. A preinitialized empty Git repo is also supported. If a nonempty B folder has files without Git metadata, those files are preserved and the server sync uses the private cache. Enter the server URL manually when Browse cannot infer one. Loading the commit list does not initialize B.

**Browse…** fills the corresponding URL if an `origin` or only remote is available. If no URL is found, it keeps your current URL and shows no popup. Browsing does not fetch or push. URLs remain manually editable.

When a valid folder is supplied, the tool fetches the latest server refs using that repo's Git/SSH/credential configuration, then imports the fetched objects into its private checkout. A new server commit on `dev` is visible even if the local `dev` branch is stale. Source A working files remain untouched. After a successful server sync, local B is refreshed from its server branch: a fresh/empty B folder is checked out and filled with code; a clean checked-out B target branch advances only by fast-forward. Uncommitted/ignored files and divergent local commits are preserved. If another local branch is currently checked out, the B target branch is made available without switching the working branch. Dedicated `refs/git-sync-tool/` and `refs/git-sync-tool-push/` refs hold fetched and outgoing objects. Push always uses B's configured URL. A server access failure stops the operation; stale local data is not substituted. If URL itself is a local path, that local repository is the authoritative source rather than a separate server.

Even when the server reports no new source commits, the local B setup/update still runs. Thus an empty B folder can be populated from an already-synced server branch without creating duplicate commits. The hosting-service repository at the B URL must already exist and allow access; local `git init` does not create a GitHub repository.

## Git identity and access

Configure your identity:

```powershell
git config --global user.name "Your Name"
git config --global user.email "you@example.com"
```

Prepare SSH keys or Git Credential Manager access to both servers. Interactive credential prompts are disabled. Repo-specific transport settings are reused when valid folders are supplied. Local files alone do not grant server access. Errors identify the failed fetch/push step and retain Git's original error. No global Git/SSH settings or ownership checks are changed. Avoid credentials in URLs.

### GitHub workflow permission rejection

If GitHub says an OAuth app cannot update `.github/workflows/...` without `workflow` scope, the server has authenticated the connection but rejected the ref update. New syncs exclude workflow changes, so no extra workflow permission is needed for those excluded files. Existing workflows on B are left unchanged. An unpushed plan created by an earlier version is backed up as `pending-before-workflow-filter-<id>.json` and rebuilt without workflows, provided B has not changed. Already-pushed B history is never rewritten. A final check rejects any proposed push whose newly introduced history still changes workflow files.

For HTTPS, authorize workflow access for the credential provider/token that Git is actually using. A classic personal access token needs the relevant repository scope plus `workflow`; a fine-grained token for the destination repository needs appropriate Contents and Workflows write permissions. Use your credential provider's normal sign-in/token update flow; do not put tokens into URLs or saved history. See [OAuth scopes](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/scopes-for-oauth-apps) and [token management](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens).

Alternatively use SSH only after confirming the key's GitHub account has access to this specific destination repo. `ssh -T git@github.com` verifies the account, but successful authentication alone does not prove repository permission. GitHub can return Repository not found for an inaccessible private repo. After access is fixed, retry Sync; do not delete the saved pending state.

## History and state

Only completed Sync operations are saved. Running and failed attempts are never written to history, and a failed retry does not replace the last completed entry. Choose an entry in **Sync history** and click **Open history** to restore both URLs, branches, folders, the optional message and selected hashes. If a message is present, the source list refreshes and saved reachable commits are reselected, including older saved commits outside the newest 200. Opening history never pushes automatically.

History keeps one entry for each A/B URL, branch and folder configuration, up to 100 unique configurations, in `%LOCALAPPDATA%\GitSyncTool\history.json`. Repeating a successful Sync updates that entry with the newest successful message/selection and time instead of adding another row. Time, message and selected commits do not make a duplicate configuration unique. Equivalent GitHub HTTPS/SSH URLs and Windows folder case/slash variations are normalized. A completed no-op Sync may update an entry; failed attempts are ignored. Old Running/Failed rows and duplicate completed rows are cleaned automatically, keeping the newest successful configuration. Configuration and messages are stored as ordinary local JSON. Corrupt history is preserved and reported.

History titles now include a readable local timestamp, a short unique record ID, the owner/repo identities for A and B, a short destination branch and status. Selecting a history entry shows both complete branch names and full folder paths beneath the list. Thus repositories with the same basename, or identically named folders on different drives/parents, remain distinguishable. Credentials and query tokens are omitted from displayed repository identities. Existing history entries use this display automatically without changing their saved inputs.

Each exact URL/branch pair has its own private checkout and sync ledger under `%LOCALAPPDATA%\GitSyncTool\<hash>`. Local folder paths do not change that key. `selected-state.json` records source hashes and pending pushes for both basic behaviors. Earlier `state.json` baseline records remain usable as automatic-sync anchors. Existing history files remain readable; restored behavior is determined by whether the message is blank. Keep state to avoid repeated imports. Changing direction or URL spelling uses separate state; equivalent changes across different hashes/directions are not automatically detected.

Do not edit the private checkout or delete state to bypass divergence. Remove a stale `lock` or `history.lock` only after confirming no app instance is active. Inputs are disabled during work and closing waits until the operation finishes.

## Tests

```powershell
python -m unittest -v
```

Tests use temporary local repositories and a test identity; they do not modify global Git settings or contact your real servers.
