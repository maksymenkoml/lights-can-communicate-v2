"""Exercise installed hooks against disposable repositories and real staged files."""

import os
import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which('git'), 'Git is required for hook contracts')
class RepositoryHookTests(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory(prefix='tmp_repository_hooks_')
        self.addCleanup(self.folder.cleanup)
        self.repo = Path(self.folder.name)
        # Git exports repository paths to hooks. cwd alone cannot isolate a
        # foreign repo; discard all inherited Git routing/config before init.
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith('GIT_')}
        self.env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1',
                        UV_CACHE_DIR=str(ROOT / '.ruff_cache/uv'),
                        UV_TOOL_DIR=str(ROOT / '.ruff_cache/tools'))
        self.git('init', '-b', 'feature/checks', check=True)
        self.git('config', 'user.name', 'Repository checks', check=True)
        self.git('config', 'user.email', 'checks@example.invalid', check=True)
        for name in ('.githooks/pre-commit', '.githooks/pre-push', '.githooks/guard-branch',
                     'scripts/check-staged.py', 'requirements-dev.txt', '.gitignore'):
            source = ROOT / name
            if source.is_file():
                target = self.repo / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        (self.repo / 'README.md').write_text('Fixture\n')
        self.git('add', '.', check=True)
        self.git('commit', '-m', 'Initial fixture', check=True)
        self.git('config', 'core.hooksPath', '.githooks', check=True)

    def git(self, *args, check=False, input=None):
        return subprocess.run(['git', *args], cwd=self.repo, env=self.env, text=True,
                              input=input, capture_output=True, check=check)

    def stage(self, name, content, force=False):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        self.git('add', *(['-f'] if force else []), '--', name, check=True)
        return path

    def test_main_commit_is_rejected_without_changing_head(self):
        self.git('switch', '-c', 'main', check=True)
        before = self.git('rev-parse', 'HEAD', check=True).stdout
        result = self.git('commit', '--allow-empty', '-m', 'Forbidden main commit')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('feature branch', result.stderr)
        self.assertEqual(self.git('rev-parse', 'HEAD', check=True).stdout, before)

    def test_detached_commit_is_rejected(self):
        self.git('switch', '--detach', check=True)
        result = self.git('commit', '--allow-empty', '-m', 'Forbidden detached commit')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('feature branch', result.stderr)

    def test_feature_branch_commit_is_allowed(self):
        self.stage('note.md', 'A normal change\n')
        self.git('commit', '-m', 'Allowed feature change', check=True)
        self.assertEqual(self.git('show', 'HEAD:note.md', check=True).stdout, 'A normal change\n')

    def test_explicit_push_from_feature_to_main_is_rejected_before_checks(self):
        remote = self.repo / 'remote.git'
        self.git('init', '--bare', str(remote), check=True)
        self.git('remote', 'add', 'origin', str(remote), check=True)
        result = self.git('push', 'origin', 'HEAD:main')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('protected destination', result.stderr)
        self.assertEqual(self.git('ls-remote', 'origin', 'refs/heads/main', check=True).stdout, '')

    def test_push_guard_allows_feature_ref_but_rejects_main_deletion(self):
        sha = self.git('rev-parse', 'HEAD', check=True).stdout.strip()
        guard = self.repo / '.githooks/guard-branch'
        for ref, allowed in (('refs/heads/feature/checks', True), ('refs/heads/master', False),
                             ('refs/heads/main', False)):
            result = subprocess.run(['sh', str(guard), 'pre-push'], cwd=self.repo,
                                    env=self.env, text=True, capture_output=True,
                                    input=f'refs/heads/feature/checks {sha} {ref} {"0" * 40}\n')
            self.assertEqual(result.returncode == 0, allowed, result.stderr)
        deleted = subprocess.run(['sh', str(guard), 'pre-push'], cwd=self.repo,
                                 env=self.env, text=True, capture_output=True,
                                 input=f'(delete) {"0" * 40} refs/heads/main {sha}\n')
        self.assertNotEqual(deleted.returncode, 0)

    def test_private_configuration_is_rejected_even_when_force_added(self):
        self.stage('.lighting.json', '{"fixture": true}\n', force=True)
        result = self.git('commit', '-m', 'Forbidden private file')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Private configuration', result.stderr)

    def test_staged_whitespace_error_is_rejected(self):
        self.stage('note.md', 'Trailing whitespace  \n')
        result = self.git('commit', '-m', 'Whitespace fixture')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('trailing whitespace', result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which('uvx'), 'uvx is required for staged Python lint')
    def test_staged_python_error_cannot_be_hidden_by_unstaged_fix(self):
        path = self.stage('file with spaces.py', 'print(undefined_name)\n')
        path.write_text('print("Working tree fix")\n')
        staged = self.git('diff', '--cached', check=True).stdout
        result = self.git('commit', '-m', 'Staged Python fixture')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('F821', result.stdout + result.stderr)
        self.assertEqual(self.git('diff', '--cached', check=True).stdout, staged)
        self.assertEqual(path.read_text(), 'print("Working tree fix")\n')

    @unittest.skipUnless(shutil.which('uvx'), 'uvx is required for staged Python lint')
    def test_valid_index_commits_without_linting_or_stashing_unstaged_edits(self):
        path = self.stage('valid.py', 'answer = 42\n')
        path.write_text('if (\n')
        self.git('commit', '-m', 'Valid staged Python', check=True)
        self.assertEqual(self.git('show', 'HEAD:valid.py', check=True).stdout, 'answer = 42\n')
        self.assertEqual(path.read_text(), 'if (\n')

    @unittest.skipUnless(shutil.which('uvx'), 'uvx is required for staged Python lint')
    def test_commit_all_checks_the_temporary_index_supplied_by_git(self):
        path = self.stage('tracked.py', 'answer = 42\n')
        self.git('commit', '-m', 'Track valid Python', check=True)
        before = self.git('rev-parse', 'HEAD', check=True).stdout
        path.write_text('print(undefined_name)\n')
        result = self.git('commit', '-a', '-m', 'Invalid temporary index')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('F821', result.stdout + result.stderr)
        self.assertEqual(self.git('rev-parse', 'HEAD', check=True).stdout, before)

    @unittest.skipUnless(shutil.which('node'), 'Node is required for staged syntax checks')
    def test_staged_javascript_error_cannot_be_hidden_by_unstaged_fix(self):
        path = self.stage('syntax.mjs', 'const value = ;\n')
        path.write_text('const value = 1;\n')
        result = self.git('commit', '-m', 'Staged JavaScript fixture')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('SyntaxError', result.stderr)
        self.assertEqual(path.read_text(), 'const value = 1;\n')

    def test_nested_fixture_ignores_inherited_hook_repository_environment(self):
        git_dir = self.repo / '.git'

        def metadata():
            names = ['HEAD', 'config', 'index']
            names += [str(path.relative_to(git_dir)) for path in
                      (git_dir / 'refs').rglob('*') if path.is_file()]
            return {name: (git_dir / name).read_bytes() for name in names}

        before = metadata()
        # Poison only this disposable outer fixture, never ROOT or its shared Git dir.
        poisoned = {'GIT_DIR': str(git_dir), 'GIT_COMMON_DIR': str(git_dir),
                    'GIT_WORK_TREE': str(self.repo), 'GIT_INDEX_FILE': str(git_dir / 'index'),
                    'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'user.name',
                    'GIT_CONFIG_VALUE_0': 'Injected fixture identity',
                    'GIT_TEMPLATE_DIR': str(self.repo / 'empty-templates'),
                    'GIT_AUTHOR_EMAIL': 'injected@example.invalid'}
        (self.repo / 'empty-templates').mkdir()
        inner = RepositoryHookTests('test_feature_branch_commit_is_allowed')
        try:
            with patch.dict(os.environ, poisoned):
                try:
                    inner.setUp()
                except subprocess.CalledProcessError:
                    self.fail('Inner Git fixture inherited its parent repository context')
            actual = inner.git('rev-parse', '--absolute-git-dir', check=True).stdout.strip()
            self.assertEqual(Path(actual).resolve(), (inner.repo / '.git').resolve())
            self.assertEqual({key for key in inner.env if key.startswith('GIT_')},
                             {'GIT_CONFIG_GLOBAL', 'GIT_CONFIG_NOSYSTEM'})
            self.assertEqual(metadata(), before)
        finally:
            inner.doCleanups()


if __name__ == '__main__':
    unittest.main()
