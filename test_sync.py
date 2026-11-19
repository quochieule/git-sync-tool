import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from sync_tool import Sync, SyncError
import sync_tool


class Integration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {
            'GIT_AUTHOR_NAME': 'Test User', 'GIT_AUTHOR_EMAIL': 'test@example.invalid',
            'GIT_COMMITTER_NAME': 'Test User', 'GIT_COMMITTER_EMAIL': 'test@example.invalid',
            'GIT_CONFIG_COUNT': '2', 'GIT_CONFIG_KEY_0': 'user.name',
            'GIT_CONFIG_VALUE_0': 'Test User', 'GIT_CONFIG_KEY_1': 'user.email',
            'GIT_CONFIG_VALUE_1': 'test@example.invalid'})
        self.env.start()
        self.addCleanup(self.env.stop)
        for label in ['a', 'b']:
            bare, work = self.root / (label + '.git'), self.root / label
            self.g(self.root, 'init', '--bare', '--initial-branch=main', str(bare))
            self.g(self.root, 'clone', str(bare), str(work))
            (work / 'shared.txt').write_text('base\n')
            self.commit(work, 'initial')
            self.g(work, 'push', 'origin', 'main')
        self.a, self.b = self.root / 'a', self.root / 'b'
        self.g(self.a, 'checkout', '-b', 'feature')
        self.g(self.a, 'push', 'origin', 'feature')
        self.sync = Sync(self.root / 'state', log=lambda _: None)

    def g(self, cwd, *args):
        p = subprocess.run(['git', '-C', str(cwd), *args], capture_output=True)
        if p.returncode:
            raise RuntimeError(p.stderr.decode())
        return p.stdout.decode().strip()

    def commit(self, repo, message):
        self.g(repo, 'add', '.')
        self.g(repo, 'commit', '-m', message)

    def run_sync(self, initial=False):
        self.sync.run(str(self.root / 'a.git'), str(self.root / 'b.git'), 'feature', 'feature', initialize=initial)

    def change_a(self, text='new\n'):
        (self.a / 'shared.txt').write_text(text)
        self.commit(self.a, 'A update')
        self.g(self.a, 'push', 'origin', 'feature')

    def test_commit_preview_fetches_only_selected_branch_without_file_blobs(self):
        bare = self.root / 'a.git'
        self.g(bare, 'config', 'uploadpack.allowFilter', 'true')
        self.change_a()
        logs = []
        self.sync.log = logs.append
        rows = self.sync.run(str(bare), str(self.root / 'b.git'),
                             'feature', 'feature', list_only=True)
        self.assertEqual(rows[0]['subject'], 'A update')
        preview = next((self.root / 'state').glob('*/preview-repo'))
        refs = self.g(preview, 'for-each-ref', '--format=%(refname)', 'refs/remotes/')
        self.assertEqual(refs, 'refs/remotes/source/feature')
        objects = self.g(preview, 'rev-list', '--objects', '--missing=print',
                         'refs/remotes/source/feature')
        self.assertTrue(any(line.startswith('?') for line in objects.splitlines()))
        self.assertFalse((preview.parent / 'repo').exists())
        self.assertTrue(any('commit preview' in line for line in logs))
        self.assertTrue(any('Receiving objects' in line or 'remote:' in line or 'From ' in line
                            for line in logs))
        # A subsequent sync uses complete objects, not the partial preview cache.
        self.run_sync(True)
        self.assertTrue((preview.parent / 'repo').exists())
        (self.a / 'after-preview.txt').write_text('after preview\n')
        self.commit(self.a, 'After preview')
        self.g(self.a, 'push', 'origin', 'feature')
        self.run_sync()
        self.assertEqual(self.g(self.root / 'b.git', 'show', 'feature:after-preview.txt'), 'after preview')

    def test_commit_preview_refreshes_tip_without_fetching_other_branches(self):
        args = (str(self.root / 'a.git'), str(self.root / 'b.git'), 'feature', 'feature')
        self.sync.run(*args, list_only=True)
        self.change_a()
        rows = self.sync.run(*args, list_only=True)
        self.assertEqual(rows[0]['sha'], self.g(self.a, 'rev-parse', 'feature'))
        preview = next((self.root / 'state').glob('*/preview-repo'))
        self.assertEqual(self.g(preview, 'for-each-ref', '--format=%(refname)', 'refs/remotes/'),
                         'refs/remotes/source/feature')

    def test_streamed_git_failure_reports_error(self):
        logs = []
        with self.assertRaises(SyncError):
            sync_tool.git(self.a, 'fetch', '--progress', 'origin', 'nonexistent-branch',
                          progress=logs.append)
        self.assertTrue(any('nonexistent-branch' in line for line in logs))

    def test_baseline_then_delta_and_noop(self):
        with self.assertRaises(SyncError):
            self.run_sync()
        self.run_sync(True)
        bare = self.root / 'b.git'
        self.assertEqual(self.g(bare, 'rev-parse', 'feature'), self.g(bare, 'rev-parse', 'main'))
        self.change_a()
        self.run_sync()
        self.assertEqual(self.g(bare, 'show', 'feature:shared.txt'), 'new')
        self.assertEqual(self.g(bare, 'show', '-s', '--format=%an <%ae>', 'feature'), 'Test User <test@example.invalid>')
        tip = self.g(bare, 'rev-parse', 'feature')
        self.run_sync()
        self.assertEqual(tip, self.g(bare, 'rev-parse', 'feature'))

    def test_existing_b_changes_preserved(self):
        self.g(self.b, 'checkout', '-b', 'feature')
        (self.b / 'only-b.txt').write_text('keep')
        self.commit(self.b, 'B work')
        self.g(self.b, 'push', 'origin', 'feature')
        self.run_sync(True)
        self.change_a()
        self.run_sync()
        self.assertEqual(self.g(self.root / 'b.git', 'show', 'feature:only-b.txt'), 'keep')

    def test_conflict_preserves_b_and_state(self):
        self.run_sync(True)
        self.g(self.b, 'fetch', 'origin')
        self.g(self.b, 'checkout', '-b', 'feature', 'origin/feature')
        (self.b / 'shared.txt').write_text('B conflict\n')
        self.commit(self.b, 'B conflicting work')
        self.g(self.b, 'push', 'origin', 'feature')
        before = self.g(self.root / 'b.git', 'rev-parse', 'feature')
        state = next((self.root / 'state').glob('*/state.json'))
        saved = state.read_text()
        self.change_a('A conflict\n')
        with self.assertRaisesRegex(SyncError, 'conflict'):
            self.run_sync()
        self.assertEqual(saved, state.read_text())
        self.assertEqual(before, self.g(self.root / 'b.git', 'rev-parse', 'feature'))

    def test_rewritten_a_rejected(self):
        self.change_a()
        self.run_sync(True)
        self.g(self.a, 'reset', '--hard', 'main')
        self.change_a_without_push()
        self.g(self.a, 'push', '--force', 'origin', 'feature')
        with self.assertRaisesRegex(SyncError, 'A history'):
            self.run_sync()

    def test_push_failure_retries_pending_commit(self):
        self.run_sync(True)
        self.change_a()
        original = sync_tool.git
        def fail_push(repo, *args, **kwargs):
            if args[0] == 'push':
                raise SyncError('Simulated push failure')
            return original(repo, *args, **kwargs)
        with patch('sync_tool.git', side_effect=fail_push):
            with self.assertRaisesRegex(SyncError, 'push failure'):
                self.run_sync()
        self.run_sync()
        self.assertEqual(self.g(self.root / 'b.git', 'show', 'feature:shared.txt'), 'new')

    def test_push_success_state_save_failure_recovers(self):
        self.run_sync(True)
        self.change_a()
        original = sync_tool.git
        def push_then_fail(repo, *args, **kwargs):
            result = original(repo, *args, **kwargs)
            if args[0] == 'push':
                raise SyncError('Simulated interruption after push')
            return result
        with patch('sync_tool.git', side_effect=push_then_fail):
            with self.assertRaises(SyncError):
                self.run_sync()
        tip = self.g(self.root / 'b.git', 'rev-parse', 'feature')
        self.run_sync()
        self.assertEqual(tip, self.g(self.root / 'b.git', 'rev-parse', 'feature'))

    def test_rewritten_b_rejected(self):
        self.run_sync(True)
        self.change_a()
        self.run_sync()
        self.g(self.b, 'push', 'origin', 'main:feature', '--force')
        with self.assertRaisesRegex(SyncError, 'B branch'):
            self.run_sync()

    def test_distinct_branch_names(self):
        def sync(initial=False):
            self.sync.run(str(self.root / 'a.git'), str(self.root / 'b.git'),
                          'feature', 'release', initialize=initial)
        sync(True)
        self.change_a()
        sync()
        self.assertEqual(self.g(self.root / 'b.git', 'show', 'release:shared.txt'), 'new')
        self.assertEqual(self.g(self.root / 'b.git', 'branch', '--list', 'feature'), '')
        # A different destination branch gets its own baseline.
        with self.assertRaisesRegex(SyncError, 'baseline'):
            self.run_sync()

    def change_a_without_push(self):
        (self.a / 'other.txt').write_text('rewritten')
        self.commit(self.a, 'rewrite')

    def selected_fixture(self):
        commits = []
        for name in ['first', 'unselected', 'last']:
            (self.a / (name + '.txt')).write_text(name + '\n')
            self.commit(self.a, name)
            commits.append(self.g(self.a, 'rev-parse', 'HEAD'))
        self.g(self.a, 'push', 'origin', 'feature')
        self.g(self.b, 'checkout', '-b', 'release')
        (self.b / 'b-work.txt').write_text('keep B work')
        self.commit(self.b, 'B existing work')
        self.g(self.b, 'push', 'origin', 'release')
        return commits

    def selected_sync(self, commits, message='My combined update'):
        return self.sync.run(str(self.root / 'a.git'), str(self.root / 'b.git'),
                             'feature', 'release', selected=commits, message=message)

    def test_url_source_single_commit_sync_omits_unselected_blobs_and_other_branches(self):
        first, skipped, last = self.selected_fixture()
        self.g(self.root / 'a.git', 'config', 'uploadpack.allowFilter', 'true')
        skipped_blob = self.g(self.a, 'rev-parse', skipped + ':unselected.txt')
        for work in [self.a, self.b]:
            current = self.g(work, 'branch', '--show-current')
            self.g(work, 'checkout', '-b', 'unrelated')
            (work / 'unrelated.txt').write_text('Not needed for this sync')
            self.commit(work, 'Unrelated branch')
            self.g(work, 'push', 'origin', 'unrelated')
            self.g(work, 'checkout', current)
        before_status = self.g(self.b, 'status', '--porcelain')
        calls = []
        original = sync_tool.git
        def record(repo, *args, **kwargs):
            calls.append((Path(repo), args))
            return original(repo, *args, **kwargs)
        args = (str(self.root / 'a.git'), str(self.root / 'b.git'), 'feature', 'release')
        with patch('sync_tool.git', side_effect=record):
            self.sync.run(*args, selected=[last], message='Only one commit', local_b=str(self.b))
        repo = next((self.root / 'state').glob('*/repo'))
        self.assertEqual(set(self.g(repo, 'for-each-ref', '--format=%(refname)', 'refs/remotes/').splitlines()),
                         {'refs/remotes/source/feature', 'refs/remotes/target/release'})
        self.assertEqual(self.g(repo, 'rev-parse', '--is-shallow-repository'), 'false')
        missing = self.g(repo, 'rev-list', '--objects', '--missing=print',
                         'refs/remotes/source/feature')
        self.assertIn('?' + skipped_blob, missing.splitlines())
        self.assertEqual(self.g(self.root / 'b.git', 'show', 'release:last.txt'), 'last')
        self.assertNotIn('unselected.txt', self.g(self.root / 'b.git', 'ls-tree', '--name-only', 'release'))
        self.assertEqual(self.g(self.b, 'status', '--porcelain'), before_status)
        fetches = [args for _, args in calls if args and args[0] == 'fetch']
        self.assertTrue(any('--filter=blob:none' in args for args in fetches))
        self.assertFalse(any('refs/heads/*:' in value for args in fetches for value in args))
        self.assertFalse(any('--depth=15' in args for args in fetches))

    def test_selected_two_squash_preserves_other_commits(self):
        first, skipped, last = self.selected_fixture()
        bare = self.root / 'b.git'
        before = self.g(bare, 'rev-parse', 'release')
        original_a = self.g(self.root / 'a.git', 'rev-parse', 'feature')
        self.selected_sync([last, first], 'Gộp hai thay đổi\n\nNội dung tùy chỉnh')
        self.assertEqual(self.g(bare, 'rev-parse', 'release^'), before)
        self.assertEqual(self.g(bare, 'rev-list', '--count', before + '..release'), '1')
        self.assertEqual(self.g(bare, 'show', 'release:first.txt'), 'first')
        self.assertEqual(self.g(bare, 'show', 'release:last.txt'), 'last')
        self.assertNotIn('unselected.txt', self.g(bare, 'ls-tree', '--name-only', 'release').splitlines())
        self.assertEqual(self.g(bare, 'show', 'release:b-work.txt'), 'keep B work')
        body = self.g(bare, 'show', '-s', '--format=%B', 'release')
        self.assertEqual(body, 'Gộp hai thay đổi')
        self.assertEqual(self.g(bare, 'show', '-s', '--format=%an <%ae>|%cn <%ce>', 'release'),
                         'Test User <test@example.invalid>|Test User <test@example.invalid>')
        self.assertEqual(self.g(self.root / 'a.git', 'rev-parse', 'feature'), original_a)
        tip = self.g(bare, 'rev-parse', 'release')
        self.selected_sync([first, last])
        self.assertEqual(tip, self.g(bare, 'rev-parse', 'release'))
        # An older, previously unselected commit can still be selected later.
        self.selected_sync([skipped])
        self.assertEqual(self.g(bare, 'show', 'release:unselected.txt'), 'unselected')

    def test_selected_conflict_keeps_b_and_ledger(self):
        self.run_sync(True)
        self.g(self.b, 'checkout', '-b', 'release')
        (self.b / 'shared.txt').write_text('B edit\n')
        self.commit(self.b, 'B conflicting change')
        self.g(self.b, 'push', 'origin', 'release')
        before = self.g(self.root / 'b.git', 'rev-parse', 'release')
        self.change_a('A edit\n')
        commit = self.g(self.a, 'rev-parse', 'HEAD')
        with self.assertRaisesRegex(SyncError, 'conflict'):
            self.selected_sync([commit])
        self.assertEqual(before, self.g(self.root / 'b.git', 'rev-parse', 'release'))
        self.assertEqual(list((self.root / 'state').glob('*/selected-state.json')), [])

    def test_selected_creates_missing_branch_from_b_default(self):
        self.change_a()
        commit = self.g(self.a, 'rev-parse', 'HEAD')
        base = self.g(self.root / 'b.git', 'rev-parse', 'main')
        self.selected_sync([commit])
        self.assertEqual(self.g(self.root / 'b.git', 'rev-parse', 'release^'), base)

    def test_new_branch_squash_uses_final_selected_snapshot_without_conflicts(self):
        # B's default code conflicts with A and has files absent from A.
        (self.b / 'shared.txt').write_text('unrelated B code\n')
        (self.b / 'only-b.txt').write_text('not in the source snapshot')
        workflow = self.b / '.github' / 'workflows' / 'ci.yml'
        workflow.parent.mkdir(parents=True)
        workflow.write_text('B workflow\n')
        self.commit(self.b, 'Different B default')
        self.g(self.b, 'push', 'origin', 'main')
        base = self.g(self.b, 'rev-parse', 'HEAD')
        commits = []
        for version in range(1, 4):
            (self.a / 'shared.txt').write_text('version ' + str(version) + '\n')
            (self.a / 'binary.bin').write_bytes(bytes([0, version, 255]))
            if version == 1:
                (self.a / 'kept-from-first.txt').write_text('keep first change')
                (self.a / 'temporary.txt').write_text('removed later')
            if version == 2:
                (self.a / 'kept-from-second.txt').write_text('keep second change')
                (self.a / 'temporary.txt').unlink()
            self.commit(self.a, 'Version ' + str(version))
            commits.append(self.g(self.a, 'rev-parse', 'HEAD'))
        # A later unselected commit must not leak into the result.
        self.change_a('unselected fourth version\n')
        source_tip = self.g(self.a, 'rev-parse', 'HEAD')
        message = 'feat(BTS-432): Change to color strategy in quote. removal of forced grouping for color'
        self.selected_sync(list(reversed(commits)), message)
        bare = self.root / 'b.git'
        self.assertEqual(self.g(bare, 'rev-parse', 'release^'), base)
        self.assertEqual(self.g(bare, 'rev-list', '--count', 'main..release'), '1')
        self.assertEqual(self.g(bare, 'show', '-s', '--format=%B', 'release'), message)
        self.assertEqual(self.g(bare, 'show', 'release:shared.txt'), 'version 3')
        for filename in ['shared.txt', 'binary.bin', 'kept-from-first.txt', 'kept-from-second.txt']:
            self.assertEqual(self.g(bare, 'rev-parse', 'release:' + filename),
                             self.g(self.a, 'rev-parse', commits[-1] + ':' + filename))
        files = self.g(bare, 'ls-tree', '-r', '--name-only', 'release').splitlines()
        self.assertNotIn('temporary.txt', files)
        self.assertNotIn('only-b.txt', files)
        self.assertEqual(self.g(bare, 'show', 'release:.github/workflows/ci.yml'), 'B workflow')
        self.assertEqual(self.g(self.a, 'rev-parse', 'HEAD'), source_tip)
        tip = self.g(bare, 'rev-parse', 'release')
        self.selected_sync(commits, message)
        self.assertEqual(self.g(bare, 'rev-parse', 'release'), tip)

    def test_explicit_base_branch_uses_latest_server_code_and_squashes_changes(self):
        self.g(self.b, 'checkout', '-b', 'project')
        (self.b / 'project-only.txt').write_text('project base')
        self.commit(self.b, 'Project base')
        old = self.g(self.b, 'rev-parse', 'HEAD')
        (self.b / 'latest-project.txt').write_text('latest server base')
        self.commit(self.b, 'Latest project')
        self.g(self.b, 'push', 'origin', 'project')
        base = self.g(self.b, 'rev-parse', 'HEAD')
        self.g(self.b, 'reset', '--hard', old)  # Local project is stale.
        commits = []
        for text in ['first', 'second', 'third']:
            self.change_a(text + '\n')
            commits.append(self.g(self.a, 'rev-parse', 'HEAD'))
        self.sync.run(str(self.root / 'a.git'), str(self.root / 'b.git'), 'feature', 'release',
                      selected=list(reversed(commits)), message='Only my title',
                      base_branch='project', local_b=str(self.b))
        bare = self.root / 'b.git'
        self.assertEqual(self.g(bare, 'rev-parse', 'release^'), base)
        self.assertEqual(self.g(bare, 'rev-list', '--count', 'project..release'), '1')
        self.assertEqual(self.g(bare, 'show', 'release:shared.txt'), 'third')
        self.assertEqual(self.g(bare, 'show', 'release:project-only.txt'), 'project base')
        self.assertEqual(self.g(bare, 'show', 'release:latest-project.txt'), 'latest server base')
        self.assertEqual(self.g(bare, 'show', '-s', '--format=%B', 'release'), 'Only my title')
        self.assertEqual(self.g(self.b, 'rev-parse', 'project'), old)
        self.assertEqual(self.g(self.b, 'rev-parse', 'release'), self.g(bare, 'rev-parse', 'release'))

    def test_missing_explicit_base_does_not_fall_back_or_push(self):
        self.change_a()
        commit = self.g(self.a, 'rev-parse', 'HEAD')
        with self.assertRaisesRegex(SyncError, 'does not exist on Source B server'):
            self.sync.run(str(self.root / 'a.git'), str(self.root / 'b.git'), 'feature', 'release',
                          selected=[commit], message='Title', base_branch='missing-project')
        self.assertEqual(self.g(self.root / 'b.git', 'for-each-ref', '--format=%(refname)', 'refs/heads/release'), '')

    def test_existing_branch_ignores_optional_base(self):
        first, _, last = self.selected_fixture()
        self.sync.run(str(self.root / 'a.git'), str(self.root / 'b.git'), 'feature', 'release',
                      selected=[first, last], message='Title', base_branch='missing-project')
        self.assertEqual(self.g(self.root / 'b.git', 'show', 'release:b-work.txt'), 'keep B work')

    def test_explicit_base_conflict_is_not_overwritten(self):
        self.g(self.b, 'checkout', '-b', 'project')
        (self.b / 'shared.txt').write_text('conflicting project code\n')
        self.commit(self.b, 'Project conflicting code')
        self.g(self.b, 'push', 'origin', 'project')
        before = self.g(self.b, 'rev-parse', 'HEAD')
        self.change_a()
        with self.assertRaisesRegex(SyncError, 'conflict'):
            self.sync.run(str(self.root / 'a.git'), str(self.root / 'b.git'), 'feature', 'release',
                          selected=[self.g(self.a, 'rev-parse', 'HEAD')], message='Title', base_branch='project')
        self.assertEqual(self.g(self.root / 'b.git', 'rev-parse', 'project'), before)
        self.assertEqual(self.g(self.root / 'b.git', 'for-each-ref', '--format=%(refname)', 'refs/heads/release'), '')

    def test_new_branch_snapshot_push_failure_recovers_one_commit(self):
        self.change_a('snapshot\n')
        commit = self.g(self.a, 'rev-parse', 'HEAD')
        original = sync_tool.git
        def fail_push(repo, *args, **kwargs):
            if args[0] == 'push':
                raise SyncError('Simulated push failure')
            return original(repo, *args, **kwargs)
        with patch('sync_tool.git', side_effect=fail_push):
            with self.assertRaisesRegex(SyncError, 'push failure'):
                self.selected_sync([commit])
        self.selected_sync([commit])
        self.assertEqual(self.g(self.root / 'b.git', 'rev-list', '--count', 'main..release'), '1')
        self.assertEqual(self.g(self.root / 'b.git', 'show', 'release:shared.txt'), 'snapshot')

    def test_selected_pending_push_recovers_without_duplicate(self):
        first, _, last = self.selected_fixture()
        original = sync_tool.git
        def fail_push(repo, *args, **kwargs):
            if args[0] == 'push':
                raise SyncError('Simulated push failure')
            return original(repo, *args, **kwargs)
        with patch('sync_tool.git', side_effect=fail_push):
            with self.assertRaises(SyncError):
                self.selected_sync([first, last])
        self.selected_sync([first, last])
        tip = self.g(self.root / 'b.git', 'rev-parse', 'release')
        self.selected_sync([first, last])
        self.assertEqual(tip, self.g(self.root / 'b.git', 'rev-parse', 'release'))

    def test_selected_successful_push_interruption_recovers(self):
        first, _, last = self.selected_fixture()
        original = sync_tool.git
        def push_then_fail(repo, *args, **kwargs):
            result = original(repo, *args, **kwargs)
            if args[0] == 'push':
                raise SyncError('Simulated interruption')
            return result
        with patch('sync_tool.git', side_effect=push_then_fail):
            with self.assertRaises(SyncError):
                self.selected_sync([first, last])
        tip = self.g(self.root / 'b.git', 'rev-parse', 'release')
        self.selected_sync([first, last])
        self.assertEqual(tip, self.g(self.root / 'b.git', 'rev-parse', 'release'))

    def test_commit_list_and_local_unpushed_commits(self):
        (self.a / 'local.txt').write_text('committed local change')
        self.commit(self.a, 'Local unpushed commit')
        commit = self.g(self.a, 'rev-parse', 'HEAD')
        (self.a / 'dirty.txt').write_text('not committed')
        rows = self.sync.run(str(self.a), str(self.root / 'b.git'), 'feature', 'release', list_only=True)
        self.assertEqual(rows[0]['sha'], commit)
        self.assertEqual(rows[0]['subject'], 'Local unpushed commit')
        self.sync.run(str(self.a), str(self.root / 'b.git'), 'feature', 'release',
                      selected=[commit], message='Sync local commit')
        files = self.g(self.root / 'b.git', 'ls-tree', '--name-only', 'release').splitlines()
        self.assertIn('local.txt', files)
        self.assertNotIn('dirty.txt', files)
        rows = self.sync.run(str(self.a), str(self.root / 'b.git'), 'feature', 'release', list_only=True)
        self.assertTrue(rows[0]['applied'])

    def test_selected_rejects_merge_and_stale_selection(self):
        self.g(self.a, 'checkout', '-b', 'side')
        (self.a / 'side.txt').write_text('side')
        self.commit(self.a, 'side change')
        side = self.g(self.a, 'rev-parse', 'HEAD')
        self.g(self.a, 'checkout', 'feature')
        with self.assertRaisesRegex(SyncError, 'no longer'):
            self.sync.run(str(self.a), str(self.root / 'b.git'), 'feature', 'release',
                          selected=[side], message='Should stop')
        self.g(self.a, 'merge', '--no-ff', 'side', '-m', 'merge side')
        merge = self.g(self.a, 'rev-parse', 'HEAD')
        with self.assertRaisesRegex(SyncError, 'Merge commits'):
            self.sync.run(str(self.a), str(self.root / 'b.git'), 'feature', 'release',
                          selected=[merge], message='Should stop')

    def test_local_folders_fetch_latest_server_without_touching_worktrees(self):
        stale_a, stale_b = self.root / 'stale-a', self.root / 'stale-b'
        self.g(self.root, 'clone', str(self.root / 'a.git'), str(stale_a))
        self.g(self.root, 'clone', str(self.root / 'b.git'), str(stale_b))
        ahead_before = self.g(stale_a, 'rev-parse', 'HEAD')
        bhead_before = self.g(stale_b, 'rev-parse', 'HEAD')
        (stale_a / 'shared.txt').write_text('local unsaved edit\n')
        (stale_b / 'private.txt').write_text('local untracked file')
        # These commits exist only on the servers relative to the stale local folders.
        self.change_a('latest on server\n')
        source_commit = self.g(self.a, 'rev-parse', 'HEAD')
        (self.b / 'server-b.txt').write_text('latest B server content')
        self.commit(self.b, 'B server update')
        self.g(self.b, 'push', 'origin', 'main')
        latest_b = self.g(self.root / 'b.git', 'rev-parse', 'main')
        opts = dict(local_a=str(stale_a), local_b=str(stale_b))
        urls = (str(self.root / 'a.git'), str(self.root / 'b.git'), 'feature', 'release')
        rows = self.sync.run(*urls, list_only=True, **opts)
        self.assertEqual(rows[0]['sha'], source_commit)
        self.sync.run(*urls, selected=[source_commit], message='Latest from server', **opts)
        self.assertEqual(self.g(self.root / 'b.git', 'show', 'release:shared.txt'), 'latest on server')
        self.assertEqual(self.g(self.root / 'b.git', 'rev-parse', 'release^'), latest_b)
        # A new branch uses A's snapshot, not files unique to B's default branch.
        self.assertNotIn('server-b.txt', self.g(self.root / 'b.git', 'ls-tree', '--name-only', 'release').splitlines())
        self.assertEqual(self.g(stale_a, 'rev-parse', 'HEAD'), ahead_before)
        self.assertEqual(self.g(stale_b, 'rev-parse', 'HEAD'), bhead_before)
        self.assertEqual((stale_a / 'shared.txt').read_text(), 'local unsaved edit\n')
        self.assertEqual((stale_b / 'private.txt').read_text(), 'local untracked file')

    def test_local_folder_does_not_fallback_when_server_unavailable(self):
        missing = self.root / 'missing-server.git'
        with self.assertRaises(SyncError):
            self.sync.run(str(missing), str(self.root / 'b.git'), 'feature', 'release',
                          list_only=True, local_a=str(self.a))
        rows = self.sync.run(str(self.root / 'a.git'), str(self.root / 'b.git'), 'feature', 'release',
                             list_only=True, local_a=str(self.root / 'missing-folder'))
        self.assertTrue(rows)

    def test_selected_applies_same_file_in_history_order(self):
        self.change_a('first change\n')
        first = self.g(self.a, 'rev-parse', 'HEAD')
        self.change_a('second change\n')
        second = self.g(self.a, 'rev-parse', 'HEAD')
        self.selected_sync([second, first])
        self.assertEqual(self.g(self.root / 'b.git', 'show', 'release:shared.txt'), 'second change')

    def test_selected_concurrent_b_push_rejected_and_not_overwritten(self):
        first, _, last = self.selected_fixture()
        original = sync_tool.git
        remote_tip = []
        def race(repo, *args, **kwargs):
            if args[0] == 'push':
                (self.b / 'concurrent.txt').write_text('concurrent B work')
                self.commit(self.b, 'B concurrent update')
                self.g(self.b, 'push', 'origin', 'release')
                remote_tip.append(self.g(self.b, 'rev-parse', 'HEAD'))
            return original(repo, *args, **kwargs)
        with patch('sync_tool.git', side_effect=race):
            with self.assertRaises(SyncError):
                self.selected_sync([first, last])
        self.assertEqual(self.g(self.root / 'b.git', 'rev-parse', 'release'), remote_tip[0])
        with self.assertRaisesRegex(SyncError, 'B changed'):
            self.selected_sync([first, last])

    def test_existing_repos_infer_origin_and_use_local_transport_configuration(self):
        self.change_a('using repo credentials\n')
        commit = self.g(self.a, 'rev-parse', 'HEAD')
        # These aliases only work with configuration from the existing clones.
        alias_a, alias_b = 'local-config-a://repo', 'local-config-b://repo'
        self.g(self.a, 'config', 'url.' + str(self.root / 'a.git') + '.insteadOf', alias_a)
        self.g(self.b, 'config', 'url.' + str(self.root / 'b.git') + '.insteadOf', alias_b)
        before_b = self.g(self.b, 'rev-parse', 'HEAD')
        self.sync.run(alias_a, alias_b, 'feature', 'release', selected=[commit],
                      message='Use existing local repo access', local_a=str(self.a), local_b=str(self.b))
        self.assertEqual(self.g(self.root / 'b.git', 'show', 'release:shared.txt'), 'using repo credentials')
        self.assertEqual(self.g(self.b, 'rev-parse', 'HEAD'), before_b)
        self.assertEqual(sync_tool.repository_settings('', str(self.a))['url'], str(self.root / 'a.git'))
        self.assertEqual(sync_tool.repository_settings('origin', str(self.b))['url'], str(self.root / 'b.git'))

    def test_parent_folder_is_ignored_without_searching_children(self):
        parent = self.root / 'parent'
        parent.mkdir()
        actual = parent / 'a'
        self.g(self.root, 'clone', str(self.root / 'a.git'), str(actual))
        self.assertIsNone(sync_tool.local_repository(str(parent)))
        settings = sync_tool.repository_settings(str(self.root / 'a.git'), str(parent))
        self.assertIsNone(settings['local'])
        self.assertEqual(settings['url'], str(self.root / 'a.git'))
        self.assertEqual(sync_tool.local_repository(str(actual)), actual.resolve())

    def test_preview_limits_history_and_saved_selections_to_latest_15(self):
        first = self.g(self.a, 'rev-parse', 'HEAD')
        for index in range(20):
            self.g(self.a, 'commit', '--allow-empty', '-m', 'Preview commit ' + str(index))
        self.g(self.a, 'push', 'origin', 'feature')
        last = self.g(self.a, 'rev-parse', 'HEAD')
        logs = []
        self.sync.log = logs.append
        args = (str(self.root / 'a.git'), str(self.root / 'b.git'), 'feature', 'release')
        for _ in range(2):
            rows = self.sync.run(*args, list_only=True, selected=[first, last])
            self.assertEqual(len(rows), 15)
            self.assertEqual(rows[0]['sha'], last)
            self.assertNotIn(first, {row['sha'] for row in rows})
        preview = next((self.root / 'state').glob('*/preview-repo'))
        self.assertEqual(self.g(preview, 'rev-parse', '--is-shallow-repository'), 'true')
        self.assertEqual(self.g(preview, 'rev-list', '--count', 'refs/remotes/source/feature'), '15')
        self.assertTrue(any('outside the latest 15' in line for line in logs))

    def test_non_root_and_missing_folder_are_ignored(self):
        inside = self.a / 'src' / 'components'
        inside.mkdir(parents=True)
        self.assertIsNone(sync_tool.local_repository(str(inside)))
        self.assertIsNone(sync_tool.local_repository(str(self.root / 'missing')))

    def test_plain_optional_folder_uses_server_url_without_modifying_folder(self):
        plain = self.root / 'source-files'
        plain.mkdir()
        (plain / 'important.txt').write_text('keep my files')
        self.change_a('latest server code\n')
        commit = self.g(self.a, 'rev-parse', 'HEAD')
        self.sync.run(str(self.root / 'a.git'), str(self.root / 'b.git'), 'feature', 'release',
                      local_a=str(plain), local_b=str(plain), selected=[commit], message='Server update')
        self.assertEqual(self.g(self.root / 'b.git', 'show', 'release:shared.txt'), 'latest server code')
        self.assertEqual([p.name for p in plain.iterdir()], ['important.txt'])
        self.assertEqual((plain / 'important.txt').read_text(), 'keep my files')
        with self.assertRaisesRegex(SyncError, 'Enter a repository URL'):
            sync_tool.repository_settings('', str(plain))

    def common_target(self):
        self.g(self.b, 'fetch', str(self.root / 'a.git'), 'main')
        self.g(self.b, 'checkout', '-B', 'release', 'FETCH_HEAD')
        (self.b / 'b-only.txt').write_text('existing B work')
        self.commit(self.b, 'Keep existing B commit')
        self.g(self.b, 'push', 'origin', 'release')
        return self.g(self.b, 'rev-parse', 'HEAD')

    def automatic_sync(self):
        return self.sync.run(str(self.root / 'a.git'), str(self.root / 'b.git'),
                             'feature', 'release', message='', preserve_commits=True)

    def test_new_b_uses_one_latest_snapshot_without_replaying_merge_history(self):
        self.g(self.a, 'checkout', '-b', 'side')
        (self.a / 'side.txt').write_text('side code')
        self.commit(self.a, 'Original side commit')
        self.g(self.a, 'checkout', 'feature')
        self.change_a('mainline update\n')
        self.g(self.a, 'merge', '--no-ff', 'side', '-m', 'Original merge title\n\nOriginal description')
        self.g(self.a, 'push', 'origin', 'feature')
        ahead = self.g(self.a, 'rev-parse', 'HEAD')
        original = sync_tool.git
        with patch('sync_tool.git', wraps=original) as commands:
            self.automatic_sync()
        invoked = [call.args[1] for call in commands.call_args_list]
        self.assertNotIn('apply', invoked)
        self.assertEqual(invoked.count('commit-tree'), 1)
        bare = self.root / 'b.git'
        self.assertEqual(self.g(bare, 'rev-list', '--count', 'release'), '1')
        self.assertEqual(self.g(bare, 'rev-parse', 'release^{tree}'), self.g(self.a, 'rev-parse', ahead + '^{tree}'))
        self.assertEqual(self.g(bare, 'show', '-s', '--format=%B', 'release'), 'Original merge title')
        tip = self.g(bare, 'rev-parse', 'release')
        self.automatic_sync()
        self.assertEqual(self.g(bare, 'rev-parse', 'release'), tip)
        self.change_a('next exact commit\n')
        self.automatic_sync()
        self.assertEqual(self.g(bare, 'rev-parse', 'release^'), tip)
        self.assertEqual(self.g(bare, 'show', 'release:shared.txt'), 'next exact commit')

    def replay_sync(self):
        return self.sync.run(str(self.root / 'a.git'), str(self.root / 'b.git'),
                             'feature', 'release', message='', preserve_commits=True, base_branch='main')

    def test_explicit_base_syncs_separate_commits_with_original_messages_and_authors(self):
        before = self.common_target()
        with patch.dict(os.environ, {'GIT_AUTHOR_NAME': 'Original Writer', 'GIT_AUTHOR_EMAIL': 'writer@example.invalid'}):
            self.change_a('first automatic change\n')
            first = self.g(self.a, 'rev-parse', 'HEAD')
            (self.a / 'extra.txt').write_text('second automatic change')
            self.commit(self.a, 'Second original subject\n\nOriginal body')
            self.g(self.a, 'push', 'origin', 'feature')
        source_tip = self.g(self.a, 'rev-parse', 'HEAD')
        self.replay_sync()
        bare = self.root / 'b.git'
        self.assertEqual(self.g(bare, 'rev-parse', 'release~2'), before)
        self.assertEqual(self.g(bare, 'rev-list', '--count', before + '..release'), '2')
        self.assertEqual(self.g(bare, 'show', '-s', '--format=%B', 'release'),
                         self.g(self.a, 'show', '-s', '--format=%B', source_tip))
        self.assertEqual(self.g(bare, 'show', '-s', '--format=%B', 'release^'),
                         self.g(self.a, 'show', '-s', '--format=%B', first))
        self.assertEqual(self.g(bare, 'show', '-s', '--format=%an <%ae>|%cn <%ce>', 'release'),
                         'Original Writer <writer@example.invalid>|Test User <test@example.invalid>')
        tip = self.g(bare, 'rev-parse', 'release')
        self.replay_sync()
        self.assertEqual(tip, self.g(bare, 'rev-parse', 'release'))
        self.assertEqual(self.g(self.root / 'a.git', 'rev-parse', 'feature'), source_tip)

    def test_explicit_base_after_squash_only_syncs_new_commits(self):
        first, _, last = self.selected_fixture()
        self.selected_sync([first, last])
        before = self.g(self.root / 'b.git', 'rev-parse', 'release')
        self.change_a('new after squash\n')
        self.replay_sync()
        self.assertEqual(self.g(self.root / 'b.git', 'rev-parse', 'release^'), before)
        self.assertEqual(self.g(self.root / 'b.git', 'show', 'release:shared.txt'), 'new after squash')

    def test_blank_message_conflict_pushes_nothing(self):
        self.common_target()
        (self.b / 'shared.txt').write_text('conflicting B work\n')
        self.commit(self.b, 'B conflicting edit')
        self.g(self.b, 'push', 'origin', 'release')
        before = self.g(self.b, 'rev-parse', 'HEAD')
        (self.a / 'first-ok.txt').write_text('first safe change')
        self.commit(self.a, 'First safe change')
        self.change_a('conflicting A work\n')
        with self.assertRaisesRegex(SyncError, 'conflict'):
            self.automatic_sync()
        self.assertEqual(self.g(self.root / 'b.git', 'rev-parse', 'release'), before)
        self.assertEqual(list((self.root / 'state').glob('*/selected-state.json')), [])

    def test_existing_unrelated_b_syncs_latest_commit_then_only_new_commits(self):
        self.g(self.b, 'checkout', '--orphan', 'release')
        (self.b / 'b-only.txt').write_text('keep B code')
        self.commit(self.b, 'Unrelated root')
        self.g(self.b, 'push', 'origin', 'release')
        before = self.g(self.b, 'rev-parse', 'HEAD')
        (self.a / 'older.txt').write_text('older unselected source change')
        self.commit(self.a, 'Older A commit')
        self.change_a('latest A code\n')
        source = self.g(self.a, 'rev-parse', 'HEAD')
        self.automatic_sync()
        bare = self.root / 'b.git'
        self.assertEqual(self.g(bare, 'rev-parse', 'release^'), before)
        self.assertEqual(self.g(bare, 'show', 'release:shared.txt'), 'latest A code')
        self.assertEqual(self.g(bare, 'show', 'release:b-only.txt'), 'keep B code')
        self.assertNotIn('older.txt', self.g(bare, 'ls-tree', '--name-only', 'release').splitlines())
        self.assertEqual(self.g(bare, 'show', '-s', '--format=%B', 'release'),
                         self.g(self.a, 'show', '-s', '--format=%B', source))
        tip = self.g(bare, 'rev-parse', 'release')
        self.automatic_sync()
        self.assertEqual(self.g(bare, 'rev-parse', 'release'), tip)
        self.change_a('next A change\n')
        self.automatic_sync()
        self.assertEqual(self.g(bare, 'rev-parse', 'release^'), tip)
        self.assertEqual(self.g(bare, 'show', 'release:shared.txt'), 'next A change')

    def test_existing_divergent_b_blank_options_replays_code_but_excludes_github(self):
        before = self.target_workflow()
        self.workflow_changes()
        self.automatic_sync()
        bare = self.root / 'b.git'
        self.assertEqual(self.g(bare, 'rev-parse', 'release^'), before)
        self.assertEqual(self.g(bare, 'show', 'release:b-only.txt'), 'existing B work')
        self.assertEqual(self.g(bare, 'show', 'release:.github/workflows/deploy.yml'), 'keep existing B workflow')
        self.assertEqual(self.g(bare, 'show', '-s', '--format=%B', 'release'), 'Mixed code and workflow')
        tip = self.g(bare, 'rev-parse', 'release')
        self.automatic_sync()
        self.assertEqual(self.g(bare, 'rev-parse', 'release'), tip)

    def test_existing_b_blank_options_failed_push_retries_without_duplicate(self):
        self.common_target()
        self.change_a('retry new code\n')
        original = sync_tool.git
        def fail_push(repo, *args, **kwargs):
            if args[0] == 'push':
                raise SyncError('Simulated push failure')
            return original(repo, *args, **kwargs)
        with patch('sync_tool.git', side_effect=fail_push):
            with self.assertRaisesRegex(SyncError, 'push failure'):
                self.automatic_sync()
        self.automatic_sync()
        bare = self.root / 'b.git'
        tip = self.g(bare, 'rev-parse', 'release')
        self.assertEqual(self.g(bare, 'show', 'release:shared.txt'), 'retry new code')
        self.automatic_sync()
        self.assertEqual(self.g(bare, 'rev-parse', 'release'), tip)

    def empty_target(self):
        self.g(self.root / 'b.git', 'update-ref', '-d', 'refs/heads/main')

    def test_empty_b_default_creates_branch_with_source_history(self):
        self.empty_target()
        self.change_a('first server import\n')
        ahead = self.g(self.a, 'rev-parse', 'HEAD')
        self.automatic_sync()
        imported = self.g(self.root / 'b.git', 'rev-parse', 'release')
        self.assertEqual(self.g(self.root / 'b.git', 'rev-parse', 'release^{tree}'), self.g(self.a, 'rev-parse', 'HEAD^{tree}'))
        self.assertEqual(self.g(self.root / 'b.git', 'for-each-ref', '--format=%(refname)', 'refs/heads'),
                         'refs/heads/release')
        self.change_a('next update\n')
        self.automatic_sync()
        self.assertEqual(self.g(self.root / 'b.git', 'rev-parse', 'release^'), imported)
        self.assertEqual(self.g(self.root / 'b.git', 'show', 'release:shared.txt'), 'next update')

    def test_empty_b_custom_message_combines_selected_commits(self):
        self.empty_target()
        baseline = self.g(self.a, 'rev-parse', 'HEAD')
        self.change_a('first selected change\n')
        first = self.g(self.a, 'rev-parse', 'HEAD')
        self.change_a('second selected change\n')
        second = self.g(self.a, 'rev-parse', 'HEAD')
        self.selected_sync([first, second], 'Initial combined import')
        self.assertEqual(self.g(self.root / 'b.git', 'rev-list', '--count', 'release'), '1')
        self.assertEqual(self.g(self.root / 'b.git', 'show', '-s', '--format=%s', 'release'), 'Initial combined import')
        self.assertEqual(self.g(self.root / 'b.git', 'show', 'release:shared.txt'), 'second selected change')

    def test_empty_b_custom_root_has_no_internal_parent(self):
        self.empty_target()
        root = self.g(self.a, 'rev-parse', 'HEAD')
        self.change_a('custom root content\n')
        last = self.g(self.a, 'rev-parse', 'HEAD')
        self.selected_sync([root, last], 'My root import')
        self.assertEqual(self.g(self.root / 'b.git', 'rev-list', '--count', 'release'), '1')
        self.assertEqual(self.g(self.root / 'b.git', 'show', '-s', '--format=%an <%ae>', 'release'),
                         'Test User <test@example.invalid>')
        self.assertEqual(self.g(self.root / 'b.git', 'show', 'release:shared.txt'), 'custom root content')

    def test_no_default_head_uses_existing_main_for_new_branch(self):
        self.common_target()
        self.g(self.root / 'b.git', 'symbolic-ref', 'HEAD', 'refs/heads/missing-default')
        baseline = self.g(self.root / 'b.git', 'rev-parse', 'main')
        self.change_a('new branch content\n')
        source = self.g(self.a, 'rev-parse', 'HEAD')
        self.sync.run(str(self.root / 'a.git'), str(self.root / 'b.git'), 'feature', 'brand-new',
                      selected=[source], message='Create missing branch')
        self.assertEqual(self.g(self.root / 'b.git', 'rev-parse', 'brand-new^'), baseline)

    def test_empty_b_pending_creation_recovers(self):
        self.empty_target()
        self.change_a('initial import retry\n')
        original = sync_tool.git
        def fail_push(repo, *args, **kwargs):
            if args[0] == 'push':
                raise SyncError('Simulated push failure')
            return original(repo, *args, **kwargs)
        with patch('sync_tool.git', side_effect=fail_push):
            with self.assertRaises(SyncError):
                self.automatic_sync()
        self.automatic_sync()
        self.assertEqual(self.g(self.root / 'b.git', 'rev-parse', 'release^{tree}'), self.g(self.a, 'rev-parse', 'HEAD^{tree}'))
        self.assertEqual(self.g(self.root / 'b.git', 'rev-list', '--count', 'release'), '1')

    def workflow_changes(self):
        path = self.a / '.github' / 'workflows' / 'deploy.yml'
        path.parent.mkdir(parents=True)
        path.write_text('source workflow version one\n')
        (self.a / 'code.txt').write_text('code to copy\n')
        self.commit(self.a, 'Mixed code and workflow')
        first = self.g(self.a, 'rev-parse', 'HEAD')
        path.write_text('source workflow version two\n')
        self.commit(self.a, 'Only workflow changes')
        second = self.g(self.a, 'rev-parse', 'HEAD')
        self.g(self.a, 'push', 'origin', 'feature')
        return first, second

    def target_workflow(self):
        before = self.common_target()
        path = self.b / '.github' / 'workflows' / 'deploy.yml'
        path.parent.mkdir(parents=True)
        path.write_text('keep existing B workflow\n')
        self.commit(self.b, 'B workflow stays')
        self.g(self.b, 'push', 'origin', 'release')
        return self.g(self.b, 'rev-parse', 'HEAD')

    def test_explicit_base_excludes_workflows_and_skips_workflow_only_commits(self):
        before = self.target_workflow()
        self.workflow_changes()
        self.replay_sync()
        bare = self.root / 'b.git'
        self.assertEqual(self.g(bare, 'show', 'release:.github/workflows/deploy.yml'), 'keep existing B workflow')
        self.assertEqual(self.g(bare, 'show', 'release:code.txt'), 'code to copy')
        self.assertEqual(self.g(bare, 'rev-list', '--count', before + '..release'), '1')
        self.assertEqual(self.g(bare, 'diff', '--name-only', before, 'release', '--', '.github/workflows'), '')

    def test_custom_message_excludes_workflows(self):
        before = self.target_workflow()
        first, second = self.workflow_changes()
        self.selected_sync([first, second], 'Copy code without workflows')
        bare = self.root / 'b.git'
        self.assertEqual(self.g(bare, 'rev-parse', 'release^'), before)
        self.assertEqual(self.g(bare, 'show', 'release:code.txt'), 'code to copy')
        self.assertEqual(self.g(bare, 'show', 'release:.github/workflows/deploy.yml'), 'keep existing B workflow')

    def test_blank_sync_excludes_github_even_when_b_can_fast_forward(self):
        source_root = self.g(self.a, 'rev-parse', 'HEAD')
        self.g(self.root / 'b.git', 'fetch', str(self.a), source_root + ':refs/heads/release')
        self.workflow_changes()
        self.automatic_sync()
        bare = self.root / 'b.git'
        self.assertEqual(self.g(bare, 'rev-parse', 'release^'), source_root)
        self.assertEqual(self.g(bare, 'log', '--format=', '--name-only', 'release', '--', '.github'), '')
        self.assertEqual(self.g(bare, 'show', 'release:code.txt'), 'code to copy')

    def test_blank_sync_preserves_b_github_configuration(self):
        self.common_target()
        folder = self.b / '.github'
        folder.mkdir()
        (folder / 'CODEOWNERS').write_text('B owners\n')
        self.commit(self.b, 'B owners')
        self.g(self.b, 'push', 'origin', 'release')
        before = self.g(self.b, 'rev-parse', 'HEAD')
        self.workflow_changes()
        (self.a / '.github' / 'CODEOWNERS').write_text('A owners\n')
        self.commit(self.a, 'A owners')
        self.g(self.a, 'push', 'origin', 'feature')
        self.automatic_sync()
        bare = self.root / 'b.git'
        self.assertEqual(self.g(bare, 'show', 'release:.github/CODEOWNERS'), 'B owners')
        self.assertEqual(self.g(bare, 'diff', '--name-only', before, 'release', '--', '.github'), '')
        self.assertEqual(self.g(bare, 'show', 'release:code.txt'), 'code to copy')

    def test_new_blank_sync_filters_deleted_github_from_all_ancestors(self):
        self.workflow_changes()
        self.g(self.a, 'rm', '-r', '.github')
        self.commit(self.a, 'Delete GitHub files')
        self.g(self.a, 'push', 'origin', 'feature')
        self.automatic_sync()
        bare = self.root / 'b.git'
        self.assertEqual(self.g(bare, 'log', '--format=', '--name-only', 'release', '--', '.github'), '')
        self.assertEqual(self.g(bare, 'show', 'release:code.txt'), 'code to copy')

    def test_empty_target_blank_sync_excludes_all_github_history(self):
        self.empty_target()
        self.workflow_changes()
        (self.a / '.github' / 'CODEOWNERS').write_text('* @source-owner\n')
        self.commit(self.a, 'GitHub configuration only')
        self.g(self.a, 'push', 'origin', 'feature')
        self.automatic_sync()
        bare = self.root / 'b.git'
        self.assertEqual(self.g(bare, 'log', '--format=', '--name-only', 'release', '--', '.github'), '')
        self.assertEqual(self.g(bare, 'show', 'release:code.txt'), 'code to copy')
        self.assertEqual(self.g(bare, 'show', '-s', '--format=%B', 'release'), 'GitHub configuration only')
        self.assertEqual(self.g(bare, 'rev-list', '--count', 'release'), '1')
        tip = self.g(bare, 'rev-parse', 'release')
        self.automatic_sync()
        self.assertEqual(self.g(bare, 'rev-parse', 'release'), tip)

    def test_exact_sync_does_not_replay_old_filtered_pending_push(self):
        import json
        self.empty_target()
        self.workflow_changes()
        self.sync.run(str(self.root / 'a.git'), str(self.root / 'b.git'), 'feature', 'release', list_only=True)
        folder = next((self.root / 'state').iterdir())
        ahead = self.g(self.a, 'rev-parse', 'HEAD')
        imported = self.g(self.a, 'rev-list', '--reverse', 'HEAD').split()
        (folder / 'selected-state.json').write_text(json.dumps({
            'b': ahead, 'applied': [], 'branch_existed': False,
            'pending': {'b': ahead, 'applied': imported}}), encoding='utf-8')
        self.automatic_sync()
        bare = self.root / 'b.git'
        self.assertEqual(self.g(bare, 'show', 'release:code.txt'), 'code to copy')
        self.assertEqual(self.g(bare, 'log', '--format=', '--name-only', 'release', '--', '.github'), '')
        self.assertEqual(self.g(bare, 'rev-list', '--count', 'release'), '1')
        self.assertEqual(len(list(folder.glob('pending-before-snapshot-*.json'))), 1)

    def test_empty_target_workflow_only_selection_pushes_filtered_baseline(self):
        self.empty_target()
        _, workflow_only = self.workflow_changes()
        self.selected_sync([workflow_only], 'Only code should be stored')
        bare = self.root / 'b.git'
        self.assertEqual(self.g(bare, 'show', 'release:code.txt'), 'code to copy')
        self.assertEqual(self.g(bare, 'show', 'release:shared.txt'), 'base')
        self.assertNotIn('internal@invalid', self.g(bare, 'log', '--format=%ae', 'release'))
        self.assertEqual(self.g(bare, 'log', '--format=', '--name-only', 'release', '--', '.github/workflows'), '')

    def test_empty_local_b_is_initialized_and_filled_even_without_new_source_commits(self):
        self.empty_target()
        self.change_a('server already synced\n')
        self.automatic_sync()
        before = self.g(self.root / 'b.git', 'rev-parse', 'release')
        local_b = self.root / 'new-local-b'
        local_b.mkdir()
        self.sync.run(str(self.root / 'a.git'), str(self.root / 'b.git'), 'feature', 'release',
                      preserve_commits=True, local_b=str(local_b))
        self.assertTrue((local_b / '.git').is_dir())
        self.assertEqual((local_b / 'shared.txt').read_text(), 'server already synced\n')
        self.assertEqual(self.g(local_b, 'branch', '--show-current'), 'release')
        self.assertEqual(self.g(local_b, 'rev-parse', 'HEAD'), before)
        self.assertEqual(self.g(local_b, 'remote', 'get-url', 'origin'), str(self.root / 'b.git'))
        self.assertEqual(self.g(self.root / 'b.git', 'rev-parse', 'release'), before)

    def test_missing_local_b_folder_and_empty_server_created_in_one_sync(self):
        self.empty_target()
        self.workflow_changes()
        local_b = self.root / 'new' / 'local-b'
        self.sync.run(str(self.root / 'a.git'), str(self.root / 'b.git'), 'feature', 'release',
                      preserve_commits=True, local_b=str(local_b))
        self.assertEqual((local_b / 'code.txt').read_text(), 'code to copy\n')
        self.assertFalse((local_b / '.github').exists())
        self.assertEqual(self.g(local_b, 'rev-parse', 'HEAD'), self.g(self.root / 'b.git', 'rev-parse', 'release'))

    def test_missing_b_branch_with_unrelated_default_imports_full_source_code(self):
        self.g(self.b, 'checkout', '--orphan', 'unrelated')
        self.g(self.b, 'rm', '-rf', '.')
        (self.b / 'b-only.txt').write_text('old default content')
        flow = self.b / '.github' / 'workflows' / 'deploy.yml'
        flow.parent.mkdir(parents=True)
        flow.write_text('keep B workflow\n')
        self.commit(self.b, 'Independent B root')
        self.g(self.b, 'push', 'origin', 'unrelated')
        self.g(self.root / 'b.git', 'symbolic-ref', 'HEAD', 'refs/heads/unrelated')
        default_before = self.g(self.root / 'b.git', 'rev-parse', 'unrelated')
        self.workflow_changes()
        self.automatic_sync()
        bare = self.root / 'b.git'
        self.assertEqual(self.g(bare, 'show', 'release:code.txt'), 'code to copy')
        self.assertEqual(self.g(bare, 'log', '--format=', '--name-only', 'release', '--', '.github'), '')
        self.assertNotIn('b-only.txt', self.g(bare, 'ls-tree', '--name-only', 'release').splitlines())
        self.assertEqual(self.g(bare, 'rev-parse', 'unrelated'), default_before)
        self.assertEqual(self.g(bare, 'show', '-s', '--format=%B', 'release'), 'Only workflow changes')
        self.assertEqual(self.g(bare, 'rev-list', '--count', 'release'), '1')

    def test_deleted_b_branch_is_recreated_from_source(self):
        self.empty_target()
        self.workflow_changes()
        self.automatic_sync()
        self.g(self.root / 'b.git', 'update-ref', '-d', 'refs/heads/release')
        self.automatic_sync()
        self.assertEqual(self.g(self.root / 'b.git', 'show', 'release:code.txt'), 'code to copy')
        self.assertEqual(self.g(self.root / 'b.git', 'log', '--format=', '--name-only', 'release', '--', '.github'), '')

    def test_existing_local_b_branch_fast_forwards_after_server_sync(self):
        self.common_target()
        self.change_a('update local and server\n')
        self.sync.run(str(self.root / 'a.git'), str(self.root / 'b.git'), 'feature', 'release',
                      preserve_commits=True, local_b=str(self.b), base_branch='main')
        self.assertEqual((self.b / 'shared.txt').read_text(), 'update local and server\n')
        self.assertEqual(self.g(self.b, 'rev-parse', 'HEAD'), self.g(self.root / 'b.git', 'rev-parse', 'release'))

    def test_preinitialized_empty_local_b_gets_origin_and_code(self):
        self.common_target()
        self.change_a('populate initialized folder\n')
        self.replay_sync()
        local_b = self.root / 'manual-init'
        local_b.mkdir()
        self.g(local_b, 'init', '--initial-branch=main')
        self.sync.run(str(self.root / 'a.git'), str(self.root / 'b.git'), 'feature', 'release',
                      preserve_commits=True, local_b=str(local_b), base_branch='main')
        self.assertEqual((local_b / 'shared.txt').read_text(), 'populate initialized folder\n')
        self.assertEqual(self.g(local_b, 'branch', '--show-current'), 'release')

    def test_loading_commits_does_not_initialize_b_folder(self):
        local_b = self.root / 'not-created-yet'
        self.sync.run(str(self.root / 'a.git'), str(self.root / 'b.git'), 'feature', 'release',
                      list_only=True, local_b=str(local_b))
        self.assertFalse(local_b.exists())

    def test_dirty_current_local_b_is_preserved_after_server_sync(self):
        self.common_target()
        before = self.g(self.b, 'rev-parse', 'HEAD')
        (self.b / 'shared.txt').write_text('keep local unfinished work\n')
        self.change_a('new server code\n')
        self.sync.run(str(self.root / 'a.git'), str(self.root / 'b.git'), 'feature', 'release',
                      preserve_commits=True, local_b=str(self.b), base_branch='main')
        self.assertEqual((self.b / 'shared.txt').read_text(), 'keep local unfinished work\n')
        self.assertEqual(self.g(self.b, 'rev-parse', 'HEAD'), before)
        self.assertEqual(self.g(self.root / 'b.git', 'show', 'release:shared.txt'), 'new server code')

    def test_local_refresh_does_not_overwrite_ignored_files(self):
        self.common_target()
        (self.b / '.git' / 'info' / 'exclude').write_text('local-settings.txt\n')
        (self.b / 'local-settings.txt').write_text('keep local settings')
        (self.a / 'local-settings.txt').write_text('server version')
        self.commit(self.a, 'Add tracked settings example')
        self.g(self.a, 'push', 'origin', 'feature')
        with self.assertRaisesRegex(SyncError, 'Server sync completed, but local B'):
            self.sync.run(str(self.root / 'a.git'), str(self.root / 'b.git'), 'feature', 'release',
                          preserve_commits=True, local_b=str(self.b), base_branch='main')
        self.assertEqual((self.b / 'local-settings.txt').read_text(), 'keep local settings')
        self.assertEqual(self.g(self.root / 'b.git', 'show', 'release:local-settings.txt'), 'server version')


