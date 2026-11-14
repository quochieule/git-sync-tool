"""Snapshot-delta Git synchronization. Python 3.10+, Git required."""
import hashlib
import json
import ntpath
import os
import re
from pathlib import Path
import subprocess
import threading
import tkinter as tk
from datetime import datetime, timedelta, timezone
from tkinter import filedialog, messagebox, ttk
from urllib.parse import urlsplit


class SyncError(RuntimeError):
    pass


def push_error_message(step, error):
    detail = str(error)
    if 'workflow' in detail.lower() and ('without `workflow` scope' in detail.lower()
                                        or 'without workflow scope' in detail.lower()):
        return ('GitHub refused the push because the current login lacks workflow permission.\n'
                'Authorize workflow access for the HTTPS credential, or use an authorized SSH key.\n'
                'The workflow file is retained and the pending sync is saved. After fixing access, click Sync again.\n'
                'Help: https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/scopes-for-oauth-apps\n\n'
                + detail)
    return (step + ' failed. Check the server URL and Git/SSH access. '
            'For an existing clone, choose its folder and use its configured remote.\n' + detail)


def history_repository(url):
    value = url.strip()
    if re.match(r'^[a-zA-Z]:[/\\]', value):
        return value
    try:
        if '://' in value:
            parsed = urlsplit(value)
            host = parsed.hostname or ''
            path = parsed.path.strip('/').removesuffix('.git')
            return path if host.lower() == 'github.com' else host + '/' + path
    except ValueError:
        return '(invalid URL)'
    scp = re.fullmatch(r'(?:[^/@:]+@)?([^/:]+):(.+)', value)
    if scp:
        host, path = scp.groups()
        path = path.strip('/').removesuffix('.git')
        return path if host.lower() == 'github.com' else host + '/' + path
    return value.split('?', 1)[0].split('#', 1)[0].rstrip('/\\')


def history_label(record):
    try:
        stamp = datetime.fromisoformat(record['time']).astimezone(timezone(timedelta(hours=7))).strftime('%d/%m %H:%M:%S')
    except ValueError:
        stamp = record['time']
    inputs = record['inputs']
    branch = inputs[3] if len(inputs[3]) <= 36 else inputs[3][:33] + '…'
    return (stamp + ' #' + record['id'][:6] + ' | '
            + history_repository(inputs[0]) + ' → ' + history_repository(inputs[1])
            + ' | ' + branch + ' | ' + record['status'])


def history_details(record):
    inputs = record['inputs']
    return ('A: ' + history_repository(inputs[0]) + ' | ' + inputs[2] + ' | Folder: ' + (inputs[4] or '(private cache)')
            + '\nB: ' + history_repository(inputs[1]) + ' | ' + inputs[3] + ' | Folder: ' + (inputs[5] or '(private cache)'))


def local_repository(value, url=''):
    """Optional root only: do not search parents/children or create a repo."""
    if not value or not value.strip():
        return None
    path = Path(value.strip().strip('"')).expanduser().resolve()
    if not path.is_dir():
        return None
    has_git = (path / '.git').exists()
    is_bare = (path / 'HEAD').is_file() and (path / 'objects').is_dir() and (path / 'config').is_file()
    if not has_git and not is_bare:
        return None
    git(path, 'rev-parse', '--git-dir')
    return path


def repository_settings(url, folder=None, use_remote=False):
    url = url.strip().strip('"')
    local = local_repository(folder, url)
    target = url
    if local:
        remotes = git(local, 'remote').decode().splitlines()
        if use_remote or not url or url in remotes:
            remote = url if url in remotes and not use_remote else ('origin' if 'origin' in remotes else None)
            if remote is None and len(remotes) == 1:
                remote = remotes[0]
            if remote is None:
                raise SyncError('This local repo needs an origin remote, or an explicit server URL.')
            url = git(local, 'remote', 'get-url', remote).decode().strip()
            target = remote
    if not url or url.startswith('-'):
        raise SyncError('Enter a repository URL, or choose an existing local repo with an origin remote.')
    return dict(url=url, local=local, target=target)


