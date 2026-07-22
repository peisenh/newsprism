# NewsPrism – How the pipeline works

*English · [Deutsch](PIPELINE.de.md)*

This file explains how NewsPrism turns a FreshRSS article list into the
finished dashboard with clusters, lean classification, blindspots, LLM titles
and hotspots. It walks the processing chain step by step and names the
adjustment knobs (config keys) at the relevant point.

The whole flow lives in `run_once()` and runs once per pass.

```
FreshRSS ──▶ filter ──▶ lean assignment ──▶ embeddings ──▶ clustering
   (1)         (2)           (3)               (4)            (5)
                                                                │
        dashboard ◀── hotspots ◀── LLM titles ◀── cluster build ◀┘
          (9)           (8)           (7)            (6)
```

---

## 1. Fetch articles (`fetch_articles` → `fetch_greader`)

Articles come in via FreshRSS's **GReader API** (`reader.base_url`). NewsPrism
paginates over the `continuation` marker in batches of 1000 until the time
window `reader.window_hours` is covered or `reader.max_items` is reached.
Credentials are read via `_cfg_secret`, preferably from environment variables
(`username_env`/`password_env`, values in `.env`), with a plaintext fallback.

**Important knob:** `window_hours` determines how much history a run sees.
Smaller = sharper snapshot, larger = more blindspots but also more noise. The
operating point is 36 h (a middle ground between 24 h sharpness and the 48 h
noise explosion).

---

## 2. Filter (`filter_articles`)

Before anything else, unwanted articles are removed. Four filters apply:

- `exclude_sources` – exclude whole sources
- `exclude_categories` – by FreshRSS category
- `exclude_title_patterns` – title regex (e.g. ad/format boilerplate)
- `exclude_url_patterns` – URL path regex (e.g. `bild.de/sport/fussball` to
  hide sports tickers)

After that the list is truncated to `reader.max_items`.

---

## 3. Lean assignment (`assign_leans`) – the bias map

Each article is given a political classification based on its source from the
**bias map** (`bias_map.json`, domain-/source-keyed). The map has two
dimensions:

1. **Left–right** as a number from **−2 to +2**
   (−2 = left, 0 = center, +2 = right).
2. **Origin** via an optional `alignment` field
   (e.g. `russia-state`, `china-state`; absent = Western default). This second
   dimension captures state-/non-Western-affiliated sources and is shown as an
   origin badge in the dashboard.

A map entry can be a plain number (`-1`) or an object
(`{"score": -1, "kind": ..., "alignment": ...}`). Sources without an entry
remain unclassified and don't count toward a cluster's lean statistics.

`lean.analyze` defines which leans feed into the blindspot analysis at all.

> The bias map is the most subjective component of the system in terms of
> content. It is deliberately transparent and user-adjustable – classifying
> specific outlets is a judgement, not an objective measurement.

---

## 4. Embeddings (`_embed_with_cache` → `embed_*`)

For clustering, each article is translated into an **embedding vector** – a
numeric representation of its meaning, so that articles with similar content
lie close together.

- **Embedding text** (`_embed_text`): `"title. teaser"` (teaser only if
  present) – this gives more discriminating power than the title alone.
- **Provider** (`clustering.embedding.provider`): `cohere` (multilingual,
  good for the German-English-French mixed source pool), alternatively
  `openai_compatible` (OpenAI/Ollama/LM Studio) or `fastembed` (local).
- **Cache** (`/cache/embeddings.json`): articles already embedded are not sent
  to the API again. The cache is written incrementally and atomically
  (batches of 480). Its retention window is coupled to `window_hours`
  (roughly `window_h/24 + 1` days, at least 2), so recurring articles stay in
  the cache between runs.

Cross-lingual property: Cohere embeds the same matter in different languages
close together – a German and an English article about the same event land in
the same cluster. This is intentional and the reason purely structural
separation methods (see the "What didn't work" appendix) reach their limits
here.

---

## 5. Clustering (`cluster_by_embeddings`, `_distance_matrix`, `split_large_clusters`)

### 5a. Distance matrix

From the vectors a pairwise **cosine distance** is computed
(`1 − v·vᵀ`, clamped to 0…2, diagonal 0). Small distance = similar content.

**Optional proper-noun distance (Jaccard):** if `clustering.entity_weight > 0`,
the cosine distance is mixed with a proper-noun distance:

```
combined = (1 − weight) · cosine_dist + weight · entity_dist
```

`_extract_entities` pulls capitalized words (minus the stopword list
`entity_stopwords.yaml`) as "proper nouns" from title+teaser; `entity_dist` is
the **Jaccard distance** of these proper-noun sets (1 − |intersection|/|union|).
Idea: articles with the same names move closer, with disjoint names further
apart. If two articles share no detected names at all, the pure cosine
distance stands for that pair (no artificial pulling-apart).

> **Practical note:** on multilingual data `entity_weight` adds little
> discriminating power (measured: nearly all clusters ~0.97 proper-noun
> distance, because the same entity is spelled differently per language –
> "München"/"Munich", "Köln"/"Cologne"). It does no harm but is not an
> effective lever. Default 0.0.

### 5b. Main clustering

**Agglomerative clustering** (`AgglomerativeClustering`, `metric="precomputed"`,
`distance_threshold = clustering.threshold`, ~0.71). It groups all articles
below the distance threshold into clusters – topically and cross-lingually. No
fixed `n_clusters`; the count follows from the threshold.

### 5c. Splitting giant clusters (`split_large_clusters`)

Large, topically broad clusters (e.g. "everything about the World Cup") are
optionally broken into more event-specific sub-clusters: clusters of at least
`clustering.split_above` articles are re-clustered with a **stricter**
`clustering.sub_threshold` (~0.66). The embedding vectors are passed through
(`vecs=`) so the split needs no further cache/API access.

