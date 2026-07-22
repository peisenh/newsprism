# ![Newsprism](logo/newsprism-wordmark.svg)

*[English](README.md) · Deutsch*

Eine kleine, selbst gehostete Pipeline zur Nachrichtenbündelung und
Medien-Bias-Analyse für den eigenen RSS-Feed-Bestand: holt Artikel aus
**FreshRSS oder Miniflux**,
ordnet jede Quelle über eine **Bias Map** (oder Kategorie-Regeln) einem
politischen Lager und einer feinen Links-Rechts-Position zu, **clustert**
dieselbe Story über mehrere Medien per Embeddings, zählt die
**Bias-Verteilung** (über distinkte Quellen, nicht Artikelzahl) und markiert
**Blindspots** – Stories, die nur eine Seite des Spektrums bringt.

Optional erzeugt ein LLM pro Cluster eine neutrale, deutsche Überschrift
(mit Caching, damit wiederkehrende Stories nicht erneut abgerechnet werden).

Ergebnis: `digest.json`, ein HTML-Dashboard (mit aufklappbaren
Einzelartikeln) und zwei Atom-Feeds (Haupt + nur Blindspots) zum Abonnieren
im eigenen Reader.

Alles über `config.yaml` umschaltbar – kein Code-Eingriff nötig.

## Verzeichnis-Layout

Das Repository ist selbst das Projekt (flach, build context "."). Versioniert
sind nur Vorlagen (`*.example`); die produktiven Dateien werden daraus erzeugt per
`cp` und sie sind in `.gitignore` (enthalten echte Hostnamen, Quellnamen, Keys).

    Dockerfile
    newsprism.py
    requirements.txt
    docker-compose.example.yml     <- kopieren nach docker-compose.yml
    config.example.yaml            <- kopieren nach config/config.yaml
    bias_map.example.json          <- kopieren nach config/bias_map.json
    entity_stopwords.example.yaml  <- kopieren nach config/entity_stopwords.yaml
    .env.example                   <- kopieren nach .env

Die produktiven Configs liegen im (gitignorierten) Verzeichnis `config/`, das
als Verzeichnis in den Container gemountet wird - so greifen Änderungen an
config.yaml/bias_map.json/entity_stopwords.yaml zur Laufzeit ohne Neustart.

## Setup

1. **Projekt klonen**, dann die Vorlagen kopieren:

       cp docker-compose.example.yml docker-compose.yml
       mkdir -p config
       cp config.example.yaml          config/config.yaml
       cp bias_map.example.json        config/bias_map.json
       cp entity_stopwords.example.yaml config/entity_stopwords.yaml
       cp .env.example .env

   Die produktive `docker-compose.yml` an das eigene Setup anpassen (Hostname,
   Netzwerk, Reverse Proxy, ob ein eigenes nginx/Webserver-Volume mitgenutzt wird).

2. **Config anpassen** (`config.yaml`), mindestens:
   - `reader.base_url` – intern empfohlen: `http://freshrss/api/greader.php`
     (FreshRSS muss im selben Docker-Netzwerk hängen).
   - `reader.username` / `password` – bei FreshRSS das **API-Passwort** aus
     *Profil → API-Verwaltung* (nicht das Login-Passwort); API-Zugriff in
     den FreshRSS-Einstellungen aktivieren.
   - `lean.rules` / `lean.bias_map_path` an die eigenen Feed-Kategorien und
     gewünschte Medien-Einordnung anpassen (`bias_map.json` ist nur ein
     Startpunkt – die Einordnung ist subjektiv und an die eigene
     Einschätzung anzupassen).
   - `output.*` URLs auf die eigene Domain/IP setzen.

3. **API-Keys** (nur die genutzten) in `.env` eintragen:

       ANTHROPIC_API_KEY=sk-ant-...
       COHERE_API_KEY=...

   Wichtig: `env_file: [.env]` auf Service-Ebene verwenden, nicht
   `environment:`-Interpolation – sonst kann der Key bei leerer
   Shell-Variable überschrieben werden.

4. **Starten**

       docker compose up -d --build
       docker compose run --rm -e RUN_ONCE=1 newsprism   # einmaliger Testlauf

   Der Container läuft als non-root User (UID/GID 1000:1000 per Default,
   siehe Dockerfile/docker-compose.yml). Falls der Host-User für die
   Docker-Volumes eine andere UID/GID hat, beim Build anpassen:

       docker compose build --build-arg UID=$(id -u) --build-arg GID=$(id -g) newsprism

   oder die `args:`-Werte direkt in der `docker-compose.yml` ändern.

5. **Ausgabe** landet im konfigurierten Output-Verzeichnis (Standard: ein
   `/prisma`-Unterordner im gemounteten Webserver-Volume):
   - Dashboard:        `.../prisma/index.html`
   - Haupt-Feed:       `.../prisma/feed.xml`
   - Blindspot-Feed:   `.../prisma/feed-blindspots.xml`

