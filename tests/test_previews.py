"""Test the exact Python embedded in the reusable publisher, without credentials."""
import contextlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import types
import unittest
from unittest.mock import patch
import zipfile

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = yaml.safe_load((ROOT / '.github/workflows/docs-preview-publish.yml').read_text())
CODE = next(s['run'] for s in WORKFLOW['jobs']['preview']['steps'] if s.get('name') == 'Publish or reconcile static previews')
publisher = types.ModuleType('publisher')
exec(compile(CODE, 'publisher-workflow', 'exec'), publisher.__dict__)
BUILD = yaml.safe_load((ROOT / '.github/workflows/docs-preview-build.yml').read_text())
REUSE_CODE = next(s['run'] for s in BUILD['jobs']['build']['steps'] if s.get('id') == 'reuse')
reuser = types.ModuleType('reuser')
exec(compile(REUSE_CODE, 'build-reuse', 'exec'), reuser.__dict__)


def archive(files):
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as output:
        for name, content in files:
            # Preserve hostile backslashes even on Windows, where ZipInfo normalizes them.
            if isinstance(name, str):
                entry = zipfile.ZipInfo('placeholder')
                entry.filename = name
                output.writestr(entry, content)
            else:
                output.writestr(name, content)
    return data.getvalue()


class ValidationTests(unittest.TestCase):
    def test_directory_rejects_traversal_and_hidden_paths(self):
        for value in ('.', '..', '../other', '/root', '.git', 'a/b', 'a\\b', ''):
            with self.subTest(value=value), self.assertRaises(ValueError):
                publisher.component(value)

    def test_https_url_with_project_path(self):
        self.assertEqual(publisher.site_url('https://example.test/product/'), 'https://example.test/product')
        for value in ('http://example.test', 'https://user:pass@example.test', 'https://:example-password@example.test',
                      'https://@example.test', 'https://:@example.test', 'https://example.test/?q=x',
                      'https://:443', 'https://example.test/a b', 'https://example.test/\npath'):
            with self.assertRaises(ValueError):
                publisher.site_url(value)

    def test_valid_archive_removes_cname_and_adds_noindex(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            publisher.extract_site(archive([('index.html', '<HEAD><title>Preview</title></HEAD>'),
                                            ('assets/style.css', 'body {}'), ('CNAME', 'example.test')]), root, 4096)
            self.assertIn('noindex', (root / 'index.html').read_text())
            self.assertTrue((root / 'assets/style.css').exists())
            self.assertFalse((root / 'CNAME').exists())

    def test_archive_validated_before_any_writes(self):
        for name in ('../escape', '/escape', 'a/../../escape', '.git/config', 'a\\escape', 'a/./b', 'C:/escape'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                with self.assertRaises(ValueError):
                    publisher.extract_site(archive([('index.html', 'valid'), (name, 'unsafe')]), root, 4096)
                self.assertEqual(list(root.iterdir()), [])

    def test_reject_duplicate_and_oversized_files(self):
        for files, limit in [([('index.html', 'x'), ('INDEX.html', 'x')], 4096), ([('index.html', 'x' * 20)], 10)]:
            with tempfile.TemporaryDirectory() as temp, self.assertRaises(ValueError):
                publisher.extract_site(archive(files), Path(temp), limit)

    def test_reject_symlinks_and_missing_entrypoint(self):
        entry = zipfile.ZipInfo('link')
        entry.create_system = 3
        entry.external_attr = (stat.S_IFLNK | 0o777) << 16
        for files in ([('index.html', 'x'), (entry, '/etc/passwd')], [('other.html', 'x')]):
            with tempfile.TemporaryDirectory() as temp, self.assertRaises(ValueError):
                publisher.extract_site(archive(files), Path(temp), 4096)

    def test_destination_cannot_escape(self):
        with tempfile.TemporaryDirectory() as temp, self.assertRaises(ValueError):
            publisher.safe_target(Path(temp), '../escape')

    def test_comment_ignores_user_spoofed_marker(self):
        writes = []
        client = types.SimpleNamespace(
            items=lambda path: iter([{'id': 1, 'user': {'login': 'contributor'}, 'body': '<!-- docs-preview -->'}]),
            request=lambda *args: writes.append(args))
        with patch.dict(os.environ, {'COMMENT_MARKER': 'docs-preview'}):
            publisher.comment(client, 1, 'Preview ready')
            self.assertEqual(writes[0][0], 'issues/1/comments')
            writes.clear()
            publisher.comment(client, 1, 'Removed', create=False)
            self.assertEqual(writes, [])

    def test_only_current_open_same_repo_head_is_eligible(self):
        def pr(state='open', repo='org/docs', branch='gh-pages', sha='abc'):
            return {'state': state, 'base': {'ref': branch}, 'head': {'repo': {'full_name': repo}, 'sha': sha}}
        self.assertTrue(publisher.eligible(pr(), 'org/docs', 'gh-pages', 'abc'))
        for candidate in (pr(state='closed'), pr(repo='fork/docs'), pr(branch='develop'), pr(sha='stale')):
            self.assertFalse(publisher.eligible(candidate, 'org/docs', 'gh-pages', 'abc'))


class MutationTests(unittest.TestCase):
    def test_real_git_publish_and_cleanup_preserve_other_content(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {
                'PREVIEW_DIR': 'previews', 'PAGES_BRANCH': 'gh-pages', 'APP_TOKEN': 'test-only-placeholder'}):
            base = Path(temp)
            remote, root, prepared = base / 'remote.git', base / 'pages', base / 'prepared'
            def run(*args):
                return subprocess.run(['git', *map(str, args)], check=True, capture_output=True, text=True)
            run('init', '--bare', remote)
            run('clone', remote, root)
            run('-C', root, 'switch', '-c', 'gh-pages')
            run('-C', root, 'config', 'user.name', 'Test')
            run('-C', root, 'config', 'user.email', 'test@example.invalid')
            (root / 'index.md').write_text('Production content')
            (root / 'previews/pr-2').mkdir(parents=True)
            (root / 'previews/pr-2/index.html').write_text('Other preview')
            run('-C', root, 'add', '.')
            run('-C', root, 'commit', '-m', 'Initial site')
            run('-C', root, 'push', 'origin', 'gh-pages')
            prepared.mkdir()
            (prepared / 'index.html').write_text('New preview')
            pr = {'state': 'open', 'base': {'ref': 'gh-pages'}, 'head': {'repo': {'full_name': 'org/docs'}, 'sha': 'abc'}}
            client = types.SimpleNamespace(repo='org/docs', pr=lambda n: pr)
            self.assertTrue(publisher.mutate(client, root, 1, prepared, 'abc', 4096))
            self.assertEqual(run('--git-dir', remote, 'show', 'gh-pages:previews/pr-1/index.html').stdout, 'New preview')
            self.assertEqual((root / 'index.md').read_text(), 'Production content')
            pr['state'] = 'closed'
            self.assertTrue(publisher.mutate(client, root, 1, None, None, 4096))
            self.assertFalse((root / 'previews/pr-1').exists())
            self.assertEqual((root / 'previews/pr-2/index.html').read_text(), 'Other preview')
            self.assertEqual((root / 'index.md').read_text(), 'Production content')

    def test_noop_cleanup_does_not_stage_missing_path(self):
        client = types.SimpleNamespace(repo='org/docs', pr=lambda n: {'state': 'closed'})
        calls = []
        def git(root, *args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess([], 0, '', '')
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'PREVIEW_DIR': 'previews', 'PAGES_BRANCH': 'gh-pages'}), patch.object(publisher, 'git', git):
            self.assertTrue(publisher.mutate(client, Path(temp), 1, None, None, 100))
        self.assertEqual([c[0] for c in calls], ['fetch', 'reset'])

    def test_reopened_pr_is_not_deleted(self):
        client = types.SimpleNamespace(repo='org/docs', pr=lambda n: {'state': 'open'})
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'PREVIEW_DIR': 'previews', 'PAGES_BRANCH': 'gh-pages'}), patch.object(publisher, 'git'):
            target = Path(temp) / 'previews/pr-1'
            target.mkdir(parents=True)
            self.assertFalse(publisher.mutate(client, Path(temp), 1, None, None, 100))
            self.assertTrue(target.exists())

    def test_retry_rechecks_pr_and_does_not_overwrite_new_head(self):
        states = iter([{'state': 'open', 'base': {'ref': 'gh-pages'}, 'head': {'repo': {'full_name': 'org/docs'}, 'sha': sha}} for sha in ('abc', 'changed')])
        client = types.SimpleNamespace(repo='org/docs', pr=lambda n: next(states))
        calls = []
        def git(root, *args, **kwargs):
            calls.append(args[0])
            return subprocess.CompletedProcess([], 1 if args[0] in ('push', 'diff') else 0, '', '')
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as prepared, patch.dict(os.environ, {'PREVIEW_DIR': 'previews', 'PAGES_BRANCH': 'gh-pages'}), patch.object(publisher, 'git', git), patch.object(publisher.time, 'sleep'):
            (Path(prepared) / 'index.html').write_text('test')
            self.assertFalse(publisher.mutate(client, Path(temp), 1, Path(prepared), 'abc', 4096))
        self.assertEqual(calls.count('push'), 1)
        self.assertEqual(calls.count('fetch'), 2)


