# NewsPrism – Wie die Pipeline funktioniert

*[English](PIPELINE.md) · Deutsch*

Diese Datei erklärt, wie NewsPrism aus einer FreshRSS-Artikelliste das fertige
Dashboard mit Clustern, Lager-Einordnung, Blindspots, LLM-Titeln und Hotspots
erzeugt. Sie beschreibt die Verarbeitungskette Schritt für Schritt und nennt die
Stellschrauben (config-Schlüssel) an der jeweils passenden Stelle.

Der gesamte Ablauf steckt in `run_once()` und läuft pro Durchlauf einmal durch.

```
FreshRSS ──▶ filter ──▶ Lager-Zuordnung ──▶ Embeddings ──▶ Clustering
   (1)         (2)            (3)               (4)            (5)
                                                                │
        Dashboard ◀── Hotspots ◀── LLM-Titel ◀── Cluster-Aufbau ◀┘
          (9)           (8)           (7)            (6)
```

---

## 1. Artikel holen (`fetch_articles` → `fetch_greader`)

Die Artikel kommen über die **GReader-API** von FreshRSS (`reader.base_url`).
NewsPrism paginiert über die `continuation`-Marke in Stapeln von 1000, bis das
Zeitfenster `reader.window_hours` abgedeckt oder `reader.max_items` erreicht ist.
Zugangsdaten werden per `_cfg_secret` bevorzugt aus Umgebungsvariablen gelesen
(`username_env`/`password_env`, Werte in `.env`), mit Klartext-Fallback.

**Wichtige Stellschraube:** `window_hours` bestimmt, wie viel Geschichte ein Lauf
sieht. Kleiner = schärferer Momentüberblick, größer = mehr Blindspots, aber auch
mehr Rauschen. Betriebspunkt ist 36 h (Mittelweg zwischen 24 h-Schärfe und
48 h-Rauschexplosion).

---

## 2. Filtern (`filter_articles`)

Vor allem Weiteren werden unerwünschte Artikel entfernt. Vier Filter greifen:

- `exclude_sources` – ganze Quellen ausschließen
- `exclude_categories` – nach FreshRSS-Kategorie
- `exclude_title_patterns` – Titel-Regex (z. B. Werbe-/Formatfloskeln)
- `exclude_url_patterns` – URL-Pfad-Regex (z. B. `bild.de/sport/fussball`, um
  Sport-Ticker auszublenden)

Danach wird auf `reader.max_items` gekürzt.

---

## 3. Lager-Zuordnung (`assign_leans`) – die Bias-Map

Jeder Artikel bekommt anhand seiner Quelle eine politische Einordnung aus der
**Bias-Map** (`bias_map.json`, domänen-/quellen-keyed). Die Map hat zwei
Dimensionen:

1. **Links–Rechts** als Zahl von **−2 bis +2**
   (−2 = links, 0 = Mitte, +2 = rechts).
2. **Herkunft** über ein optionales `alignment`-Feld
   (z. B. `russia-state`, `china-state`; fehlt = westlicher Standard). Diese
   zweite Dimension erfasst staatlich-/nicht-westlich-affiliierte Quellen und
   wird im Dashboard als Herkunfts-Badge angezeigt.

Ein Map-Eintrag kann eine reine Zahl sein (`-1`) oder ein Objekt
(`{"score": -1, "kind": ..., "alignment": ...}`). Quellen ohne Eintrag bleiben
ohne Einordnung und zählen nicht in die Lager-Statistik der Cluster.

`lean.analyze` legt fest, welche Lager überhaupt in die Blindspot-Analyse
einfließen.

> Die Bias-Map ist die inhaltlich subjektivste Komponente des Systems. Sie ist
> bewusst transparent und vom Nutzer anpassbar – die Einordnung konkreter Medien
> ist eine Wertung, keine objektive Messung.

---

## 4. Embeddings (`_embed_with_cache` → `embed_*`)

Für das Clustering wird jeder Artikel in einen **Embedding-Vektor** übersetzt –
eine Zahlenrepräsentation seiner Bedeutung, sodass inhaltlich ähnliche Artikel
nahe beieinander liegen.

