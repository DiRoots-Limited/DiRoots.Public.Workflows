# DiRoots.Public.Workflows
Shared GitHub Actions workflows for DiRoots public repositories, including documentation preview builds, publishing, and cleanup.

## Documentation PR previews

These reusable workflows generalize DiStem's documentation preview flow. A caller repository builds a documentation PR without publishing credentials, uploads static files, and lets a trusted workflow publish them under its own Pages site. Closing the PR removes that preview. Daily and manual reconciliation repair missed cleanup.

Start with the sandbox repository. This implementation is ready for review and automated checks; the GitHub App publishing lifecycle must be validated there before onboarding production documentation.

### Workflows

| File | Purpose |
| --- | --- |
| `.github/workflows/docs-preview-build.yml` | Unprivileged Jekyll build and artifact upload. |
| `.github/workflows/docs-preview-publish.yml` | Verified publication, close cleanup and reconciliation. |
| `.github/workflows/validate.yml` | Run focused tests against the exact embedded publishing code. |

The publisher is self-contained. It does not download executable helper code or execute scripts from the caller's Pages branch or artifact. Python's standard library handles validated extraction, API calls and git mutations. The build keeps the Ruby/Jekyll approach used by DiStem.

### Caller prerequisites

1. Configure GitHub Pages to deploy the documentation branch, normally `gh-pages`, from its root. Keep its existing deployment mode and production domain.
2. Commit a tested `Gemfile` and `Gemfile.lock` to the documentation branch. DiStem uses `github-pages` 232 and `webrick`; choose and lock compatible dependencies for your site. The default build uses Ruby 3.3. A remote theme reference is separate from the gem lockfile.
3. Install the publishing GitHub App on the selected caller repository. It needs Contents write and Metadata read. If the documentation branch is protected, allow the App's required writes through all applicable protection controls.
4. Write the public site URL, including a project path when applicable, as `site-url` in both callers. Do not read it from a repository variable: GitHub does not pass variables to pull requests from forks, and the URL is public anyway. Set `DOCS_PREVIEW_APP_ID` and secret `DOCS_PREVIEW_APP_PRIVATE_KEY` at organization level, restricted to selected repositories, or at repository level.
5. Allow these reusable workflows and their pinned third-party actions under the caller's Actions policy.

No credential values belong in this repository or caller YAML. The reusable publisher accepts credentials explicitly; it never assumes a particular App, owner, product repository or domain.

### Install the callers

Copy [the build caller](examples/docs-preview-build.yml) to the documentation branch and [the publish caller](examples/docs-preview-publish.yml) to the default branch. Replace `REVIEWED_COMMIT_SHA` with the same reviewed full commit SHA in both callers. When the documentation branch is also the default branch, both files live there.

The examples use `gh-pages`. If the caller uses another docs branch, change the caller trigger filters and pass the corresponding `pages-branch` to the publisher. The publisher's `build-workflow` must match the build caller's filename; its `workflow_run.workflows` filter must match the build caller's display name. Keep the build filename stable or update both.

