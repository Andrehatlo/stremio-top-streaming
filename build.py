#!/usr/bin/env python3
"""Builds the static Stremio catalog addon from TMDB.

Edit the CONFIG section below to change services, regions, filters, or row
sizes. Run locally with `python3 build.py` (reads .env) or in CI where
TMDB_TOKEN / RPDB_KEY are already in the environment as Actions secrets.
"""
import datetime
import json
import os
import random
import sys
import time

import requests

# ---------------------------------------------------------------------------
# CONFIG - edit this section to change what shows up on the TV.
# ---------------------------------------------------------------------------

# Services to count for the rows. Matched by name against TMDB's provider
# list at runtime (no hardcoded provider IDs) - edit ALIASES below if TMDB
# renames a service or you want to add/drop one.
SERVICE_ALIASES = {
    "Netflix": ["netflix"],
    "Max": ["max", "hbo max"],
    "Disney+": ["disney plus", "disney+"],
    "Hulu": ["hulu"],
    "Apple TV+": ["apple tv"],  # TMDB calls this provider plain "Apple TV", not "Apple TV+"
    "Prime Video": ["amazon prime video", "prime video"],
    "Paramount+": ["paramount plus", "paramount+"],
    "Peacock": ["peacock"],
    "Discovery+": ["discovery plus", "discovery+"],
    "SkyShowtime": ["skyshowtime"],
    "Viaplay": ["viaplay"],
}

# Regions to check availability in. First region in the list is also used
# for TMDB's single-region discover() calls, merged across all regions.
REGIONS = ["NO", "US"]

# TMDB genre IDs to exclude from every row. Left empty on request - the
# user chose to include everything (reality/talk/news/kids were NOT
# excluded). Reference IDs if you want to turn this back on:
# Reality=10764, Talk=10767, News=10763, Kids=10762 (tv); Documentary
# genre IDs differ between tv and movie - check /3/genre/{tv|movie}/list.
EXCLUDED_GENRE_IDS = []

MIN_VOTE_AVERAGE = 7.0
MIN_VOTE_COUNT = 200
TRENDING_VOTE_COUNT_EXEMPT_RANK = 20  # top N trending titles skip the vote-count floor

NEW_WINDOW_DAYS = 30
TOPRATED_WINDOW_YEARS = 3
# Movies rack up far more TMDB votes than series for equivalent popularity,
# so the same floor leaves the series row almost empty - verified empirically
# (1500 -> 8 qualifying series vs. 20+ at 1000) - use a lower floor for series.
TOPRATED_MIN_VOTE_COUNT = {"movie": 1500, "tv": 1000}

ROW_CAP = 40
MIN_ROW_ITEMS = 15  # below this, keep yesterday's file instead of overwriting
RPDB_SPOTCHECK_COUNT = 5
TRENDING_PAGES = 10
DISCOVER_PAGES = 4

# ---------------------------------------------------------------------------

TMDB_BASE = "https://api.themoviedb.org/3"
RPDB_BASE = "https://api.ratingposterdb.com"
TMDB_IMG_BASE = "https://image.tmdb.org/t/p/w500"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(SCRIPT_DIR, "docs", "catalog")

ROW_SPECS = [
    # (dir_type, tmdb_media_type, row_id, kind)
    ("series", "tv", "top-streaming", "trending"),
    ("movie", "movie", "top-streaming", "trending"),
    ("series", "tv", "new-streaming", "new"),
    ("movie", "movie", "new-streaming", "new"),
    ("series", "tv", "toprated-streaming", "toprated"),
    ("movie", "movie", "toprated-streaming", "toprated"),
]


def load_env():
    """Reads TMDB_TOKEN / RPDB_KEY from the environment, falling back to a
    local .env file for manual runs (CI sets them as real env vars)."""
    env_path = os.path.join(SCRIPT_DIR, ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())

    tmdb_token = os.environ.get("TMDB_TOKEN")
    rpdb_key = os.environ.get("RPDB_KEY")
    if not tmdb_token:
        sys.exit("TMDB_TOKEN not set (checked environment and .env)")
    if not rpdb_key:
        sys.exit("RPDB_KEY not set (checked environment and .env)")
    return tmdb_token, rpdb_key


class RateLimiter:
    """Caps requests to ~N per second across the whole run."""

    def __init__(self, per_second=20):
        self.min_interval = 1.0 / per_second
        self.last = 0.0

    def wait(self):
        now = time.monotonic()
        delta = now - self.last
        if delta < self.min_interval:
            time.sleep(self.min_interval - delta)
        self.last = time.monotonic()


