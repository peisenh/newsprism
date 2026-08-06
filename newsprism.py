#!/usr/bin/env python3
#
# newsprism - a self-hosted RSS bias-analysis pipeline
# Copyright (C) 2026 Peter Eisenhauer <github@peter-e.de>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""
newsprism – a small, configurable news-aggregation and media-bias
analysis pipeline.

Flow:
  1. fetch articles from the last N hours from FreshRSS/Miniflux
  2. assign each source a lean based on its category (left/center/right)
  3. cluster articles into stories (embeddings + clustering, or via LLM)
  4. count the lean distribution per cluster and flag blindspots
  5. optional: generate a neutral headline per cluster via the LLM
  6. write the result as JSON and (optionally) as an HTML dashboard

Everything is controlled via config.yaml. See config.example.yaml.
"""

import os
import sys
import json
import time
import signal
import threading
import html
import re
import datetime as dt
from dataclasses import dataclass, field
from typing import Optional

import yaml
import requests

# --------------------------------------------------------------------------- #
#  Jinja2 templating for the HTML output (dashboard, cards, archive).
#  The environment is built lazily on first use so that non-HTML code paths
#  (and imports) don't pay for it. Templates live in templates/ next to this
#  script (copied into the image alongside static/).
# --------------------------------------------------------------------------- #
_JINJA_ENV = None


def _jinja_env():
    """Return the shared Jinja2 environment, building it on first use."""
    global _JINJA_ENV
    if _JINJA_ENV is None:
        import jinja2
        tpl_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "templates")
        env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(tpl_dir),
            autoescape=True,
        )
        # Constants the macros/templates reference by name.
        env.globals.update(
            LEAN_LABEL=LEAN_LABEL, SCORE_LABEL=SCORE_LABEL,
            SCORE_ORDER=SCORE_ORDER,
        )
        _JINJA_ENV = env
    return _JINJA_ENV

# scikit-learn/numpy are only needed for method=embedding.
try:
    import numpy as np
    from sklearn.cluster import AgglomerativeClustering
    _HAVE_SKLEARN = True
except Exception:
    _HAVE_SKLEARN = False


def _resolve_version() -> str:
    """Version identifier for the dashboard. Hierarchy:
    1. NEWSPRISM_VERSION (burned into the image at build time: release tag or Git hash)
    2. git describe (only if .git is present at runtime - usually local only)
    3. "dev" as a fallback."""
    v = (os.environ.get("NEWSPRISM_VERSION") or "").strip()
    if v and v != "dev":
        return v
    try:
        import subprocess
        out = subprocess.run(
            ["git", "describe", "--tags", "--always", "--dirty"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            capture_output=True, text=True, timeout=3,
            check=False,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    return v or "dev"


NEWSPRISM_VERSION = _resolve_version()


# ----------------------------------------------------------------------------- #
#  Datenmodell
# ----------------------------------------------------------------------------- #
@dataclass
class Article:
    title: str
    url: str
    source: str
    category: str
    published: int                 # unix epoch
    lean: str = "other"
    summary: str = ""              # teaser/description (for embedding only)
    bias: Optional[int] = None     # fine score from the bias map: -2 (left) .. +2 (right)
    origin: Optional[str] = None   # 2nd dimension: state-controlled | state-funded
                                   #   | independent-nonwestern (None = westl. Standard)
    alignment: Optional[str] = None  # detail tag, e.g. russia-state, china-state (tooltip only)

    def __post_init__(self):
        # Defensive against feeds with overlong titles/source names (cost/layout/prompt size).
        self.title = (self.title or "")[:300]
        self.source = (self.source or "")[:120]
        self.category = (self.category or "")[:200]


@dataclass
class Cluster:
    label: str
    size: int
    articles: list = field(default_factory=list)
    lean_counts: dict = field(default_factory=dict)
    bias_counts: dict = field(default_factory=dict)   # score level -> distinct sources (bias map)
    origin_counts: dict = field(default_factory=dict) # origin/perspective -> distinct sources
    analyzed: int = 0
    blindspot: Optional[str] = None   # None | "left_only" | "right_only"
    label_ai: bool = False            # True if the label comes from the LLM (not the original title)
    hotspot: Optional[str] = None     # higher-level topic (LLM-grouped), display only


# ----------------------------------------------------------------------------- #
#  Config
# ----------------------------------------------------------------------------- #
def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def env_key(name: Optional[str]) -> str:
    if not name:
        return ""
    return os.environ.get(name, "")


def _cfg_secret(d: dict, base: str) -> str:
    """Reads a secret from a config block. Prefers the env variant
    '<base>_env' (name of an environment variable, value comes from .env),
    otherwise falls back to the plaintext value '<base>'. This lets passwords be
    kept out of config.yaml and placed in .env, without breaking the old
    plaintext format."""
    env_name = d.get(f"{base}_env")
    if env_name:
        val = os.environ.get(env_name, "")
        if val:
            return val
        print(f"[warn] {base}_env='{env_name}' set, but the variable is empty/missing",
              file=sys.stderr)
    return str(d.get(base, "") or "")


def _clean_text(s: str, maxlen: int = 500) -> str:
    """Strip HTML, normalize whitespace, truncate (for the teaser in the embedding)."""
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:maxlen]


def _safe_url(url: str) -> str:
    """Allow only http(s) URLs. Prevents javascript:/data: links from
    manipulated feeds ending up as <a href=...> in the dashboard/Atom feed."""
    u = (url or "").strip()
    if re.match(r"^https?://", u, re.IGNORECASE):
        return u
    return "#"


# ----------------------------------------------------------------------------- #
#  Reader 1: Google Reader API (FreshRSS AND Miniflux)
# ----------------------------------------------------------------------------- #
def fetch_greader(rc: dict) -> list:
    base = rc["base_url"].rstrip("/")
    verify = rc.get("verify_tls", True)
    if not verify:
        import urllib3
        urllib3.disable_warnings()
    sess = requests.Session()

    # ClientLogin -> Auth-Token
    r = sess.post(
        f"{base}/accounts/ClientLogin",
        data={"Email": _cfg_secret(rc, "username"), "Passwd": _cfg_secret(rc, "password")},
        timeout=30, verify=verify,
    )
    r.raise_for_status()
    auth = None
    for line in r.text.splitlines():
        if line.startswith("Auth="):
            auth = line[5:].strip()
    if not auth:
        raise RuntimeError("GReader login failed (no auth token). "
                           "API enabled? API password correct?")
    headers = {"Authorization": f"GoogleLogin auth={auth}"}

    ot = int(time.time()) - rc["window_hours"] * 3600
    max_items = rc["max_items"]
    stream = "user/-/state/com.google/reading-list"
    url_endpoint = f"{base}/reader/api/0/stream/contents/{stream}"

    # Pagination: GReader returns up to n entries per request plus a
    # "continuation" token if there are more. Without following the token, one
    # would silently truncate with many articles (or an internally capped n).
    # We load in batches until no continuation comes anymore or the safety
    # limit max_items is reached.
    BATCH = 1000                       # per request (more robust than one huge n)
    raw_items: list = []
    continuation = None
    pages = 0
    while len(raw_items) < max_items:
        params = {
            "output": "json",
            "n": min(BATCH, max_items - len(raw_items)),
            "ot": ot,                  # only articles newer than the time window
        }
        if continuation:
            params["c"] = continuation
        r = sess.get(url_endpoint, headers=headers, params=params,
                     timeout=60, verify=verify)
        r.raise_for_status()
        data = r.json()
        batch = data.get("items", [])
        raw_items.extend(batch)
        pages += 1
        continuation = data.get("continuation")
        if not continuation or not batch:
            break                      # nothing left -> complete
        if pages > 50:                 # emergency brake against an infinite loop
            print("[warn] Reader-Pagination: >50 Seiten, breche ab", file=sys.stderr)
            break

    truncated = bool(continuation) and len(raw_items) >= max_items
    if truncated:
        print(f"[warn] Reader: max_items ({max_items}) reached, possibly not "
              f"all articles in the window were loaded", file=sys.stderr)
    print(f"[*] Reader: fetched {len(raw_items)} articles in {pages} page(s)"
          + (" (abgeschnitten)" if truncated else ""), file=sys.stderr)

    out = []
    for it in raw_items:
        title = (it.get("title") or "").strip()
        url = ""
        for key in ("canonical", "alternate"):
            arr = it.get(key) or []
            if arr and arr[0].get("href"):
                url = arr[0]["href"]
                break
        source = ((it.get("origin") or {}).get("title") or "").strip()
        category = ""
        for c in it.get("categories", []):
            if "/label/" in c:
                category = c.split("/label/", 1)[1]
                break
        published = int(it.get("published") or it.get("crawlTimeMsec", 0) or 0)
        if published > 10_000_000_000:        # ms -> s
            published //= 1000
        summary = _clean_text(((it.get("summary") or {}).get("content")
                               or (it.get("content") or {}).get("content") or ""))
        if title and url:
            out.append(Article(title, url, source, category, published, summary=summary))
    return out


# ----------------------------------------------------------------------------- #
#  Reader 2: native Miniflux-API
# ----------------------------------------------------------------------------- #
def fetch_miniflux(rc: dict) -> list:
    base = rc["base_url"].rstrip("/")
    verify = rc.get("verify_tls", True)
    if not verify:
        import urllib3
        urllib3.disable_warnings()
    after = int(time.time()) - rc["window_hours"] * 3600
    headers = {"X-Auth-Token": _cfg_secret(rc, "api_token")}
    params = {
        "published_after": after,
        "limit": rc["max_items"],
        "direction": "desc",
        "order": "published_at",
    }
    r = requests.get(f"{base}/v1/entries", headers=headers, params=params,
                     timeout=60, verify=verify)
    r.raise_for_status()
    data = r.json()

    out = []
    for e in data.get("entries", []):
        feed = e.get("feed") or {}
        cat = (feed.get("category") or {}).get("title", "")
        published = e.get("published_at", "")
        try:
            ts = int(dt.datetime.fromisoformat(published.replace("Z", "+00:00")).timestamp())
        except Exception:
            ts = int(time.time())
        title = (e.get("title") or "").strip()
        url = (e.get("url") or "").strip()
        summary = _clean_text(e.get("content") or "")
        if title and url:
            out.append(Article(title, url, feed.get("title", ""), cat, ts, summary=summary))
    return out


def fetch_articles(cfg: dict) -> list:
    rc = cfg["reader"]
    if rc["type"] == "miniflux":
        return fetch_miniflux(rc)
    return fetch_greader(rc)


# ----------------------------------------------------------------------------- #
#  Lean assignment
# ----------------------------------------------------------------------------- #
def _bucket_from_score(score: int) -> str:
    return "left" if score < 0 else "right" if score > 0 else "center"


_COARSE_SCORE = {"left": -1, "center": 0, "right": 1}


def assign_leans(articles: list, cfg: dict) -> None:
    lc = cfg["lean"]
    rules = lc["rules"]
    default = lc.get("default", "other")
    overrides = {k.lower(): v for k, v in (lc.get("source_overrides") or {}).items()}
    # Bias map: first from the external JSON file, then overridden by inline entries.
    # The value per domain is EITHER a number (score only, old format) OR an
    # object {"score":, "kind":, "alignment":} (new, 2nd dimension origin).
    # kind: state-controlled | state-funded | independent-nonwestern
    #       (absent/None = Western default). score may be null (left/right
    #       not applicable, e.g. pure state media).
    def _norm_bias_entry(v):
        if isinstance(v, dict):
            sc = v.get("score")
            return {"score": (int(sc) if sc is not None else None),
                    "kind": v.get("kind"), "alignment": v.get("alignment")}
        return {"score": int(v), "kind": None, "alignment": None}

    bias_map: dict = {}
    bm_path = lc.get("bias_map_path")
    # Keys starting with "_" are doc/comment fields in the JSON
    # (e.g. _README, _scale) and are skipped - JSON allows no
    # real comments.
    def _load_map(d: dict) -> dict:
        return {k.lower(): _norm_bias_entry(v)
                for k, v in d.items() if not k.startswith("_")}

    if bm_path:
        try:
            with open(bm_path, "r", encoding="utf-8") as fh:
                bias_map.update(_load_map(json.load(fh)))
        except Exception as exc:
            print(f"[warn] bias_map_path not loaded ({bm_path}): {exc}", file=sys.stderr)
    bias_map.update(_load_map(lc.get("bias_map") or {}))
    for a in articles:
        src = (a.source or "").lower()
        lean = None
        bias = None

        # 1) bias map (source -> fine score + origin) takes precedence
        for key, entry in bias_map.items():
            if key in src:
                sc = entry["score"]
                if sc is not None:
                    bias = max(-2, min(2, sc))
                    lean = _bucket_from_score(bias)
                a.origin = entry["kind"]
                a.alignment = entry["alignment"]
                if sc is not None or entry["kind"]:
                    break

        # 2) otherwise a coarse source override
        if lean is None:
            for key, val in overrides.items():
                if key in src:
                    lean = val
                    break

        # 3) otherwise category rules
        if lean is None:
            cat = (a.category or "").lower()
            lean = default
            for rule in rules:
                if rule["match"].lower() in cat:
                    lean = rule["lean"]
                    break

        a.lean = lean
        # coarse score if no fine one came from the map (for the bias bar)
        a.bias = bias if bias is not None else _COARSE_SCORE.get(lean)


def filter_articles(articles: list, cfg: dict) -> list:
    """Excludes own feeds/categories (prevents the feedback loop),
    filters title patterns and removes duplicates (same URL or same
    source+title - some feeds deliver the same article multiple times with
    slightly differing URLs/slugs or as a text and a video variant)."""
    rc = cfg["reader"]
    excat = [s.lower() for s in rc.get("exclude_categories", [])]
    exsrc = [s.lower() for s in rc.get("exclude_sources", [])]
    extitle = [s.lower() for s in rc.get("exclude_title_patterns", [])]
    exurl = [s.lower() for s in rc.get("exclude_url_patterns", [])]
    dedup = rc.get("deduplicate", True)
    seen_urls: set = set()
    seen_src_title: set = set()
    out = []
    for a in articles:
        cat = (a.category or "").lower()
        src = (a.source or "").lower()
        title = (a.title or "").lower()
        url = (a.url or "").lower()
        if any(e in cat for e in excat):
            continue
        if any(e in src for e in exsrc):
            continue
        if extitle and any(e in title for e in extitle):
            continue
        if exurl and any(e in url for e in exurl):
            continue
        if dedup:
            url_key = (a.url or "").strip()
            st_key = f"{src}\u0000{title}".strip()
            if url_key and url_key in seen_urls:
                continue
            if title and st_key in seen_src_title:
                continue
            if url_key:
                seen_urls.add(url_key)
            if title:
                seen_src_title.add(st_key)
        out.append(a)
    return out


# ----------------------------------------------------------------------------- #
#  Embeddings
# ----------------------------------------------------------------------------- #
def _prefix_for(model: str, text: str) -> str:
    # e5 models expect a "query:" prefix
    if "e5" in model.lower():
        return f"query: {text}"
    return text


def embed_openai_compatible(texts: list, ec: dict) -> list:
    base = ec["base_url"].rstrip("/")
    key = env_key(ec.get("api_key_env"))
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    vecs = []
    CHUNK = 96
    for i in range(0, len(texts), CHUNK):
        batch = [_prefix_for(ec["model"], t) for t in texts[i:i + CHUNK]]
        r = requests.post(
            f"{base}/embeddings",
            headers=headers,
            json={"model": ec["model"], "input": batch},
            timeout=120,
        )
        r.raise_for_status()
        resp = r.json()
        u = resp.get("usage", {})
        _usage["embed_tokens"] += int(u.get("total_tokens", 0) or u.get("prompt_tokens", 0))
        _usage["embed_calls"] += 1
        for d in resp["data"]:
            vecs.append(d["embedding"])
    return vecs


def embed_fastembed(texts: list, ec: dict) -> list:
    from fastembed import TextEmbedding
    kwargs = {}
    if ec.get("cache_dir"):
        kwargs["cache_dir"] = ec["cache_dir"]
    model = TextEmbedding(model_name=ec["model"], **kwargs)
    inputs = [_prefix_for(ec["model"], t) for t in texts]
    return [v.tolist() for v in model.embed(inputs)]


def embed_cohere(texts: list, ec: dict, on_batch=None) -> list:
    """Cohere Embed (v3/v4 multilingual). Own API format (texts/input_type),
    not OpenAI-compatible. input_type=clustering is optimized for cluster
    similarity. Cross-lingual: the same meaning in DE/EN/FR lands close together
    (solves the language split without local compute load)."""
    base = ec.get("base_url", "https://api.cohere.com/v2").rstrip("/")
    key = env_key(ec.get("api_key_env"))
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    input_type = ec.get("input_type", "clustering")
    out_dim = ec.get("output_dimension")
    # Trial keys are heavily throttled (e.g. 20 req/min on the embed endpoint).
    # request_delay sets a pause between batches; on 429 it retries with
    # exponential backoff (max_retries). on_batch(start_index, vectors) is
    # called after each SUCCESSFUL batch so the caller can persist partial
    # progress - important on a trial key, where a large backlog can hit the
    # per-minute limit and abort mid-way; without this every retry would start
    # from zero and never catch up.
    delay = float(ec.get("request_delay", 0) or 0)
    max_retries = int(ec.get("max_retries", 5))
    vecs = []
    CHUNK = 96   # Cohere limit: max 96 texts per call
    for i in range(0, len(texts), CHUNK):
        batch = texts[i:i + CHUNK]
        body = {
            "model": ec["model"],
            "texts": batch,
            "input_type": input_type,
            "embedding_types": ["float"],
        }
        if out_dim:
            body["output_dimension"] = int(out_dim)
        attempt = 0
        while True:
            r = requests.post(f"{base}/embed", headers=headers, json=body, timeout=120)
            if r.status_code == 429:
                # Read Cohere's message: a 429 can mean two very different things.
                # (a) monthly quota exhausted (trial: 1000 calls/month) -> waiting
                #     NEVER helps, so fail fast with a clear message.
                # (b) a per-minute rate limit -> a short wait can help, so retry
                #     with backoff (floored at 60s so the budget regenerates).
                msg = ""
                try:
                    msg = (r.json() or {}).get("message", "") or ""
                except Exception:
                    msg = (r.text or "")[:300]
                low = msg.lower()
                quota_exhausted = ("month" in low or "1000 api" in low
                                   or "monthly" in low)
                if quota_exhausted:
                    raise RuntimeError(
                        "Cohere quota exhausted (429): " + msg.strip() +
                        " -- waiting will not help; switch to a production key "
                        "(clustering.embedding.request_delay can be lowered then).")
                if attempt < max_retries:
                    # per-minute rate limit: respect Retry-After, else backoff
                    # floored at 60s from the 2nd attempt on.
                    wait = r.headers.get("Retry-After")
                    if wait:
                        wait = float(wait)
                    else:
                        wait = 5.0 * (2 ** attempt)
                        if attempt >= 1:
                            wait = max(wait, 60.0)
                    print(f"[*] Cohere 429 (rate limit) – waiting {wait:.0f}s and "
                          f"retrying (attempt {attempt + 1}/{max_retries}); "
                          f"msg: {msg.strip()[:120]}", file=sys.stderr)
                    time.sleep(wait)
                    attempt += 1
                    continue
                # retries exhausted on a rate limit
                raise RuntimeError(f"Cohere 429 after {max_retries} retries: "
                                   f"{msg.strip()[:200]}")
            r.raise_for_status()
            break
        resp = r.json()
        # token capture (Cohere: billed_units.input_tokens under meta)
        meta = resp.get("meta", {}) or {}
        billed = meta.get("billed_units", {}) or {}
        _usage["embed_tokens"] += int(billed.get("input_tokens", 0) or 0)
        _usage["embed_calls"] += 1
        # v2: embeddings.float ; fallback v1: embeddings (list)
        emb = resp.get("embeddings", {})
        floats = emb.get("float") if isinstance(emb, dict) else emb
        vecs.extend(floats)
        if on_batch is not None:
            on_batch(i, floats)          # let the caller persist this batch
        # optional pause before the next batch (spare the trial rate limit)
        if delay and i + CHUNK < len(texts):
            time.sleep(delay)
    return vecs


def get_embeddings(texts: list, cfg: dict, on_batch=None) -> list:
    ec = cfg["clustering"]["embedding"]
    if ec["provider"] == "fastembed":
        return embed_fastembed(texts, ec)
    if ec["provider"] == "cohere":
        return embed_cohere(texts, ec, on_batch=on_batch)
    return embed_openai_compatible(texts, ec)


# ----------------------------------------------------------------------------- #
#  Clustering
# ----------------------------------------------------------------------------- #
def _embed_text(a) -> str:
    """Text for the embedding: title plus teaser (if present) for more discrimination."""
    return f"{a.title}. {a.summary}" if a.summary else a.title


# Coarse proper-noun detection for German/English text. Strategy:
#  1. Multi-word capitalized phrases, acronyms, mixed case
#     (full personal names, party/organization acronyms) - high confidence, never filtered.
#  2. Single capitalized words, UNLESS they are in the stopword
#     list of generic news nouns ("Regierung", "Bericht", "Kritik").
#     In German normal nouns are capitalized too - without
#     this filter the signal would be flooded with noise.
# The list is a heuristic, not a claim to completeness - extend
# it as needed.
_ENTITY_PHRASE_RE = re.compile(
    r"\b(?:"
    r"[A-ZÄÖÜ][\wäöüß-]*(?:\s+[A-ZÄÖÜ][\wäöüß-]*){1,3}"  # 2-4-Wort-Phrasen
    r"|[A-ZÄÖÜ]{2,}"                                      # acronyms: EU, NATO, NGO
    r"|[A-ZÄÖÜ][a-zäöüß]*[A-ZÄÖÜ]\w*"                     # mixed case: eBay, iPhone
    r")\b"
)
_ENTITY_WORD_RE = re.compile(r"\b[A-ZÄÖÜ][a-zäöüß]{2,}\b")

_ENTITY_STOPWORDS = frozenset(w.lower() for w in [
    # institutions/roles (generic)
    "Regierung", "Bundesregierung", "Opposition", "Minister", "Ministerin",
    "Ministerium", "Präsident", "Präsidentin", "Kanzler", "Kanzlerin",
    "Partei", "Parteien", "Fraktion", "Parteitag", "Koalition",
    # processes/events (generic)
    "Bericht", "Studie", "Untersuchung", "Umfrage", "Analyse", "Gesetz",
    "Gesetze", "Reform", "Reformen", "Debatte", "Diskussion", "Kritik",
    "Vorwürfe", "Vorwurf", "Streit", "Konflikt", "Konflikte", "Krieg",
    "Kriege", "Angriff", "Angriffe", "Anschlag", "Anschläge", "Urteil",
    "Gericht", "Polizei", "Justiz", "Proteste", "Protest", "Demonstration",
    "Demonstrationen", "Streik", "Wahl", "Wahlen", "Abstimmung", "Sitzung",
    "Treffen", "Gespräch", "Gespräche", "Verhandlung", "Verhandlungen",
    "Vertrag", "Verträge", "Abkommen", "Forderung", "Forderungen",
    "Entscheidung", "Entscheidungen", "Plan", "Pläne", "Vorschlag",
    "Vorschläge", "Maßnahme", "Maßnahmen", "Krise", "Krisen", "Blockade",
    "Blockaden", "Militäraktion", "Militär", "Aktion",
    # places/time/quantities (generic)
    "Staat", "Staaten", "Land", "Länder", "Bundesland", "Stadt", "Städte",
    "Dorf", "Gemeinde", "Woche", "Wochen", "Monat", "Monate", "Jahr",
    "Jahre", "Tag", "Tage", "Morgen", "Abend", "Nacht", "Wochenende",
    "Prozent", "Milliarden", "Millionen", "Euro", "Dollar",
    # people/society (generic)
    "Mensch", "Menschen", "Bürger", "Bürgerin", "Bürgerinnen", "Familie",
    "Familien", "Kinder", "Kind", "Frauen", "Frau", "Mann", "Männer",
    "Opfer", "Täter", "Verdächtige", "Verdächtiger", "Politiker",
    "Politikerin",
    # topic fields/misc (generic)
    "Wirtschaft", "Politik", "Gesellschaft", "Welt", "Zukunft", "Frage",
    "Fragen", "Antwort", "Antworten", "Problem", "Probleme", "Lösung",
    "Lösungen", "Ergebnis", "Ergebnisse", "Zahl", "Zahlen", "Unternehmen",
    "Firma", "Firmen", "Konzern", "Konzerne", "Markt", "Märkte", "Preis",
    "Preise", "Kosten", "Steuer", "Steuern", "Budget", "Haushalt",
    "Inflation", "Wachstum", "System", "Systeme", "Daten", "Information",
    "Informationen", "Nachricht", "Nachrichten", "Medien", "Presse",
    "Zeitung", "Zeitungen", "Artikel", "Sendung", "Interview",
    # English (for English-language sources)
    "Government", "Minister", "President", "Report", "Study", "Crisis",
    "War", "Attack", "Court", "Police", "State", "City", "Week", "Month",
    "Year", "Day", "Plan", "Deal", "Talks", "Vote", "Election",
])


def _extract_entities(text: str, stopwords: frozenset) -> set:
    """Returns a set (lowercased) of detected proper nouns/acronyms from a text."""
    text = text or ""
    ents = {m.group().strip().lower() for m in _ENTITY_PHRASE_RE.finditer(text)}
    for m in _ENTITY_WORD_RE.finditer(text):
        w = m.group().lower()
        if w not in stopwords:
            ents.add(w)
    return ents


def _combine_entity_distance(cosine_dist: "np.ndarray", articles: list,
                              weight: float, stopwords: frozenset) -> "np.ndarray":
    """Mixes the cosine distance with a proper-noun Jaccard distance."""
    from scipy import sparse

    ent_sets = [_extract_entities(_embed_text(a), stopwords) for a in articles]
    vocab: dict = {}
    rows, cols = [], []
    for i, ents in enumerate(ent_sets):
        for e in ents:
            j = vocab.setdefault(e, len(vocab))
            rows.append(i)
            cols.append(j)

    n = len(articles)
    if not vocab:
        return cosine_dist

    M = sparse.csr_matrix((np.ones(len(rows), dtype="float32"), (rows, cols)),
                           shape=(n, len(vocab)))
    inter = (M @ M.T).toarray()
    sizes = np.array([len(s) for s in ent_sets], dtype="float32")
    union = sizes[:, None] + sizes[None, :] - inter
    both_empty = union == 0
    union_safe = np.where(both_empty, 1.0, union)
    entity_dist = 1.0 - (inter / union_safe)
    np.fill_diagonal(entity_dist, 0.0)

    combined = (1 - weight) * cosine_dist + weight * entity_dist
    combined = np.where(both_empty, cosine_dist, combined)
    return np.clip(combined, 0.0, None)


def _write_embed_cache(cache: dict, cache_path: str, cfg: dict) -> None:
    """Clean up the cache (cutoff coupled to window_hours) and write it to disk."""
    now = int(time.time())
    window_h = int(cfg.get("reader", {}).get("window_hours", 24))
    cutoff_days = max(2, window_h / 24 + 1)
    cutoff = now - cutoff_days * 86400
    pruned = {k: v for k, v in cache.items() if v.get("ts", now) >= cutoff}
    try:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        tmp = cache_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(pruned, fh)
        os.replace(tmp, cache_path)        # atomic: no half-written cache
    except Exception as exc:
        print(f"[warn] writing the embedding cache failed: {exc}", file=sys.stderr)


def _embed_with_cache(texts: list, cfg: dict) -> list:
    """Fetch embeddings, but reuse already-computed vectors from the cache.
    Key = hash(model + embedding text). On the 6h cadence this saves the ~75% of
    articles that were already embedded in the previous run."""
    ec = cfg["clustering"]["embedding"]
    cache_path = ec.get("cache_path")
    if not cache_path:
        return get_embeddings(texts, cfg)

    import hashlib
    model = ec.get("model", "")
    cache = {}
    try:
        with open(cache_path, "r", encoding="utf-8") as fh:
            cache = json.load(fh)
    except Exception:
        cache = {}
    now = int(time.time())

    keys = [hashlib.sha1((model + "\n" + t).encode("utf-8")).hexdigest()[:20] for t in texts]
    miss_idx = [i for i, k in enumerate(keys) if k not in cache]

    if miss_idx:
        # Compute missing embeddings and write them to the cache. The cache file
        # can be large (tens of MB), so writing it after every small batch is
        # wasteful - rewriting the whole file 3-4x per run cost ~20s on slow
        # hardware. SAVE_EVERY is therefore a crash-safety checkpoint, not a
        # per-batch save: on a normal run (~1-2k new embeddings) nothing is
        # written until the final write below; only a very large run (e.g. a
        # cold cache with many thousands of new texts) triggers intermediate
        # checkpoints so a mid-run abort doesn't lose everything.
        SAVE_EVERY = 3000
        miss_texts = [texts[i] for i in miss_idx]
        try:
            done = 0
            while done < len(miss_idx):
                chunk_idx = miss_idx[done:done + SAVE_EVERY]
                chunk_txt = miss_texts[done:done + SAVE_EVERY]
                # Persist each successful batch into the in-memory cache right
                # away via the callback, so a mid-way abort (e.g. trial 429 on a
                # large backlog) keeps what was already fetched - the next run
                # continues instead of restarting from zero. on_batch's start is
                # the offset WITHIN chunk_txt.
                def _persist(start, batch_vecs, _base=chunk_idx):
                    for off, v in enumerate(batch_vecs):
                        gi = _base[start + off]
                        cache[keys[gi]] = {"v": [round(float(x), 6) for x in v],
                                           "ts": now}
                new_vecs = get_embeddings(chunk_txt, cfg, on_batch=_persist)
                # (batches already persisted by _persist; the return value is a
                # full consistency check / fallback)
                for j, i in enumerate(chunk_idx):
                    if keys[i] not in cache:
                        cache[keys[i]] = {"v": [round(float(x), 6) for x in new_vecs[j]],
                                          "ts": now}
                done += len(chunk_idx)
                # intermediate checkpoint only if there is still more to come
                if done < len(miss_idx):
                    _write_embed_cache(cache, cache_path, cfg)
        except Exception as exc:
            # partial progress (batches persisted by _persist) is saved here, so
            # the next run resumes from where this one stopped. But if nothing
            # new was fetched (e.g. quota exhausted on the very first batch), the
            # cache is unchanged - skip the write, which on slow hardware costs
            # ~30s for an 80 MB file for no benefit.
            saved = sum(1 for i in miss_idx if keys[i] in cache)
            print(f"[warn] embedding aborted ({exc}); {saved} of {len(miss_idx)} "
                  f"recomputed and saved", file=sys.stderr)
            if saved:
                _write_embed_cache(cache, cache_path, cfg)
            raise

    out = []
    for k in keys:
        entry = cache[k]
        entry["ts"] = now
        out.append(entry["v"])

    _write_embed_cache(cache, cache_path, cfg)
    print(f"[*] Embeddings: {len(texts) - len(miss_idx)} from cache, "
          f"{len(miss_idx)} recomputed", file=sys.stderr)
    _usage["embed_total"] += len(texts)
    _usage["embed_cache_hits"] += len(texts) - len(miss_idx)
    return out


def _distance_matrix(articles: list, cfg: dict, vecs=None):
    """Cosine distance matrix (optionally with entity weighting) for a list of
    articles. Uses the embedding cache. Factored out so both the normal
    clustering and the sub-clustering of large clusters can use it.

    If vecs (already-computed, NOT yet normalized embedding vectors) are
    passed, the embedding cache is not touched again - the sub-clustering of
    large clusters thus saves the repeated cache reads/writes (the main cost
    item on slow hardware)."""
    cc = cfg["clustering"]
    if vecs is None:
        vecs = np.array(_embed_with_cache([_embed_text(a) for a in articles], cfg),
                        dtype="float32")
    else:
        vecs = np.asarray(vecs, dtype="float32")
    # normalize → cosine distance = 1 - cosine similarity
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    vecs = vecs / norms

    cosine_dist = np.clip(1.0 - vecs @ vecs.T, 0.0, 2.0).astype("float32")
    np.fill_diagonal(cosine_dist, 0.0)

    entity_weight = float(cc.get("entity_weight", 0.0))
    if entity_weight > 0:
        stopwords = frozenset()
        sw_path = cc.get("entity_stopwords_path")
        if sw_path:
            try:
                with open(sw_path, "r", encoding="utf-8") as fh:
                    words = yaml.safe_load(fh) or []
                stopwords = frozenset(w.lower() for w in words)
            except Exception as exc:
                print(f"[warn] entity_stopwords_path not loaded ({sw_path}): {exc}",
                      file=sys.stderr)
                stopwords = _ENTITY_STOPWORDS   # fall back to the built-in list
        else:
            stopwords = _ENTITY_STOPWORDS       # fall back to the built-in list
        return _combine_entity_distance(cosine_dist, articles, entity_weight, stopwords)
    return cosine_dist


def cluster_by_embeddings(articles: list, cfg: dict, return_vecs: bool = False):
    if not _HAVE_SKLEARN:
        raise RuntimeError("numpy/scikit-learn missing – required for method=embedding.")
    if len(articles) == 1:
        return ([0], np.zeros((1, 1), dtype="float32")) if return_vecs else [0]
    cc = cfg["clustering"]
    # Fetch embeddings once (cache) so the sub-clustering can reuse them
    # instead of accessing the cache again.
    vecs = np.array(_embed_with_cache([_embed_text(a) for a in articles], cfg),
                    dtype="float32")
    dist = _distance_matrix(articles, cfg, vecs=vecs)

    model = AgglomerativeClustering(
        n_clusters=None,
        distance_threshold=cc.get("threshold", 0.55),
        metric="precomputed",
        linkage="average",
    )
    labels = model.fit_predict(dist).tolist()
    return (labels, vecs) if return_vecs else labels


def split_large_clusters(articles: list, labels: list, cfg: dict, vecs=None) -> list:
    """Breaks overly large clusters into more event-specific sub-clusters.

    The normal clustering (threshold ~0.73) groups topically and cross-
    lingually - which occasionally produces giant clusters (one broad topic, 380 articles)
    spanning several separate events. This post-processing step re-subdivides
    only clusters of at least split_above articles with a STRICTER
    sub_threshold. Since the sub-division runs WITHIN an already topically
    closed cluster, cross-lingual references to the same event are preserved -
    only different events are separated.
    Purely computational (existing embeddings), no additional API call.

    If the embedding vectors (vecs, parallel to articles) are passed, the
    sub-clustering uses them directly and does NOT access the embedding cache
    again - on slow hardware this saves a complete cache read/write per large
    cluster.
    """
    cc = cfg["clustering"]
    split_above = int(cc.get("split_above", 0))
    if split_above <= 0:
        return labels                         # feature disabled
    sub_threshold = float(cc.get("sub_threshold", 0.55))

    groups: dict = {}
    for i, lab in enumerate(labels):
        groups.setdefault(lab, []).append(i)

    new_labels = list(labels)
    next_label = (max(labels) + 1) if labels else 0
    for lab, idxs in groups.items():
        if len(idxs) < split_above:
            continue
        sub_articles = [articles[i] for i in idxs]
        sub_vecs = vecs[idxs] if vecs is not None else None
        dist = _distance_matrix(sub_articles, cfg, vecs=sub_vecs)
        sub = AgglomerativeClustering(
            n_clusters=None,
            distance_threshold=sub_threshold,
            metric="precomputed",
            linkage="average",
        ).fit_predict(dist).tolist()
        n_sub = len(set(sub))
        if n_sub <= 1:
            continue                          # does not split -> leave unchanged
        # the first sub-group keeps the old label, further ones get new ones
        sublabel_map = {}
        for local_i, s in zip(idxs, sub):
            if s not in sublabel_map:
                sublabel_map[s] = lab if not sublabel_map else next_label
                if sublabel_map[s] == next_label:
                    next_label += 1
            new_labels[local_i] = sublabel_map[s]
        print(f"[*] cluster ({len(idxs)} articles) split into {n_sub} event clusters"
              f"", file=sys.stderr)
    return new_labels


def diagnose_entity_separation(clusters: list, cfg: dict) -> None:
    """Diagnostic log (only with clustering.entity_diag): shows, per larger cluster,
    how far cosine and proper-noun distance diverge WITHIN the cluster. A
    'merged' cluster (two different events with DISJOINT proper nouns but similar
    wording) shows up as: low mean cosine distance, but markedly HIGHER
    proper-noun distance. That is the signal that more entity_weight would
    separate the two events. If both are low, no weight helps (the proper-noun
    extraction does not separate).
    Purely diagnostic - changes nothing in the clustering."""
    if not cfg.get("clustering", {}).get("entity_diag"):
        return
    if not _HAVE_SKLEARN:
        return
    cc = cfg["clustering"]
    # load stopwords as in the real distance step
    stopwords = _ENTITY_STOPWORDS
    sw_path = cc.get("entity_stopwords_path")
    if sw_path:
        try:
            with open(sw_path, "r", encoding="utf-8") as fh:
                words = yaml.safe_load(fh) or []
            stopwords = frozenset(w.lower() for w in words)
        except Exception:
            pass

    rows = []
    for c in clusters:
        if c.size < 6:
            continue
        arts = c.articles
        ent_sets = [_extract_entities(_embed_text(a), stopwords) for a in arts]
        n = len(arts)
        # mittlere paarweise Eigennamen-Jaccard-Distanz
        tot = 0.0
        cnt = 0
        for i in range(n):
            for j in range(i + 1, n):
                a, b = ent_sets[i], ent_sets[j]
                if not a and not b:
                    continue
                union = len(a | b)
                inter = len(a & b)
                tot += 1.0 - (inter / union if union else 0.0)
                cnt += 1
        ent_d = (tot / cnt) if cnt else 0.0
        # share of articles with any detected proper nouns at all
        with_ents = sum(1 for s in ent_sets if s) / n
        rows.append((ent_d, c.size, with_ents, c.label))

    rows.sort(reverse=True)
    print("[entity-diag] clusters with high internal proper-noun distance "
          "(= possibly merged events):", file=sys.stderr)
    print("[entity-diag]  EntDist | size | %w.names | label", file=sys.stderr)
    for ent_d, size, we, label in rows[:25]:
        print(f"[entity-diag]   {ent_d:.3f}  | {size:4d}  |  {we*100:3.0f}%   | "
              f"{label[:55]}", file=sys.stderr)


def cluster_by_llm(articles: list, cfg: dict) -> list:
    """Lets the LLM group the titles. Returns a list of labels."""
    titles = [a.title for a in articles]
    numbered = "\n".join(f"{i}: {t}" for i, t in enumerate(titles))
    prompt = (
        "Hier sind nummerierte Schlagzeilen. Gruppiere die Nummern, die über "
        "DIESELBE konkrete Nachricht/dasselbe Ereignis berichten. Gib AUSSCHLIESSLICH "
        "JSON zurück: eine Liste von Gruppen, jede Gruppe eine Liste von Nummern. "
        "Schlagzeilen ohne Partner kommen in eine eigene Einzelgruppe.\n\n" + numbered
    )
    raw = llm_call(prompt, cfg, max_tokens=2000)
    raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    groups = json.loads(raw)
    labels = [-1] * len(articles)
    for gid, grp in enumerate(groups):
        for idx in grp:
            if isinstance(idx, int) and 0 <= idx < len(articles):
                labels[idx] = gid
    for i, l in enumerate(labels):           # unassigned -> own cluster
        if l == -1:
            labels[i] = 10_000 + i
    return labels


def build_clusters(articles: list, labels: list, cfg: dict) -> list:
    analyze = set(cfg["lean"]["analyze"])
    minsize = cfg["clustering"]["min_cluster_size"]
    bs = cfg["blindspot"]

    groups: dict = {}
    for art, lab in zip(articles, labels):
        groups.setdefault(lab, []).append(art)

    clusters = []
    for arts in groups.values():
        arts.sort(key=lambda a: a.published, reverse=True)
        # lean distribution over DISTINCT sources (not article count), so a
        # single prolific outlet does not fake a blindspot.
        srcset: dict = {}
        bias_src: dict = {}
        origin_src: dict = {}
        for a in arts:
            if a.lean in analyze:
                srcset.setdefault(a.lean, set()).add(a.source or "?")
                if a.bias is not None:
                    bias_src.setdefault(str(a.bias), set()).add(a.source or "?")
            # origin (2nd dimension) - capture independently of the lean filter,
            # so pure state media (score null, not in 'analyze') count.
            if a.origin:
                origin_src.setdefault(a.origin, set()).add(a.source or "?")
        counts = {lean: len(s) for lean, s in srcset.items()}
        bias_counts = {score: len(s) for score, s in bias_src.items()}
        origin_counts = {k: len(s) for k, s in origin_src.items()}
        analyzed = sum(counts.values())
        n_distinct_sources = len({a.source for a in arts if a.source})
        from collections import Counter
        _src_freq = Counter(a.source for a in arts if a.source)
        top_source_share = (_src_freq.most_common(1)[0][1] / len(arts)) if _src_freq else 0.0

        left = counts.get("left", 0)
        right = counts.get("right", 0)
        min_src = bs.get("min_sources", 3)
        blindspot = None
        if len(arts) >= bs.get("min_articles", 0):
            if right == 0 and left >= min_src:
                blindspot = "left_only"
            elif left == 0 and right >= min_src:
                blindspot = "right_only"

        # Fallback label: prefer a German article, then English, then the newest.
        # Prevents a random ES/FR/IT article from becoming the cluster title.
        _de_cats = {"links", "rechts", "konservativ", "mitte", "öffentlich", "wirtschaft",
                    "lokal", "regional"}
        _en_srcs = {s.lower() for s in cfg["lean"].get("english_sources", [])}
        def _pref(a, _de_cats=_de_cats, _en_srcs=_en_srcs):
            cat = (a.category or "").lower()
            src = (a.source or "").lower()
            if any(k in cat for k in _de_cats):
                return 0   # deutsch zuerst
            if any(k in src for k in _en_srcs):
                return 1   # then English
            return 2       # rest
        fallback = min(arts, key=_pref)

        # Suppress single-source AND dominated clusters: a cluster whose
        # articles (almost) all come from ONE source is not a cross-outlet
        # story cluster, but one source's own production (e.g. a
        # regional paper with many unrelated local items -> format cluster,
        # "Bonbonwolke"). Zwei Bedingungen:
        #  - min_distinct_sources: at least this many distinct sources (default 2)
        #  - max_source_share: no single source may contribute >= this share of the
        #    articles (default 0.9). Catches disguised in-house clusters where a
        #    single foreign article attaches to 30+ items from one source and thus
        #    seemingly satisfies the 2-source threshold.
        # To disable: min_distinct_sources 1 or max_source_share 1.0.
        min_distinct = cfg["clustering"].get("min_distinct_sources", 2)
        max_share = cfg["clustering"].get("max_source_share", 0.9)
        if n_distinct_sources < min_distinct:
            continue
        if top_source_share >= max_share and len(arts) >= minsize:
            continue

        clusters.append(Cluster(
            label=fallback.title,
            size=len(arts),
            articles=arts,
            lean_counts=counts,
            bias_counts=bias_counts,
            origin_counts=origin_counts,
            analyzed=analyzed,
            blindspot=blindspot,
        ))

    # Sorting: blindspots first, then by size
    clusters.sort(key=lambda c: (c.blindspot is None, -c.size))
    # solo clusters to the end
    multi = [c for c in clusters if c.size >= minsize]
    solo = [c for c in clusters if c.size < minsize]
    return multi + solo


# ----------------------------------------------------------------------------- #
#  LLM (Summaries + optionales Clustering)
# ----------------------------------------------------------------------------- #
# ----------------------------------------------------------------------------- #
#  Cost/token tracker (reset per run)
# ----------------------------------------------------------------------------- #
_usage = {"embed_tokens": 0, "llm_in": 0, "llm_out": 0,
          "embed_calls": 0, "llm_calls": 0,
          "embed_cache_hits": 0, "embed_total": 0,
          "summary_cache_hits": 0, "summary_total": 0}


def _reset_usage() -> None:
    for k in _usage:
        _usage[k] = 0


def _usage_cost(cfg: dict) -> dict:
    """Converts the accumulated tokens into euros (prices from cfg['pricing'],
    given per 1M tokens). If a price is missing, 0 is assumed."""
    pr = cfg.get("pricing", {})
    embed_eur = _usage["embed_tokens"] / 1e6 * float(pr.get("embed_per_mtok", 0) or 0)
    in_eur = _usage["llm_in"] / 1e6 * float(pr.get("llm_input_per_mtok", 0) or 0)
    out_eur = _usage["llm_out"] / 1e6 * float(pr.get("llm_output_per_mtok", 0) or 0)
    return {
        "embed_eur": embed_eur,
        "llm_eur": in_eur + out_eur,
        "total_eur": embed_eur + in_eur + out_eur,
    }


def _update_cost_log(total_eur: float, cfg: dict) -> Optional[dict]:
    """Maintains a compact monthly cost aggregate in a JSON file
    (one entry per month, not a per-run log -> no growth problem) and
    returns the three figures for display:
      - last_month_eur:   total of the previous month (fixed)
      - this_month_eur:   total of this month so far
      - this_month_est:   linear projection onto the whole month (from day 2)
    Returns None if no path is configured or costs are 0."""
    path = cfg.get("pricing", {}).get("cost_log_path")
    if not path or total_eur <= 0:
        return None
    import calendar
    tzname = cfg.get("output", {}).get("timezone", "Europe/Berlin")
    now = _now_local(tzname)
    month_key = now.strftime("%Y-%m")

    try:
        with open(path, "r", encoding="utf-8") as fh:
            log = json.load(fh)
    except Exception:
        log = {}

    # aktuellen Monat aktualisieren
    entry = log.get(month_key, {"sum": 0.0, "runs": 0})
    entry["sum"] = round(entry.get("sum", 0.0) + total_eur, 4)
    entry["runs"] = entry.get("runs", 0) + 1
    log[month_key] = entry

    # Keep the log lean: retain only the last 4 months
    for k in sorted(log)[:-4]:
        del log[k]

    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(log, fh)
    except Exception as exc:
        print(f"[warn] writing the cost log failed: {exc}", file=sys.stderr)

    # Kennzahlen berechnen
    last_key = (now.replace(day=1) - dt.timedelta(days=1)).strftime("%Y-%m")
    this_sum = log.get(month_key, {}).get("sum", 0.0)
    last_sum = log.get(last_key, {}).get("sum")

    days_in_month = calendar.monthrange(now.year, now.month)[1]
    # Fraction of the month already elapsed (incl. the current day pro rata)
    elapsed = (now.day - 1) + (now.hour * 3600 + now.minute * 60 + now.second) / 86400.0
    est = None
    if elapsed >= 1.0 and this_sum > 0:   # erst ab Tag 2 sinnvoll
        est = round(this_sum / elapsed * days_in_month, 4)

    return {
        "last_month_eur": round(last_sum, 4) if last_sum is not None else None,
        "this_month_eur": round(this_sum, 4),
        "this_month_est": est,
    }


def _last_run_path(cfg: dict) -> Optional[str]:
    """Path of the small persistent file holding the wall-clock timestamp of the
    last completed run. Lives in the persistent cache volume, derived from the
    summaries cache directory (falls back to the embeddings cache, then None)."""
    for key in ("llm", "clustering"):
        sub = cfg.get(key, {})
        cp = sub.get("cache_path") if key == "llm" else \
            sub.get("embedding", {}).get("cache_path")
        if cp:
            return os.path.join(os.path.dirname(cp), "last_run.txt")
    return None


def _read_last_run(cfg: dict) -> Optional[float]:
    """Unix timestamp of the last completed run, or None if unknown. Survives
    container restarts (file in the cache volume), unlike the monotonic clock."""
    path = _last_run_path(cfg)
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return float(fh.read().strip())
    except Exception:
        return None


def _write_last_run(cfg: dict) -> None:
    """Record the wall-clock time of a just-completed run (best effort)."""
    path = _last_run_path(cfg)
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(str(int(time.time())))
    except Exception as exc:
        print(f"[warn] writing the last-run timestamp failed: {exc}", file=sys.stderr)


def llm_call(prompt: str, cfg: dict, max_tokens: int = 80) -> str:
    lc = cfg["llm"]
    if lc["provider"] == "anthropic":
        key = env_key(lc.get("api_key_env"))
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": lc["model"],
                "max_tokens": max_tokens,
                "temperature": 0,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=120,
        )
        r.raise_for_status()
        data = r.json()
        u = data.get("usage", {})
        _usage["llm_in"] += int(u.get("input_tokens", 0))
        _usage["llm_out"] += int(u.get("output_tokens", 0))
        _usage["llm_calls"] += 1
        parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
        return "".join(parts)

    # openai_compatible (OpenAI, Ollama, Gemini-OpenAI-Endpoint, Groq, OpenRouter, ...)
    base = lc["base_url"].rstrip("/")
    key = env_key(lc.get("api_key_env"))
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    r = requests.post(
        f"{base}/chat/completions",
        headers=headers,
        json={
            "model": lc["model"],
            "temperature": 0,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=120,
    )
    r.raise_for_status()
    data = r.json()
    u = data.get("usage", {})
    _usage["llm_in"] += int(u.get("prompt_tokens", 0))
    _usage["llm_out"] += int(u.get("completion_tokens", 0))
    _usage["llm_calls"] += 1
    return data["choices"][0]["message"]["content"]


def _is_cross_topic_digest(title: str) -> bool:
    """True if a headline looks like a cross-topic daily round-up whose teaser
    bundles several unrelated stories (e.g. 'News des Tages: A, B, C'). Such
    teasers poison the cluster label with foreign topics (the Stade cluster once
    got 'Autobomben sowie Schüsse in Israel' from one such teaser), so we drop
    the teaser (but keep the title, which often names the main story).

    Deliberately NARROW: matches only genuine multi-topic round-ups, NOT
    topic-bound tickers ('+++ Iran-Krieg +++', 'Liveblog Ukrainekrieg', a single
    match's live ticker) whose teasers are on-topic and valuable."""
    t = (title or "").lower()
    patterns = (
        "news des tages", "tagesüberblick", "tagesueberblick",
        "das wichtigste des tages", "das wichtigste am", "was heute wichtig",
        "morning briefing", "morgenbriefing", "abend-briefing", "abendbriefing",
        "die lage am", "nachrichtenüberblick", "nachrichtenueberblick",
        "news of the day", "today's briefing", "morning digest",
    )
    return any(p in t for p in patterns)


def summarize_clusters(clusters: list, cfg: dict) -> None:
    import hashlib
    lc = cfg["llm"]
    if not lc.get("enabled") or lc.get("provider") == "none":
        return
    minsize = cfg["clustering"].get("min_cluster_size", 2)

    # Selection of which clusters get an LLM label:
    #   - all blindspots (most important in content), regardless of size
    #   - plus all clusters of size >= label_min_size (m)
    #   - capped at label_max_total (n) total; blindspots count toward it.
    # With more candidates than n, the SMALLEST non-blindspot clusters drop out.
    # (clusters is already sorted: blindspots first, then by size descending.)
    m = int(lc.get("label_min_size", 5))
    n = int(lc.get("label_max_total", 120))
    todo = []
    seen = set()
    # 1) blindspots always (count toward the limit)
    for c in clusters:
        if c.blindspot and len(todo) < n:
            todo.append(c)
            seen.add(id(c))
    # 2) fill up with the largest clusters of size >= m until n is reached
    for c in clusters:                     # already sorted by size
        if len(todo) >= n:
            break
        if id(c) in seen:
            continue
        if c.size >= m and c.size >= minsize:
            todo.append(c)
            seen.add(id(c))

    # Load cache: key = anchor URL (oldest article) -> label.
    # Same story -> hit -> NO LLM call (saves 6h repetitions).
    cache_path = lc.get("cache_path")
    cache = {}
    if cache_path:
        try:
            with open(cache_path, "r", encoding="utf-8") as fh:
                cache = json.load(fh)
        except Exception:
            cache = {}
    now = int(time.time())
    hits = misses = 0

    for c in todo:
        # Cache identity via the article URLs. A cluster is "the same" as in
        # an earlier run if its articles overlap - so the label is stored
        # under MULTIPLE article URLs as keys, and on lookup the first
        # already-known URL is taken. This survives both cluster growth
        # and a changing "oldest"/"smallest" URL (previously the key
        # jumped around -> many unnecessary LLM calls). No size_bucket anymore:
        # once assigned, a label stays valid even as the cluster grows.
        c_urls = sorted(a.url for a in c.articles if a.url)
        cand_keys = [hashlib.sha1(u.encode("utf-8")).hexdigest()[:16] for u in c_urls]
        cached = None
        if cache_path:
            for k in cand_keys:
                if k in cache:
                    cached = cache[k]
                    break
        if cached is not None:
            if cached.get("label"):
                c.label = cached["label"]
                c.label_ai = True
            # apply the cached relevance verdict: remove the blindspot false positive
            if cached.get("irrelevant") and c.blindspot:
                c.blindspot = None
            cached["ts"] = now            # keep negative hits alive too
            hits += 1
            continue

        # Title + teaser as context for the LLM. For large clusters, show a wider
        # title sample so a possible second event in the cluster becomes
        # visible (otherwise, with e.g. 54 articles, the LLM sees only the 12
        # newest - which for a dominant event are all alike, while a
        # second event stays invisible and never makes it into the label). Teasers
        # only for the first few (token brake); titles are cheap.
        max_titles = int(lc.get("max_label_titles", 50))
        max_teasers = int(lc.get("max_label_teasers", 8))
        title_lines = []
        for i, a in enumerate(c.articles[:max_titles]):
            line = f"- {a.source}: {a.title}"
            # Keep the title, but drop the teaser of cross-topic daily round-ups
            # ("News des Tages: A, B, C ..."): their teasers bundle unrelated
            # stories and would poison the label with foreign topics.
            if a.summary and i < max_teasers and not _is_cross_topic_digest(a.title):
                line += f"\n  {a.summary[:150]}"
            title_lines.append(line)
        titles = "\n".join(title_lines)
        # For blindspot candidates, also obtain a relevance verdict: is this
        # a supraregionally significant political/societal topic, or
        # just local/tabloid/celebrity/sports that happens to cover only one lean?
        # The latter are blindspot false positives (festival, celebrity concert,
        # local accident) that dilute the real blindspots. Structural signals
        # (source count/dominance) do not separate this - only a content judgement.
        is_bs_candidate = bool(c.blindspot)
        if is_bs_candidate:
            relevance_block = (
                "SCHRITT 0 - Bewerte die ÜBERREGIONALE RELEVANZ: Ist das ein "
                f"bedeutsames politisches, wirtschaftspolitisches oder "
                f"gesellschaftliches Thema von überregionalem Interesse "
                f"(RELEVANT)? Oder nur Lokales/Regionales ohne überregionale "
                f"Bedeutung, Boulevard, Promi-/Society-News, Kultur-/Konzert-"
                f"Termine, Film-/Kino-/Box-Office-Themen, Sport-Ergebnisse und "
                f"Sport-Verbandsthemen (Ligen, Olympia/IOC-Vergabe), Service-/"
                f"Ratgeber-Inhalte (Steuertipps, Verbraucher, Gesundheits-/"
                f"Lifestyle-Ratgeber), einzelne Unternehmens-/Aktionärsthemen "
                f"(Hauptversammlung, Quartalszahlen, Personalien einer Firma) "
                f"ohne breitere politische Tragweite, einzelne Lokalunfälle oder "
                f"reine Meinungs-/Kommentarsammlungen ohne konkretes Ereignis "
                f"(IRRELEVANT)? Im Zweifel zählt: betrifft es politische "
                f"Entscheidungen, Regierungen, Konflikte oder die breite "
                f"Gesellschaft (RELEVANT) - oder ist es Unterhaltung, Sport, "
                f"Ratgeber oder eine einzelne Firma (IRRELEVANT)?\n"
            )
            fmt = ("RELEVANT | EINS | <Überschrift>   (Reihenfolge: Relevanz, "
                   "dann Ereignis-Klassifikation, dann Label; statt RELEVANT ggf. "
                   "IRRELEVANT, statt EINS ggf. MEHRERE)")
        else:
            relevance_block = ""
            fmt = "EINS | <Überschrift>   ODER   MEHRERE | <Sammel-Label>"
        prompt = (
            f"Unten stehen mehrere Meldungen, die laut Clustering zusammengehören. "
            f"Erzeuge eine deutsche Überschrift.\n\n"
            f"{relevance_block}"
            f"SCHRITT 1 - Prüfe: Behandeln die Meldungen EIN einzelnes "
            f"Ereignis (dieselbe Tat, dasselbe Spiel, dieselbe Entscheidung), oder "
            f"MEHRERE verschiedene Ereignisse, die nur thematisch ähnlich sind "
            f"(z. B. zwei verschiedene Fußballspiele, mehrere getrennte Unfälle, "
            f"verschiedene Kriminalfälle)?\n"
            f"SCHRITT 2 - Schreibe die Überschrift:\n"
            f"  - Bei EINEM Ereignis: KONKRET und unterscheidbar - nenne die "
            f"konkreten Akteure, Orte, Zahlen oder das Ergebnis (z. B. 'Kolumbien "
            f"schlägt Usbekistan 2:0', nicht 'Spieltag-Auftakt').\n"
            f"  - Bei MEHREREN Ereignissen: ein ehrliches SAMMEL-Label, das die "
            f"Gemeinsamkeit nennt und die wichtigsten Fälle aufzählt (z. B. "
            f"'Zwei Spiele: Team A gegen Team B und Team C gegen Team D' oder 'Mehrere "
            f"Badeunfälle mit Kindern'). Greife NIEMALS nur EINEN Fall heraus und "
            f"mache ihn zum Label des ganzen Clusters - das wäre irreführend, weil "
            f"die anderen Meldungen davon nicht handeln.\n\n"
            f"PFLICHT - das Label muss das HÄUFIGSTE Thema der Meldungen nennen: "
            f"Wenn die Mehrheit der Meldungen von einem Thema handelt, MUSS dieses "
            f"Thema im Label stehen. Ein Thema, von dem nur ein bis zwei Meldungen "
            f"handeln, darf das Label NICHT bestimmen und das Hauptthema nicht "
            f"verdrängen.\n"
            f"STRIKT - keine Erfindungen: Jedes Thema, jeder Ort, jede Zahl und "
            f"jeder Name im Label MUSS wörtlich in den unten stehenden Meldungen "
            f"vorkommen. Ergänze NICHTS aus deinem Vorwissen darüber, was zu einem "
            f"solchen Thema typischerweise gehört - auch dann nicht, wenn es "
            f"naheliegend erscheint. Steht es nicht unten, gehört es nicht ins "
            f"Label. Max. 16 Wörter.\n\n"
            f"Antworte in GENAU einer Zeile im Format:\n"
            f"{fmt}\n"
            f"Keine Erklärung, kein Markdown, keine Anführungszeichen.\n\n"
            f"Meldungen:\n{titles}"
        )
        try:
            raw = llm_call(prompt, cfg, max_tokens=70).strip().strip('"')
            misses += 1
            # Parse the answer format. Fields separated by '|'; the LABEL is always
            # the LAST field. Leading fields are classifications (EINS/MEHRERE
            # and - only for blindspot candidates - RELEVANT/IRRELEVANT). If the
            # LLM doesn't keep the format (no '|'), the whole line is the label.
            label = raw
            irrelevant = False
            if "|" in raw:
                parts = [p.strip().strip('"') for p in raw.split("|")]
                tail = parts[-1]
                heads = [p.upper() for p in parts[:-1]]
                # only treat as a classification prefix if the leading fields
                # really consist of the expected keywords (otherwise
                # a '|' in the label itself could split it wrongly)
                known = {"EINS", "MEHRERE", "EIN", "MEHR", "RELEVANT", "IRRELEVANT"}
                if tail and heads and all(h in known for h in heads):
                    label = tail
                    irrelevant = "IRRELEVANT" in heads
            label = label.strip().strip('"')
            # Blindspot false positive: a blindspot candidate the LLM rates as
            # supraregionally IRRELEVANT (local/tabloid/celebrity) loses
            # its blindspot status - it stays visible as a normal cluster but no
            # longer appears in the blindspot box and does not dilute it.
            if irrelevant and c.blindspot:
                c.blindspot = None
            # Store the label under several article URLs (the first up to 5) so
            # a slightly changed/grown cluster on the next run hits via one
            # of the known URLs. Limited so as not to bloat the cache.
            store_keys = cand_keys[:5] or [hashlib.sha1(c.label.encode("utf-8")).hexdigest()[:16]]
            # Catch refusals/prose: real headlines are short and single-line.
            if label and "\n" not in label and len(label) <= 140:
                c.label = label
                c.label_ai = True
                if cache_path:
                    for k in store_keys:
                        cache[k] = {"label": label, "ts": now, "irrelevant": irrelevant}
            elif cache_path:
                # unusable answer -> cache negatively, otherwise it is re-requested every run
                for k in store_keys:
                    cache[k] = {"label": None, "ts": now}
        except Exception as exc:
            print(f"[warn] summary failed: {exc}", file=sys.stderr)

    # Clean up the cache (drop older than 7 days) and write it back
    if cache_path:
        cutoff = now - 7 * 86400
        cache = {k: v for k, v in cache.items() if v.get("ts", now) >= cutoff}
        try:
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            with open(cache_path, "w", encoding="utf-8") as fh:
                json.dump(cache, fh, ensure_ascii=False)
        except Exception as exc:
            print(f"[warn] writing the summary cache failed: {exc}", file=sys.stderr)
        print(f"[*] Summaries: {hits} from cache, {misses} via LLM", file=sys.stderr)
        _usage["summary_cache_hits"] += hits
        _usage["summary_total"] += hits + misses


def assign_hotspots(clusters: list, cfg: dict) -> None:
    """Groups the (already labeled) clusters via the LLM into a few
    higher-level topics ("hotspots"), e.g. a major sporting event, a country/
    leader, a region in conflict. Purely for the display grouping in the dashboard -
    has NO influence on clustering or blindspot logic.
    A single LLM call per run (over the cluster labels, not the articles)."""
    hc = cfg.get("hotspots", {})
    if not hc.get("enabled"):
        return
    # Consider only clusters of a minimum size (small stories stay
    # without a hotspot and land under "Other" in the dashboard).
    min_size = hc.get("min_cluster_size", 4)
    eligible = [c for c in clusters if c.size >= min_size]
    if len(eligible) < hc.get("min_clusters", 5):
        return  # too little material, not worth it

    target = hc.get("target_count", 8)
    min_per = int(hc.get("min_stories", 2))
    lo = max(3, target - 2)   # lower bound of the range

    # User-defined hotspots: fixed topics the user specifies and that are
    # preferred in EVERY run (in addition to the automatically
    # generated ones). Source: hotspots.user_topics (config) and/or the JSON file
    # hotspots.user_topics_path (runtime-editable, target of a later web UI).
    # An entry is either
    #   - a string  -> name only, assignment by LLM (good for broad topics), or
    #   - an object {name, keywords:[...]} -> deterministic keyword matching
    #     (good for short/unambiguous terms like a party acronym or a city name that
    #     the LLM would otherwise dilute into a top-level topic).
    raw_topics = list(hc.get("user_topics", []) or [])
    ut_path = hc.get("user_topics_path")
    if ut_path:
        try:
            with open(ut_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, list):
                raw_topics += list(data)
        except FileNotFoundError:
            pass
        except Exception as exc:
            print(f"[warn] user_topics_path not loaded ({ut_path}): {exc}",
                  file=sys.stderr)

    # Split into name-only (LLM) and keyword topics.
    llm_topics: list = []          # names only -> LLM assignment
    kw_topics: list = []           # (name, [keywords]) -> deterministic
    seen_names = set()
    for t in raw_topics:
        if isinstance(t, dict):
            name = str(t.get("name", "")).strip()
            kws = [str(k).strip().lower() for k in (t.get("keywords") or []) if str(k).strip()]
        else:
            name = str(t).strip()
            kws = []
        if not name or name in seen_names:
            continue
        seen_names.add(name)
        if kws:
            kw_topics.append((name, kws))
        else:
            llm_topics.append(name)
    # All user topic names (for the prompt hint and protection from mini-dissolution).
    user_topics = [n for n, _ in kw_topics] + llm_topics

    # Limit the input: group only the LARGEST clusters. With very many
    # clusters (large window_hours) the prompt would otherwise become huge and the
    # JSON answer would blow past the output limit ("Unterminated string"). The
    # small clusters need no hotspot assignment (they land under "Other").
    max_in = int(hc.get("max_label_input", 200))
    eligible = sorted(eligible, key=lambda c: c.size, reverse=True)[:max_in]

    # 1) Keyword topics first, deterministic. A hit in the LABEL counts fully
    # (the label is the cluster core distilled by the LLM). A hit ONLY in
    # article titles counts only if the keyword appears in enough articles
    # (kw_min_share, default 0.34) - otherwise a casual side mention
    # (e.g. a passing mention of a party in an unrelated cluster) would assign
    # the cluster wrongly to that party's hotspot. This way short/unambiguous terms hit
    # reliably without falling for scattered mentions. Assigned clusters
    # are NOT sent to the LLM anymore.
    kw_min_share = float(hc.get("kw_min_share", 0.34))
    kw_assigned = set()
    if kw_topics:
        for c in eligible:
            label_l = (c.label or "").lower()
            titles_l = [(a.title or "").lower() for a in c.articles]
            n_art = max(1, len(titles_l))
            for name, kws in kw_topics:
                in_label = any(k in label_l for k in kws)
                n_hits = sum(1 for t in titles_l if any(k in t for k in kws))
                if in_label or (n_hits / n_art) >= kw_min_share:
                    c.hotspot = name
                    kw_assigned.add(id(c))
                    break

    # The LLM only gets the clusters not yet assigned via keyword.
    llm_eligible = [c for c in eligible if id(c) not in kw_assigned]

    # Numbered list of the labels for the prompt
    lines = [f"{i}. {c.label}" for i, c in enumerate(llm_eligible)]
    listing = "\n".join(lines)
    user_topics_block = ""
    if llm_topics:
        ut_list = "; ".join(llm_topics)
        user_topics_block = (
            f"- BEVORZUGTE THEMEN: Ordne Schlagzeilen, die thematisch passen, "
            f"diesen vom Nutzer vorgegebenen Themen zu (verwende die Namen "
            f"WÖRTLICH): {ut_list}. Diese Themen NUR verwenden, wenn mindestens "
            f"{min_per} Schlagzeilen wirklich dazu passen - erzwinge sie nicht. "
            f"Zusätzlich darfst du wie üblich eigene Themen für den Rest bilden.\n"
        )
    prompt = (
        f"Hier ist eine nummerierte Liste aktueller Nachrichten-Schlagzeilen. "
        f"Gruppiere sie in übergeordnete Themen (Hotspots), "
        f"z. B. ein großes Sportereignis, ein Land/Staatschef, eine Konfliktregion, 'Innenpolitik', 'Wirtschaft'.\n\n"
        f"WICHTIGE REGELN:\n"
        f"{user_topics_block}"
        f"- Verwende GENAU {lo} bis {target} Themen, nicht mehr"
        f"{' (vorgegebene Themen zählen mit)' if user_topics else ''}.\n"
        f"- Jedes Thema muss mindestens {min_per} Schlagzeilen umfassen. "
        f"Erfinde KEINE Themen für einzelne Schlagzeilen.\n"
        f"- Lieber wenige breite Themen als viele schmale. Im Zweifel eine "
        f"Schlagzeile einem bestehenden größeren Thema zuordnen.\n"
        f"- Schlagzeilen, die in kein größeres Thema passen, dem Thema "
        f"'Sonstiges' zuordnen (das zählt nicht zur Themenzahl).\n"
        f"- Jede Schlagzeile genau einem Thema zuordnen. Kurze, prägnante "
        f"deutsche Themennamen (1-3 Wörter).\n\n"
        f"Antworte AUSSCHLIESSLICH als JSON-Objekt: Schlüssel = Zeilennummer "
        f"(als String), Wert = Themenname. Keine Erklärung, kein Markdown.\n\n"
        f"Schlagzeilen:\n{listing}"
    )
    try:
        raw = llm_call(prompt, cfg, max_tokens=4000)
        raw = raw.strip()
        # strip possible ```json fences
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-z]*\s*|\s*```$", "", raw)
        try:
            mapping = json.loads(raw)
        except json.JSONDecodeError:
            # answer possibly truncated (output limit): trim back to the last
            # complete "key": "value" pair and close it,
            # instead of losing the whole hotspot step.
            cut = raw.rstrip().rstrip(",")
            m = list(re.finditer(r'"\s*:\s*"[^"]*"', cut))
            if m:
                cut = cut[: m[-1].end()] + "}"
                mapping = json.loads(cut)
                print("[warn] hotspot JSON truncated, partially recovered",
                      file=sys.stderr)
            else:
                raise
    except Exception as exc:
        print(f"[warn] hotspot assignment failed: {exc}", file=sys.stderr)
        return

    assigned = 0
    for i, c in enumerate(llm_eligible):
        name = mapping.get(str(i)) or mapping.get(i)
        if isinstance(name, str) and name.strip() and name.strip().lower() != "sonstiges":
            c.hotspot = name.strip()[:60]
            assigned += 1

    # Dissolve mini-hotspots: the LLM sometimes invents, run-dependently, many
    # topics with only one story (e.g. "Taxes", "Media" with 1 cluster each).
    # A hotspot only stays if it has at least min_stories clusters OR (for
    # important single topics like "Middle East") at least keep_if_articles articles;
    # otherwise its clusters are reset to "no hotspot".
    min_stories = int(hc.get("min_stories", 2))
    keep_if_articles = int(hc.get("keep_if_articles", 15))
    if min_stories > 1 or keep_if_articles > 0:
        from collections import defaultdict
        by_hs = defaultdict(list)
        for c in eligible:
            if c.hotspot:
                by_hs[c.hotspot].append(c)
        for name, cl in by_hs.items():
            # Never dissolve user-specified topics - they should appear,
            # even if only one cluster was assigned to them.
            if name in user_topics:
                continue
            n_stories = len(cl)
            n_articles = sum(c.size for c in cl)
            if n_stories < min_stories and n_articles < keep_if_articles:
                for c in cl:
                    c.hotspot = None

    n_hot = len({c.hotspot for c in eligible if c.hotspot})
    print(f"[*] Hotspots: {n_hot} topics for "
          f"{sum(1 for c in eligible if c.hotspot)} clusters", file=sys.stderr)


# ----------------------------------------------------------------------------- #
#  Ausgabe
# ----------------------------------------------------------------------------- #
_WD_DE = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]


def _fmt_local(iso: str, tzname: str) -> str:
    try:
        ts = dt.datetime.fromisoformat(iso)
    except Exception:
        ts = dt.datetime.now(dt.timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        ts = ts.astimezone(ZoneInfo(tzname))
    except Exception:
        ts = ts.astimezone()        # fallback: system local time
    return f"{_WD_DE[ts.weekday()]}, {ts.strftime('%d.%m.%Y %H:%M')} Uhr"


def _now_local(tzname: str) -> dt.datetime:
    now = dt.datetime.now(dt.timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        return now.astimezone(ZoneInfo(tzname))
    except Exception:
        return now.astimezone()


def _secs_until_local_hour(tzname: str, hour: int) -> int:
    now = _now_local(tzname)
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += dt.timedelta(days=1)
    return max(60, int((target - now).total_seconds()))


def to_payload(clusters: list, cfg: dict) -> dict:
    now = dt.datetime.now(dt.timezone.utc)
    oc = cfg["output"]
    origin_cfg = cfg.get("origin", {}) or {}
    return {
        "generated": now.isoformat(),
        "generated_local": _fmt_local(now.isoformat(),
                                      cfg["output"].get("timezone", "Europe/Berlin")),
        "title": oc.get("title", "NewsPrism"),
        "origin_min_share": float(origin_cfg.get("badge_min_share", 0.25)),
        "origin_min_count": int(origin_cfg.get("badge_min_count", 2)),
        "min_cluster_size": int(cfg["clustering"].get("min_cluster_size", 2)),
        "clusters": [
            {
                "label": c.label,
                "label_ai": c.label_ai,
                "size": c.size,
                "analyzed": c.analyzed,
                "lean_counts": c.lean_counts,
                "bias_counts": c.bias_counts,
                "origin_counts": c.origin_counts,
                "blindspot": c.blindspot,
                "hotspot": c.hotspot,
                "articles": [
                    {"title": a.title, "url": a.url, "source": a.source,
                     "lean": a.lean, "origin": a.origin}
                    for a in c.articles
                ],
            }
            for c in clusters
        ],
    }


def write_json(payload: dict, path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)


# Coarse colours (reference values; the dashboard uses the CSS vars --lean-*
# defined in write_html, which support the EU/US toggle. These hex values mirror
# the EU default and document the canonical mapping.)
LEAN_COLORS = {"left": "#b5341f", "center": "#6b7280", "right": "#2c6fbb"}
LEAN_LABEL = {"left": "links", "center": "mitte", "right": "rechts"}

# Bias map: fine 5-level scale -2 .. +2 (reference; dashboard uses --score-* vars)
SCORE_ORDER = ["-2", "-1", "0", "1", "2"]
SCORE_COLORS = {"-2": "#7f1d1d", "-1": "#dc2626", "0": "#6b7280",
                "1": "#3b82f6", "2": "#1e3a8a"}
SCORE_LABEL = {"-2": "links", "-1": "Mitte-links", "0": "Mitte",
               "1": "Mitte-rechts", "2": "rechts"}

# 2nd dimension: origin/perspective (orthogonal to the left-right axis).
ORIGIN_ORDER = ["state-controlled", "state-funded", "independent-nonwestern"]
ORIGIN_COLORS = {"state-controlled": "#78350f",      # state-controlled (dark brown)
                 "state-funded": "#a16207",          # state-funded journalistic (ochre)
                 "independent-nonwestern": "#6e6a2c"} # independent non-Western/exile (olive green)
ORIGIN_LABEL = {"state-controlled": "staatlich kontrolliert",
                "state-funded": "staatsfinanziert",
                "independent-nonwestern": "unabh. nicht-westlich"}

# Thresholds at which the origin badge is shown (settable from config,
# see write_html). Default: at least 2 non-Western sources AND >= 25% share.
_ORIGIN_MIN_SHARE = 0.25
_ORIGIN_MIN_COUNT = 2


ORIGIN_CLASS = {"state-controlled": "s-sc", "state-funded": "s-sf",
                "independent-nonwestern": "s-in"}

# Favicon (Prisma icon) as an inline data URI - so every generated page
# (dashboard and archive snapshot) carries its own favicon, without an external
# file in the output volume (no path/reset breakage risk). Source: logo/favicon.svg.
FAVICON_TAG = '<link rel="icon" href="data:image/svg+xml,%3Csvg%20width%3D%2264%22%20height%3D%2264%22%20viewBox%3D%220%200%2064%2064%22%20role%3D%22img%22%20xmlns%3D%22http%3A//www.w3.org/2000/svg%22%3E%3Ctitle%3ENewsPrism%3C/title%3E%3Cdesc%3ENewsPrism%20Favicon%3A%20aufgef%C3%A4chertes%20Bias-Spektrum.%3C/desc%3E%3Crect%20x%3D%220%22%20y%3D%220%22%20width%3D%2264%22%20height%3D%2264%22%20rx%3D%2214%22%20fill%3D%22%23f1f3f7%22/%3E%3Ccircle%20cx%3D%2218%22%20cy%3D%2232%22%20r%3D%225%22%20fill%3D%22%231f2a44%22/%3E%3Cline%20x1%3D%2222%22%20y1%3D%2232%22%20x2%3D%2252%22%20y2%3D%2214%22%20stroke%3D%22%231e3a8a%22%20stroke-width%3D%225.5%22%20stroke-linecap%3D%22round%22/%3E%3Cline%20x1%3D%2222%22%20y1%3D%2232%22%20x2%3D%2254%22%20y2%3D%2225%22%20stroke%3D%22%233b82f6%22%20stroke-width%3D%225.5%22%20stroke-linecap%3D%22round%22/%3E%3Cline%20x1%3D%2222%22%20y1%3D%2232%22%20x2%3D%2255%22%20y2%3D%2232%22%20stroke%3D%22%236b7280%22%20stroke-width%3D%225.5%22%20stroke-linecap%3D%22round%22/%3E%3Cline%20x1%3D%2222%22%20y1%3D%2232%22%20x2%3D%2254%22%20y2%3D%2239%22%20stroke%3D%22%23dc2626%22%20stroke-width%3D%225.5%22%20stroke-linecap%3D%22round%22/%3E%3Cline%20x1%3D%2222%22%20y1%3D%2232%22%20x2%3D%2252%22%20y2%3D%2250%22%20stroke%3D%22%237f1d1d%22%20stroke-width%3D%225.5%22%20stroke-linecap%3D%22round%22/%3E%3C/svg%3E">'


def _src_attr(a: dict) -> str:
    """style/class attributes for a source link. Non-Western/state sources
    get a CSS class (color via stylesheet so dark mode can lighten it);
    Western sources get the lean color inline."""
    origin = a.get("origin")
    cls = ORIGIN_CLASS.get(origin) if origin else None
    if cls:
        return f'class="{cls}"'
    lean = a.get("lean")
    if lean in ("left", "center", "right"):
        return f'style="color:var(--lean-{lean})"'
    return 'style="color:#555"'


def _origin_badge_data(origin_counts: dict, n_total_sources: int = 0,
                       min_share: Optional[float] = None,
                       min_count: Optional[int] = None) -> Optional[dict]:
    """Decide whether the non-Western/state origin badge applies and, if so,
    return {total, color, detail} for the template; otherwise None.

    The badge appears only if the non-Western sources shape the cluster
    noticeably: at least min_count distinct non-Western sources AND a share of
    >= min_share of all distinct sources. This drops the many cases where only a
    single state source happens to be present (noise), while dominated clusters
    (e.g. a story reported almost only by state media) stay highlighted."""
    if not origin_counts:
        return None
    if min_share is None:
        min_share = _ORIGIN_MIN_SHARE
    if min_count is None:
        min_count = _ORIGIN_MIN_COUNT
    strongest = next((k for k in ORIGIN_ORDER if origin_counts.get(k)), None)
    if not strongest:
        return None
    total = sum(origin_counts.values())
    if total < min_count:
        return None
    if n_total_sources > 0 and (total / n_total_sources) < min_share:
        return None
    detail = ", ".join(f"{origin_counts[k]} {ORIGIN_LABEL[k]}"
                       for k in ORIGIN_ORDER if origin_counts.get(k))
    return {"total": total, "color": ORIGIN_COLORS[strongest], "detail": detail}


def origin_badges(origin_counts: dict, n_total_sources: int = 0,
                  min_share: Optional[float] = None,
                  min_count: Optional[int] = None) -> str:
    """Second dimension as ONE collective badge in the card header (see
    _origin_badge_data for the logic). Thin wrapper rendering the macro."""
    data = _origin_badge_data(origin_counts, n_total_sources, min_share,
                              min_count)
    macros = _jinja_env().get_template("macros.html.j2").module
    return str(macros.origin_badge(data))


def lean_bar(counts: dict) -> str:
    macros = _jinja_env().get_template("macros.html.j2").module
    return str(macros.lean_bar(counts))


def bias_bar(bias_counts: dict) -> str:
    """Fine 5-level bar from the bias map (distinct sources per score level)."""
    macros = _jinja_env().get_template("macros.html.j2").module
    return str(macros.bias_bar(bias_counts))


def _render_card(c: dict, in_bs_box: bool = False) -> str:
    badge = ""
    if c["blindspot"] == "left_only":
        badge = '<span class="bs bs-l">Blindspot · nur links</span>'
    elif c["blindspot"] == "right_only":
        badge = '<span class="bs bs-r">Blindspot · nur rechts</span>'
    # In the highlighted blindspots box, the badge gets an x to drop this
    # cluster from the box (client-side only, resets on the next generated
    # page). The cluster stays in the flat list below; only the box copy goes.
    if badge and in_bs_box:
        badge = badge.replace(
            "</span>",
            '<button class="bs-remove" type="button" '
            'title="Aus der Blindspot-Box ausblenden (bis zum nächsten Lauf)" '
            'aria-label="Aus Blindspot-Box ausblenden">\u00d7</button></span>',
        )
    ai_badge = ('<span class="ai" title="KI-generierte Überschrift">KI</span>'
                if c.get("label_ai") else "")
    hotspot = c.get("hotspot") or ""
    hs_badge = (f'<span class="hs-tag">{html.escape(hotspot)}'
                f'<button class="hs-remove" type="button" '
                f'title="Aus Hotspot-Gruppierung ausblenden (bis zum nächsten Lauf)" '
                f'aria-label="Aus Hotspot ausblenden">\u00d7</button></span>'
                if hotspot else "")
    # Sources line: each source only ONCE (first article of that source as the
    # link), no limit - shows the full media spectrum instead of the first N
    # articles (which for large clusters are often dominated by a few outlets).
    seen_src = set()
    src_links = []
    for a in c["articles"]:
        name = a["source"] or "?"
        if name in seen_src:
            continue
        seen_src.add(name)
        src_links.append(
            f'<a href="{html.escape(_safe_url(a["url"]))}" '
            f'{_src_attr(a)}>'
            f'{html.escape(name)}</a>'
        )
    srcs = " · ".join(src_links)
    n_src = len(src_links)
    art_items = "".join(
        f'<li><a href="{html.escape(_safe_url(a["url"]))}" '
        f'{_src_attr(a)}>'
        f'{html.escape(a["source"] or "?")}:</a> '
        f'{html.escape(a["title"])}</li>'
        for a in c["articles"]
    )
    art_count = len(c["articles"])
    # Raw values for attributes; Jinja autoescaping handles the escaping in the
    # template (escaping here too would double-encode).
    hs_attr = hotspot if hotspot else "\u00ffrest"

    # Share text (plain text, WhatsApp-compatible: *bold* for the title, raw
    # URLs are made clickable automatically by mail/chat apps). Stored in the
    # data-share attribute; the JS uses navigator.share (mobile) or
    # Clipboard/mailto (Desktop).
    share_lines = [f"*{c['label']}*"]
    lc = c.get("lean_counts") or {}
    lean_parts = []
    for key, lab in (("left", "links"), ("center", "mitte"), ("right", "rechts")):
        if lc.get(key):
            lean_parts.append(f"{lab} {lc[key]}")
    if lean_parts:
        share_lines.append("Lager: " + " · ".join(lean_parts))
    share_lines.append("")
    for a in c["articles"]:
        src = a["source"] or "?"
        url = a["url"] or ""
        if url:
            share_lines.append(f"{src}: {url}")
    share_text = "\n".join(share_lines)
    share_attr = html.escape(share_text, quote=True)
    share_title_attr = html.escape(c["label"], quote=True)
    share_btn = (f'<button class="share" type="button" '
                 f'data-share="{share_attr}" data-title="{share_title_attr}" '
                 f'title="Cluster teilen" aria-label="Cluster teilen">\u2197</button>')

    # Stable per-cluster id so the box copy and the flat-list copy of the same
    # blindspot cluster can be linked client-side (anchor = last article's URL,
    # identical in both copies). Falls back to the label if there is no URL.
    anchor = ""
    if c["articles"]:
        anchor = c["articles"][-1].get("url") or ""
    cid = anchor or c["label"]          # raw; Jinja escapes in the template

    # Search corpus for the keyword filter: label + all article titles + all
    # source names, lowercased, so the client-side search is a simple substring
    # check against one prepared attribute (fast, no live DOM scraping).
    search_parts = [c["label"]]
    for a in c["articles"]:
        if a.get("title"):
            search_parts.append(a["title"])
        if a.get("source"):
            search_parts.append(a["source"])
    search_blob = " ".join(search_parts).lower()   # raw; Jinja escapes it

    tmpl = _jinja_env().get_template("card.html.j2")
    return tmpl.render(
        c=c,
        badge=badge,
        ai_badge=ai_badge,
        hs_badge=hs_badge,
        origin_badge=_origin_badge_data(c.get("origin_counts"), n_src),
        srcs=srcs,
        n_src=n_src,
        art_items=art_items,
        art_count=art_count,
        hs_attr=hs_attr,
        share_btn=share_btn,
        cid=cid,
        search_blob=search_blob,
    )


def _config_warn_html(msg) -> str:
    """Conspicuous banner when the config is broken and the old one is used -
    in the log alone it would be missed."""
    if not msg:
        return ""
    return (f'<div class="cfg-warn">⚠ Konfigurationsfehler: '
            f'{html.escape(str(msg))}</div>')


def _version_str() -> str:
    """Subtle version display for the meta line: version (tag or hash),
    optionally with build date. Empty if nothing meaningful is known."""
    v = NEWSPRISM_VERSION
    if not v or v == "dev":
        return ' · <span title="keine Release-/Git-Info">dev</span>'
    bd = (os.environ.get("NEWSPRISM_BUILD_DATE") or "").strip()
    label = html.escape(v)
    if bd:
        return f' · {label} <span class="ver-date">({html.escape(bd)})</span>'
    return f' · {label}'


def _usage_line(usage: Optional[dict]) -> str:
    """Subtle, collapsible statistics box for the dashboard (values of this run)."""
    if not usage:
        return ""
    total = usage.get("total_eur", 0) or 0
    # summary line (always visible): total cost
    if total > 0:
        head_cost = f'~{total:.4f} €'
    else:
        head_cost = "—"

    rt = usage.get("runtime_s")
    if rt is None:
        runtime = "?"
    elif rt < 60:
        runtime = f"{rt:.1f} s"
    else:
        m_, s_ = divmod(int(round(rt)), 60)
        runtime = f"{m_} min {s_} s"

    def fmt_cache(hits, tot):
        if not tot:
            return "—"
        pct = round(hits / tot * 100)
        return f"{hits}/{tot} ({pct} %)"

    rows = []
    rows.append(("Laufzeit", runtime))
    rows.append(("Artikel", str(usage.get("n_articles", "?"))))
    rows.append(("Cluster", str(usage.get("n_clusters", "?"))))
    rows.append(("Blindspots", str(usage.get("n_blindspots", "?"))))
    rows.append(("Quellen", str(usage.get("n_sources", "?"))))
    rows.append(("Cache Embeddings",
                 fmt_cache(usage.get("embed_cache_hits", 0), usage.get("embed_total", 0))))
    rows.append(("Cache Summaries",
                 fmt_cache(usage.get("summary_cache_hits", 0), usage.get("summary_total", 0))))
    if total > 0:
        rows.append(("Kosten Embeddings", f"~{usage.get('embed_eur',0):.4f} €"))
        rows.append(("Kosten LLM", f"~{usage.get('llm_eur',0):.4f} €"))
        rows.append(("Kosten gesamt", f"~{total:.4f} €"))
    else:
        rows.append(("Kosten", "keine Preise hinterlegt"))
    rows.append(("Token",
                 f"{usage.get('embed_tokens',0)} Embed · "
                 f"{usage.get('llm_in',0)}+{usage.get('llm_out',0)} LLM "
                 f"({usage.get('llm_calls',0)} Calls)"))

    # monthly aggregate (optional, only if cost_log is active)
    mo = usage.get("monthly")
    if mo:
        if mo.get("last_month_eur") is not None:
            rows.append(("Kosten Vormonat", f"~{mo['last_month_eur']:.4f} €"))
        rows.append(("Kosten Monat bisher", f"~{mo.get('this_month_eur',0):.4f} €"))
        if mo.get("this_month_est") is not None:
            rows.append(("Kosten Monat geschätzt", f"~{mo['this_month_est']:.4f} €"))

    body = "".join(
        f'<div class="stat-row"><span class="stat-k">{html.escape(k)}</span>'
        f'<span class="stat-v">{html.escape(v)}</span></div>'
        for k, v in rows
    )
    return (f'<details class="stats"><summary>Statistik · Lauf {runtime} · '
            f'Kosten {head_cost}</summary>{body}</details>')


def write_html(payload: dict, path: str, refresh_enabled: bool = False) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # Publish CSS/JS under content-hashed names first, so the document can
    # reference exactly the versions written for this run (keeps old archive
    # snapshots faithful - see _publish_static_assets).
    assets = _publish_static_assets(os.path.dirname(path))
    css_href = assets["style.css"]
    js_src = assets["app.js"]
    # Take the origin-badge thresholds from the payload (from to_payload/cfg).
    global _ORIGIN_MIN_SHARE, _ORIGIN_MIN_COUNT
    if "origin_min_share" in payload:
        _ORIGIN_MIN_SHARE = payload["origin_min_share"]
    if "origin_min_count" in payload:
        _ORIGIN_MIN_COUNT = payload["origin_min_count"]
    minsize = payload.get("min_cluster_size", 2)
    visible = [c for c in payload["clusters"] if c["size"] >= minsize or c["blindspot"]]
    visible.sort(key=lambda x: x["size"], reverse=True)

    # Zone 1: blindspots box (all blindspot clusters, highlighted at the top).
    blindspots = [c for c in visible if c["blindspot"]]
    bs_section = ""
    if blindspots:
        bs_cards = "".join(_render_card(c, in_bs_box=True) for c in blindspots)
        bs_section = f"""
    <div class="bs-box" id="bs-box">
      <div class="bs-box-title">⚠ Blindspots · einseitig berichtet <span id="bs-count">({len(blindspots)})</span></div>
      {bs_cards}
    </div>"""

    # Zone 2: hotspot chip bar (only if hotspots exist). Filters zone 3.
    chips = ""
    hotspots_present = any(c.get("hotspot") for c in visible)
    if hotspots_present:
        counts: dict = {}
        for c in visible:
            key = c.get("hotspot") or "\u00ffrest"
            counts[key] = counts.get(key, 0) + c["size"]
        def chip_key(item):
            name, n = item
            return (name.startswith("\u00ff"), -n)
        chip_html = ['<button class="chip active" data-filter="*">Alle</button>']
        for name, n in sorted(counts.items(), key=chip_key):
            display = "Weitere" if name.startswith("\u00ff") else name
            chip_html.append(
                f'<button class="chip" data-filter="{html.escape(name, quote=True)}">'
                f'{html.escape(display)} <span class="chip-n">{n}</span></button>'
            )
        chips = f'<div class="chips">{"".join(chip_html)}</div>'

    # Zone 3: flat cluster list (size-sorted), each card with data-hotspot.
    list_cards = "".join(_render_card(c) for c in visible)

    # The client-side interactions (hotspot filter, blindspots box, colour
    # toggle, keyword search, share) all live in static/app.js and self-guard on
    # the DOM elements they need, so nothing has to be prepared here.

    # Refresh button (only if refresh_server is active). Auth is handled by the
    # reverse proxy.
    refresh_btn = ""
    if refresh_enabled:
        refresh_btn = (' · <button id="refresh-btn" class="refresh">Aktualisieren</button>'
                       '<span id="refresh-msg" class="refresh-msg"></span>')

    doc = _jinja_env().get_template("dashboard.html.j2").render(
        title=payload["title"],
        favicon_tag=FAVICON_TAG,
        css_href=css_href,
        js_src=js_src,
        generated_local=payload.get("generated_local", payload["generated"]),
        n_visible=len(visible),
        refresh_btn=refresh_btn,
        version_str=_version_str(),
        config_warn=_config_warn_html(payload.get("config_warning")),
        usage_line=_usage_line(payload.get("usage")),
        bs_section=bs_section,
        chips=chips,
        list_cards=list_cards,
    )
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(doc)


def _publish_static_assets(dest_dir: str) -> dict:
    """Copy the dashboard's static assets into dest_dir under content-hashed
    names (e.g. style.a1b2c3d4.css) and return {logical_name: hashed_name}.

    Content-hashing keeps archive snapshots faithful: each snapshot references
    the asset versions that existed when it was written, so changing the CSS/JS
    later cannot break the styling or interactivity of old snapshots. Unchanged
    content keeps the same hash (no duplicate files); changed content gets a new
    name and the old file simply stays behind for the older snapshots. Sources
    live in a static/ directory alongside this script (copied into the image)."""
    import hashlib
    import shutil
    src_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
    mapping = {}
    for name in ("style.css", "app.js"):
        src = os.path.join(src_dir, name)
        if not os.path.exists(src):
            mapping[name] = name          # fallback: unhashed reference
            continue
        with open(src, "rb") as fh:
            digest = hashlib.sha1(fh.read()).hexdigest()[:8]
        stem, ext = os.path.splitext(name)
        hashed = f"{stem}.{digest}{ext}"
        mapping[name] = hashed
        dst = os.path.join(dest_dir, hashed)
        try:
            if not os.path.exists(dst):      # hashed name => identical if present
                shutil.copy2(src, dst)
        except OSError as exc:
            print(f"[warn] could not copy static asset {name}: {exc}",
                  file=sys.stderr)
            mapping[name] = name
    return mapping


def _lean_summary(counts: dict) -> str:
    return " · ".join(f"{LEAN_LABEL[l]} {counts.get(l, 0)}"
                      for l in ("left", "center", "right"))


def _atom_entry(c, today: str, now_iso: str, guid_mode: str = "change") -> str:
    import hashlib
    anchor = c.articles[-1].url if c.articles else c.label   # oldest article = stable anchor
    base = hashlib.sha1(anchor.encode("utf-8")).hexdigest()[:16]
    if guid_mode == "run":
        # fresh entry on every run (maximal currency, but repetitions)
        eid = f"tag:newsprism:{today}:{base}:{int(time.time())}"
    elif guid_mode == "daily":
        eid = f"tag:newsprism:{today}:{base}"                 # once per story and day
    else:  # "change": new only on a content change (source count/blindspot)
        state = hashlib.sha1(f"{anchor}|{c.analyzed}|{c.blindspot}".encode("utf-8")).hexdigest()[:8]
        eid = f"tag:newsprism:{base}:{state}"

    prefix = ""
    if c.blindspot == "left_only":
        prefix = "[Blindspot · nur links] "
    elif c.blindspot == "right_only":
        prefix = "[Blindspot · nur rechts] "
    etitle = html.escape(prefix + c.label)

    items = "".join(
        f"<li><a href=\"{html.escape(_safe_url(a.url))}\">{html.escape(a.source or '?')}</a> "
        f"– {html.escape(a.title)} <em>({LEAN_LABEL.get(a.lean, a.lean)})</em></li>"
        for a in c.articles
    )
    ai_note = " · Überschrift: KI-generiert" if getattr(c, "label_ai", False) else ""
    body = (f"<p><strong>Lager:</strong> {html.escape(_lean_summary(c.lean_counts))} "
            f"· {c.size} Artikel{ai_note}</p><ul>{items}</ul>")
    content = html.escape(body)              # type=html -> HTML must be entity-encoded

    link = _safe_url(c.articles[0].url) if c.articles else ""
    upd = (dt.datetime.fromtimestamp(c.articles[0].published, dt.timezone.utc).isoformat()
           if c.articles else now_iso)
    return (
        f"  <entry>\n"
        f"    <title>{etitle}</title>\n"
        f"    <id>{html.escape(eid)}</id>\n"
        f"    <updated>{upd}</updated>\n"
        f"    <link href=\"{html.escape(link)}\"/>\n"
        f"    <content type=\"html\">{content}</content>\n"
        f"  </entry>"
    )


def _atom_feed(clusters: list, title: str, feed_id: str, self_url: str,
               guid_mode: str = "change") -> str:
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
    today = dt.date.today().isoformat()
    entries = [_atom_entry(c, today, now_iso, guid_mode) for c in clusters]
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<feed xmlns="http://www.w3.org/2005/Atom">\n'
        f"  <title>{html.escape(title)}</title>\n"
        f"  <id>{html.escape(feed_id)}</id>\n"
        f"  <updated>{now_iso}</updated>\n"
        + (f"  <link rel=\"self\" href=\"{html.escape(self_url)}\"/>\n" if self_url else "")
        + "\n".join(entries)
        + "\n</feed>\n"
    )


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def write_atom(clusters: list, cfg: dict) -> None:
    """Main feed (all relevant clusters) plus optionally a separate blindspot feed."""
    oc = cfg["output"]
    title = oc.get("title", "NewsPrism")
    min_size = oc.get("atom_min_size", 2)
    guid_mode = oc.get("feed_guid", "change")   # change | daily | run

    # Main feed: clusters of minimum size OR blindspots
    path = oc.get("atom_path")
    if path:
        main_feed = [c for c in clusters if c.size >= min_size or c.blindspot]
        _write(path, _atom_feed(main_feed, title, "tag:newsprism:feed",
                                oc.get("feed_url", ""), guid_mode))

    # Separater Blindspot-Feed -> eigene Kategorie in FreshRSS
    bs_path = oc.get("blindspot_feed_path")
    if oc.get("blindspot_feed") and bs_path:
        bs = [c for c in clusters if c.blindspot]
        _write(bs_path, _atom_feed(bs, f"{title} – Blindspots",
                                   "tag:newsprism:blindspots",
                                   oc.get("blindspot_feed_url", ""), guid_mode))


# ----------------------------------------------------------------------------- #
#  One pass
# ----------------------------------------------------------------------------- #
def run_once(cfg: dict) -> None:
    _reset_usage()
    _t_start = time.monotonic()
    print("[*] fetching articles ...", file=sys.stderr)
    articles = fetch_articles(cfg)
    articles = filter_articles(articles, cfg)[: cfg["reader"]["max_items"]]
    print(f"[*] {len(articles)} Artikel", file=sys.stderr)
    if not articles:
        return
    assign_leans(articles, cfg)

    print("[*] clustering ...", file=sys.stderr)
    if cfg["clustering"]["method"] == "llm":
        labels = cluster_by_llm(articles, cfg)
    else:
        labels, vecs = cluster_by_embeddings(articles, cfg, return_vecs=True)
        # Break giant clusters (several events on one topic) into more
        # event-specific sub-clusters - only if clustering.split_above is set.
        # The embedding vectors are reused (no further cache access
        # per large cluster).
        labels = split_large_clusters(articles, labels, cfg, vecs=vecs)

    clusters = build_clusters(articles, labels, cfg)
    print(f"[*] {len(clusters)} clusters, "
          f"{sum(1 for c in clusters if c.blindspot)} blindspots", file=sys.stderr)

    diagnose_entity_separation(clusters, cfg)

    summarize_clusters(clusters, cfg)
    assign_hotspots(clusters, cfg)

    cost = _usage_cost(cfg)
    print(f"[*] Cost: embeddings {_usage['embed_tokens']} tok (~{cost['embed_eur']:.4f} €), "
          f"LLM {_usage['llm_calls']} calls / {_usage['llm_in']}+{_usage['llm_out']} tok "
          f"(~{cost['llm_eur']:.4f} €), total ~{cost['total_eur']:.4f} €", file=sys.stderr)
    monthly = _update_cost_log(cost["total_eur"], cfg)

    payload = to_payload(clusters, cfg)
    if _config_warning:
        payload["config_warning"] = _config_warning
    # The statistics refer to the DISPLAYED clusters (same threshold as
    # write_html) so the numbers match what the user sees.
    # Filtered-out clusters (too small, single-source, dominated) are not
    # counted.
    _minsize = cfg["clustering"].get("min_cluster_size", 2)
    shown = [c for c in clusters if c.size >= _minsize or c.blindspot]
    distinct_sources = len({a.source for c in shown for a in c.articles if a.source})
    payload["usage"] = {
        "embed_tokens": _usage["embed_tokens"],
        "llm_calls": _usage["llm_calls"],
        "llm_in": _usage["llm_in"],
        "llm_out": _usage["llm_out"],
        "embed_eur": round(cost["embed_eur"], 4),
        "llm_eur": round(cost["llm_eur"], 4),
        "total_eur": round(cost["total_eur"], 4),
        "runtime_s": round(time.monotonic() - _t_start, 1),
        "n_articles": sum(c.size for c in shown),
        "n_clusters": len(shown),
        "n_blindspots": sum(1 for c in shown if c.blindspot),
        "n_sources": distinct_sources,
        "embed_cache_hits": _usage["embed_cache_hits"],
        "embed_total": _usage["embed_total"],
        "summary_cache_hits": _usage["summary_cache_hits"],
        "summary_total": _usage["summary_total"],
        "monthly": monthly,
    }
    write_json(payload, cfg["output"]["json_path"])
    if cfg["output"].get("html"):
        html_path = cfg["output"]["html_path"]
        refresh_enabled = bool(cfg.get("refresh_server", {}).get("enabled"))
        write_html(payload, html_path, refresh_enabled=refresh_enabled)
        _archive_html(html_path, payload, cfg)
    if cfg["output"].get("atom"):
        write_atom(clusters, cfg)
    print("[*] done.", file=sys.stderr)


def _archive_html(html_path: str, payload: dict, cfg: dict) -> None:
    """Writes a timestamped copy of the dashboard into an archive
    subfolder and updates an archive index page. No cleanup -
    the archive grows unbounded. Controlled via output.archive (default true)."""
    if not cfg["output"].get("archive", True):
        return
    archive_dir = os.path.join(os.path.dirname(html_path), "archiv")
    os.makedirs(archive_dir, exist_ok=True)

    # File name from local creation time: 2026-06-14_1543.html
    tzname = cfg.get("output", {}).get("timezone", "Europe/Berlin")
    now = _now_local(tzname)
    stamp = now.strftime("%Y-%m-%d_%H%M")
    snap_path = os.path.join(archive_dir, f"{stamp}.html")
    try:
        # archive snapshots without a refresh button (old states should not be
        # "refreshed" - that would trigger the current run).
        with open(html_path, "r", encoding="utf-8") as fh:
            content = fh.read()
        content = re.sub(r' · <button id="refresh-btn".*?</span>', "", content, flags=re.DOTALL)
        content = re.sub(r'<script>\s*\(function\(\)\{\s*var btn = document\.getElementById\(.refresh-btn.\).*?</script>',
                         "", content, flags=re.DOTALL)
        # Snapshots live one level down in archiv/, so the shared assets (which
        # sit next to the main index.html, under content-hashed names) must be
        # referenced via ../ . Rewrite whatever hashed names this run used.
        content = re.sub(r'href="(style\.[0-9a-f]+\.css)"',
                         r'href="../\1"', content)
        content = re.sub(r'src="(app\.[0-9a-f]+\.js)"',
                         r'src="../\1"', content)
        with open(snap_path, "w", encoding="utf-8") as fh:
            fh.write(content)
    except Exception as exc:
        print(f"[warn] archive copy failed: {exc}", file=sys.stderr)
        return

    # Archive index: group snapshots by month -> day (newest first),
    # times as a chip row (uses the full width).
    try:
        snaps = sorted(
            (f for f in os.listdir(archive_dir)
             if re.match(r"\d{4}-\d{2}-\d{2}_\d{4}\.html$", f)),
            reverse=True,
        )
        title = payload.get("title", "Newsprism")   # raw; Jinja escapes it
        MONTHS = ["", "Januar", "Februar", "März", "April", "Mai", "Juni",
                  "Juli", "August", "September", "Oktober", "November", "Dezember"]
        WEEKDAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag",
                    "Freitag", "Samstag", "Sonntag"]

        # Struktur: {(jahr,monat): {tag: [(hh,mm,filename), ...]}}
        from collections import OrderedDict
        tree: "OrderedDict" = OrderedDict()
        for f in snaps:
            mts = re.match(r"(\d{4})-(\d{2})-(\d{2})_(\d{2})(\d{2})", f)
            if not mts:
                continue
            y, mo, d, hh, mm = (int(mts.group(i)) for i in range(1, 6))
            tree.setdefault((y, mo), OrderedDict()).setdefault(d, []).append((hh, mm, f))

        sections = []
        for (y, mo), days in tree.items():
            day_blocks = []
            for d, times in days.items():
                try:
                    wd = WEEKDAYS[dt.date(y, mo, d).weekday()]
                except Exception:
                    wd = ""
                chips = "".join(
                    f'<a href="{html.escape(fn)}">{hh:02d}:{mm:02d}</a>'
                    for hh, mm, fn in times
                )
                day_blocks.append(
                    f'<div class="day"><div class="day-h">{wd}, {d:02d}.{mo:02d}. '
                    f'<span class="day-n">{len(times)}</span></div>'
                    f'<div class="chips">{chips}</div></div>'
                )
            sections.append(
                f'<section><h2>{MONTHS[mo]} {y}</h2>{"".join(day_blocks)}</section>'
            )

        doc = _jinja_env().get_template("archive.html.j2").render(
            title=title,
            favicon_tag=FAVICON_TAG,
            n_snaps=len(snaps),
            sections="".join(sections),
        )
        with open(os.path.join(archive_dir, "index.html"), "w", encoding="utf-8") as fh:
            fh.write(doc)
    except Exception as exc:
        print(f"[warn] archive index failed: {exc}", file=sys.stderr)


# ----------------------------------------------------------------------------- #
#  SIGUSR1 = force an immediate run (without a container restart)
#  z. B.:  docker kill -s SIGUSR1 newsprism
# ----------------------------------------------------------------------------- #
_force_run = threading.Event()
_last_run_monotonic = 0.0   # time of the last run (for the HTTP cooldown)
_config_warning = ""        # set when the config is broken and the old one
                            # is reused -> shown as a banner in the dashboard


def _handle_force_run(signum, frame):
    print("[*] SIGUSR1 received – forcing an immediate run", file=sys.stderr)
    _force_run.set()


def _sleep_interruptible(seconds: float) -> None:
    """Like time.sleep(), but returns immediately as soon as _force_run is set."""
    _force_run.wait(timeout=max(0, seconds))


def _start_refresh_server(cfg: dict) -> None:
    """Optional HTTP listener: POST /refresh triggers a run (sets
    _force_run), with a cooldown. NO authentication of its own - put a
    reverse proxy (Traefik BasicAuth) in front. Disabled by default."""
    rs = cfg.get("refresh_server", {})
    if not rs.get("enabled"):
        return
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    port = int(rs.get("port", 8080))
    cooldown = float(rs.get("min_interval_minutes", 15)) * 60.0
    # Text shown in the "reload in ca. X min" hint after triggering a run.
    # Configurable so the typical run duration can be stated without code
    # changes (e.g. "2-3" or "5"); default "1-2".
    reload_hint = str(rs.get("reload_hint_minutes", "1-2")).strip() or "1-2"

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, msg):
            body = msg.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            global _last_run_monotonic
            if self.path.rstrip("/") != "/refresh":
                self._send(404, "not found")
                return
            # Case 1: a run was requested and is still running.
            if _force_run.is_set():
                self._send(429, "Aktualisierung läuft gerade – die Seite in Kürze neu laden.")
                return
            # Case 2: the last run is done but the cooldown is still active.
            since = time.monotonic() - _last_run_monotonic
            if cooldown > 0 and since < cooldown:
                wait_min = int((cooldown - since) // 60) + 1
                self._send(429, f"Bereits aktualisiert – erneute Aktualisierung erst "
                                f"in ~{wait_min} min möglich.")
                return
            # set the cooldown IMMEDIATELY on trigger, not only after the run ends -
            # otherwise one could trigger multiple times in the window between
            # click and run start (reload the page + press again).
            _last_run_monotonic = time.monotonic()
            _force_run.set()
            self._send(202, f"Aktualisierung angestoßen – die Seite in ca. "
                            f"{reload_hint} min neu laden.")

        def do_GET(self):
            self._send(404, "not found")

        def log_message(self, *args):
            pass   # no request logging to stderr

    def serve():
        try:
            httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
            print(f"[*] Refresh server listening on port {port} "
                  f"(POST /refresh, cooldown {int(cooldown // 60)} min)", file=sys.stderr)
            httpd.serve_forever()
        except Exception as exc:
            print(f"[warn] refresh server not started: {exc}", file=sys.stderr)

    threading.Thread(target=serve, daemon=True).start()


def main() -> None:
    global _last_run_monotonic
    cfg_path = os.environ.get("CONFIG", "/config/config.yaml")
    cfg = load_config(cfg_path)

    if os.environ.get("RUN_ONCE"):
        run_once(cfg)
        return

    signal.signal(signal.SIGUSR1, _handle_force_run)
    _start_refresh_server(cfg)

    # Schedule + refresh server are read from the config ONCE at startup.
    # Changes to schedule.*/refresh_server still require a restart.
    # All other values (reader, clustering, hotspots, filter, output ...) are
    # loaded fresh before EVERY run - so a config change takes effect without
    # a restart from the next run on (also via SIGUSR1/--force-run). On an
    # error in the config (e.g. a YAML typo) the last good config is
    # reused instead of crashing.
    sc = cfg.get("schedule", {})
    interval = sc.get("interval_hours", 6) * 3600
    active_start = sc.get("active_start")     # e.g. 7  (None = around the clock)
    active_end = sc.get("active_end")         # z. B. 23
    tzname = cfg.get("output", {}).get("timezone", "Europe/Berlin")
    has_window = isinstance(active_start, int) and isinstance(active_end, int)
    # run_on_start: whether a freshly started container runs immediately (true)
    # or waits one interval first and lets the scheduler take over (false,
    # default). A manual SIGUSR1/--force-run always triggers a run regardless.
    run_on_start = bool(sc.get("run_on_start", False))
    first_iteration = True

    while True:
        # On the very first iteration, unless run_on_start is set (or a run was
        # explicitly forced), skip straight to sleeping - so deploying does not
        # trigger an immediate run; the next one happens on the normal schedule.
        do_initial_wait = first_iteration and not run_on_start and not _force_run.is_set()
        first_iteration = False
        if do_initial_wait:
            # Wait only the REMAINING time since the last run, not a full
            # interval: if the last run was 5 h ago and the interval is 6 h,
            # wait 1 h. So deploying/restarting doesn't reset the rhythm or
            # create a long gap. If no last run is known (first ever start) or
            # the interval is already overdue, wait the full interval - i.e.
            # still don't run immediately on a fresh deploy.
            last = _read_last_run(cfg)
            if last is not None:
                remaining = interval - (time.time() - last)
                wait = int(min(interval, max(0, remaining)))
            else:
                wait = interval
            print(f"[*] deployed - waiting for the schedule "
                  f"(next run in ~{wait // 3600} h {wait % 3600 // 60} min; "
                  f"SIGUSR1 for an immediate run)", file=sys.stderr)
            if wait > 0:
                _sleep_interruptible(wait)
            continue
        if has_window and not _force_run.is_set():
            h = _now_local(tzname).hour
            if not active_start <= h < active_end:
                secs = _secs_until_local_hour(tzname, active_start)
                print(f"[*] quiet hours – sleeping until {active_start:02d}:00 "
                      f"(~{secs // 3600} h) ... (SIGUSR1 for an immediate run)",
                      file=sys.stderr)
                _sleep_interruptible(secs)
                if not _force_run.is_set():
                    continue
        _force_run.clear()
        # reload the config before the run (no restart needed for reader/clustering/
        # hotspots/filter/output). On error, keep the last good config and
        # set a visible warning for the dashboard.
        global _config_warning
        try:
            new_cfg = load_config(cfg_path)
            if isinstance(new_cfg, dict):
                cfg = new_cfg
                _config_warning = ""
            else:
                _config_warning = ("Die Konfiguration ist ungültig (kein gültiges "
                                   "YAML-Mapping). Es läuft die zuletzt funktionierende "
                                   "Konfiguration.")
                print("[warn] config invalid (not a mapping) – keeping the old config",
                      file=sys.stderr)
        except Exception as exc:
            _config_warning = (f"Die Konfiguration konnte nicht geladen werden "
                               f"({exc}). Es läuft die zuletzt funktionierende "
                               f"Konfiguration.")
            print(f"[warn] config not reloaded ({exc}) – keeping the old config",
                  file=sys.stderr)
        try:
            run_once(cfg)
            _write_last_run(cfg)          # persist wall-clock time of the run
        except Exception as exc:
            print(f"[error] run failed: {exc}", file=sys.stderr)
        _last_run_monotonic = time.monotonic()
        print(f"[*] sleeping {interval // 3600} h ... (SIGUSR1 for an immediate run)",
              file=sys.stderr)
        _sleep_interruptible(interval)


if __name__ == "__main__":
    main()