class MediaReuseTests(unittest.TestCase):
    """Run the exact build reuse step against a real git history and a built site."""
    SAME, OLD, NEW = b'GIF89a\0same', b'PNG\0old', b'PNG\0new'

    def git(self, root, *args):
        return subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True, text=True)

    def write(self, root, relative, content):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode())

    def repository(self, temp, with_parent=True):
        """Docs branch head with media, then a PR merge commit that changes one image and adds one."""
        root = Path(temp)
        self.git(root, 'init', '-q')
        for key, value in (('core.autocrlf', 'false'), ('user.name', 'Test'), ('user.email', 'test@example.invalid')):
            self.git(root, 'config', key, value)
        self.write(root, 'assets/images/same.gif', self.SAME)
        self.write(root, 'docs/images/changed.png', self.OLD)
        self.write(root, 'docs/page.md', 'Page')
        self.git(root, 'add', '.')
        self.git(root, 'commit', '-q', '-m', 'Docs branch head')
        if with_parent:
            self.write(root, 'docs/images/changed.png', self.NEW)
            self.write(root, 'docs/images/new.gif', b'GIF89a\0new')
            self.git(root, 'add', '.')
            self.git(root, 'commit', '-q', '-m', 'PR merge commit')
        site = root / '_site'
        self.write(site, 'assets/images/same.gif', self.SAME)
        self.write(site, 'docs/images/changed.png', self.NEW)
        self.write(site, 'docs/images/new.gif', b'GIF89a\0new')
        self.write(site, 'index.html', '<img src="assets/images/same.gif"><img src="docs/images/new.gif">')
        self.write(site, 'docs/page.html', '\n'.join([
            '<img src="../assets/images/same.gif">',
            '<img src="..\\assets\\images\\same.gif">',
            '<img src="/proj/previews/pr-7/assets/images/same.gif?v=1#top">',
            '<a href="https://docs.example.test/proj/previews/pr-7/assets/images/same.gif">full size</a>',
            '<img srcset="../assets/images/same.gif 1x, images/new.gif 2x">',
            '<img src="images/changed.png">',
            '<img src="images/new.gif">',
            '<img src="../../../etc/same.gif">',
            '<img src="data:image/gif;base64,AAAA">',
            '<a href="https://other.example/assets/images/same.gif">external</a>',
            "<div style=\"background: url('../assets/images/same.gif')\"></div>",
            '<img src="/proj/previews/pr-7/assets/images/same.gif%20">',
        ]))
        self.write(site, 'assets/css/site.css',
                   '.a{background:url("../images/same.gif")} .b{background:url(/proj/previews/pr-7/assets/images/same.gif)}')
        return root

    def test_unchanged_media_is_dropped_and_every_reference_points_to_the_live_site(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.repository(temp)
            with contextlib.chdir(root):
                reused, references = reuser.reuse(Path('_site'), '/proj/previews/pr-7', '/proj', reuser.base_blobs())
            site = root / '_site'
            self.assertEqual(set(reused), {'assets/images/same.gif'})
            self.assertEqual(references, 10)
            self.assertFalse((site / 'assets/images/same.gif').exists())
            self.assertFalse((site / 'assets/images').exists(), 'empty directories are removed')
            self.assertEqual((site / 'docs/images/changed.png').read_bytes(), self.NEW)
            self.assertTrue((site / 'docs/images/new.gif').exists())
            page = (site / 'docs/page.html').read_text().splitlines()
            self.assertEqual(page, [
                '<img src="/proj/assets/images/same.gif">',
                '<img src="/proj/assets/images/same.gif">',
                '<img src="/proj/assets/images/same.gif?v=1#top">',
                '<a href="https://docs.example.test/proj/assets/images/same.gif">full size</a>',
                '<img srcset="/proj/assets/images/same.gif 1x, images/new.gif 2x">',
                '<img src="images/changed.png">',
                '<img src="images/new.gif">',
                '<img src="../../../etc/same.gif">',
                '<img src="data:image/gif;base64,AAAA">',
                '<a href="https://other.example/assets/images/same.gif">external</a>',
                "<div style=\"background: url('/proj/assets/images/same.gif')\"></div>",
                '<img src="/proj/assets/images/same.gif">',
            ])
            self.assertEqual((site / 'index.html').read_text(),
                             '<img src="/proj/assets/images/same.gif"><img src="docs/images/new.gif">')
            self.assertEqual((site / 'assets/css/site.css').read_text(),
                             '.a{background:url("/proj/assets/images/same.gif")} .b{background:url(/proj/assets/images/same.gif)}')

    def test_site_at_domain_root_uses_root_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.repository(temp)
            with contextlib.chdir(root):
                reuser.reuse(Path('_site'), '/proj/previews/pr-7', '', reuser.base_blobs())
            self.assertIn('<img src="/assets/images/same.gif">', (root / '_site/docs/page.html').read_text())

    def test_main_reports_savings_in_the_step_summary(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.repository(temp)
            summary = root / 'summary.md'
            env = {'OUTPUT_DIR': '_site', 'SITE_URL': 'https://docs.example.test/proj',
                   'PREVIEW_BASEURL': '/proj/previews/pr-7', 'GITHUB_STEP_SUMMARY': str(summary)}
            with contextlib.chdir(root), patch.dict(os.environ, env), contextlib.redirect_stdout(io.StringIO()):
                reuser.main()
            self.assertIn('Reused 1 unchanged media files', summary.read_text())

    def test_missing_docs_branch_head_keeps_every_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.repository(temp, with_parent=False)
            env = {'OUTPUT_DIR': '_site', 'SITE_URL': 'https://docs.example.test/proj', 'PREVIEW_BASEURL': '/proj/previews/pr-7'}
            output = io.StringIO()
            with contextlib.chdir(root), patch.dict(os.environ, env), contextlib.redirect_stdout(output):
                reuser.main()
            self.assertIn('::warning::', output.getvalue())
            self.assertTrue((root / '_site/assets/images/same.gif').exists())
            self.assertIn('src="../assets/images/same.gif"', (root / '_site/docs/page.html').read_text())


class WorkflowTests(unittest.TestCase):
    def test_build_skips_fork_pull_requests_and_fetches_the_docs_branch_head(self):
        job = BUILD['jobs']['build']
        self.assertIn('github.event.pull_request.head.repo.full_name == github.repository', job['if'])
        self.assertEqual(job['steps'][0]['with']['fetch-depth'], 2)
        triggers = BUILD.get('on', BUILD.get(True))  # PyYAML reads the bare 'on' key as a boolean.
        self.assertIs(triggers['workflow_call']['inputs']['reuse-live-media']['default'], True)

    def test_build_rejects_user_info_and_keeps_configurable_project_path(self):
        build = yaml.safe_load((ROOT / '.github/workflows/docs-preview-build.yml').read_text())
        code = next(s['run'] for s in build['jobs']['build']['steps'] if s.get('id') == 'configure')
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / 'outputs'
            env = {'PREVIEW_DIR': 'review-sites', 'OUTPUT_DIR': 'html-output', 'PR_NUMBER': '7', 'GITHUB_OUTPUT': str(output)}
            for url in ('https://user:pass@example.test', 'https://:example-password@example.test',
                        'https://@example.test', 'https://:@example.test', 'https://:443', 'https://example.test/a b'):
                with self.subTest(url=url), patch.dict(os.environ, {**env, 'SITE_URL': url}), self.assertRaises(ValueError):
                    exec(compile(code, 'build-configure', 'exec'), {})
            self.assertFalse(output.exists())
            with patch.dict(os.environ, {**env, 'SITE_URL': 'https://another.example.test/custom-project/'}):
                exec(compile(code, 'build-configure', 'exec'), {})
            self.assertEqual(output.read_text(), 'baseurl=/custom-project/review-sites/pr-7\n')

    def test_embedded_python_compiles(self):
        for path in (ROOT / '.github/workflows').glob('*.yml'):
            workflow = yaml.safe_load(path.read_text())
            for job in workflow['jobs'].values():
                for step in job.get('steps', []):
                    if step.get('shell') == 'python':
                        compile(step['run'], str(path), 'exec')

    def test_resolver_ignores_wrong_workflow_fork_and_stale_head(self):
        script = next(s['with']['script'] for s in WORKFLOW['jobs']['preview']['steps'] if s.get('id') == 'resolve')
        harness = r'''
          const script = JSON.parse(process.argv[1]);
          const scenario = process.argv[2];
          const outputs = {};
          const run = {workflow_id: scenario === 'wrong' ? 2 : 1, event:'pull_request', conclusion:'success',
            repository:{full_name:'org/docs'}, head_repository:{full_name: scenario === 'fork' ? 'fork/docs':'org/docs'},
            head_branch:'docs/test', head_sha:'abc', id:10, run_attempt:2};
          const pr = {number:3, base:{ref:'gh-pages'}, head:{repo:{full_name:'org/docs'}, sha:scenario === 'stale'?'old':'abc'}};
          const github = {rest:{actions:{getWorkflowRun:async()=>({data:run}),getWorkflow:async()=>({data:{id:1}})},
            pulls:{list:()=>{}}},paginate:async()=>[pr]};
          const context = {repo:{owner:'org',repo:'docs'},eventName:'workflow_run',payload:{workflow_run:{id:10}}};
          const core = {setOutput:(k,v)=>outputs[k]=v};
          const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;
          new AsyncFunction('github','context','core',script)(github,context,core).then(()=>console.log(JSON.stringify(outputs)));
        '''
        for scenario in ('valid', 'wrong', 'fork', 'stale'):
            with self.subTest(scenario=scenario), patch.dict(os.environ, {'PAGES_BRANCH': 'gh-pages', 'BUILD_WORKFLOW': 'build.yml'}):
                result = subprocess.run(['node', '-e', harness, json.dumps(script), scenario], check=True, capture_output=True, text=True)
                outputs = json.loads(result.stdout)
                self.assertEqual(outputs['eligible'], 'true' if scenario == 'valid' else 'false')
                if scenario == 'valid':
                    self.assertEqual(outputs['attempt'], '2')

    def test_every_external_action_is_pinned(self):
        import re
        for path in (ROOT / '.github/workflows').glob('*.yml'):
            workflow = yaml.safe_load(path.read_text())
            for job in workflow['jobs'].values():
                for step in job.get('steps', []):
                    if 'uses' in step:
                        self.assertRegex(step['uses'], r'^[^@]+@[0-9a-f]{40}$')

    def test_build_has_no_secret_access_and_checkout_does_not_persist(self):
        path = ROOT / '.github/workflows/docs-preview-build.yml'
        source = path.read_text()
        self.assertNotIn('secrets.', source)
        workflow = yaml.safe_load(source)
        self.assertEqual(workflow['permissions'], {'contents': 'read'})
        checkout = workflow['jobs']['build']['steps'][0]
        self.assertFalse(checkout['with']['persist-credentials'])


if __name__ == '__main__':
    unittest.main()
