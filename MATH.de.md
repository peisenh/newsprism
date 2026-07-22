# NewsPrism – Mathematische und statistische Grundlagen

*[English](MATH.md) · Deutsch*

Diese Datei erklärt die Mathematik hinter NewsPrism: was ein Embedding ist, wie
Cosine- und Jaccard-Distanz berechnet werden und warum, wie das agglomerative
Clustering arbeitet und welche statistischen Schwellen die Blindspot-Erkennung
nutzt. Sie ergänzt `PIPELINE.md` (die das *Was* und *Wie* der Verarbeitungskette
beschreibt) um das *Warum* auf der mathematischen Ebene.

> **Formel-Darstellung:** Formeln stehen in LaTeX (`$…$` inline, `$$…$$` als
> Block) und rendern auf Plattformen mit Math-Unterstützung (Forgejo/Codeberg
> mit KaTeX, GitHub mit MathJax). Wo eine Formel komplex ist, steht zusätzlich
> eine Klartext-Form im Code-Block, damit die Datei auch ohne Rendering (Terminal,
> Editor) lesbar bleibt.

---

## 1. Embeddings – Bedeutung als Geometrie

### Idee

Ein **Embedding** bildet einen Text auf einen Punkt in einem hochdimensionalen
Vektorraum ab. Ein Embedding-Modell (hier Cohere, mehrsprachig) ist darauf
trainiert, dass **inhaltlich ähnliche Texte nahe beieinander** liegen und
unähnliche weit auseinander. Bedeutung wird dadurch zu Geometrie: „Abstand im
Raum" wird zu „inhaltlicher Unterschied".

Formal: Ein Text $t$ wird auf einen Vektor abgebildet

$$\mathbf{v} = E(t) \in \mathbb{R}^d$$

```
v = E(t),  v ist ein Vektor aus d reellen Zahlen (v ∈ ℝ^d)
```

wobei $d$ die Dimension des Modells ist (bei Cohere typisch einige hundert bis
über tausend). Jede Komponente ist eine gelernte latente Eigenschaft – einzeln
nicht interpretierbar, aber gemeinsam kodieren sie die Bedeutung.

In NewsPrism wird pro Artikel der Text `"Titel. Teaser"` eingebettet (Teaser nur,
falls vorhanden) – das gibt mehr Signal als der Titel allein.

### Warum das funktioniert

Das Modell lernt während des Trainings, semantisch verwandte Formulierungen auf
nahe Vektoren zu legen – auch **über Sprachgrenzen hinweg**. Ein deutscher und
ein englischer Artikel zum selben Ereignis landen nahe beieinander. Das ist die
Grundlage für das cross-linguale Clustering, hat aber eine Kehrseite (siehe
Abschnitt 6): Zwei Sprachfassungen *eines* Ereignisses können sich näher sein als
zwei *verschiedene* Ereignisse zum selben Thema.

---

## 2. Cosine-Ähnlichkeit und -Distanz

### Definition

Die **Cosine-Ähnlichkeit** zweier Vektoren misst den **Winkel** zwischen ihnen,
nicht ihren Abstand. Sie ist das Skalarprodukt, normiert auf die Längen:

$$\text{sim}(\mathbf{a}, \mathbf{b}) = \frac{\mathbf{a} \cdot \mathbf{b}}{\lVert \mathbf{a}\rVert \, \lVert \mathbf{b}\rVert} = \frac{\sum_{i=1}^{d} a_i b_i}{\sqrt{\sum_i a_i^2}\;\sqrt{\sum_i b_i^2}}$$

In Klartext:

```
sim(a,b) = (a · b) / (|a| · |b|)
         = Skalarprodukt / (Norm(a) · Norm(b))
```

Der Wert liegt in $[-1, 1]$: $1$ = gleiche Richtung (maximal ähnlich), $0$ =
orthogonal (unabhängig), $-1$ = entgegengesetzt.

Die **Cosine-Distanz** ist daraus abgeleitet:

$$d_{\cos}(\mathbf{a}, \mathbf{b}) = 1 - \text{sim}(\mathbf{a}, \mathbf{b})$$