6. **In FreshRSS/Miniflux abonnieren** – beide Feeds in eine eigene
   Kategorie, z. B. „Prisma", und diese Kategorie in
   `reader.exclude_categories` aufnehmen (sonst liest newsprism seine
   eigene Ausgabe als Input – Feedback-Loop).

7. **Sofortigen Lauf erzwingen** (ohne Neustart, z. B. nach Config-Änderung
   an `bias_map.json` oder zum Testen): an den laufenden Container
   `SIGUSR1` senden – unterbricht den Schlaf-Zyklus und startet sofort
   einen Durchlauf, danach läuft der normale Takt weiter.

       docker kill -s SIGUSR1 newsprism

8. **Optional: „Aktualisieren"-Button im Dashboard** – mit
   `refresh_server.enabled: true` startet newsprism einen kleinen
   HTTP-Listener (`POST /refresh`), der einen Lauf anstößt (mit Cooldown
   via `min_interval_minutes`). Der Listener hat **keine eigene
   Authentifizierung** – er MUSS hinter einem Reverse-Proxy mit Auth
   liegen. Beispiel-Labels für Traefik mit BasicAuth stehen in der
   `docker-compose.yml`; der Passwort-Hash wird erzeugt mit:

       htpasswd -nb benutzer geheim
       # Ausgabe in das basicauth.users-Label eintragen,
       # dabei $ als $$ escapen (Docker-Compose-Interpolation).

   Der Listener-Port wird nicht nach außen gemappt, nur intern für den
   Proxy. SIGUSR1 unterliegt dem Cooldown nicht.

## Die wichtigsten Schalter (config.yaml)

| Block | Was es umschaltet |
|-------|-------------------|
| `reader.type` | `greader` (FreshRSS & Miniflux) oder `miniflux` (nativ) |
| `reader.exclude_categories` / `exclude_sources` / `exclude_title_patterns` | Feedback-Loop & Rauschen ausschließen |
| `clustering.method` | `embedding` (empfohlen) oder `llm` |
| `clustering.embedding.provider` | `openai_compatible` (API/Ollama) oder `fastembed` (lokal/CPU) |
| `clustering.embedding.cache_path` | Embedding-Cache – nur neue Artikel werden erneut eingebettet |
| `clustering.threshold` | strenger (kleiner) ↔ lockerer (größer) clustern |
| `clustering.entity_weight` | optionales zweites Signal: Eigennamen-Overlap (0 = aus) |
| `lean.bias_map_path` / `bias_map` | feine Quelle→Score-Zuordnung (−2…+2), Vorrang vor Kategorie |
| `lean.source_overrides` | grobe Lager-Zuordnung für einzelne Quellen |
| `blindspot.min_articles` / `min_sources` | Schwellen für die Blindspot-Erkennung (über distinkte Quellen) |
| `llm.enabled` / `provider` / `model` | Story-Überschriften an/aus; `anthropic` / `openai_compatible` / `none` |
| `llm.max_clusters` | Kosten-/Call-Bremse für die Zusammenfassungen |
| `llm.cache_path` | Summary-Cache (inkl. negativer Treffer) |
| `hotspots.enabled` | Cluster im Dashboard zu Oberthemen gruppieren (1 LLM-Call/Lauf, nur Anzeige) |
| `refresh_server.enabled` | HTTP-Listener + „Aktualisieren"-Button (hinter Reverse-Proxy-Auth) |
| `output.archive` | zeitgestempelte HTML-Snapshots unter `archiv/` |
| `schedule.interval_hours` | Lauf-Takt |
| `schedule.active_start` / `active_end` | Tag-Fenster (z. B. nur 7-23 Uhr), Rest = Nachtruhe |
| `output.feed_guid` | `change` (empfohlen) / `daily` / `run` - wie oft eine Story im Reader neu erscheint |

## Caching & Kosten

Mit aktivem Embedding- und Summary-Cache verarbeitet jeder Lauf nur **neue**
Artikel bzw. Stories erneut - wiederkehrende Inhalte kosten nichts zusätzlich,
unabhängig vom Lauf-Takt. Typische Kosten bei mehreren tausend Artikeln/Tag:

- **Embeddings** (`text-embedding-3-small`/`-large`): Cent-Bereich/Monat
- **LLM-Summaries** (`claude-haiku-*`, `max_clusters: 40-80`): ~0,5-3 EUR/Monat

Komplett kostenlos/offline möglich mit `clustering.embedding.provider:
fastembed` und `llm.enabled: false` - auf schwacher CPU (kein AVX2) sind
Embeddings darüber zwar möglich, aber spürbar langsamer und semantisch
schwächer als API-Modelle.

## Feintuning

- `clustering.threshold` ist der zentrale Regler: zu viele Mini-Cluster ->
  erhöhen; zu viel thematisch Verschiedenes in einem Cluster -> senken. Bei
  großen, lang laufenden Themen (z. B. Sportgroßereignisse) kollidieren
  beide Ziele - das ist ein bekannter Trade-off ohne perfekte Lösung.