- **Embedding-Text** (`_embed_text`): `"Titel. Teaser"` (Teaser nur, falls
  vorhanden) – das gibt mehr Trennschärfe als der Titel allein.
- **Provider** (`clustering.embedding.provider`): `cohere` (mehrsprachig, gut für
  den deutsch-englisch-französisch gemischten Quellenmix), alternativ
  `openai_compatible` (OpenAI/Ollama/LM Studio) oder `fastembed` (lokal).
- **Cache** (`/cache/embeddings.json`): Schon einmal eingebettete Artikel werden
  nicht erneut an die API geschickt. Der Cache wird inkrementell und atomar
  geschrieben (Stapel von 480). Sein Aufbewahrungsfenster ist an `window_hours`
  gekoppelt (rund `window_h/24 + 1` Tage, mindestens 2), damit wiederkehrende
  Artikel zwischen Läufen im Cache bleiben.

Cross-linguale Eigenschaft: Cohere bettet denselben Sachverhalt in verschiedenen
Sprachen nah beieinander ein – ein deutscher und ein englischer Artikel zum
selben Ereignis landen im selben Cluster. Das ist gewollt und der Grund, warum
rein strukturelle Trennverfahren (siehe Abschnitt „Was nicht funktioniert hat")
hier an Grenzen stoßen.

---

## 5. Clustering (`cluster_by_embeddings`, `_distance_matrix`, `split_large_clusters`)

### 5a. Distanzmatrix

Aus den Vektoren wird eine paarweise **Cosine-Distanz** berechnet
(`1 − v·vᵀ`, auf 0…2 geklemmt, Diagonale 0). Kleine Distanz = ähnlicher Inhalt.

**Optionale Eigennamen-Distanz (Jaccard):** Ist `clustering.entity_weight > 0`,
wird die Cosine-Distanz mit einer Eigennamen-Distanz gemischt:

```
combined = (1 − weight) · cosine_dist + weight · entity_dist
```

`_extract_entities` zieht großgeschriebene Wörter (minus Stopwortliste
`entity_stopwords.yaml`) als „Eigennamen" aus Titel+Teaser; `entity_dist` ist die
**Jaccard-Distanz** dieser Eigennamen-Mengen (1 − |Schnitt|/|Vereinigung|).
Idee: Artikel mit denselben Namen rücken näher, mit disjunkten Namen weiter
auseinander. Teilen zwei Artikel gar keine erkannten Namen, bleibt für dieses
Paar die reine Cosine-Distanz stehen (kein künstliches Auseinanderziehen).

> **Praxis-Hinweis:** Bei mehrsprachigen Daten bringt `entity_weight` wenig
> Trennschärfe (gemessen: nahezu alle Cluster ~0,97 Eigennamen-Distanz, weil
> dieselbe Entität sprachabhängig unterschiedlich geschrieben wird –
> „München"/„Munich", „Köln"/„Cologne"). Es schadet nicht, ist aber kein
> wirksamer Hebel. Default 0.0.

### 5b. Haupt-Clustering

**Agglomeratives Clustering** (`AgglomerativeClustering`, `metric="precomputed"`,
`distance_threshold = clustering.threshold`, ~0.71). Es gruppiert alle Artikel
unterhalb der Distanzschwelle zu Clustern – thematisch und cross-lingual. Kein
fester `n_clusters`; die Zahl ergibt sich aus der Schwelle.

### 5c. Riesencluster aufbrechen (`split_large_clusters`)

Große, thematisch breite Cluster (z. B. „alles zur WM") werden optional in
ereignis-nähere Teilcluster zerlegt: Cluster ab `clustering.split_above` Artikeln
werden mit einem **strengeren** `clustering.sub_threshold` (~0.66) erneut
geclustert. Die Embedding-Vektoren werden dabei durchgereicht (`vecs=`), sodass
beim Splitten kein erneuter Cache-/API-Zugriff nötig ist.

Restfehler: Zwei **semantisch sehr ähnliche** Ereignisse (zwei Zugunglücke am
selben Tag, zwei Fußballspiele) lassen sich so nicht zuverlässig trennen – ihre
Cosine-Distanz ist kleiner als die zwischen zwei Sprachfassungen desselben
Ereignisses. Das wird beim Labeling aufgefangen (Abschnitt 7).

---

## 6. Cluster-Aufbau & Blindspots (`build_clusters`)

Aus den Label-Gruppen werden `Cluster`-Objekte gebaut: Artikel nach Zeit
sortiert (neueste zuerst), Lager-/Bias-/Herkunfts-Zählungen aggregiert, ein
vorläufiges Label (Titel des repräsentativen Artikels) gesetzt.

**Blindspot-Erkennung:** Ein Cluster gilt als Blindspot, wenn er praktisch nur
von **einem** Lager berichtet wird:

- `left_only` – nur linke Quellen
- `right_only` – nur rechte Quellen

Zwei Plausibilitäts-Hürden verhindern Trivial-Blindspots:

- `clustering.min_distinct_sources` (Default 2) – mindestens so viele
  **verschiedene** Quellen müssen berichten.
- `clustering.max_source_share` (Default 0.9) – keine **einzelne** Quelle darf
  mehr als diesen Anteil der Artikel stellen.

Deaktivieren: `min_distinct_sources: 1` bzw. `max_source_share: 1.0`.

> Diese strukturellen Hürden filtern offensichtliche Fälle (eine einzelne Quelle,
> ein Quelle-plus-Schwesterblatt). Sie trennen aber **nicht** bedeutsame von
> belanglosen Blindspots – ein echter politischer Blindspot und ein
> Lokalunfall können dieselbe Quellenstruktur haben. Diese Unterscheidung
> passiert erst im LLM-Schritt (Abschnitt 7).

---

## 7. LLM-Titel (`summarize_clusters`)

Cluster bekommen vom LLM (Haiku) eine prägnante deutsche Überschrift. Welche
Cluster gelabelt werden, steuert eine zweiteilige Regel:

- **alle Blindspots** (inhaltlich am wichtigsten), unabhängig von der Größe,
- **plus** alle Cluster ab Größe `llm.label_min_size` (Default 5),
- **gedeckelt** bei `llm.label_max_total` gesamt (Default 120; Blindspots zählen
  mit, bei Überlauf fallen die kleinsten Nicht-Blindspot-Cluster raus).

### Was das LLM pro Cluster sieht

Bis zu `llm.max_label_titles` Artikel-**Titel** (Default 30) und für die ersten
`llm.max_label_teasers` davon (Default 8) zusätzlich den Teaser-Text. Grund: Bei
einem großen Cluster mit zwei Ereignissen sähe das LLM bei zu kleinem Fenster
nur das dominante Ereignis (die neuesten Titel) und übersähe das zweite. Titel
sind billig (mehr davon), Teaser teuer (begrenzt).

### Der Prompt – zwei bzw. drei Stufen

Der Label-Call ist **ein** LLM-Aufruf, der das LLM aber mehrere Dinge nacheinander
entscheiden lässt und in einem festen Format antworten lässt
(`… | … | <Label>`, Label immer das letzte Feld):

- **SCHRITT 0 – Relevanz (nur für Blindspot-Kandidaten):** Ist das ein
  überregional bedeutsames politisches/gesellschaftliches Thema (`RELEVANT`) oder
  nur Lokales/Boulevard/Promi/Meinung (`IRRELEVANT`)? Ein `IRRELEVANT`-Cluster
  verliert seinen Blindspot-Status – er bleibt als normaler Cluster sichtbar,
  verwässert aber die Blindspot-Box nicht mehr. Dies fängt die Fehlalarm-Klasse
  ab, die strukturell nicht trennbar ist (siehe Abschnitt 6).
- **SCHRITT 1 – ein oder mehrere Ereignisse:** `EINS` (ein konkretes Ereignis)
  oder `MEHRERE` (verschiedene, nur thematisch ähnliche Ereignisse).
- **SCHRITT 2 – Label:** Bei `EINS` ein konkretes, unterscheidbares Label; bei
  `MEHRERE` ein ehrliches Sammel-Label, das die Gemeinsamkeit nennt und die
  wichtigsten Fälle aufzählt (statt einen Einzelfall fälschlich zum Label des
  ganzen Clusters zu machen).

Die Klassifikations-Präfixe werden beim Parsen abgetrennt; nur das Label landet
im Dashboard und im Cache. Die explizite Vorab-Klassifikation (statt bloßer
Ermahnung im Prompt) ist das, was diffuse Sammelcluster zuverlässig zu ehrlichen
Sammel-Labels führt.

### Cache

Labels werden unter bis zu 5 Artikel-URLs des Clusters gecacht
(`/cache/summaries.json`), inklusive des Relevanz-Urteils (`irrelevant`-Flag).
So greift bei einem leicht veränderten/gewachsenen Cluster im nächsten Lauf ein
Treffer, und das Relevanz-Urteil wirkt auch bei Cache-Treffern – ohne erneuten
LLM-Aufruf.

---

## 8. Hotspots (`assign_hotspots`)

Optionale zweite Hierarchie-Ebene (`hotspots.enabled`): **ein** zusätzlicher
LLM-Call gruppiert die Cluster-Labels in wenige Oberthemen (ein großes
Sportereignis, eine Konfliktregion, ein Land/Staatschef). Rein für die Anzeige – ohne Einfluss auf Clustering oder
Blindspots.

Mini-Hotspots (Themen mit nur einer Story) werden aufgelöst: Ein Hotspot bleibt
nur, wenn er mindestens `hotspots.min_stories` Cluster (oder für wichtige
Einzelthemen genug Artikel) umfasst. Optional können in `hotspots.user_topics`
eigene Themen vorgegeben werden – als reiner String (LLM-Zuordnung) oder als
`{name, keywords}` (deterministische Zuordnung über Schlüsselwörter, die nur bei
Treffer im Label oder ausreichendem Titel-Anteil zählt).

---

## 9. Ausgabe (`to_payload`, `write_html`)

Aus den fertigen Clustern wird ein Payload gebaut und daraus geschrieben:

- das **HTML-Dashboard** (Cluster-Liste, Blindspot-Box, Hotspot-Gruppierung,
  Lager-/Herkunfts-Badges, Teilen-Button, Versionsanzeige, Lauf-Kosten),
- **Atom-Feeds** für Abonnenten.

Optionales Archiv (`output.archive`) legt pro Lauf einen Snapshot ab.

---

## Anhang: Was nicht funktioniert hat (und warum es so bleibt)

Drei strukturelle Ansätze, „diffuse" oder „verschmolzene" Cluster automatisch zu
erkennen, wurden erprobt und **verworfen**, weil sie an den mehrsprachigen Daten
scheitern:

1. **Kohärenz-Geometrie** (mittlere Zentroid-Distanz): Ein diffuser Mehr-
   Ereignis-Cluster und ein kohärenter cross-lingualer Cluster (ein Ereignis in
   drei Sprachen) haben fast dieselbe Streuung. Keine Trennschwelle.
2. **Eigennamen-Distanz als Trennsignal**: Bei deutschen/mehrsprachigen Texten
   nahezu konstant hoch (~0,97), weil jede großgeschriebene Form als eigener
   „Name" zählt und Sprachvarianten nicht zusammengeführt werden.
3. **Reine Prompt-Ermahnungen** („falls mehrere Ereignisse, Sammel-Label"): zu
   schwach; das LLM griff weiter Einzelfälle heraus.

Die Lehre: Die Unterscheidung „ein Ereignis vs. mehrere" und „bedeutsam vs.
belanglos" ist **semantisch**, nicht strukturell. Deshalb sitzt die Lösung im
LLM-Label-Schritt (explizite Vorab-Klassifikation, Abschnitt 7) und nicht in der
Clustering-Geometrie. Akzeptierter Restfehler: zwei semantisch fast identische
Ereignisse am selben Tag können verschmolzen bleiben – das LLM benennt dann
beide im Sammel-Label, statt eines davon zu verschweigen.

Das Diagnose-Werkzeug `clustering.entity_diag` (Default aus) bleibt im Code, um
die Eigennamen-Distanz bei Bedarf erneut an realen Daten messen zu können.
