# NewsPrism – Mathematical and statistical foundations

*English · [Deutsch](MATH.de.md)*

This file explains the math behind NewsPrism: what an embedding is, how cosine
and Jaccard distance are computed and why, how agglomerative clustering works,
and which statistical thresholds the blindspot detection uses. It complements
`PIPELINE.md` (which describes the *what* and *how* of the processing chain)
with the *why* at the mathematical level.

> **Formula rendering:** formulas are in LaTeX (`$…$` inline, `$$…$$` as a
> block) and render on platforms with math support (Forgejo/Codeberg with
> KaTeX, GitHub with MathJax). Where a formula is complex, a plaintext form is
> also given in a code block, so the file stays readable without rendering
> (terminal, editor).

---

## 1. Embeddings – meaning as geometry

### Idea

An **embedding** maps a text to a point in a high-dimensional vector space. An
embedding model (here Cohere, multilingual) is trained so that **texts with
similar content lie close together** and dissimilar ones far apart. Meaning
thus becomes geometry: "distance in space" becomes "difference in content".

Formally: a text $t$ is mapped to a vector

$$\mathbf{v} = E(t) \in \mathbb{R}^d$$

```
v = E(t),  v is a vector of d real numbers (v ∈ ℝ^d)
```

where $d$ is the model's dimension (for Cohere typically a few hundred to over
a thousand). Each component is a learned latent feature – not interpretable on
its own, but together they encode the meaning.

In NewsPrism the text `"title. teaser"` is embedded per article (teaser only
if present) – this gives more signal than the title alone.

### Why it works

During training the model learns to place semantically related phrasings on
nearby vectors – also **across language boundaries**. A German and an English
article about the same event land close together. This is the basis for
cross-lingual clustering, but it has a downside (see section 6): two language
versions of *one* event can be closer to each other than two *different* events
on the same topic.

---

## 2. Cosine similarity and distance

### Definition

The **cosine similarity** of two vectors measures the **angle** between them,
not their distance. It is the dot product normalized by the lengths:

$$\text{sim}(\mathbf{a}, \mathbf{b}) = \frac{\mathbf{a} \cdot \mathbf{b}}{\lVert \mathbf{a}\rVert \, \lVert \mathbf{b}\rVert} = \frac{\sum_{i=1}^{d} a_i b_i}{\sqrt{\sum_i a_i^2}\;\sqrt{\sum_i b_i^2}}$$

In plain text:

```
sim(a,b) = (a · b) / (|a| · |b|)
         = dot product / (norm(a) · norm(b))
```

The value lies in $[-1, 1]$: $1$ = same direction (maximally similar), $0$ =
orthogonal (independent), $-1$ = opposite.

The **cosine distance** is derived from it:

$$d_{\cos}(\mathbf{a}, \mathbf{b}) = 1 - \text{sim}(\mathbf{a}, \mathbf{b})$$

```
cosine_dist(a,b) = 1 - sim(a,b)
```

For normalized embeddings it lies practically in $[0, 2]$, mostly $[0, 1]$.
Small = similar, large = different.

### Why angle instead of distance?

Cosine ignores the **length** of the vectors and considers only their
**direction**. This is desirable for text embeddings: length often depends on
text length or emphasis, while *direction* carries the meaning. A short and a
long text on the same topic should be similar, even if their vectors differ in
"length".

### Implementation trick: normalized vectors

If the vectors are normalized to length 1 beforehand
($\hat{\mathbf{v}} = \mathbf{v}/\lVert\mathbf{v}\rVert$), the similarity
simplifies to the plain **dot product**:

$$\text{sim}(\hat{\mathbf{a}}, \hat{\mathbf{b}}) = \hat{\mathbf{a}} \cdot \hat{\mathbf{b}}$$

```
sim(a^,b^) = a^ · b^        (a^, b^ = normalized to length 1)
```

NewsPrism computes the entire pairwise distance matrix in one step as a matrix
product:

```
cosine_dist = clip(1 - V · V^T, 0, 2)
```

where $V$ is the matrix of all (normalized) article vectors. The diagonal
(distance of an article to itself) is set to 0.

### Property: not a true metric

The cosine distance does **not** satisfy the triangle inequality and is
therefore strictly speaking not a metric. For clustering this is uncritical
(the algorithm only needs a consistent distance matrix), but it is the reason
one should not naively interpret cosine distances like Euclidean distances.

### Relation to Euclidean distance

For **normalized** vectors there is a direct relationship:

$$\lVert \hat{\mathbf{a}} - \hat{\mathbf{b}} \rVert^2 = 2\,(1 - \hat{\mathbf{a}} \cdot \hat{\mathbf{b}}) = 2\, d_{\cos}(\hat{\mathbf{a}}, \hat{\mathbf{b}})$$

```
|a^ - b^|² = 2·(1 - a^·b^) = 2·cosine_dist(a^,b^)
```

