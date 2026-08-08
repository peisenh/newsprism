# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]
### Added
- Two-step release automation: `prepare-release.sh X.Y.Z` turns the Unreleased
  changelog section into a dated version block, refreshes the compare links,
  and commits/pushes it; `release.sh` then tags and pushes. Splitting the
  changelog push from the tag push keeps the tag push a clean, isolated event
  so the release workflow triggers reliably.

### Changed
- The release workflow now runs on both GitHub Actions and Gitea Actions. It
  creates the release via the platform's REST API with `curl` instead of the
  `gh` CLI, detecting the platform from `GITHUB_SERVER_URL` and switching the
  API base URL and auth header accordingly. The two APIs share the same release
  endpoint and JSON fields.

## [0.2.1] - 2026-08-06
### Changed
- Moved the favicon out of the Python source into `static/favicon.svg`,
  published under a content-hashed name (`favicon.<hash>.svg`) like the other
  static assets — it was an inline `data:` URI before. Output is functionally
  identical; archive snapshots reference the favicon version they were written
  with, same as for CSS/JS.

## [0.2.0] - 2026-08-06
### Changed
- Moved the HTML, CSS, and JavaScript out of `newsprism.py` into external
  files: the dashboard, cards, and archive index are now Jinja2 templates
  (`templates/`), and the stylesheet and client-side script live in `static/`.
  No HTML is generated in Python anymore. The rendered output is visually
  identical (verified whitespace-normalized identical); only insignificant
  whitespace in the markup differs.

### Added
- `Jinja2` runtime dependency (for the HTML templating).
- Static assets are published under content-hashed names
  (`style.<hash>.css`, `app.<hash>.js`). Each archive snapshot references the
  asset versions it was written with, so later CSS/JS changes can no longer
  break the styling or interactivity of older snapshots.

### Notes
- Deployment: the image now also ships `templates/` and `static/`, and the
  dashboard's stylesheet and script are served as separate files next to
  `index.html` (the page is no longer a single self-contained file). The Atom
  feed is deliberately still rendered in Python (XML escaping differs from
  Jinja's HTML autoescaping).

## [0.1.3] - 2026-08-04
### Added
- Changelog and an automated release workflow (release notes are derived
  from this file on tag push).

## [0.1.2] - 2026-08-01
### Added
- `.pylintrc` with documented rule choices.

### Fixed
- Resolved pylint findings and removed dead code; minor lint tidy-ups.
  HTML output is unchanged (verified byte-identical).

## [0.1.1] - 2026-07-22
### Added
- Screenshots in the README.

## [0.1.0] - 2026-07-22
### Added
- First public release: RSS aggregator that clusters German-language articles
  by political lean and surfaces bias and blindspots, using Cohere `embed-v4.0`
  multilingual embeddings and a domain-keyed media bias map.

[Unreleased]: https://github.com/peisenh/newsprism/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/peisenh/newsprism/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/peisenh/newsprism/compare/v0.1.3...v0.2.0
[0.1.3]: https://github.com/peisenh/newsprism/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/peisenh/newsprism/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/peisenh/newsprism/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/peisenh/newsprism/releases/tag/v0.1.0
