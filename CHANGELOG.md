# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/). Releases
in the 0.x series are published as pre-releases.

## [Unreleased]

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

[Unreleased]: https://github.com/peisenh/newsprism/compare/v0.1.3...HEAD
[0.1.3]: https://github.com/peisenh/newsprism/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/peisenh/newsprism/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/peisenh/newsprism/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/peisenh/newsprism/releases/tag/v0.1.0