Current GitHub documentation places `workflow_run`, schedules and `pull_request_target` in the default branch context. Verify the actual close-run workflow revision during the sandbox pilot before removing duplicate legacy files. The publisher never checks out PR code. See [GitHub's trigger reference](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows).

Merge the trusted publisher caller first, then the build caller, then open a fresh docs PR. A PR's source must include the build caller to exercise the new flow. Do not leave the old and new publishers competing for the same build events during migration.

### Build inputs

| Input | Default | Meaning |
| --- | --- | --- |
| `site-url` | Required | HTTPS site URL; the URL path becomes part of the preview base path. |
| `preview-dir` | `previews` | One non-hidden directory component, shared with the publisher. |
| `output-dir` | `_site` | Relative output directory containing `index.html`. |
| `ruby-version` | `3.3` | Version supported by the caller's locked dependencies. |
| `build-command` | Empty | Optional Bash command executed only in the unprivileged build. |
| `artifact-name` | `docs-preview-site` | Artifact prefix, shared with the publisher. |
| `reuse-live-media` | `true` | Drop media the PR did not change and load it from the live site. See [Media reuse](#media-reuse). |

The build runs only for same-repository PRs. Fork PRs show the build as skipped, because the publisher would never publish them; the publisher still rejects fork runs independently.

The default command builds with the computed `PREVIEW_BASEURL`, `OUTPUT_DIR` and a temporary configuration that excludes existing previews, installed gems and build output. Existing Jekyll exclusions are retained. This configuration is never committed to production. Custom commands receive these variables and `PREVIEW_CONFIG`; they must apply that override, for example:

```bash
bundle exec jekyll build --config "_config.yml,$PREVIEW_CONFIG" --baseurl "$PREVIEW_BASEURL" --destination "$OUTPUT_DIR"
```

Override a hard-coded production `url` in your custom build configuration if the theme generates absolute URLs. Internal links should respect Jekyll's `relative_url` filter. The actual checkout is the PR merge commit; the trusted publisher identifies its associated current PR head from GitHub's run and PR API data.

### Media reuse

Documentation sites are mostly images and GIFs, and a docs PR rarely changes them. Copying the whole built site into each preview would put hundreds of megabytes per open PR into the Pages branch, beyond the publisher's budgets for large manuals. After the Jekyll build, the build compares every media file (`gif`, `png`, `jpg`, `jpeg`, `webp`, `svg`, `avif`, `bmp`, `ico`, `mp4`, `webm`, `mov`) with the same path in the docs branch head that GitHub merged the PR into. A byte-identical file is already served by the live site, so the preview drops its copy and rewrites references in HTML and CSS to the live URL. HTML pages, and new or changed media, stay in the preview, so it remains a complete, navigable site.

References are rewritten in `src`, `href`, `poster`, `data-src` and `srcset` attributes, quoted or unquoted, and in CSS `url()` values. Relative paths (including Windows-style `..\` separators), absolute paths under the preview base and absolute URLs on the docs site's own origin are resolved against each page's location; queries and fragments keep their original escaping. URLs on other hosts, `data:` URIs and paths that escape the site are left alone. Before deleting a file, the build checks that every mention of its name in HTML, CSS, JavaScript, JSON and XML output was a reference it rewrote or confirmed points elsewhere. Any other mention, such as an image path built in JavaScript, keeps that file in the preview, so a reference it cannot parse never breaks; the summary reports how many files were kept this way. The checkout fetches the merge commit's first parent for the comparison; if it is unavailable, the preview keeps every file. The build summary reports the preview size before and after reuse.

While a PR is open, a reused image shows the live version. If someone changes that image on the docs branch meanwhile, the preview shows the new live image until the PR is updated.

Artifacts are named `<artifact-name>-<run-attempt>` and retained for one day. The publisher selects the expected attempt from the triggering run, avoiding ambiguity after reruns.

### Publisher inputs

| Input | Default | Meaning |
| --- | --- | --- |
| `site-url` | Required | Same HTTPS URL as the build. |
| `app-id` | Required | App identifier, normally passed from an Actions variable. |
| `pages-branch` | `gh-pages` | Branch receiving static preview files. |
| `preview-dir` | `previews` | Preview directory; must match the build input. |
| `build-workflow` | `docs-preview-build.yml` | Caller workflow filename whose numeric workflow identity is trusted. |
| `artifact-name` | `docs-preview-site` | Must match the build prefix. |
| `comment-marker` | `docs-preview` | Marker identifying the workflow's bot-owned comment. Preserve an existing marker during migration. |
| `max-preview-mib` | `100` | Maximum compressed and expanded artifact size. |
| `max-source-mib` | `800` | Conservative limit on checked-out Pages source files after a publish. |
| `wait-seconds` | `600` | Maximum wait for each preview's served state, from 1 to 900 seconds. |

Pass secret `app-private-key` explicitly. The caller grants `contents: read`, `actions: read`, and `pull-requests: write`; the App token supplies Contents write for git pushes. Build callers grant only `contents: read` and pass no publishing secrets.

The source-size budget is not a measurement of generated production output. Before onboarding a site, measure its built output plus retained previews against GitHub Pages' 1 GB published-site limit. Preview updates and cleanup also trigger legacy Pages deployments. Increase budgets only after checking capacity. Media reuse keeps previews of large manuals small; check a large site's preview size in the build summary during onboarding.

### Publication and cleanup safeguards

- Accept only successful `pull_request` runs from the expected workflow identity and calling repository, with a same-repository PR targeting the configured docs branch and a matching current head SHA. Fork runs are ignored, and the build skips fork PRs before building.
- Download only the named artifact from the triggering run and current attempt. Reject ambiguous or expired artifacts.
- Validate the entire archive before extracting: refuse traversal, hidden paths, duplicate paths, symlinks, unsupported file types, excessive entries and excessive expanded size. Require `index.html`, remove preview-local `CNAME`, and add `noindex` metadata to HTML head elements.
- Write only the validated preview directory. Reject symlink ancestors and paths escaping the destination checkout. Never build or execute downloaded content in the publishing job.
- Coordinate publication, close cleanup and sweeps with one mutation concurrency group per caller repository/Pages branch. `queue: max` retains pending operations and `cancel-in-progress: false` lets writes complete. Do not add a caller concurrency group that cancels the called job.
- On push conflict, fetch the current branch and reapply only the intended directory change, rechecking PR state/head each time. Use at most five attempts; never force-push.
- Post the preview comment only after the served marker identifies the expected PR, head, build run and attempt. Match the bot author as well as the configured comment marker.
- On close, check the PR is still closed before deleting, wait for the marker to disappear and expire the comment. Reopened PRs are preserved. The sweep also repairs comments when an earlier deletion succeeded but comment update failed.

GitHub's PR API and git writes are not one atomic transaction. The publisher rechecks after deployment and repairs a PR that closed during publication; the scheduled sweep is the fallback if a job is interrupted. Run it manually after a failed close cleanup. Schedules may be delayed or disabled by GitHub after repository inactivity. A sweep across many previews may reach the job timeout; rerun it to continue.

The shared App key remains a trust boundary: repository writers who can access it can request other tokens within that App installation. Restrict both App installation and organization-secret access. Separate keys for the same App do not isolate repositories. Private-repository previews on a public Pages site publish draft content publicly and require an explicit visibility decision.

Previews contain active HTML and may contain JavaScript on the docs origin. Publish only trusted same-repository contributions. `noindex` reduces indexing; it is not access control. This implementation publishes into each caller's Pages branch; it does not implement a central cross-repository preview host.

### Validate before rollout

```text
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```

PyYAML is the only development dependency. It parses the workflow YAML so tests can compile and execute the exact embedded Python and inspect workflow permissions and pinned action references. Python does not include a YAML parser in its standard library, and this repository had no existing parser to reuse. Using a YAML parser avoids brittle line-based extraction. The version is pinned in `requirements-dev.txt`; it is installed only by the validation workflow and local test command, not by the reusable build or publisher. This is the intended test-only parsing approach.

Tests load the exact Python embedded in both reusable workflows, so there is no separate test-only implementation. They cover archive validation, paths, PR eligibility, idempotent cleanup, reopen handling, and state revalidation after a push conflict. All external actions are pinned by full SHA.

GitHub's documented `concurrency.queue` option is newer than actionlint 1.7.12's schema. That version flags the `queue` key; do not remove the queue to silence it, as the default queue can discard pending operations. Validate the remaining syntax with that known schema limitation and confirm GitHub acceptance in the sandbox. See [GitHub concurrency documentation](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency).

Before any production rollout, test a sandbox PR through open, update, rapid pushes, simultaneous PRs, build failure, close without merge, merge closure, reopen, and a close during publication. Verify actual Pages availability, cleanup, theme assets, search, comment ownership and credential masking. Confirm that the App token is scoped to the sandbox. Local automated tests do not establish that App permissions, Pages settings or organization policies are configured correctly.
