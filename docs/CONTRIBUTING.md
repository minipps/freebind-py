# Contributing and releasing

From a checkout, create a virtual environment and install the package with all
optional integrations. Use pip 25.1 or newer for dependency groups.

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install --editable '.[all]' --group dev
```

## Checks and compatibility

Install and run the pinned Ruff linter with:

```sh
python -m pip install --group dev
ruff check .
```

Ruff checks Python sources, tests, and examples using its default error rules.
The `Ruff` CI job runs on pushes and pull requests and is required on `main`.

Run unit tests with:

```sh
.venv/bin/python -m unittest discover -s tests
```

Outside the namespace harness, the eight real-network tests are intentionally
skipped. To run the eight parity checks with real IPv4/IPv6 source and return
traffic, use:

```sh
.venv/bin/python tests/network_harness.py -- .venv/bin/python -m unittest discover -s tests -p test_network.py -v
```

The [CI workflow](../.github/workflows/tests.yml) configures unit jobs for CPython
3.11–3.14 with minimum and newest allowed optional dependencies, namespace jobs
for both dependency sets on CPython 3.12, and an ARM64 CPython 3.14 smoke job
with newest allowed dependencies. This describes configured coverage; consult
the workflow run status for results for a particular revision.

## Publishing and repository security

Publishing is manual and restricted to `minipps` running
[Publish to PyPI](../.github/workflows/publish.yml) from `main`.
The build verifies that the requested tag belongs to `main`, matches the package
version, passes unit tests, and produces valid distributions. The separate
publisher job downloads only this run's artifacts and uses PyPI Trusted
Publishing with attestations; package code runs without publishing credentials.

Before the first release, configure a [PyPI Trusted Publisher](https://docs.pypi.org/trusted-publishers/adding-a-publisher/)
(or a [pending publisher](https://pypi.org/manage/account/publishing/) for a new project):

- PyPI project: `freebind-py`
- GitHub owner: `minipps`
- Repository: `freebind-py`
- Workflow filename: `publish.yml`
- Environment: `pypi`

Set the version in `pyproject.toml`, update `CHANGELOG.md` with the release date
and notable changes, get the change and passing compatibility
checks onto `main`, and push the matching tag (for example `v0.1.0`). Then run
`gh workflow run publish.yml --ref main -f tag=v0.1.0` and inspect the run.
This publishes the package source, including files selected by `MANIFEST.in`,
to the public PyPI index even while the GitHub repository is private.

PR workflows use read-only tokens, do not persist checkout credentials, and
must never execute PR code using `pull_request_target` or privileged
`workflow_run` workflows. Actions are pinned to full commit SHAs; review
Dependabot updates before merging. Review package changes as carefully as
workflow changes: a malicious package can still harm its users after release.
The sole maintainer retains GitHub's administrator bypass so they can merge
their own changes; reviews cannot be self-approved. The private repository's
plan does not support environment reviewers, so explicit owner-only dispatch
from `main` is the publishing approval gate. Do not remove that gate when
adding maintainers; configure environment reviewers first.

Live repository settings require Ruff and all 11 compatibility checks, code-owner review,
stale-review dismissal, and resolved conversations on `main`; fork workflows
remain disabled, token defaults are read-only, Actions PR approvals are disabled,
and dependency alerts/security updates are enabled. The Actions allowlist
contains only the five actions used by these workflows, and full commit SHA
pinning is enforced repository-wide.
