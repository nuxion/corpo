# Release process

`ado` ships as a single-file executable built with [shiv](https://github.com/linkedin/shiv).
There is no PyPI publish step — releases are a git tag plus a binary attached to a
GitHub Release.

## One-shot command

```fish
make release RELEASE_VERSION=x.y.z
```

This runs the bump, clean, and build steps below, then stops and prints the two
commands that publish the release (`git push` and `gh release create`). Those are
left manual on purpose — pushing and creating a public release are visible,
hard-to-undo actions, so you review the result before they happen.

## Step by step

### 1. Bump version

```fish
make bump-version RELEASE_VERSION=x.y.z
```

- Runs `uv version x.y.z`, which updates the version in `pyproject.toml` and
  `uv.lock`.
- Rewrites `__version__` in `ado_actions/__about__.py` to match via `sed`.
- These two are kept in sync manually because `ado --version` (Click's
  `version_option` in `ado_actions/cli.py`) reads `__about__.__version__`, while
  packaging tools read `pyproject.toml`. Letting them drift is how you end up with
  a binary that reports the wrong version.

### 2. Commit and tag

Also part of `bump-version`:

```fish
git add pyproject.toml uv.lock ado_actions/__about__.py
git commit -m "release vx.y.z"
git tag vx.y.z
```

- One commit, one tag, nothing else bundled in — keeps the release commit easy to
  audit and revert if the build fails downstream.
- The tag is what `git describe --tags` (used elsewhere in the Makefile as
  `GIT_TAG`) and `gh release create` key off of.

### 3. Clean build

```fish
make clean
make zipapps
```

- `clean` removes `dist/`, `build/`, and stray `.pyc`/`.pyo`/`.coverage` files, so
  the zipapp can't accidentally bundle stale artifacts from a previous build.
- `zipapps` runs
  `uv run shiv -c ado -o dist/ado -p "/usr/bin/env python3" --compressed .`,
  which resolves `pyproject.toml`'s dependencies, packages them with the
  `ado_actions` source into a self-contained `dist/ado`, and points its shebang at
  `/usr/bin/env python3` so it runs against whatever Python 3 is first on the
  target machine's `PATH`.

### 4. Smoke test the artifact

```fish
./dist/ado --version
```

- Confirms the built binary actually runs and reports the version you just set —
  catches a broken shiv build (missing dependency, wrong entry point) before it
  ships. For anything beyond a version check, exercise a couple of real
  subcommands (`./dist/ado board --help`, etc.), ideally outside your dev venv so
  you're not accidentally relying on packages already on your `PYTHONPATH`.

### 5. Push

```fish
git push && git push --tags
```

- Publishes the release commit and tag. Left as a manual step since it's a
  shared, hard-to-reverse action.

### 6. Publish the binary

```fish
gh release create vx.y.z dist/ado --notes "..."
```

- Creates the GitHub Release for the tag and attaches `dist/ado` as a downloadable
  asset. Write real release notes in place of `"..."`.

### 7. Install/update on a machine

```fish
curl -L <release-asset-url> -o ~/.local/bin/ado
chmod +x ~/.local/bin/ado
```

or, from a fresh checkout of the tagged commit:

```fish
make install-zipapps
```

which rebuilds and installs to `~/.local/bin/ado` in one step.