```
cosine_dist(a,b) = 1 - sim(a,b)
```

Sie liegt bei normalisierten Embeddings praktisch in $[0, 2]$, meist $[0, 1]$.
Klein = ähnlich, groß = verschieden.

### Warum Winkel statt Abstand?

Cosine ignoriert die **Länge** der Vektoren und betrachtet nur ihre **Richtung**.
Das ist bei Text-Embeddings gewünscht: Die Länge hängt oft von Textlänge oder
Betonung ab, die *Richtung* trägt die Bedeutung. Ein kurzer und ein langer Text
zum selben Thema sollen ähnlich sein, auch wenn ihre Vektoren unterschiedlich
„lang" sind.

### Implementierungstrick: normalisierte Vektoren

Normalisiert man die Vektoren vorab auf Länge 1 ($\hat{\mathbf{v}} = \mathbf{v}/\lVert\mathbf{v}\rVert$),
vereinfacht sich die Ähnlichkeit zum reinen **Skalarprodukt**:

$$\text{sim}(\hat{\mathbf{a}}, \hat{\mathbf{b}}) = \hat{\mathbf{a}} \cdot \hat{\mathbf{b}}$$

```
sim(a^,b^) = a^ · b^        (a^, b^ = auf Länge 1 normalisiert)
```

NewsPrism berechnet die gesamte paarweise Distanzmatrix in einem Schritt als
Matrixprodukt:

```
cosine_dist = clip(1 - V · V^T, 0, 2)
```

wobei $V$ die Matrix aller (normalisierten) Artikel-Vektoren ist. Die Diagonale
(Distanz eines Artikels zu sich selbst) wird auf 0 gesetzt.

### Eigenschaft: keine echte Metrik

Die Cosine-Distanz erfüllt **nicht** die Dreiecksungleichung und ist damit streng
genommen keine Metrik. Für das Clustering ist das unkritisch (der Algorithmus
braucht nur eine konsistente Distanzmatrix), aber es ist der Grund, warum man
Cosine-Distanzen nicht naiv wie euklidische Abstände interpretieren sollte.

### Beziehung zur euklidischen Distanz

Für **normalisierte** Vektoren gilt ein direkter Zusammenhang:

$$\lVert \hat{\mathbf{a}} - \hat{\mathbf{b}} \rVert^2 = 2\,(1 - \hat{\mathbf{a}} \cdot \hat{\mathbf{b}}) = 2\, d_{\cos}(\hat{\mathbf{a}}, \hat{\mathbf{b}})$$

```
|a^ - b^|² = 2·(1 - a^·b^) = 2·cosine_dist(a^,b^)
```

Auf der Einheitskugel sind Cosine-Distanz und (quadrierte) euklidische Distanz
also bis auf den Faktor 2 dasselbe. Cosine ist hier nur die bequemere, direkt aus
dem Skalarprodukt berechenbare Form.

---

## 3. Jaccard-Distanz – Ähnlichkeit von Mengen

### Definition

Die **Jaccard-Ähnlichkeit** misst die Überlappung zweier **Mengen** $A$ und $B$
als Anteil der gemeinsamen Elemente an allen Elementen:

$$J(A, B) = \frac{|A \cap B|}{|A \cup B|}$$

```
J(A,B) = |Schnitt| / |Vereinigung|
       = (Elemente in beiden) / (Elemente in mindestens einer)
```

Wert in $[0, 1]$: $1$ = identische Mengen, $0$ = keine gemeinsamen Elemente. Die
**Jaccard-Distanz** ist:

$$d_J(A, B) = 1 - J(A, B) = 1 - \frac{|A \cap B|}{|A \cup B|}$$

### Wofür in NewsPrism?

Optional (`clustering.entity_weight > 0`) fließt eine Eigennamen-Distanz ins
Clustering ein. Dazu wird aus jedem Artikel eine **Menge von Eigennamen**
extrahiert (großgeschriebene Wörter minus Stopwortliste) und die Jaccard-Distanz
dieser Mengen berechnet. Idee: Zwei Artikel über dieselben Akteure (großer
Namens-Schnitt) sollen näher rücken, Artikel mit disjunkten Namen weiter
auseinander – ein Signal, das die Cosine-Distanz so nicht liefert.