class TmdbClient:
    def __init__(self, token):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        })
        self.limiter = RateLimiter(20)
        self.providers_cache = {}
        self.external_ids_cache = {}

    def get(self, path, params=None, max_retries=5):
        url = f"{TMDB_BASE}{path}"
        for attempt in range(max_retries):
            self.limiter.wait()
            resp = self.session.get(url, params=params, timeout=15)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code in (429, 500, 502, 503, 504):
                backoff = min(2 ** attempt, 30)
                retry_after = resp.headers.get("Retry-After")
                if retry_after:
                    backoff = max(backoff, float(retry_after))
                time.sleep(backoff)
                continue
            resp.raise_for_status()
        raise RuntimeError(f"TMDB request failed after {max_retries} retries: {url} {params}")

    def watch_providers(self, media_type, tmdb_id):
        key = (media_type, tmdb_id)
        if key not in self.providers_cache:
            self.providers_cache[key] = self.get(f"/{media_type}/{tmdb_id}/watch/providers")
        return self.providers_cache[key]

    def external_ids(self, media_type, tmdb_id):
        key = (media_type, tmdb_id)
        if key not in self.external_ids_cache:
            self.external_ids_cache[key] = self.get(f"/{media_type}/{tmdb_id}/external_ids")
        return self.external_ids_cache[key]


def resolve_provider_ids(client):
    """Matches SERVICE_ALIASES against TMDB's provider list per region.
    Returns (set of matched provider IDs, dict of id->display name)."""
    matched_ids = set()
    id_to_name = {}
    found_services = set()

    def normalize(s):
        return s.lower().replace("+", " plus").strip()

    for media_type in ("tv", "movie"):
        for region in REGIONS:
            data = client.get(f"/watch/providers/{media_type}", params={"watch_region": region})
            for provider in data.get("results", []):
                name = provider.get("provider_name", "")
                norm_name = normalize(name)
                for service, aliases in SERVICE_ALIASES.items():
                    for alias in aliases:
                        if norm_name.startswith(normalize(alias)):
                            matched_ids.add(provider["provider_id"])
                            id_to_name[provider["provider_id"]] = name
                            found_services.add(service)
                            break

    missing = set(SERVICE_ALIASES) - found_services
    for service in sorted(missing):
        print(f"WARNING: service not found on TMDB in any region: {service}", file=sys.stderr)

    return matched_ids, id_to_name


def title_available(client, media_type, tmdb_id, provider_ids):
    data = client.watch_providers(media_type, tmdb_id)
    results = data.get("results", {})
    for region in REGIONS:
        region_data = results.get(region, {})
        for offer in region_data.get("flatrate", []):
            if offer["provider_id"] in provider_ids:
                return True
    return False


def passes_quality(item, exempt=False):
    vote_average = item.get("vote_average") or 0
    vote_count = item.get("vote_count") or 0
    if vote_average < MIN_VOTE_AVERAGE:
        return False
    if not exempt and vote_count < MIN_VOTE_COUNT:
        return False
    genre_ids = set(item.get("genre_ids") or [])
    if genre_ids & set(EXCLUDED_GENRE_IDS):
        return False
    return True


def to_meta(client, media_type, item, rpdb_key, do_rpdb_check):
    ext = client.external_ids(media_type, item["id"])
    imdb_id = ext.get("imdb_id")
    if not imdb_id:
        return None

    title = item.get("name") or item.get("title") or ""
    date_str = item.get("first_air_date") or item.get("release_date") or ""
    year = date_str[:4] if date_str else ""
    poster_path = item.get("poster_path")
    tmdb_poster = f"{TMDB_IMG_BASE}{poster_path}" if poster_path else None

    poster = f"{RPDB_BASE}/{rpdb_key}/imdb/poster-default/{imdb_id}.jpg"
    if do_rpdb_check:
        try:
            r = requests.head(poster, timeout=8)
            if r.status_code != 200 or "image" not in r.headers.get("content-type", ""):
                poster = tmdb_poster
        except requests.RequestException:
            poster = tmdb_poster
    if poster is None:
        poster = tmdb_poster

    meta = {
        "id": imdb_id,
        "type": "series" if media_type == "tv" else "movie",
        "name": title,
        "poster": poster,
        "description": item.get("overview", ""),
        "releaseInfo": year,
        "imdbRating": f"{item.get('vote_average', 0):.1f}",
    }
    if tmdb_poster:
        meta["background"] = tmdb_poster
    return meta


def build_trending_row(client, media_type, provider_ids, rpdb_key):
    items = []
    for page in range(1, TRENDING_PAGES + 1):
        data = client.get(f"/trending/{media_type}/day", params={"page": page})
        items.extend(data.get("results", []))

    metas = []
    for rank, item in enumerate(items):
        if len(metas) >= ROW_CAP:
            break
        exempt = rank < TRENDING_VOTE_COUNT_EXEMPT_RANK
        if not passes_quality(item, exempt=exempt):
            continue
        if not title_available(client, media_type, item["id"], provider_ids):
            continue
        do_check = len(metas) < RPDB_SPOTCHECK_COUNT
        meta = to_meta(client, media_type, item, rpdb_key, do_check)
        if meta:
            metas.append(meta)
    return metas