On the unit sphere, cosine distance and (squared) Euclidean distance are thus
the same up to a factor of 2. Cosine is just the more convenient form here,
computable directly from the dot product.

---

## 3. Jaccard distance – similarity of sets

### Definition

The **Jaccard similarity** measures the overlap of two **sets** $A$ and $B$ as
the share of common elements among all elements:

$$J(A, B) = \frac{|A \cap B|}{|A \cup B|}$$

```
J(A,B) = |intersection| / |union|
       = (elements in both) / (elements in at least one)
```

Value in $[0, 1]$: $1$ = identical sets, $0$ = no common elements. The
**Jaccard distance** is:

$$d_J(A, B) = 1 - J(A, B) = 1 - \frac{|A \cap B|}{|A \cup B|}$$

### What for in NewsPrism?

Optionally (`clustering.entity_weight > 0`) a proper-noun distance feeds into
clustering. For this, a **set of proper nouns** is extracted from each article
(capitalized words minus a stopword list) and the Jaccard distance of these
sets is computed. Idea: two articles about the same actors (large name
intersection) should move closer, articles with disjoint names further apart –
a signal that the cosine distance does not provide on its own.

### Why it fails on multilingual data

In practice (measured) the Jaccard distance for this corpus is constantly high
for almost **all** clusters (~0.97). Two reasons:

1. **Over-extraction:** in German every noun is capitalized, not just proper
   nouns. The sets become large and noisy.
2. **Language variants:** the same entity is named differently per language
   ("München"/"Munich", "Köln"/"Cologne"). The sets barely overlap even
   for an identical event.

As a result $d_J$ carries no usable separating signal for "same vs. different
event". Default therefore 0.0. Details see `PIPELINE.md`, appendix.

---

## 4. Combining the distances

If both distances are active, NewsPrism mixes them as a **weighted linear
combination** (convex combination) with weight $w \in [0, 1]$
(`entity_weight`):

$$d = (1 - w)\, d_{\cos} + w\, d_J$$

```
combined = (1 - w) · cosine_dist + w · entity_dist
```

Geometrically this is an interpolation between the two distance spaces: $w = 0$
is pure cosine distance, $w = 1$ pure proper-noun distance, in between a
mixture. If two articles share **no** detected proper nouns, the pure cosine
distance is used for that pair (no artificial pulling-apart when the name
signal is missing).

---

## 5. Agglomerative clustering

### Principle

NewsPrism uses **agglomerative hierarchical clustering** (bottom-up): each
article starts as its own cluster; iteratively the two **nearest** clusters
are merged until a stopping criterion is reached. The result is a hierarchy
(dendrogram) that is "cut" at a distance threshold.

### Distance threshold instead of a fixed cluster count

Crucially: NewsPrism does **not** prescribe a cluster count $k$, but a
**distance threshold** (`distance_threshold = clustering.threshold`, ~0.71).
Two clusters are merged only as long as their distance is below the threshold.
The number of clusters follows from the data on its own.

This fits the problem: the number of news topics per run is unknown and
fluctuates. A method like k-means (which needs a fixed $k$ and assumes
spherical clusters) would be unsuitable here.

### Linkage – how is distance between clusters measured?

When merging, the distance between two *groups* must be defined (linkage
criterion). Common variants:

- **single** – distance of the two nearest points (tends to chain)
- **complete** – distance of the two farthest points (compact clusters)
- **average** – mean pairwise distance
- **ward** – minimizes the variance increase on merging

NewsPrism works on a **precomputed distance matrix**
(`metric="precomputed"`), which rules out the geometry-dependent linkages
(e.g. ward) and fits average/complete.

### Giant-cluster splitting

Very large, topically broad clusters are optionally re-clustered: clusters of
at least `clustering.split_above` articles are subdivided a second time with a
**stricter** `sub_threshold` (~0.66 < 0.71). Since the sub-step works
**within** an already-formed cluster and reuses the vectors, it is cheap.

### Complexity

In terms of pure computation, the most expensive item is the **pairwise
distance matrix**: for $n$ articles that is
$\binom{n}{2} = \tfrac{n(n-1)}{2}$ distances, i.e. $O(n^2)$ in time and memory.
The agglomerative clustering itself lies, depending on the implementation,
between $O(n^2)$ and $O(n^3)$. Both scale with $n$, which is set by
`reader.max_items` and `window_hours`.

In practice, which of the two dominates depends on the embedding setup, because
they scale differently: the matrix grows **quadratically with $n$** (all
articles in the window), while the embedding phase grows only **linearly with
the number of articles that are new since the last run** (the rest come from the
cache).

- **Rate-limited API key (e.g. Cohere trial):** the embedding phase dominates by
  far. A throttled key has a per-minute token limit, which forces deliberate
  pauses between batches, so a run can spend minutes waiting on the embeddings of
  the newly arrived articles while the full distance matrix over all of them
  takes only seconds. Here the runtime is driven by **how many new articles
  arrived since the last run**, not by $n$.
