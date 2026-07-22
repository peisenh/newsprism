# Mitwirken

*[English](CONTRIBUTING.md) · Deutsch*

Danke für das Interesse an NewsPrism. Dies ist ein kleines, selbst gehostetes
Hobby-Projekt; Beiträge sind willkommen, werden aber bewusst schlank gehalten.

## Lizenz der Beiträge

NewsPrism steht unter der **GNU Affero General Public License v3.0**
(AGPL-3.0, siehe [LICENSE](LICENSE)). Mit einem Beitrag erklärst du dich damit
einverstanden, dass dein Beitrag unter derselben Lizenz bereitgestellt wird.

## Sign-off (Developer Certificate of Origin)

Damit die Herkunft des Codes nachvollziehbar bleibt, müssen Beiträge nach dem
[Developer Certificate of Origin](DCO) (DCO 1.1) **mit Sign-off** versehen
sein. Der Sign-off ist eine einfache Erklärung, dass du das Recht hast, den
Code unter der Projektlizenz beizutragen — er überträgt **keine** Rechte an
eine einzelne Person oder Firma (es gibt kein CLA).

Den Sign-off fügst du je Commit mit `-s` hinzu:

    git commit -s -m "Deine Nachricht"

Das hängt eine Zeile wie diese an:

    Signed-off-by: Dein Name <du@example.com>

Verwende deinen echten Namen (oder ein dauerhaftes Pseudonym) und eine
erreichbare E-Mail-Adresse. Mit dem Sign-off bestätigst du die Punkte aus der
Datei [DCO](DCO).

## Praktische Hinweise

- Die Bias Map (`bias_map.json`) ist bewusst subjektiv. Pull Requests, die
  einzelne Medien neu einordnen, werden eher nicht übernommen — die
  mitgelieferte Map ist nur ein Platzhalter; jede/r pflegt die eigene.
  Verbesserungen am *Mechanismus* (wie die Map geladen wird, zweite Dimension
  usw.) sind willkommen.
- Die deutschsprachigen Teile, die deutsch bleiben müssen, sind Absicht: die
  LLM-Prompts (das Modell erzeugt deutsche Labels), die Dashboard-UI-Texte und
  die Eigennamen-Stopwortliste. Bitte nicht übersetzen.
- Halte Änderungen fokussiert und erläutere das „Warum" in der Commit-Nachricht.
- Bei größeren Änderungen ist es willkommen, vorher ein Issue zur Abstimmung zu
  eröffnen.

Keine Garantie zur Review-Geschwindigkeit — dies ist ein Freizeitprojekt.
