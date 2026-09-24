# Releasing

| Workflow | Trigger | Does |
| --- | --- | --- |
| `version.yml` | by hand: *Actions → version → Run workflow* | bumps `patch`/`minor`/`major` on `main` and pushes the commit |
| `tests.yml` | a PR into `publish`; called by `publish.yml` | pytest with coverage on 3.10–3.13 (gating) and 3.14 (not yet) |
| `publish.yml` | a push to `publish` | `tests.yml`, then build, then upload to PyPI |
| `pages.yml` | a push to `main` or `publish` touching `docs/`, `landing/` or `mkdocs.yml` | the landing page at `/`, these docs at `/docs/` |

## Cutting a release

1. Run the `version` workflow (or edit `version` in `pyproject.toml` and run
   `uv lock`). PyPI never accepts the same version twice; the build job checks first
   and fails with a readable error if you forgot.
2. Open a PR from `main` into `publish`. The test matrix runs on the PR.
3. Merge it. Approve the `pypi` environment when the run asks, if you configured
   reviewers.

The bump is one click rather than automatic on purpose: a release number says what
kind of change it is, and bumping `patch` on every merge would call a breaking change a
bugfix.

## One-time setup

No PyPI token is stored anywhere. The workflow uses **Trusted Publishing**: GitHub
mints a short-lived OIDC token for one named workflow in one named environment, and
PyPI accepts uploads from exactly that pair.

### PyPI

1. Sign in at pypi.org with 2FA enabled.
2. *Account settings → Publishing → Add a new pending publisher → GitHub*:

    | Field | Value |
    | --- | --- |
    | PyPI project name | `stcode` |
    | Owner | `Ming-doan` |
    | Repository name | `stcode` |
    | Workflow name | `publish.yml` |
    | Environment name | `pypi` |

    A *pending* publisher reserves the name until the first upload creates the project.
    After that it lives under the project's own *Settings → Publishing*.

### GitHub

1. *Settings → General*: make the repository public. Pages on a free plan and the
   public PyPI "Source" link both need it.
2. *Settings → Environments → New environment* `pypi`. Optionally add yourself as a
   required reviewer — each release then waits for a click — and restrict deployment
   branches to `publish`.
3. *Settings → Pages → Build and deployment → Source*: **GitHub Actions**. Then under
   *Settings → Environments → github-pages → Deployment branches*, add `publish` —
   the environment GitHub creates allows only the default branch.
4. *Settings → Actions → General → Workflow permissions*: **Read and write**, so
   `version.yml` can push its commit. If `main` is protected, allow
   `github-actions[bot]` to bypass, or the push is refused.
5. Create the branch: `git push origin main:publish`. That push is itself a release of
   the current version, so do it after the PyPI step above. Protect the branch under
   *Settings → Branches* (require a PR) so a stray push cannot release.
6. Recommended for an open-source repository: add a `LICENSE` (Apache-2.0, already
   present), enable *Private vulnerability reporting* under *Security*, and set the
   repository's description and website to `https://ming-doan.github.io/stcode/`.