def build_discover_row(client, media_type, provider_ids, rpdb_key, sort_by,
                        date_gte=None, vote_count_gte=MIN_VOTE_COUNT,
                        require_recent_air_date=False, vote_count_exempt=False):
    date_field = "first_air_date.gte" if media_type == "tv" else "primary_release_date.gte"
    provider_ids_str = "|".join(str(i) for i in provider_ids)

    seen_ids = set()
    collected = []  # keeps first-seen order across regions
    for region in REGIONS:
        for page in range(1, DISCOVER_PAGES + 1):
            params = {
                "with_watch_providers": provider_ids_str,
                "watch_region": region,
                "with_watch_monetization_types": "flatrate",
                "sort_by": sort_by,
                "vote_count.gte": vote_count_gte,
                "page": page,
            }
            if date_gte:
                params[date_field] = date_gte
                if media_type == "tv" and require_recent_air_date:
                    # also catch new seasons of ongoing shows
                    params["air_date.gte"] = date_gte
            data = client.get(f"/discover/{media_type}", params=params)
            results = data.get("results", [])
            for item in results:
                if item["id"] not in seen_ids:
                    seen_ids.add(item["id"])
                    collected.append(item)
            if page >= data.get("total_pages", 1) or not results:
                break

    metas = []
    for item in collected:
        if len(metas) >= ROW_CAP:
            break
        if not passes_quality(item, exempt=vote_count_exempt):
            continue
        do_check = len(metas) < RPDB_SPOTCHECK_COUNT
        meta = to_meta(client, media_type, item, rpdb_key, do_check)
        if meta:
            metas.append(meta)
    return metas


def build_row(client, kind, media_type, provider_ids, rpdb_key):
    if kind == "trending":
        return build_trending_row(client, media_type, provider_ids, rpdb_key)
    if kind == "new":
        cutoff = (datetime.date.today() - datetime.timedelta(days=NEW_WINDOW_DAYS)).isoformat()
        # Vote-count floor is waived here, same idea as the trending top-20
        # exemption: a title can't accumulate 200 votes in its first 30 days,
        # so requiring it would make this row permanently empty.
        return build_discover_row(client, media_type, provider_ids, rpdb_key,
                                   sort_by="popularity.desc", date_gte=cutoff,
                                   vote_count_gte=0, require_recent_air_date=True,
                                   vote_count_exempt=True)
    if kind == "toprated":
        cutoff = (datetime.date.today() - datetime.timedelta(days=365 * TOPRATED_WINDOW_YEARS)).isoformat()
        return build_discover_row(client, media_type, provider_ids, rpdb_key,
                                   sort_by="vote_average.desc", date_gte=cutoff,
                                   vote_count_gte=TOPRATED_MIN_VOTE_COUNT[media_type])
    raise ValueError(kind)


def main():
    tmdb_token, rpdb_key = load_env()
    client = TmdbClient(tmdb_token)

    provider_ids, id_to_name = resolve_provider_ids(client)
    print(f"Resolved {len(provider_ids)} provider IDs across {len(REGIONS)} regions:")
    for pid, name in sorted(id_to_name.items(), key=lambda kv: kv[1]):
        print(f"  {pid}: {name}")

    os.makedirs(os.path.join(OUT_DIR, "series"), exist_ok=True)
    os.makedirs(os.path.join(OUT_DIR, "movie"), exist_ok=True)

    had_failure = False
    summary = []

    for dir_type, media_type, row_id, kind in ROW_SPECS:
        print(f"\nBuilding {dir_type}/{row_id} ({kind}, {media_type})...")
        try:
            metas = build_row(client, kind, media_type, provider_ids, rpdb_key)
        except Exception as e:
            print(f"ERROR building {dir_type}/{row_id}: {e}", file=sys.stderr)
            had_failure = True
            continue

        if len(metas) < MIN_ROW_ITEMS:
            print(f"ERROR: {dir_type}/{row_id} only has {len(metas)} items "
                  f"(need {MIN_ROW_ITEMS}) - leaving existing file untouched.", file=sys.stderr)
            had_failure = True
            continue

        out_path = os.path.join(OUT_DIR, dir_type, f"{row_id}.json")
        with open(out_path, "w") as f:
            json.dump({"metas": metas, "cacheMaxAge": 3600}, f, indent=2)

        print(f"  {len(metas)} items written to {out_path}")
        summary.append((f"{dir_type}/{row_id}", metas))

    print("\n=== Summary ===")
    for name, metas in summary:
        print(f"{name}: {len(metas)} items")
        for m in metas[:5]:
            print(f"    - {m['name']} ({m['releaseInfo']}, {m['imdbRating']})")

    if had_failure:
        print("\nOne or more rows failed - exiting non-zero so CI doesn't commit "
              "a partial update. Yesterday's files are untouched.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
