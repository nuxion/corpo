# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-09-18

First tagged release, packaged as a single-file `shiv` executable.

### Added

- CLI refactor covering the full command surface (`ado`), built on Click.
- Agnostic board command, independent of any single Azure DevOps org layout.
- `work-status` command (moved from `story-status`) under `board`.
- Test plan and test case fetching from Azure DevOps.
- Release process and spec (`ado.spec.md`, `RELEASE.md`).

### Changed

- Source org/project/repo names from environment variables instead of
  org-internal identifiers, so the tool no longer hardcodes a specific
  organization's structure.

[Unreleased]: https://github.com/nuxion/corpo/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/nuxion/corpo/releases/tag/v0.2.0