Residual error: two **semantically very similar** events (two train accidents
on the same day, two football matches) cannot be reliably separated this way –
their cosine distance is smaller than that between two language versions of
the same event. This is caught at the labeling stage (section 7).

---

## 6. Cluster build & blindspots (`build_clusters`)

From the label groups, `Cluster` objects are built: articles sorted by time
(newest first), lean/bias/origin counts aggregated, a provisional label (title
of the representative article) set.

**Blindspot detection:** a cluster is considered a blindspot if it is reported
on practically only by **one** lean:

- `left_only` – left-leaning sources only
- `right_only` – right-leaning sources only

Two plausibility hurdles prevent trivial blindspots:

- `clustering.min_distinct_sources` (default 2) – at least this many
  **different** sources must report on it.
- `clustering.max_source_share` (default 0.9) – no **single** source may
  contribute more than this share of the articles.

Disable: `min_distinct_sources: 1` or `max_source_share: 1.0`.

> These structural hurdles filter obvious cases (a single source, a
> source-plus-sister-outlet). But they do **not** separate significant from
> trivial blindspots – a genuine political blindspot and a local accident can
> have the same source structure. That distinction only happens at the LLM
> stage (section 7).

---

## 7. LLM titles (`summarize_clusters`)

Clusters receive a concise German headline from the LLM (Haiku). Which
clusters get labeled is governed by a two-part rule:

- **all blindspots** (most important in terms of content), regardless of size,
- **plus** all clusters of size at least `llm.label_min_size` (default 5),
- **capped** at `llm.label_max_total` total (default 120; blindspots count
  toward it, and on overflow the smallest non-blindspot clusters drop out).

### What the LLM sees per cluster

Up to `llm.max_label_titles` article **titles** (default 30) and, for the
first `llm.max_label_teasers` of them (default 8), the teaser text as well.
Reason: for a large cluster with two events, too small a window would let the
LLM see only the dominant event (the newest titles) and miss the second one.
Titles are cheap (show more of them), teasers expensive (limited).

### The prompt – two or three stages

The label call is **one** LLM call, but it has the LLM decide several things
in sequence and answer in a fixed format (`… | … | <label>`, label always the
last field):

- **STEP 0 – relevance (blindspot candidates only):** is this a supraregionally
  significant political/societal topic (`RELEVANT`) or just
  local/tabloid/celebrity/opinion (`IRRELEVANT`)? An `IRRELEVANT` cluster loses
  its blindspot status – it stays visible as a normal cluster but no longer
  dilutes the blindspot box. This catches the false-positive class that is not
  structurally separable (see section 6).
- **STEP 1 – one or several events:** `EINS` (one concrete event) or `MEHRERE`
  (different, only topically similar events).
- **STEP 2 – label:** for `EINS` a concrete, distinguishable label; for
  `MEHRERE` an honest collective label that names the commonality and lists the
  main cases (instead of falsely making one single case the label of the whole
  cluster).

The classification prefixes are stripped off when parsing; only the label
lands in the dashboard and cache. The explicit up-front classification (rather
than a mere admonition in the prompt) is what reliably leads diffuse
collective clusters to honest collective labels.

### Cache

Labels are cached under up to 5 article URLs of the cluster
(`/cache/summaries.json`), including the relevance verdict (`irrelevant` flag).
So a slightly changed/grown cluster gets a hit on the next run, and the
relevance verdict applies on cache hits too – without another LLM call.

---

## 8. Hotspots (`assign_hotspots`)

Optional second hierarchy level (`hotspots.enabled`): **one** additional LLM
call groups the cluster labels into a few top-level topics ("World Cup 2026",
a region in conflict, a country/leader). Purely for display – with no influence on
clustering or blindspots.

Mini-hotspots (topics with only one story) are dissolved: a hotspot only
remains if it spans at least `hotspots.min_stories` clusters (or, for
important single topics, enough articles). Optionally, custom topics can be
defined in `hotspots.user_topics` – as a plain string (LLM assignment) or as
`{name, keywords}` (deterministic assignment via keywords, counting only on a
hit in the label or a sufficient title share).

---

## 9. Output (`to_payload`, `write_html`)

From the finished clusters a payload is built and from it written:

- the **HTML dashboard** (cluster list, blindspot box, hotspot grouping,
  lean/origin badges, share button, version display, run cost),
- **Atom feeds** for subscribers.

An optional archive (`output.archive`) stores a snapshot per run.

---

## Appendix: What didn't work (and why it stays that way)

Three structural approaches to automatically detect "diffuse" or "merged"
clusters were tried and **rejected** because they fail on the multilingual
data:

1. **Coherence geometry** (mean centroid distance): a diffuse multi-event
   cluster and a coherent cross-lingual cluster (one event in three languages)
   have almost the same spread. No separating threshold.
2. **Proper-noun distance as a separating signal**: on German/multilingual
   text it is nearly constantly high (~0.97), because every capitalized form
   counts as its own "name" and language variants are not merged.
3. **Prompt admonitions alone** ("if several events, collective label"): too
   weak; the LLM kept singling out individual cases.

The lesson: the distinction "one event vs. several" and "significant vs.
trivial" is **semantic**, not structural. That's why the solution lives in the
LLM label step (explicit up-front classification, section 7) and not in the
clustering geometry. Accepted residual error: two semantically near-identical
events on the same day can stay merged – the LLM then names both in the
collective label rather than hiding one of them.

The diagnostic tool `clustering.entity_diag` (off by default) stays in the
code to measure the proper-noun distance on real data again when needed.
