# Changelog

All notable trackinizer changes are documented here. This project follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

## 0.1.1 - 2026-07-31

### Fixed

- Cascade-dependent semantics for `proves` / `favors` citations.
- Citation-edge provenance inference regression.

### Changed

- `Final` annotations on protocol and structural constants; module-level
  response constants inlined in the edge API.
- Testing module public and internal exports reorganized.

## 0.1.0 - 2026-07-29

- Initial public release of trackinizer: a Postgres-backed agent-tracking
  server and client for Inquiries (Issues + Artifacts), with a wire protocol,
  a typed client, and a `trax` CLI.