### Warum es bei mehrsprachigen Daten scheitert

In der Praxis (gemessen) liegt die Jaccard-Distanz bei diesem Korpus für nahezu
**alle** Cluster konstant hoch (~0,97). Zwei Gründe:

1. **Über-Extraktion:** Im Deutschen ist jedes Substantiv großgeschrieben, nicht
   nur Eigennamen. Die Mengen werden groß und verrauscht.
2. **Sprachvarianten:** Dieselbe Entität heißt je nach Sprache anders
   („München"/„Munich", „Köln"/„Cologne"). Die Mengen überlappen selbst bei
   identischem Ereignis kaum.

Dadurch enthält $d_J$ kein nutzbares Trennsignal für „gleiches vs. verschiedenes
Ereignis". Default daher 0.0. Details siehe `PIPELINE.md`, Anhang.

---

## 4. Kombination der Distanzen

Sind beide Distanzen aktiv, mischt NewsPrism sie als **gewichtete
Linearkombination** (Konvexkombination) mit Gewicht $w \in [0, 1]$
(`entity_weight`):

$$d = (1 - w)\, d_{\cos} + w\, d_J$$

```
combined = (1 - w) · cosine_dist + w · entity_dist
```

Geometrisch ist das eine Interpolation zwischen den beiden Distanzräumen: $w = 0$
ist reine Cosine-Distanz, $w = 1$ reine Eigennamen-Distanz, dazwischen eine
Mischung. Teilen zwei Artikel **keine** erkannten Eigennamen, wird für dieses Paar
auf die reine Cosine-Distanz zurückgefallen (kein künstliches Auseinanderziehen
bei fehlendem Namenssignal).

---

## 5. Agglomeratives Clustering

### Prinzip

NewsPrism nutzt **agglomeratives hierarchisches Clustering** (bottom-up): Jeder
Artikel startet als eigener Cluster; iterativ werden die zwei **nächstgelegenen**
Cluster verschmolzen, bis ein Abbruchkriterium erreicht ist. Das Ergebnis ist
eine Hierarchie (Dendrogramm), die an einer Distanzschwelle „abgeschnitten" wird.

### Distanzschwelle statt fester Clusterzahl

Entscheidend: NewsPrism gibt **keine** Clusterzahl $k$ vor, sondern eine
**Distanzschwelle** (`distance_threshold = clustering.threshold`, ~0.71). Zwei
Cluster werden nur verschmolzen, solange ihre Distanz unter der Schwelle liegt.
Die Zahl der Cluster ergibt sich von selbst aus den Daten.

Das passt zum Problem: Die Zahl der Nachrichten-Themen pro Lauf ist unbekannt und
schwankt. Ein Verfahren wie k-means (das ein festes $k$ braucht und kugelförmige
Cluster annimmt) wäre hier ungeeignet.

### Linkage – wie misst man Distanz zwischen Clustern?

Beim Verschmelzen muss die Distanz zwischen zwei *Gruppen* definiert werden
(Linkage-Kriterium). Gängige Varianten:

- **single** – Distanz der nächsten zwei Punkte (neigt zu Ketten)
- **complete** – Distanz der entferntesten zwei Punkte (kompakte Cluster)
- **average** – mittlere paarweise Distanz
- **ward** – minimiert die Varianzzunahme beim Verschmelzen

NewsPrism arbeitet auf einer **vorberechneten Distanzmatrix**
(`metric="precomputed"`), was die geometrieabhängigen Linkages (z. B. ward)
ausschließt und zu average/complete passt.

### Riesencluster-Splitting

Sehr große, thematisch breite Cluster werden optional erneut geclustert: Cluster
ab `clustering.split_above` Artikeln werden mit einem **strengeren**
`sub_threshold` (~0,66 < 0,71) ein zweites Mal unterteilt. Da der Sub-Schritt
**innerhalb** eines bereits gebildeten Clusters arbeitet und die Vektoren
wiederverwendet, ist er günstig.

### Komplexität