class HistoryStore:
    def __init__(self, root=None):
        self.root = Path(root or Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'GitSyncTool')
        self.path = self.root / 'history.json'
        self.pending = {}

    @staticmethod
    def key(inputs):
        urls = []
        for value in inputs[:2]:
            value = value.strip().rstrip('/')
            if re.match(r'^(?:https?://github\.com/|ssh://git@github\.com/|git@github\.com:)', value, re.I):
                value = 'github.com/' + history_repository(value).casefold()
            else:
                value = value.removesuffix('.git')
            urls.append(value)
        folders = [ntpath.normcase(ntpath.normpath(value.strip().strip('"'))) if value.strip() else ''
                   for value in inputs[4:6]]
        return tuple(urls + [value.strip() for value in inputs[2:4]] + folders)

    def read(self):
        if not self.path.exists():
            return []
        try:
            rows = json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(rows, list) or any(not isinstance(row, dict)
                    or not isinstance(row.get('id'), str)
                    or not isinstance(row.get('inputs'), list) or len(row['inputs']) != 6
                    or not all(isinstance(item, str) for item in row['inputs'])
                    or not isinstance(row.get('message'), str)
                    or not isinstance(row.get('time'), str) or not isinstance(row.get('status'), str)
                    or row.get('mode') not in ('Selected commits (squash)', 'Changes since baseline', 'Preserve commits')
                    or not isinstance(row.get('selected'), list)
                    or not all(isinstance(item, str) and re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', item)
                               for item in row['selected']) for row in rows):
                raise ValueError('Invalid history structure')
            unique, seen = [], set()
            for row in rows:
                key = self.key(row['inputs'])
                if row['status'] == 'Completed' and key not in seen:
                    unique.append(row)
                    seen.add(key)
            return unique[:100]
        except (ValueError, OSError) as exc:
            raise SyncError('Cannot read saved history; the existing file was preserved: ' + str(self.path)) from exc

    def _write(self, change):
        self.root.mkdir(parents=True, exist_ok=True)
        lock = self.root / 'history.lock'
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise SyncError('History is being saved by another instance, or history.lock is stale.') from exc
        os.close(fd)
        try:
            rows = self.read()
            change(rows)
            temp = self.path.with_suffix('.tmp')
            temp.write_text(json.dumps(rows[:100], ensure_ascii=False, indent=2), encoding='utf-8')
            temp.replace(self.path)
        finally:
            lock.unlink()

    def add(self, inputs, mode, message, selected):
        import uuid
        self.read()
        record = dict(id=uuid.uuid4().hex, inputs=list(inputs), mode=mode, message=message,
                      selected=list(selected or []), status='Running',
                      time=datetime.now(timezone(timedelta(hours=7))).isoformat(timespec='seconds'))
        self.pending[record['id']] = record
        return record['id']

    def finish(self, record_id, status):
        record = self.pending.pop(record_id, None)
        if not record or status != 'Completed':
            return
        record['status'] = 'Completed'
        def change(rows):
            key = self.key(record['inputs'])
            previous = next((row for row in rows if self.key(row['inputs']) == key), None)
            if previous:
                record['id'] = previous['id']
            rows[:] = [row for row in rows if self.key(row['inputs']) != key]
            rows.insert(0, record)
        self._write(change)

    def cleanup(self):
        if self.path.exists():
            self._write(lambda rows: None)


