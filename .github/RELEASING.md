# Release Process

How releases work for `bog-agents` (SDK) and `bog-agents-cli` (CLI).

## Overview

Releases are fully automated via [release-please](https://github.com/googleapis/release-please):

1. Conventional commits land on `main`
2. release-please creates/updates a release PR with changelog + version bump
3. You review and merge the PR
4. CI builds, tests, publishes to PyPI, and creates a GitHub release

## Triggering a Release

1. Merge commits to `main` using [conventional commit](https://www.conventionalcommits.org/) format:
   ```
   feat(sdk): add streaming support      # → minor bump
   fix(cli): handle missing config        # → patch bump
   feat(sdk)!: redesign config format     # → major bump
   ```

2. Wait for release-please to create a release PR (automatic)

3. Review the PR — check the changelog and version bump

4. Merge the PR — the release pipeline runs automatically

## Version Bumping

The packages are pre-1.0, and release-please is configured
(`bump-minor-pre-major` + `bump-patch-for-minor-pre-major` in
`release-please-config.json`) to keep pre-1.0 releases conservative. While the
version is `< 1.0.0`:

| Commit Type | Version Bump (pre-1.0) | Example |
|-------------|------------------------|---------|
| `fix:` | Patch (`0.9.x`) | `fix(cli): resolve config loading issue` |
| `feat:` | **Patch** (`0.9.x`) | `feat(sdk): add new middleware` |
| `feat!:` / `BREAKING CHANGE:` | **Minor** (`0.x.0`) | `feat(sdk)!: redesign API` |

So a batch of plain `feat` commits releases as a patch (e.g. `0.9.13 → 0.9.14`),
and a breaking change releases as a minor (`0.9.13 → 0.10.0`). No commit type
auto-bumps to a new *major* while pre-1.0.

Once a package reaches `1.0.0`, normal SemVer applies (`feat` → minor,
`feat!`/`BREAKING CHANGE` → major).

### Forcing a specific version

To cut a version release-please wouldn't pick on its own — a deliberate `0.10.0`,
or the first `1.0.0` — add a `Release-As:` footer to a commit on `main`:

```
chore: cut 1.0.0

Release-As: 1.0.0
```

The `linked-versions` plugin groups the three packages, so they release together
at the same number. To make features bump the *minor* from now on, set
`bump-patch-for-minor-pre-major` to `false` in `release-please-config.json`.

## Release Pipeline

When a release PR is merged, `release.yml` runs:

1. **Build** — creates sdist + wheel
2. **Test** — installs the built package, runs import check + unit tests
3. **Publish** — uploads to PyPI via trusted publishing (OIDC)
4. **GitHub Release** — creates a release with artifacts

## Release Order

When releasing both packages:
1. Release the SDK first
2. Update the CLI's SDK dependency if needed
3. Release the CLI

## Manual Release (Hotfix)

For emergency releases outside the normal flow:

1. Go to **Actions** > **Release**
2. Click **Run workflow**
3. Select the package (`bog-agents` or `bog-agents-cli`)

## Configuration

| File | Purpose |
|------|---------|
| `release-please-config.json` | Release-please behavior and changelog sections |
| `.release-please-manifest.json` | Current version of each package (auto-updated) |

## Troubleshooting

### Release PR stuck with "autorelease: pending" label

```bash
gh pr list --state merged --search "release(bog-agents)" --limit 5
gh pr edit <PR_NUMBER> --remove-label "autorelease: pending" --add-label "autorelease: tagged"
```

### Yanking a release

1. Yank from PyPI via the web interface
2. Delete the GitHub release and tag:
   ```bash
   gh release delete "bog-agents==<VERSION>" --yes
   git push origin --delete "bog-agents==<VERSION>"
   ```
3. Update `.release-please-manifest.json` to the last good version

### Re-releasing a version

PyPI doesn't allow re-uploading the same version. Bump the version and release again.