class History(unittest.TestCase):
    def test_labels_distinguish_same_repo_and_folder_names(self):
        one = dict(id='abcdef123456', time='2026-11-13T10:30:00+07:00', status='Completed',
                   inputs=['https://github.com/team-one/BizHub.git', 'git@github.com:quochieule/BizHub.git',
                           'dev', 'BTS-428-' + 'long-branch-' * 10,
                           r'C:\team-one\BizHub', r'D:\team-two\BizHub'])
        two = dict(one, id='123456abcdef', inputs=[
            'https://github.com/team-two/BizHub.git', 'https://github.com/quochieule/BizHub',
            'dev', one['inputs'][3], r'E:\team-one\BizHub', r'F:\team-two\BizHub'])
        label = sync_tool.history_label(one)
        self.assertIn('13/11 10:30:00', label)
        self.assertIn('team-one/BizHub', label)
        self.assertIn('quochieule/BizHub', label)
        self.assertNotEqual(label, sync_tool.history_label(two))
        detail = sync_tool.history_details(one)
        self.assertIn(r'C:\team-one\BizHub', detail)
        self.assertIn(r'D:\team-two\BizHub', detail)
        self.assertIn(one['inputs'][3], detail)
        self.assertNotEqual(detail, sync_tool.history_details(two))

    def test_history_display_does_not_include_url_credentials(self):
        display = sync_tool.history_repository('https://user:secret@github.com/owner/repo.git?token=hidden')
        self.assertEqual(display, 'owner/repo')

    def test_workflow_rejection_has_specific_recovery_instructions(self):
        error = SyncError('refusing to allow an OAuth App to create or update workflow '
                          '`.github/workflows/deploy.yml` without `workflow` scope')
        text = sync_tool.push_error_message('Push to Source B', error)
        self.assertIn('lacks workflow permission', text)
        self.assertIn('No workflow files were removed', text)
        self.assertIn('.github/workflows/deploy.yml', text)
        self.assertNotIn('delete', text.lower())

    def test_only_completed_is_saved_and_repeat_updates_the_same_entry(self):
        with tempfile.TemporaryDirectory() as temp:
            store = sync_tool.HistoryStore(temp)
            inputs = ['repo-a', 'repo-b', 'dev', 'release', 'folder a', 'folder b']
            first = store.add(inputs, 'Selected commits (squash)', 'Gộp commit\n\nChi tiết', ['a' * 40, 'b' * 40])
            self.assertFalse(store.path.exists())
            store.finish(first, 'Completed')
            second = store.add(inputs, 'Selected commits (squash)', 'Try again', ['c' * 40])
            store.finish(second, 'Failed')
            rows = sync_tool.HistoryStore(temp).read()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['id'], first)
            self.assertEqual(rows[0]['status'], 'Completed')
            self.assertEqual(rows[0]['inputs'], inputs)
            self.assertEqual(rows[0]['message'], 'Gộp commit\n\nChi tiết')
            third = store.add(inputs, 'Selected commits (squash)', 'Newest message', ['c' * 40])
            store.finish(third, 'Completed')
            rows = store.read()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['id'], first)
            self.assertEqual(rows[0]['message'], 'Newest message')
            self.assertEqual(rows[0]['selected'], ['c' * 40])

    def test_cleanup_removes_old_failures_and_duplicates_with_different_times(self):
        import json
        with tempfile.TemporaryDirectory() as temp:
            store = sync_tool.HistoryStore(temp)
            inputs = ['https://github.com/team/a.git', 'https://github.com/team/b', 'dev', 'release', r'C:\repo\a', r'D:\repo\b']
            first = store.add(inputs, 'Preserve commits', '', [])
            store.finish(first, 'Completed')
            row = store.read()[0]
            older = dict(row, id='older', time='2020-01-01T10:00:00+07:00')
            failed = dict(row, id='failed', status='Failed')
            running = dict(row, id='running', status='Running')
            variants = list(inputs)
            variants[0] = 'git@github.com:TEAM/a.git'
            variants[4] = 'c:/REPO/a/'
            older['inputs'] = variants
            store.path.write_text(json.dumps([failed, row, older, running]), encoding='utf-8')
            store.cleanup()
            raw = json.loads(store.path.read_text(encoding='utf-8'))
            self.assertEqual(len(raw), 1)
            self.assertEqual(raw[0]['id'], first)

    def test_corrupt_history_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            store = sync_tool.HistoryStore(temp)
            store.path.write_text('{broken', encoding='utf-8')
            with self.assertRaises(SyncError):
                store.add([''] * 6, 'Changes since baseline', '', [])
            self.assertEqual(store.path.read_text(), '{broken')

    def test_history_is_limited_to_100_unique_completed_configurations(self):
        with tempfile.TemporaryDirectory() as temp:
            store = sync_tool.HistoryStore(temp)
            for i in range(102):
                record = store.add(['a', 'b', 'dev', 'release-' + str(i), '', ''], 'Preserve commits', str(i), [])
                store.finish(record, 'Completed')
            rows = store.read()
            self.assertEqual(len(rows), 100)
            self.assertEqual(rows[0]['message'], '101')
            self.assertEqual(rows[-1]['message'], '2')