def git(repo, *args, data=None, identity=None, author=None):
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='Never')
    if identity:
        for role in ('AUTHOR', 'COMMITTER'):
            env['GIT_' + role + '_NAME'], env['GIT_' + role + '_EMAIL'] = identity
    if author:
        env['GIT_AUTHOR_NAME'], env['GIT_AUTHOR_EMAIL'], env['GIT_AUTHOR_DATE'] = author
    p = subprocess.run(['git', '-C', str(repo), *args], input=data,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    if p.returncode:
        raise SyncError(p.stderr.decode(errors='replace').strip() or 'Git command failed')
    return p.stdout


def sync_key(a, b, branch_a, branch_b):
    # Preserve existing state for the original same-branch workflow.
    values = [a, b, branch_a] if branch_a == branch_b else [a, b, branch_a, branch_b]
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()


def switch_sources(fields):
    values = [field.get() for field in fields]
    swapped = [values[1], values[0], values[3], values[2]]
    if len(values) == 6:
        swapped += [values[5], values[4]]
    for field, value in zip(fields, swapped):
        field.delete(0, 'end')
        field.insert(0, value)


class Sync:
    def __init__(self, root=None, log=print):
        self.root = Path(root or Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'GitSyncTool')
        self.log = log

    def _prepare_target_folder(self, target, branch):
        hint = target.get('folder_hint')
        if target['local'] or not hint:
            return
        path = Path(hint.strip().strip('"')).expanduser().resolve()
        if path.exists() and (not path.is_dir() or any(path.iterdir())):
            self.log('Source B folder has existing files without Git metadata; keeping those files and syncing through the private cache.')
            return
        path.mkdir(parents=True, exist_ok=True)
        git(path, 'init', '--initial-branch=' + branch)
        git(path, 'remote', 'add', 'origin', target['url'])
        target['local'] = path
        self.log('Initialized the empty Source B folder and configured its server URL.')

    def _refresh_target_folder(self, target, branch):
        local = target['local']
        if not local:
            return
        self.log('Server sync is complete. Updating local Source B from the server branch…')
        try:
            remotes = git(local, 'remote').decode().splitlines()
            matches = [name for name in remotes if git(local, 'remote', 'get-url', name).decode().strip() == target['url']]
            remote = 'origin' if 'origin' in matches else (matches[0] if matches else None)
            if remote is None:
                remote = 'origin' if 'origin' not in remotes else 'git-sync-' + hashlib.sha256(target['url'].encode()).hexdigest()[:10]
                if remote in remotes:
                    raise SyncError('A local remote name conflicts with the selected server URL.')
                git(local, 'remote', 'add', remote, target['url'])
            tracking = 'refs/remotes/' + remote + '/' + branch
            git(local, 'fetch', '--no-tags', remote, '+refs/heads/' + branch + ':' + tracking)
            head = subprocess.run(['git', '-C', str(local), 'rev-parse', '--verify', 'HEAD'], capture_output=True)
            dirty = git(local, 'status', '--porcelain', '--untracked-files=all')
            if head.returncode:
                if dirty:
                    self.log('Local B has existing files. Server branch is ready; local files were preserved.')
                    return
                git(local, 'checkout', '--no-overwrite-ignore', '-B', branch, '--track', remote + '/' + branch)
                self.log('Source B folder now contains the synced code on branch ' + branch + '.')
                return
            current = subprocess.run(['git', '-C', str(local), 'symbolic-ref', '--quiet', '--short', 'HEAD'], capture_output=True)
            if current.stdout.decode().strip() == branch:
                if dirty:
                    self.log('Local B has uncommitted changes; server sync succeeded and those local changes were preserved.')
                    return
                ff = subprocess.run(['git', '-C', str(local), 'merge-base', '--is-ancestor', 'HEAD', tracking], capture_output=True)
                if ff.returncode == 0:
                    git(local, 'merge', '--ff-only', '--no-edit', '--no-overwrite-ignore', tracking)
                    self.log('Local Source B is up to date with the server branch.')
                else:
                    self.log('Local B has separate commits; server sync succeeded and local history was preserved.')
            else:
                existing = git(local, 'for-each-ref', '--format=%(refname)', 'refs/heads/' + branch).decode().splitlines()
                if 'refs/heads/' + branch not in existing:
                    git(local, 'branch', '--track', branch, remote + '/' + branch)
                self.log('Local B branch ' + branch + ' is available. The current working branch was left unchanged.')
        except SyncError as exc:
            raise SyncError('Server sync completed, but local B could not be updated: ' + str(exc)) from exc

    def run(self, a, b, branch_a, branch_b, initialize=False, *, selected=None,
            message=None, list_only=False, local_a=None, local_b=None, preserve_commits=False):
        a, b, branch_a, branch_b = (value.strip() for value in (a, b, branch_a, branch_b))
        source = repository_settings(a, local_a)
        target = repository_settings(b, local_b)
        target['folder_hint'] = local_b
        for label, requested, settings in [('A', local_a, source), ('B', local_b, target)]:
            if requested and not settings['local']:
                if label == 'B':
                    continue
                self.log('Source ' + label + ' folder has no Git metadata; using its server URL with the private cache.')
        a, b = source['url'], target['url']
        if a == b:
            raise SyncError('Enter two distinct repository URLs or paths.')
        key = sync_key(a, b, branch_a, branch_b)
        folder = self.root / key
        folder.mkdir(parents=True, exist_ok=True)
        lock = folder / 'lock'
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise SyncError('Sync is already running, or a stale lock exists: ' + str(lock))
        os.close(fd)
        try:
            result = self._run(folder, a, b, branch_a, branch_b, initialize,
                               selected=selected, message=message, list_only=list_only,
                               source=source, target=target, preserve_commits=preserve_commits)
            if not list_only:
                self._refresh_target_folder(target, branch_b)
            return result
        finally:
            lock.unlink()

    def _run(self, folder, a, b, branch_a, branch_b, initialize, *, selected, message,
             list_only, source, target, preserve_commits):
        repo, statefile = folder / 'repo', folder / 'state.json'
        if not repo.exists():
            repo.mkdir()
            git(repo, 'init')
        for branch in (branch_a, branch_b):
            git(repo, 'check-ref-format', '--branch', branch)
            if branch.startswith('-') or branch == 'HEAD':
                raise SyncError('Invalid branch name.')
        if not list_only:
            self._prepare_target_folder(target, branch_b)
        def save(state):
            temp = statefile.with_suffix('.tmp')
            temp.write_text(json.dumps(state, indent=2), encoding='utf-8')
            temp.replace(statefile)
        def sha(ref):
            return git(repo, 'rev-parse', ref).decode().strip()
        def ancestor(old, new):
            p = subprocess.run(['git', '-C', str(repo), 'merge-base', '--is-ancestor', old, new], capture_output=True)
            if p.returncode not in (0, 1):
                raise SyncError('Cannot validate commit history.')
            return p.returncode == 0
        def network(context, *args, step):
            try:
                return git(context, *args)
            except SyncError as exc:
                raise SyncError(push_error_message(step, exc)) from exc
        def push(commit):
            introduced = git(repo, 'log', '--format=', '--name-only', commit,
                             '--not', '--remotes=target', '--', '.github/workflows')
            if introduced.strip():
                raise SyncError('The proposed push still contains workflow changes. Nothing was pushed; rebuild the sync with workflow exclusion.')
            if target['local']:
                local = target['local']
                # Import objects into a private ref, never into a user's branch/index.
                ref = 'refs/git-sync-tool-push/' + folder.name
                git(local, 'fetch', '--no-tags', str(repo), '+' + commit + ':' + ref)
                return network(local, 'push', target['target'], commit + ':refs/heads/' + branch_b,
                               step='Push to Source B using its local repo configuration')
            return network(repo, 'push', 'target', commit + ':refs/heads/' + branch_b,
                           step='Push to Source B')
        for remote, settings in [('source', source), ('target', target)]:
            if list_only and remote == 'target':
                continue
            url, local = settings['url'], settings['local']
            names = git(repo, 'remote').decode().split()
            git(repo, 'remote', 'set-url', remote, url) if remote in names else git(repo, 'remote', 'add', remote, url)
            self.log('Fetching ' + remote + '…')
            if local:
                namespace = 'refs/git-sync-tool/' + hashlib.sha256(url.encode()).hexdigest() + '/'
                self.log('Updating local ' + remote + ' Git data from server; working files remain untouched.')
                network(local, 'fetch', '--no-tags', '--prune', settings['target'],
                        '+refs/heads/*:' + namespace + '*', step='Fetch ' + remote + ' using its existing local repo')
                git(repo, 'fetch', '--no-tags', '--prune', str(local), '+' + namespace + '*:refs/remotes/' + remote + '/*')
            else:
                network(repo, 'fetch', '--no-tags', '--prune', remote,
                        '+refs/heads/*:refs/remotes/' + remote + '/*', step='Fetch ' + remote)
        ahead = sha('refs/remotes/source/' + branch_a)
        if list_only:
            ledger = folder / 'selected-state.json'
            state = json.loads(ledger.read_text(encoding='utf-8')) if ledger.exists() else {}
            applied = set(state.get('applied', []))
            rows = git(repo, 'log', '--topo-order', '--max-count=200',
                       '--format=%H%x00%P%x00%an%x00%s', ahead).decode('utf-8', errors='replace')
            listed = {line.split('\x00', 1)[0] for line in rows.splitlines() if line}
            for commit in selected or []:
                if commit in listed:
                    continue
                if not isinstance(commit, str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', commit):
                    raise SyncError('Invalid saved source commit ID.')
                try:
                    reachable = ancestor(commit, ahead)
                except SyncError:
                    reachable = False
                if not reachable:
                    self.log('Saved commit is no longer available on A: ' + commit[:12])
                    continue
                rows += git(repo, 'log', '-1', '--format=%H%x00%P%x00%an%x00%s', commit).decode('utf-8', errors='replace')
                listed.add(commit)
            return [dict(zip(('sha', 'parents', 'author', 'subject'), line.split('\x00', 3)),
                         applied=line.split('\x00', 1)[0] in applied)
                    for line in rows.splitlines() if line]
        refs = git(repo, 'for-each-ref', '--format=%(refname)', 'refs/remotes/target/').decode().splitlines()
        bref = 'refs/remotes/target/' + branch_b
        exists = bref in refs
        empty_target = not refs
        root_commit = False
        source_base = None
        if exists:
            bhead = sha(bref)
        else:
            head = network(target['local'] or repo, 'ls-remote', '--symref',
                           target['target'] if target['local'] else 'target', 'HEAD',
                           step='Read Source B default branch').decode()
            defaults = [line.split()[1] for line in head.splitlines() if line.startswith('ref: refs/heads/')]
            default_refs = ['refs/remotes/target/' + ref[len('refs/heads/'):] for ref in defaults]
            candidates = [ref for ref in default_refs if ref in refs]
            if not candidates:
                candidates = [ref for ref in ('refs/remotes/target/main', 'refs/remotes/target/master') if ref in refs]
            if not candidates and len(refs) == 1:
                candidates = refs
            if candidates:
                bhead = sha(candidates[0])
                self.log('Creating B branch ' + branch_b + ' from ' + candidates[0].removeprefix('refs/remotes/target/') + ' on push.')
            elif empty_target:
                self.log('Source B is empty. Branch ' + branch_b + ' will be created on the first successful push.')
                tree = git(repo, 'mktree', data=b'').strip()
                helper = b'tree ' + tree + b'\nauthor Git Sync Tool <internal@invalid> 0 +0000\ncommitter Git Sync Tool <internal@invalid> 0 +0000\n\nPrivate empty-tree anchor\n'
                bhead = git(repo, 'hash-object', '-t', 'commit', '-w', '--stdin', data=helper).decode().strip()
                root_commit = True
                if selected and not preserve_commits:
                    selected_ids = set(selected)
                    if any(not isinstance(commit, str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', commit)
                           for commit in selected_ids):
                        raise SyncError('Invalid selected commit ID.')
                    order = git(repo, 'rev-list', '--topo-order', '--reverse', ahead).decode().split()
                    first = next((commit for commit in order if commit in selected_ids), None)
                    if first is None:
                        raise SyncError('Selected commits are not on the source branch. Reload the list.')
                    parents = git(repo, 'rev-list', '--parents', '-n', '1', first).decode().split()[1:]
                    if len(parents) > 1:
                        raise SyncError('Select ordinary commits rather than a merge commit.')
                    source_base = parents[0] if parents else None
            else:
                raise SyncError('B has several branches but no default branch. Choose an existing B branch or set its server default.')
        if preserve_commits or selected is not None or empty_target:
            return self._selected(repo, folder, branch_b, ahead, bhead, exists,
                                  selected, message, ancestor, sha, push, automatic=preserve_commits or (selected is None and empty_target),
                                  empty_target=empty_target, root_commit=root_commit, source_base=source_base)
        state = json.loads(statefile.read_text(encoding='utf-8')) if statefile.exists() else None
        if state and state.get('pending'):
            pending = state['pending']
            if exists and ancestor(pending['b'], bhead):
                state = {'a': pending['a'], 'b': pending['b']}
                save(state)
            elif bhead == state['b']:
                push(pending['b'])
                state = {'a': pending['a'], 'b': pending['b']}
                save(state)
                self.log('Recovered previous sync push.')
                return
            else:
                raise SyncError('Unfinished push and B changed. Preserve state and review manually.')
        if state is None:
            if not initialize:
                raise SyncError('First sync requires baseline confirmation.')
            state = {'a': ahead, 'b': bhead, 'pending': {'a': ahead, 'b': bhead}}
            save(state)
            if not exists:
                push(bhead)
            save({'a': ahead, 'b': bhead})
            self.log('Baseline saved. Existing A content was not imported. Future A changes will sync.')
            return
        if not exists or not ancestor(state['b'], bhead):
            raise SyncError('B branch was deleted or rewritten. Manual review required.')
        if not ancestor(state['a'], ahead):
            raise SyncError('A history was rewritten or diverged from the saved baseline. Manual review required.')
        if ahead == state['a']:
            self.log('No new A changes.')
            return
        patch = git(repo, 'diff', '--binary', '--full-index', '--no-renames', '--no-ext-diff', '--no-textconv', state['a'], ahead, '--', '.', ':(top,exclude).github/workflows')
        git(repo, 'checkout', '--detach', '--force', bhead)
        git(repo, 'clean', '-fd')
        if patch:
            try:
                git(repo, 'apply', '--3way', '--index', data=patch)
            except SyncError:
                git(repo, 'reset', '--hard', bhead)
                raise SyncError('Changes conflict with B. Nothing was pushed; baseline is unchanged. Resolve differences in the source repositories and retry.')
        changed = git(repo, 'diff', '--cached', '--name-only')
        if changed:
            name = git(repo, 'config', 'user.name').decode().strip()
            email = git(repo, 'config', 'user.email').decode().strip()
            if not name or not email:
                raise SyncError('Configure Git user.name and user.email before syncing.')
            title = (message or '').strip() or ('Sync source changes ' + state['a'][:12] + '..' + ahead[:12])
            git(repo, 'commit', '-m', title, identity=(name, email))
            result = sha('HEAD')
        else:
            result = bhead
        state['pending'] = {'a': ahead, 'b': result}
        save(state)
        push(result)
        save({'a': ahead, 'b': result})
        self.log('Sync completed safely.')

    def _selected(self, repo, folder, branch_b, ahead, bhead, exists,
                  selected, message, ancestor, sha, push, automatic=False,
                  empty_target=False, root_commit=False, source_base=None):
        if not automatic and not selected:
            raise SyncError('Select at least one source commit.')
        if not automatic and (not message or not message.strip()):
            raise SyncError('Enter the new commit message.')
        selected = set(selected or [])
        if any(not isinstance(commit, str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', commit)
               for commit in selected):
            raise SyncError('Invalid selected commit ID. Reload the commit list.')
        statefile = folder / 'selected-state.json'
        def save(state):
            temp = statefile.with_suffix('.tmp')
            temp.write_text(json.dumps(state, indent=2), encoding='utf-8')
            temp.replace(statefile)
        state = json.loads(statefile.read_text(encoding='utf-8')) if statefile.exists() else None
        if state and not exists and (not state.get('pending') or state.get('branch_existed')):
            import uuid
            backup = folder / ('state-before-branch-recreation-' + uuid.uuid4().hex + '.json')
            backup.write_text(json.dumps(state, indent=2), encoding='utf-8')
            state = None
            self.log('B branch is absent. Rebuilding it from the source; the previous sync state was backed up.')
        if state and state.get('pending'):
            pending = state['pending']
            if exists and ancestor(pending['b'], bhead):
                state = {'b': pending['b'], 'applied': pending['applied']}
                save(state)
                self.log('Recovered an already-pushed sync.')
            elif (bhead == state['b'] or empty_target) and exists == state['branch_existed'] and not state.get('exclude_workflows'):
                import uuid
                backup = folder / ('pending-before-workflow-filter-' + uuid.uuid4().hex + '.json')
                backup.write_text(json.dumps(state, indent=2), encoding='utf-8')
                state = {'b': state['b'], 'applied': state['applied']} if exists else None
                self.log('Rebuilding the unpushed sync without .github/workflows/. The previous plan was backed up.')
            elif (bhead == state['b'] or empty_target) and exists == state['branch_existed']:
                push(pending['b'])
                save({'b': pending['b'], 'applied': pending['applied']})
                self.log('Recovered the previous push. Review the log before another sync.')
                return dict(applied=pending['applied'])
            else:
                raise SyncError('Unfinished push and B changed. Manual review required.')
        if state and (not exists or not ancestor(state['b'], bhead)):
            raise SyncError('B branch was deleted or rewritten. Manual review required.')
        applied = state.get('applied', []) if state else []
        if automatic:
            anchors = [bhead]
            reachable = set(git(repo, 'rev-list', ahead).decode().split())
            for commit in applied:
                if commit not in reachable:
                    raise SyncError('A history was rewritten since a previous sync. Review manually.')
                anchors.append(commit)
            legacy_file = folder / 'state.json'
            use_legacy = legacy_file.exists() and exists and not applied
            if use_legacy:
                legacy = json.loads(legacy_file.read_text(encoding='utf-8'))
                if legacy.get('pending'):
                    raise SyncError('An earlier baseline push is unfinished. Review it before automatic sync.')
                if not ancestor(legacy['a'], ahead) or not exists or not ancestor(legacy['b'], bhead):
                    raise SyncError('A or B history diverged from the saved baseline. Review manually.')
                anchors.append(legacy['a'])
            common = subprocess.run(['git', '-C', str(repo), 'merge-base', ahead, bhead], capture_output=True)
            if common.returncode not in (0, 1):
                raise SyncError('Cannot determine the common source/target history.')
            if not exists and not empty_target and not applied and common.returncode == 1:
                self.log('New B branch has no common history with A. Initializing its full code from A, preserving B workflows.')
                git(repo, 'checkout', '--detach', '--force', bhead)
                git(repo, 'clean', '-fd')
                git(repo, 'read-tree', '--reset', '-u', ahead)
                git(repo, 'rm', '-r', '-f', '--ignore-unmatch', '--', '.github/workflows')
                if git(repo, 'ls-tree', '--name-only', bhead, '--', '.github/workflows').strip():
                    git(repo, 'checkout', bhead, '--', '.github/workflows')
                result = bhead
                if git(repo, 'diff', '--cached', '--name-only').strip():
                    identity = (git(repo, 'config', 'user.name').decode().strip(),
                                git(repo, 'config', 'user.email').decode().strip())
                    if not all(identity):
                        raise SyncError('Configure Git user.name and user.email before syncing.')
                    git(repo, 'commit', '-m', 'Initialize branch from source ' + ahead[:12] + ' (workflows excluded)', identity=identity)
                    result = sha('HEAD')
                imported = git(repo, 'rev-list', '--reverse', ahead).decode().split()
                save({'b': bhead, 'applied': [], 'branch_existed': False, 'exclude_workflows': True,
                      'pending': {'b': result, 'applied': imported}})
                push(result)
                save({'b': result, 'applied': imported})
                self.log('Created B branch and pushed the full source code. Future syncs will copy new commits separately.')
                return dict(applied=imported)
            if not empty_target and not applied and not use_legacy and common.returncode == 1:
                raise SyncError('A and B have unrelated histories, so the first automatic range is unknown. '
                                'Select the initial commits and enter a message for the first sync.')
            revisions = ahead + '\n' + ''.join('^' + anchor + '\n' for anchor in anchors)
            selected = set(git(repo, 'rev-list', '--first-parent', '--reverse', '--stdin', data=revisions.encode()).decode().split())
        for commit in selected:
            if automatic:
                continue
            if not ancestor(commit, ahead):
                raise SyncError('A selected commit is no longer on the source branch. Reload and review.')
            parents = git(repo, 'rev-list', '--parents', '-n', '1', commit).decode().split()[1:]
            if len(parents) > 1 and not automatic:
                raise SyncError('Merge commits need a mainline decision; select ordinary commits instead.')
        requested = selected - set(applied)
        if not requested:
            if automatic and not exists:
                save({'b': bhead, 'applied': applied, 'branch_existed': False,
                      'exclude_workflows': True, 'pending': {'b': bhead, 'applied': applied}})
                push(bhead)
                save({'b': bhead, 'applied': applied})
                self.log('Created the missing B branch on the server; its code is already up to date.')
            else:
                self.log('Server B is already up to date; no new source commits to push.' if automatic else 'All selected commits were already synced. No new commit or push.')
            return dict(applied=applied)
        order = git(repo, 'rev-list', '--topo-order', '--reverse', ahead).decode().split()
        commits = [commit for commit in order if commit in requested]
        self.log(('Syncing separate commits' if automatic else 'Combining source commits')
                 + ', oldest first: ' + ', '.join(c[:12] for c in commits))
        git(repo, 'checkout', '--detach', '--force', bhead)
        git(repo, 'reset', '--hard', bhead)
        git(repo, 'clean', '-fd')
        identity = (git(repo, 'config', 'user.name').decode().strip(),
                    git(repo, 'config', 'user.email').decode().strip())
        if not all(identity):
            raise SyncError('Configure Git user.name and user.email before syncing.')
        self.log('Skipping .github/workflows/; existing workflows on B are preserved.')
        if source_base:
            git(repo, 'read-tree', '--reset', '-u', source_base)
            git(repo, 'rm', '-r', '-f', '--ignore-unmatch', '--', '.github/workflows')
            tree = git(repo, 'write-tree').decode().strip()
            checkpoint = git(repo, 'commit-tree', tree, '-F', '-',
                             data=('Initialize source baseline without workflows\n\nSource: ' + source_base + '\n').encode(),
                             identity=identity).decode().strip()
            git(repo, 'checkout', '--detach', '--force', checkpoint)
            root_commit = False
        try:
            for commit in commits:
                parents = git(repo, 'rev-list', '--parents', '-n', '1', commit).decode().split()[1:]
                base = parents[0] if parents else git(repo, 'mktree', data=b'').decode().strip()
                patch = git(repo, 'diff', '--binary', '--full-index', '--no-renames', '--no-ext-diff', '--no-textconv',
                            base, commit, '--', '.', ':(top,exclude).github/workflows')
                if patch:
                    git(repo, 'apply', '--3way', '--index', data=patch)
                if automatic and git(repo, 'diff', '--cached', '--name-only'):
                    metadata = git(repo, 'show', '-s', '--format=format:%an%x00%ae%x00%aI%x00%B', commit).split(b'\x00', 3)
                    author = metadata[0].decode('utf-8') + ' <' + metadata[1].decode('utf-8') + '>'
                    if root_commit:
                        tree = git(repo, 'write-tree').decode().strip()
                        result = git(repo, 'commit-tree', tree, '-F', '-', data=metadata[3], identity=identity,
                                     author=(metadata[0].decode(), metadata[1].decode(), metadata[2].decode())).decode().strip()
                        git(repo, 'checkout', '--detach', '--force', result)
                        root_commit = False
                    else:
                        git(repo, 'commit', '--cleanup=verbatim', '--author=' + author,
                            '--date=' + metadata[2].decode(), '--file=-', data=metadata[3], identity=identity)
        except SyncError:
            git(repo, 'reset', '--hard', bhead)
            raise SyncError('Source commits conflict with B or depend on missing changes. Nothing was pushed; no new source commits were marked synced.')
        if automatic:
            if root_commit:
                tree = git(repo, 'write-tree').decode().strip()
                result = git(repo, 'commit-tree', tree, '-m', 'Initialize synced branch (workflows excluded)', identity=identity).decode().strip()
                git(repo, 'checkout', '--detach', '--force', result)
            result = sha('HEAD')
        elif root_commit or git(repo, 'diff', '--cached', '--name-only'):
            name = git(repo, 'config', 'user.name').decode().strip()
            email = git(repo, 'config', 'user.email').decode().strip()
            if not name or not email:
                raise SyncError('Configure Git user.name and user.email before syncing.')
            body = message.strip() + '\n\nSource commits (original authorship retained in A):\n'
            body += '\n'.join(commits) + '\n'
            if root_commit:
                tree = git(repo, 'write-tree').decode().strip()
                result = git(repo, 'commit-tree', tree, '-F', '-', data=body.encode('utf-8'),
                             identity=(name, email)).decode().strip()
            else:
                git(repo, 'commit', '--cleanup=verbatim', '--file=-', data=body.encode('utf-8'),
                    identity=(name, email))
                result = sha('HEAD')
        else:
            result = sha('HEAD')
            self.log('Selected changes have no net difference on B; no new commit needed.')
        done = list(dict.fromkeys(applied + commits))
        save({'b': bhead, 'applied': applied, 'branch_existed': exists, 'exclude_workflows': True,
              'pending': {'b': result, 'applied': done}})
        push(result)
        save({'b': result, 'applied': done})
        self.log('New source commits synced separately.' if automatic else
                 ('Selected changes synced as one commit.' if result != bhead else 'Selection recorded safely.'))
        return dict(applied=done)


def main():
    import queue
    window = tk.Tk()
    window.title('Git Sync Tool')
    window.geometry('980x840')
    window.minsize(760, 680)
    # Keep the stored field order compatible with existing history and sync state.
    fields, variables = [None] * 6, [None] * 6
    browse_buttons = []
    repositories = ttk.Frame(window, name='repositories')
    repositories.grid(row=1, column=0, columnspan=3, padx=10, pady=6, sticky='ew')
    for source_index, (name, title) in enumerate([('source_a', 'Source A — From'), ('source_b', 'Source B — To')]):
        group = ttk.LabelFrame(repositories, name=name, text=title, padding=10)
        group.grid(row=0, column=source_index, padx=(0, 6) if source_index == 0 else (6, 0), sticky='nsew')
        repositories.columnconfigure(source_index, weight=1, uniform='repo')
        group.columnconfigure(1, weight=1)
        for row, (name, label, index) in enumerate([('folder', 'Folder', source_index + 4),
                                                   ('url', 'URL', source_index),
                                                   ('branch', 'Branch', source_index + 2)]):
            ttk.Label(group, text=label).grid(row=row, column=0, padx=(0, 8), pady=5, sticky='w')
            variable = tk.StringVar()
            entry = ttk.Entry(group, name=name, width=32, textvariable=variable)
            entry.grid(row=row, column=1, columnspan=1 if row == 0 else 2, pady=5, sticky='ew')
            variables[index], fields[index] = variable, entry
        def browse(source_index=source_index):
            path = filedialog.askdirectory(title='Choose source folder')
            if not path:
                return
            variables[source_index + 4].set(path)
            # URL discovery is only a typing aid. Validation happens on Load/Sync.
            try:
                path = select_local_folder(source_index, '', path)
                if path is None:
                    return
                variables[source_index + 4].set(path)
                settings = repository_settings('', path, use_remote=True)
                if settings['url']:
                    variables[source_index + 4].set(str(settings['local']))
                    variables[source_index].set(settings['url'])
            except (SyncError, OSError):
                pass
        chooser = ttk.Button(group, name='browse', text='Browse…', command=browse)
        chooser.grid(row=0, column=2, padx=(8, 0), pady=5)
        browse_buttons.append(chooser)
    controls = ttk.Frame(window)
    controls.grid(row=2, column=0, columnspan=3, padx=10, pady=6, sticky='ew')
    table_frame = ttk.Frame(window)
    table_frame.grid(row=3, column=0, columnspan=3, padx=10, sticky='nsew')
    tree = ttk.Treeview(table_frame, columns=('sha', 'subject', 'author', 'status'),
                        show='headings', selectmode='extended', height=9)
    for name, title, width in [('sha', 'Commit', 105), ('subject', 'Subject', 380),
                               ('author', 'Original author', 150), ('status', 'Status', 115)]:
        tree.heading(name, text=title)
        tree.column(name, width=width, minwidth=60, stretch=name == 'subject')
    scroll = ttk.Scrollbar(table_frame, orient='vertical', command=tree.yview)
    tree.configure(yscrollcommand=scroll.set)
    scroll.pack(side='right', fill='y')
    tree.pack(side='left', fill='both', expand=True)
    hint = tk.StringVar(value='Blank message: sync new commits separately. Custom message: select commits to combine.')
    ttk.Label(window, textvariable=hint).grid(row=4, column=0, columnspan=3, padx=10, pady=5, sticky='w')
    ttk.Label(window, text='New commit message (optional)').grid(row=5, column=0, padx=10, sticky='nw')
    message = tk.Text(window, height=3, wrap='word')
    message.grid(row=5, column=1, columnspan=2, padx=10, pady=6, sticky='ew')
    output = tk.Text(window, height=9, wrap='word', state='disabled')
    output.grid(row=7, column=0, columnspan=3, padx=10, pady=10, sticky='nsew')
    events = queue.Queue()
    history_store = HistoryStore()
    history_rows = []
    loaded_config = None
    busy = False
    def values():
        return tuple(field.get().strip() for field in fields)
    def local_options(config):
        return dict(local_a=config[4], local_b=config[5])
    def select_local_folder(index, url, folder):
        local = local_repository(folder, url)
        return str(local) if local else folder
    def prepare_config(use_remotes=False):
        config = list(values())
        for index in (0, 1):
            folder = select_local_folder(index, config[index], config[index + 4])
            if folder is None:
                return None
            config[index + 4] = folder
            settings = repository_settings(config[index], config[index + 4],
                                           use_remote=use_remotes and bool(config[index + 4]))
            config[index] = settings['url']
            config[index + 4] = str(settings['local']) if settings['local'] else folder
        for variable, value in zip(variables, config):
            if variable.get() != value:
                variable.set(value)
        return tuple(config)
    def invalidate(*_):
        nonlocal loaded_config
        loaded_config = None
        tree.delete(*tree.get_children())
        hint.set('Sync directly with a blank message, or load and select commits to combine with a custom message.')
    for variable in variables:
        variable.trace_add('write', invalidate)
    def log(text):
        events.put(('log', str(text)))
    def set_busy(value):
        nonlocal busy
        busy = value
        state = 'disabled' if value else 'normal'
        for widget in [button, switch_button, load_button, open_history_button,
                       message, *fields, *browse_buttons]:
            widget.configure(state=state)
        history_box.configure(state='disabled' if value else 'readonly')
        tree.state(['disabled'] if value else ['!disabled'])
    def launch(action, history_id=None):
        set_busy(True)
        def worker():
            status = 'Completed'
            try:
                action()
            except Exception as exc:
                status = 'Failed'
                log('Stopped: ' + str(exc))
            finally:
                if history_id:
                    try:
                        history_store.finish(history_id, status)
                    except Exception as history_error:
                        log('Could not update history status: ' + str(history_error))
                if history_id:
                    events.put(('history', None))
                events.put(('done', None))
        threading.Thread(target=worker, daemon=True).start()
    def load(restore=None):
        try:
            config = prepare_config()
            if config is None:
                return
        except (SyncError, OSError) as exc:
            messagebox.showerror('Repository configuration', str(exc))
            return
        invalidate()
        def action():
            rows = Sync(log=log).run(*config[:4], list_only=True, selected=restore, **local_options(config))
            events.put(('commits', (config, rows, restore)))
        launch(action)
    def poll():
        nonlocal loaded_config
        while not events.empty():
            kind, value = events.get()
            if kind == 'done':
                set_busy(False)
            elif kind == 'history':
                refresh_history()
            elif kind == 'commits':
                config, rows, restore = value
                if config == values():
                    loaded_config = config
                    tree.delete(*tree.get_children())
                    for row in rows:
                        status = 'Already synced' if row['applied'] else ('Merge (unsupported)' if len(row['parents'].split()) > 1 else 'Available')
                        tree.insert('', 'end', iid=row['sha'], values=(row['sha'][:12], row['subject'], row['author'], status))
                    hint.set('Ctrl/Shift-click to combine selected commits. A blank message syncs all new commits separately.')
                    if restore:
                        available = [commit for commit in restore if tree.exists(commit)]
                        tree.selection_set(available)
                        if len(available) != len(restore):
                            log('Some saved commits are outside the current list or source history. Review before syncing.')
            elif kind == 'applied':
                for commit in value:
                    if tree.exists(commit):
                        tree.set(commit, 'status', 'Already synced')
                tree.selection_remove(*tree.selection())
            else:
                output.configure(state='normal')
                output.insert('end', value + '\n')
                output.see('end')
                output.configure(state='disabled')
        window.after(100, poll)
    def start():
        try:
            config = prepare_config()
            if config is None:
                return
        except (SyncError, OSError) as exc:
            messagebox.showerror('Repository configuration', str(exc))
            return
        title = message.get('1.0', 'end-1c').strip()
        selected_mode = bool(title)
        selected = list(tree.selection()) if selected_mode else None
        if selected_mode:
            if loaded_config != config:
                messagebox.showinfo('Load commits', 'Load the commit list for the current URLs and branches first.')
                return
            if not selected:
                messagebox.showinfo('Select commits', 'A custom message combines selected commits. Select one or more commits, or leave the message blank to sync normally.')
                return
        try:
            history_id = history_store.add(config, 'Selected commits (squash)' if selected_mode else 'Preserve commits', title, selected)
            refresh_history()
        except (SyncError, OSError) as exc:
            messagebox.showerror('Cannot save history', str(exc))
            return
        def action():
            result = Sync(log=log).run(*config[:4], selected=selected, message=title,
                                      preserve_commits=not selected_mode, **local_options(config))
            events.put(('applied', (result or {}).get('applied', [])))
        launch(action, history_id)
    switch_button = ttk.Button(controls, text='Switch A ↔ B', command=lambda: switch_sources(fields))
    switch_button.pack(side='left', padx=(0, 10))
    load_button = ttk.Button(controls, text='Load commits from A', command=load)
    load_button.pack(side='left')
    button = ttk.Button(window, text='Sync A → B', command=start)
    button.grid(row=6, column=1, padx=10, pady=6, sticky='w')
    history_frame = ttk.Frame(window)
    history_frame.grid(row=0, column=0, columnspan=3, padx=10, pady=8, sticky='ew')
    history_controls = ttk.Frame(history_frame)
    history_controls.pack(fill='x')
    ttk.Label(history_controls, text='Sync history').pack(side='left', padx=(0, 10))
    history_box = ttk.Combobox(history_controls, state='readonly', width=90)
    history_box.pack(side='left', fill='x', expand=True)
    history_description = tk.StringVar()
    description = ttk.Label(history_frame, textvariable=history_description, wraplength=940, justify='left')
    description.pack(fill='x', pady=(6, 0))
    history_frame.bind('<Configure>', lambda event: description.configure(wraplength=max(200, event.width - 12)))
    def describe_history(*_):
        index = history_box.current()
        history_description.set(history_details(history_rows[index]) if 0 <= index < len(history_rows) else '')
    history_box.bind('<<ComboboxSelected>>', describe_history)
    def refresh_history():
        nonlocal history_rows
        try:
            history_store.cleanup()
            history_rows = history_store.read()
        except (SyncError, OSError) as exc:
            log(str(exc))
            history_rows = []
        labels = [history_label(row) for row in history_rows]
        history_box.configure(values=labels)
        if labels:
            history_box.current(0)
        else:
            history_box.set('No saved syncs')
        describe_history()
    def open_history():
        index = history_box.current()
        if index < 0 or index >= len(history_rows):
            return
        record = history_rows[index]
        for variable, value in zip(variables, record['inputs']):
            variable.set(value)
        message.delete('1.0', 'end')
        message.insert('1.0', record['message'])
        log('Restored saved inputs. Opening history never pushes automatically.')
        if record['message'].strip():
            load(restore=record['selected'])
    open_history_button = ttk.Button(history_controls, text='Open history', command=open_history)
    open_history_button.pack(side='left', padx=(10, 0))
    refresh_history()
    def close():
        if busy:
            messagebox.showinfo('Sync in progress', 'Wait for the current operation to finish before closing.')
        else:
            window.destroy()
    window.protocol('WM_DELETE_WINDOW', close)
    window.columnconfigure(1, weight=1)
    window.rowconfigure(3, weight=2)
    window.rowconfigure(7, weight=1)
    poll()
    window.mainloop()


if __name__ == '__main__':
    main()
