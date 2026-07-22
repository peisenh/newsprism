# Contributing

*English · [Deutsch](CONTRIBUTING.de.md)*

Thanks for your interest in NewsPrism. This is a small, self-hosted hobby
project; contributions are welcome but kept lightweight.

## License of contributions

NewsPrism is licensed under the **GNU Affero General Public License v3.0**
(AGPL-3.0, see [LICENSE](LICENSE)). By contributing, you agree that your
contribution is provided under the same license.

## Sign-off (Developer Certificate of Origin)

To keep the provenance of the code clear, contributions must be **signed off**
under the [Developer Certificate of Origin](DCO) (DCO 1.1). The sign-off is a
simple statement that you have the right to submit the code under the project
license — it does **not** transfer any rights to a single person or company
(there is no CLA).

Add the sign-off line to each commit by committing with `-s`:

    git commit -s -m "Your message"

This appends a line like:

    Signed-off-by: Your Name <you@example.com>

Use your real name (or a stable pseudonym) and a reachable e-mail address. By
signing off you certify the points listed in the [DCO](DCO) file.

## Practical notes

- The bias map (`bias_map.json`) is deliberately subjective. Pull requests that
  re-rate specific outlets are unlikely to be merged — the shipped map is only a
  placeholder; everyone maintains their own. Improvements to the *mechanism*
  (how the map is loaded, the second dimension, etc.) are welcome.
- The German-language parts that must stay German are intentional: the LLM
  prompts (the model produces German labels), the dashboard UI strings, and the
  entity stopword list. Please don't translate those.
- Keep changes focused and explain the "why" in the commit message.
- For larger changes, opening an issue first to discuss is appreciated.

No guarantees about review speed — this is a spare-time project.