- Clustering nutzt Titel **und** Teaser/Beschreibung - deutlich trennschärfer
  als Titel allein.
- `clustering.entity_weight` (0 = aus per Default) ist ein optionales zweites
  Signal neben der reinen Embedding-Distanz: erkannte Eigennamen/Akronyme
  (Personen, Orte, Parteien, Organisationen) werden zwischen zwei Artikeln
  per Jaccard-Overlap verglichen. Teilen sie Namen, sinkt die effektive
  Distanz (fördert Merge von "gleiche Story, andere Quelle/Formulierung");
  teilen sie keine, steigt sie (verhindert Merge verschiedener Stories im
  selben Themenfeld, die nur lexikalisch ähnlich sind). Die Eigennamen-
  Erkennung ist eine Heuristik mit Stopwortliste für generische deutsche
  News-Substantive - kein NER-Modell, also nicht perfekt. Bei Aktivierung
  ggf. `threshold` leicht neu kalibrieren, da sich die effektive Distanz
  verschiebt.
- `lean.bias_map` ist bewusst eine einfache, editierbare JSON-Datei - Werte
  sind Einschätzungen, kein Anspruch auf Objektivität. An die
  eigene Quellenliste und Bewertung anpassen.
- LLM-Prompt verlangt explizit deutsche, neutrale Zeitungsüberschriften und
  erlaubt bei heterogenen Clustern Sammel-Überschriften ("X: Debatte um A, B
  und C") statt ein Einzelthema fälschlich als repräsentativ darzustellen.

## Sicherheit

Artikel-Inhalte (Titel, Teaser, Links) stammen aus RSS-Feeds - also letztlich
von Dritten. newsprism geht damit so um:

- **Links**: nur `http(s)://`-URLs werden als `<a href=...>` ausgegeben
  (Dashboard und Atom-Feeds). `javascript:`/`data:`-URIs aus manipulierten
  Feeds werden zu `#` neutralisiert.
- **Längen**: Titel/Quellname/Kategorie werden beim Einlesen gekappt
  (300/120/200 Zeichen), damit ein überlanger Wert nicht Embedding-/LLM-Kosten
  oder das Layout sprengt.
- **HTML-Escaping**: alle aus Artikeln stammenden Texte werden beim Schreiben
  von HTML/Atom escaped.
- **Prompt Injection**: Artikel-Titel/Teaser werden Teil des LLM-Prompts (für
  Cluster-Überschriften). Ein böswilliger Feed könnte versuchen, das LLM zu
  einer irreführenden Überschrift zu bewegen. Das Ergebnis wird aber weiterhin
  HTML-escaped ausgegeben (kein Code-Ausführungsrisiko), und `min_sources`/
  `min_cluster_size` begrenzen den Einfluss einer einzelnen Quelle auf einen
  Cluster. Vollständig ausschließen lässt sich Prompt Injection bei diesem
  Design nicht - das LLM muss den Artikelinhalt lesen können.
- **TLS**: `reader.verify_tls: false` deaktiviert die Zertifikatsprüfung
  komplett - nur für vertrauenswürdige interne Hosts (z. B. eigene CA)
  verwenden.
- **Secrets**: API-Keys werden ausschließlich über Umgebungsvariablen
  (`.env` + `env_file:`) gelesen, nie geloggt oder in Output-Dateien
  geschrieben.
- **Container-Härtung**: läuft als non-root User (UID/GID 1000:1000 per
  Default, anpassbar über Build-Args), Root-Filesystem read-only (nur `/tmp`
  via tmpfs beschreibbar, sowie die gemounteten `/data`- und `/cache`-Volumes),
  `no-new-privileges` und alle Linux-Capabilities gedroppt (`cap_drop: ALL`) -
  der Prozess braucht keine davon (reine HTTP-Requests + Dateizugriff auf die
  Volumes).

## Mitwirken

Beiträge sind willkommen. Bitte beachte die [DCO](DCO)-Sign-off-Pflicht
(`git commit -s`) — siehe [CONTRIBUTING.de.md](CONTRIBUTING.de.md)
([English](CONTRIBUTING.md)).

## Lizenz

Lizenziert unter der **GNU Affero General Public License v3.0** (AGPL-3.0) –
siehe [LICENSE](LICENSE). Kurz gesagt: frei nutzbar, studierbar, veränderbar
und teilbar, aber Änderungen müssen unter derselben Lizenz offen bleiben –
und das gilt auch, wenn die Software als Netzwerkdienst angeboten wird (wer
eine veränderte Version als öffentliche Instanz betreibt, muss deren
Quellcode den Nutzern zugänglich machen).

Keine Garantie für die Richtigkeit der Bias-Einordnung oder Cluster-Qualität
- das ist ein persönliches Homelab-Tool, kein journalistisches Produkt.

## Autor

Peter Eisenhauer – Fragen und Feedback über
[GitHub-Issues](../../issues) oder `github@peter-e.de`.