Rein rechnerisch ist der teuerste Posten die **paarweise Distanzmatrix**: Bei
$n$ Artikeln sind das $\binom{n}{2} = \tfrac{n(n-1)}{2}$ Distanzen, also
$O(n^2)$ in Zeit und Speicher. Das agglomerative Clustering selbst liegt je
nach Implementierung zwischen $O(n^2)$ und $O(n^3)$. Beides skaliert mit $n$,
das über `reader.max_items` und `window_hours` festgelegt wird.

In der Praxis hängt es vom Embedding-Setup ab, welcher der beiden Posten
dominiert, denn sie skalieren unterschiedlich: Die Matrix wächst **quadratisch
mit $n$** (alle Artikel im Fenster), die Embedding-Phase dagegen nur **linear
mit der Zahl der seit dem letzten Lauf neuen Artikel** (der Rest kommt aus dem
Cache).

- **Ratenbegrenzter API-Key (z. B. Cohere-Trial):** Die Embedding-Phase
  dominiert deutlich. Ein gedrosselter Key hat ein Token-pro-Minute-Limit, das
  bewusste Pausen zwischen den Batches erzwingt – ein Lauf kann also minutenlang
  auf die Embeddings der neu hinzugekommenen Artikel warten, während die volle
  Distanzmatrix über alle nur Sekunden braucht. Hier bestimmt **die Zahl der
  seit dem letzten Lauf neuen Artikel** die Laufzeit, nicht $n$.
- **Production-API-Key (ohne Drosselung) oder lokale Embeddings (fastembed):**
  Die erzwungenen Pausen entfallen, die Embedding-Phase schrumpft auf die reine
  API-Latenz (bzw. lokale Rechenzeit). Sie wird dann bei moderatem $n$
  vergleichbar mit der Matrix – und weil die Matrix quadratisch wächst,
  **übernimmt die $O(n^2)$-Matrix den Engpass, sobald $n$ groß genug ist**; dann
  dominieren `reader.max_items` und `window_hours` die Laufzeit tatsächlich
  wieder.

Die Annahme „die Distanzmatrix ist der teure Teil" stimmt also nur ohne
Rate-Limit und bei großem $n$; mit einem gedrosselten Key dominiert stattdessen
die Embedding-Wartezeit. Für mehrere tausend Artikel berechnet moderne BLAS die
Matrix in Sekunden.

---

## 6. Warum strukturelle Trennung an Grenzen stößt

Ein wiederkehrendes Problem: Zwei **verschiedene** Ereignisse zum selben Thema
(zwei Fußballspiele am selben Tag) landen im selben Cluster, weil ihre
Embedding-Vektoren nah beieinander liegen. Man könnte hoffen, das geometrisch zu
erkennen – tut es aber nicht, und der Grund ist mathematisch sauber benennbar:

Sei $d_{\cos}(\text{gleiches Ereignis, andere Sprache})$ die Distanz zwischen zwei
Sprachfassungen *desselben* Ereignisses, und
$d_{\cos}(\text{verschiedene Ereignisse, gleiche Sprache})$ die zwischen zwei
*verschiedenen* Ereignissen. Empirisch gilt häufig:

$$d_{\cos}(\text{gleiches Ereignis, andere Sprache}) \gtrsim d_{\cos}(\text{verschiedene Ereignisse, gleiche Sprache})$$

```
cosine_dist(gleiches Ereignis, andere Sprache)
     ≳  cosine_dist(verschiedene Ereignisse, gleiche Sprache)
```

Die Sprachgrenze erzeugt also eine **größere** Vektordistanz als der
Ereignisunterschied. Jede Schwelle, die zwei verschiedene Ereignisse trennen
würde, zerreißt damit auch die cross-lingualen Cluster, die man erhalten will.
Geometrie und Eigennamen-Statistik können „ein Ereignis vs. mehrere" deshalb
nicht zuverlässig trennen – die Unterscheidung ist **semantisch**. Sie wird daher
im LLM-Schritt getroffen (siehe `PIPELINE.md`, Abschnitt 7).

---

## 7. Statistik der Blindspot-Erkennung