class Interface(unittest.TestCase):
    @staticmethod
    def close_window(window):
        window.update_idletasks()
        for job in window.tk.call('after', 'info'):
            window.after_cancel(job)
        window.destroy()

    @staticmethod
    def repo_fields(window):
        return [window.nametowidget('repositories.' + source + '.' + name)
                for name in ('url', 'branch', 'folder') for source in ('source_a', 'source_b')]

    def test_load_select_message_and_local_folder_wiring(self):
        calls = []
        rows = [dict(sha='a' * 40, parents='c' * 40, author='Original A', subject='First', applied=False),
                dict(sha='b' * 40, parents='a' * 40, author='Original B', subject='Second', applied=False)]
        class FakeSync:
            def __init__(self, **kwargs):
                pass
            def run(self, *args, **kwargs):
                calls.append((args, kwargs))
                return rows if kwargs.get('list_only') else None
        class ImmediateThread:
            def __init__(self, target, **kwargs):
                self.target = target
            def start(self):
                self.target()
        original_loop = sync_tool.tk.Tk.mainloop
        def inspect(window):
            try:
                window.withdraw()
                def descendants(widget):
                    result = []
                    for child in widget.winfo_children():
                        result += [child] + descendants(child)
                    return result
                widgets = descendants(window)
                self.assertFalse(any(isinstance(w, sync_tool.ttk.Button) and w.cget('text') == 'Use repo remotes'
                                     for w in widgets))
                self.assertEqual(len([w for w in widgets if isinstance(w, sync_tool.ttk.Combobox)]), 1)
                entries = self.repo_fields(window)
                base_entry = window.nametowidget('repositories.source_b.base_branch')
                base_entry.insert(0, 'project')
                for field, value in zip(entries, ['a-url', 'b-url', 'dev', 'release', 'a-folder', 'b-folder']):
                    field.insert(0, value)
                browse = next(w for w in widgets if isinstance(w, sync_tool.ttk.Button) and w.cget('text').startswith('Browse'))
                with patch.object(sync_tool.filedialog, 'askdirectory', return_value='chosen-a-folder'):
                    browse.invoke()
                load = next(w for w in widgets if isinstance(w, sync_tool.ttk.Button) and w.cget('text').startswith('Load commits'))
                load.invoke()
                window.after(130, window.quit)
                original_loop(window)
                tree = next(w for w in widgets if isinstance(w, sync_tool.ttk.Treeview))
                self.assertEqual(len(tree.get_children()), 2)
                tree.selection_set(['a' * 40, 'b' * 40])
                message = next(w for w in window.winfo_children() if isinstance(w, sync_tool.tk.Text)
                               and str(w.cget('state')) == 'normal')
                message.insert('1.0', 'My custom squash\n\nDetails')
                button = next(w for w in widgets if isinstance(w, sync_tool.ttk.Button) and w.cget('text').startswith('Sync A'))
                button.invoke()
                self.assertEqual(calls[-1][0], ('a-url', 'b-url', 'dev', 'release'))
                self.assertEqual(set(calls[-1][1]['selected']), {'a' * 40, 'b' * 40})
                self.assertEqual(calls[-1][1]['message'], 'My custom squash\n\nDetails')
                self.assertEqual(calls[-1][1]['local_a'], 'chosen-a-folder')
                self.assertEqual(calls[-1][1]['local_b'], 'b-folder')
                self.assertEqual(calls[-1][1]['base_branch'], 'project')
                window.after(130, window.quit)
                original_loop(window)
                saved = sync_tool.HistoryStore(Path(temp) / 'GitSyncTool').read()
                self.assertEqual(len(saved), 1)
                self.assertEqual(saved[0]['status'], 'Completed')
                self.assertEqual(saved[0]['inputs'], ['a-url', 'b-url', 'dev', 'release', 'chosen-a-folder', 'b-folder', 'project'])
                entries[2].delete(0, 'end')
                self.assertEqual(tree.get_children(), ())
                message.delete('1.0', 'end')
                message.insert('1.0', 'Changed text')
                open_history = next(w for w in widgets if isinstance(w, sync_tool.ttk.Button)
                                    and w.cget('text') == 'Open history')
                before_calls = len(calls)
                open_history.invoke()
                window.after(130, window.quit)
                original_loop(window)
                self.assertEqual([w.get() for w in entries] + [base_entry.get()], saved[0]['inputs'])
                self.assertEqual(message.get('1.0', 'end-1c'), 'My custom squash\n\nDetails')
                self.assertEqual(set(tree.selection()), {'a' * 40, 'b' * 40})
                self.assertEqual(len(calls), before_calls + 1)
                self.assertTrue(calls[-1][1]['list_only'])
                # Failed syncs also remain selectable from history.
                def failure(*args, **kwargs):
                    raise SyncError('Simulated access failure')
                with patch.object(FakeSync, 'run', failure):
                    button.invoke()
                window.after(130, window.quit)
                original_loop(window)
                saved = sync_tool.HistoryStore(Path(temp) / 'GitSyncTool').read()
                self.assertEqual(len(saved), 1)
                self.assertEqual(saved[0]['status'], 'Completed')
                # Blank message starts default sync without requiring a loaded list or selection.
                message.delete('1.0', 'end')
                entries[2].delete(0, 'end')
                entries[2].insert(0, 'another-branch')
                button.invoke()
                self.assertTrue(calls[-1][1]['preserve_commits'])
                self.assertIsNone(calls[-1][1]['selected'])
                self.assertEqual(calls[-1][1]['message'], '')
                window.after(130, window.quit)
                original_loop(window)
                # A custom message requires a real selection before running.
                message.insert('1.0', 'Combine these')
                count = len(calls)
                with patch.object(sync_tool.messagebox, 'showinfo') as notice:
                    button.invoke()
                notice.assert_called_once()
                self.assertEqual(len(calls), count)
            finally:
                self.close_window(window)
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'LOCALAPPDATA': temp}), \
                patch('sync_tool.Sync', FakeSync), patch('sync_tool.threading.Thread', ImmediateThread), \
                patch('sync_tool.local_repository', side_effect=lambda folder, url='': Path(folder) if folder else None), \
                patch('sync_tool.repository_settings', side_effect=lambda url, folder=None, **kw:
                      dict(url='a-url' if kw.get('use_remote') and folder == 'chosen-a-folder' else url,
                           local=Path(folder) if folder else None, target=url)), \
                patch.object(sync_tool.tk.Tk, 'mainloop', inspect):
            sync_tool.main()

    def test_browse_fills_matching_url_from_existing_repo(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name, remote, url in [('a', 'origin', 'https://example.invalid/a.git'),
                                      ('b', 'upstream', 'git@example.invalid:b.git'),
                                      ('no-remote', None, None)]:
                repo = root / name
                repo.mkdir()
                sync_tool.git(repo, 'init')
                if remote:
                    sync_tool.git(repo, 'remote', 'add', remote, url)
            def inspect(window):
                try:
                    window.withdraw()
                    entries = self.repo_fields(window)
                    browsers = [window.nametowidget('repositories.' + source + '.browse')
                                for source in ('source_a', 'source_b')]
                    for source in ('source_a', 'source_b'):
                        group = window.nametowidget('repositories.' + source)
                        self.assertIsInstance(group, sync_tool.ttk.LabelFrame)
                        self.assertEqual(group.nametowidget('folder').grid_info()['row'], 0)
                        self.assertEqual(group.nametowidget('browse').grid_info()['row'], 0)
                        self.assertEqual(group.nametowidget('url').grid_info()['row'], 1)
                        self.assertEqual(group.nametowidget('branch').grid_info()['row'], 2)
                    entries[0].insert(0, 'old-a-url')
                    entries[1].insert(0, 'old-b-url')
                    for index, name, url in [(0, 'a', 'https://example.invalid/a.git'),
                                              (1, 'b', 'git@example.invalid:b.git')]:
                        with patch.object(sync_tool.filedialog, 'askdirectory', return_value=str(root / name)):
                            browsers[index].invoke()
                        self.assertEqual(entries[index].get(), url)
                        self.assertEqual(Path(entries[index + 4].get()), (root / name).resolve())
                    before = [w.get() for w in entries]
                    with patch.object(sync_tool.filedialog, 'askdirectory', return_value=''):
                        browsers[0].invoke()
                    self.assertEqual([w.get() for w in entries], before)
                    with patch.object(sync_tool.filedialog, 'askdirectory', return_value=str(root / 'no-remote')), \
                            patch.object(sync_tool.messagebox, 'showinfo') as notice:
                        browsers[0].invoke()
                    self.assertEqual(entries[0].get(), 'https://example.invalid/a.git')
                    self.assertEqual(entries[1].get(), 'git@example.invalid:b.git')
                    notice.assert_not_called()
                finally:
                    self.close_window(window)
            with patch.dict(os.environ, {'LOCALAPPDATA': str(root)}), \
                    patch.object(sync_tool.tk.Tk, 'mainloop', inspect):
                sync_tool.main()

    def test_switch_swaps_urls_and_branches_and_restores(self):
        def inspect(window):
            try:
                window.withdraw()
                fields = self.repo_fields(window)
                self.assertEqual(len(fields), 6)
                values = ['repo-a', 'repo-b', 'feature-a', 'release-b', 'folder-a', 'folder-b']
                for field, value in zip(fields, values):
                    field.insert(0, value)
                controls = next(w for w in window.winfo_children() if isinstance(w, sync_tool.ttk.Frame)
                                and any(isinstance(child, sync_tool.ttk.Button) and child.cget('text').startswith('Switch')
                                        for child in w.winfo_children()))
                button = next(w for w in controls.winfo_children()
                              if isinstance(w, sync_tool.ttk.Button) and w.cget('text').startswith('Switch'))
                button.invoke()
                self.assertEqual([w.get() for w in fields], ['repo-b', 'repo-a', 'release-b', 'feature-a', 'folder-b', 'folder-a'])
                button.invoke()
                self.assertEqual([w.get() for w in fields], values)
            finally:
                self.close_window(window)
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'LOCALAPPDATA': temp}), \
                patch.object(sync_tool.tk.Tk, 'mainloop', inspect):
            sync_tool.main()

    def test_same_branch_preserves_legacy_state_key(self):
        import hashlib
        import json
        expected = hashlib.sha256(json.dumps(['a', 'b', 'main']).encode()).hexdigest()
        self.assertEqual(sync_tool.sync_key('a', 'b', 'main', 'main'), expected)
        self.assertNotEqual(sync_tool.sync_key('a', 'b', 'main', 'release'), expected)


if __name__ == '__main__':
    unittest.main()