- **Production API key (no throttling) or local embeddings (fastembed):** the
  enforced pauses disappear, so the embedding phase shrinks to the raw API
  latency (or local compute). It then becomes comparable to the matrix at
  moderate $n$, and because the matrix grows quadratically, the **$O(n^2)$
  matrix takes over as the bottleneck once $n$ is large enough** – at which point
  `reader.max_items` and `window_hours` do dominate the runtime again.

So "the distance matrix is the expensive part" holds only without a rate limit
and at large $n$; with a throttled key the embedding wait dominates instead. For
several thousand articles, modern BLAS computes the matrix in seconds.

---

## 6. Why structural separation reaches its limits

A recurring problem: two **different** events on the same topic (two football
matches on the same day) end up in the same cluster because their embedding
vectors lie close together. One might hope to detect this geometrically – but
it doesn't work, and the reason is cleanly stateable mathematically:

Let $d_{\cos}(\text{same event, different language})$ be the distance between
two language versions of *the same* event, and
$d_{\cos}(\text{different events, same language})$ the distance between two
*different* events. Empirically it often holds that:

$$d_{\cos}(\text{same event, different language}) \gtrsim d_{\cos}(\text{different events, same language})$$

```
cosine_dist(same event, different language)
     ≳  cosine_dist(different events, same language)
```

The language boundary thus produces a **larger** vector distance than the
event difference. Any threshold that would separate two different events
therefore also tears apart the cross-lingual clusters one wants to keep.
Geometry and proper-noun statistics therefore cannot reliably separate "one
event vs. several" – the distinction is **semantic**. It is therefore made at
the LLM step (see `PIPELINE.md`, section 7).

---

## 7. Statistics of blindspot detection

A cluster is a **blindspot** if it is reported on almost exclusively by one
political lean. To ensure this is not an artifact of a single source, two
simple distributional measures over a cluster's sources apply.

Let a cluster consist of $N$ articles, distributed over sources with
frequencies $n_1, n_2, \dots, n_k$ (i.e. $k$ different sources,
$\sum_j n_j = N$).

**Source diversity** (`min_distinct_sources`, default 2):

$$k \ge \texttt{min\_distinct\_sources}$$

```
k ≥ min_distinct_sources        (k = number of distinct sources)
```

At least this many *different* sources must report on it. Filters the case
"only one source (plus a possible duplicate)".

**Source dominance** (`max_source_share`, default 0.9):

$$\max_j \frac{n_j}{N} < \texttt{max\_source\_share}$$

```
max(n_j / N) < max_source_share
```

No single source may contribute more than this share of the articles. Filters
the case "one source dominates, the rest is incidental".

### Limit of the structural statistics

These measures filter trivial cases but do **not** distinguish the significant
blindspot from the trivial one: a genuine political blindspot and a local
traffic accident can have the same source distribution (measured: both ~7
different sources, ~14% dominance). Here too the missing signal is in the
**content**, not the statistics – which is why an LLM relevance verdict is
added (`PIPELINE.md`, section 7).

---

## 8. Cost model (tokens)

The running cost is essentially linear in the token count. Per run:

- **Embeddings:** roughly proportional to the total text length of the *new*
  (uncached) articles. The cache reduces this strongly for recurring articles.
- **LLM labels:** one call per labeled cluster (blindspots + clusters of size
  $m$ and up, capped at $n$); the cache avoids repeats. Tokens per call ≈
  prompt scaffold + up to `max_label_titles` titles + `max_label_teasers`
  teasers.
- **Hotspots:** a single additional call per run over the cluster labels.

Since titles are much shorter than teasers, "show more titles" is cheap and
"show more teasers" is expensive – hence the separate caps.

---

## Quick reference of the formulas

| Quantity | Formula (LaTeX) | Plain text | Range |
|---|---|---|---|
| Cosine similarity | $\dfrac{\mathbf{a}\cdot\mathbf{b}}{\lVert\mathbf{a}\rVert\lVert\mathbf{b}\rVert}$ | `(a·b) / (norm(a)·norm(b))` | $[-1, 1]$ |
| Cosine distance | $1 - \text{sim}$ | `1 - sim` | $[0, 2]$ |
| Jaccard similarity | $\dfrac{\lvert A\cap B\rvert}{\lvert A\cup B\rvert}$ | `intersection(A,B) / union(A,B)` | $[0, 1]$ |
| Jaccard distance | $1 - J$ | `1 - J` | $[0, 1]$ |
| combined distance | $(1-w)\,d_{\cos} + w\,d_J$ | `(1-w)·cos + w·jaccard` | $[0, 2]$ |
| Source dominance | $\max_j n_j / N$ | `max(n_j / N)` | $(0, 1]$ |
| Distance-matrix cost | $\binom{n}{2} = n(n-1)/2$ | `n(n-1)/2` | $O(n^2)$ |