Ein Cluster ist ein **Blindspot**, wenn er fast nur von einem politischen Lager
berichtet wird. Damit das kein Artefakt einer einzelnen Quelle ist, greifen zwei
einfache Verteilungs-Kennzahlen über die Quellen eines Clusters.

Sei ein Cluster aus $N$ Artikeln, verteilt auf Quellen mit Häufigkeiten
$n_1, n_2, \dots, n_k$ (also $k$ verschiedene Quellen, $\sum_j n_j = N$).

**Quellen-Vielfalt** (`min_distinct_sources`, Default 2):

$$k \ge \texttt{min\_distinct\_sources}$$

```
k ≥ min_distinct_sources        (k = Zahl verschiedener Quellen)
```

Es müssen mindestens so viele *verschiedene* Quellen berichten. Filtert den Fall
„nur eine Quelle (plus evtl. Dublette)".

**Quellen-Dominanz** (`max_source_share`, Default 0,9):

$$\max_j \frac{n_j}{N} < \texttt{max\_source\_share}$$

```
max(n_j / N) < max_source_share
```

Keine einzelne Quelle darf mehr als diesen Anteil der Artikel stellen. Filtert
den Fall „eine Quelle dominiert, der Rest ist Beiwerk".

### Grenze der strukturellen Statistik

Diese Kennzahlen filtern triviale Fälle, **unterscheiden aber nicht** den
bedeutsamen Blindspot vom belanglosen: Ein echter politischer Blindspot und ein
lokaler Verkehrsunfall können dieselbe Quellenverteilung haben (gemessen: beide
~7 verschiedene Quellen, ~14 % Dominanz). Auch hier ist das fehlende Signal
**inhaltlich**, nicht statistisch – weshalb ein LLM-Relevanzurteil ergänzt
(`PIPELINE.md`, Abschnitt 7).

---

## 8. Kostenmodell (Tokens)

Die laufenden Kosten sind im Wesentlichen linear in der Token-Zahl. Pro Lauf:

- **Embeddings:** ungefähr proportional zur Gesamt-Textlänge der *neuen*
  (nicht gecachten) Artikel. Der Cache senkt das bei wiederkehrenden Artikeln
  stark.
- **LLM-Labels:** ein Call je gelabeltem Cluster (Blindspots + Cluster ab Größe
  $m$, gedeckelt bei $n$); der Cache erspart Wiederholungen. Token pro Call ≈
  Prompt-Gerüst + bis zu `max_label_titles` Titel + `max_label_teasers` Teaser.
- **Hotspots:** ein einziger zusätzlicher Call pro Lauf über die Cluster-Labels.

Da Titel deutlich kürzer sind als Teaser, ist „mehr Titel zeigen" billig und
„mehr Teaser zeigen" teuer – daher die getrennten Obergrenzen.

---

## Kurzreferenz der Formeln

| Größe | Formel (LaTeX) | Klartext | Wertebereich |
|---|---|---|---|
| Cosine-Ähnlichkeit | $\dfrac{\mathbf{a}\cdot\mathbf{b}}{\lVert\mathbf{a}\rVert\lVert\mathbf{b}\rVert}$ | `(a·b) / (norm(a)·norm(b))` | $[-1, 1]$ |
| Cosine-Distanz | $1 - \text{sim}$ | `1 - sim` | $[0, 2]$ |
| Jaccard-Ähnlichkeit | $\dfrac{\lvert A\cap B\rvert}{\lvert A\cup B\rvert}$ | `Schnitt(A,B) / Vereinigung(A,B)` | $[0, 1]$ |
| Jaccard-Distanz | $1 - J$ | `1 - J` | $[0, 1]$ |
| kombinierte Distanz | $(1-w)\,d_{\cos} + w\,d_J$ | `(1-w)·cos + w·jaccard` | $[0, 2]$ |
| Quellen-Dominanz | $\max_j n_j / N$ | `max(n_j / N)` | $(0, 1]$ |
| Distanzmatrix-Aufwand | $\binom{n}{2} = n(n-1)/2$ | `n(n-1)/2` | $O(n^2)$ |

