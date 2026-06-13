#!/usr/bin/env python3
"""
Fetch finished World Cup 2026 match results from football-data.org and
patch worldcup.json with scores and goalscorers.

Usage:
    export FOOTBALL_DATA_API_KEY=your_key_here
    python update_scores.py [--dry-run]

Get a free key at: https://www.football-data.org/client/register
"""

import json
import os
import sys
import time
import argparse
from pathlib import Path
import urllib.request
import urllib.error

API_BASE = "https://api.football-data.org/v4"
# football-data.org uses "WC" for the current/upcoming World Cup competition
COMPETITION_CODE = "WC"
JSON_PATH = Path(__file__).parent / "worldcup.json"

# Map football-data.org team names → names used in worldcup.json
TEAM_NAME_MAP = {
    "United States": "USA",
    "Korea Republic": "South Korea",
    "Bosnia and Herzegovina": "Bosnia & Herzegovina",
    "Côte d'Ivoire": "Ivory Coast",
    "Cote d'Ivoire": "Ivory Coast",
    "Cape Verde": "Cape Verde",
    "Cabo Verde": "Cape Verde",
    "Congo DR": "DR Congo",
    "Democratic Republic of the Congo": "DR Congo",
    "Iran": "Iran",
    "Islamic Republic of Iran": "Iran",
    "New Zealand": "New Zealand",
    "Curaçao": "Curaçao",
    "Curacao": "Curaçao",
}


def api_get(path: str, api_key: str) -> dict:
    url = f"{API_BASE}{path}"
    req = urllib.request.Request(url, headers={"X-Auth-Token": api_key})
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"HTTP {e.code} for {url}: {body}", file=sys.stderr)
        raise


def normalize_name(name: str) -> str:
    return TEAM_NAME_MAP.get(name, name)


def fetch_matches(api_key: str) -> list[dict]:
    data = api_get(f"/competitions/{COMPETITION_CODE}/matches", api_key)
    return data.get("matches", [])


def fetch_match_detail(match_id: int, api_key: str) -> dict:
    return api_get(f"/matches/{match_id}", api_key)


def build_index(matches: list[dict]) -> dict:
    """Index worldcup.json matches by (date, team1, team2) for fast lookup."""
    index = {}
    for i, m in enumerate(matches):
        if "score" in m:
            continue  # already has a result, skip
        key = (m["date"], m.get("team1", ""), m.get("team2", ""))
        index[key] = i
    return index


def map_goals(api_goals: list[dict], team_name: str) -> list[dict]:
    """Filter API goals for one team and convert to worldcup.json format."""
    result = []
    for g in api_goals:
        if normalize_name(g.get("team", {}).get("name", "")) == team_name:
            scorer = g.get("scorer", {}).get("name", "Unknown")
            minute = str(g.get("minute", "?"))
            # Own goals credited to the scoring team in the API but belong to goals2
            result.append({"name": scorer, "minute": minute})
    return result


def update_match(entry: dict, api_match: dict, detail: dict) -> bool:
    """Apply API data to a worldcup.json match entry. Returns True if changed."""
    score = api_match.get("score", {})
    ft = score.get("fullTime", {})
    ht = score.get("halfTime", {})

    ft_home = ft.get("home")
    ft_away = ft.get("away")
    ht_home = ht.get("home")
    ht_away = ht.get("away")

    if ft_home is None or ft_away is None:
        return False

    entry["score"] = {"ft": [ft_home, ft_away]}
    if ht_home is not None and ht_away is not None:
        entry["score"]["ht"] = [ht_home, ht_away]

    api_goals = detail.get("goals", [])
    if api_goals:
        t1 = entry["team1"]
        t2 = entry["team2"]
        entry["goals1"] = map_goals(api_goals, t1)
        entry["goals2"] = map_goals(api_goals, t2)

    return True


def main():
    parser = argparse.ArgumentParser(description="Update worldcup.json with live results")
    parser.add_argument("--dry-run", action="store_true", help="Print changes without saving")
    args = parser.parse_args()

    api_key = os.environ.get("FOOTBALL_DATA_API_KEY", "").strip()
    if not api_key:
        print("Error: set FOOTBALL_DATA_API_KEY environment variable", file=sys.stderr)
        sys.exit(1)

    print("Loading worldcup.json...")
    with open(JSON_PATH) as f:
        wc = json.load(f)

    matches = wc["matches"]
    index = build_index(matches)

    print(f"Fetching matches from football-data.org (competition: {COMPETITION_CODE})...")
    api_matches = fetch_matches(api_key)
    finished = [m for m in api_matches if m.get("status") == "FINISHED"]
    print(f"  {len(finished)} finished match(es) found")

    updated = 0
    for api_match in finished:
        date = api_match.get("utcDate", "")[:10]  # "2026-06-11T..."  → "2026-06-11"
        home = normalize_name(api_match.get("homeTeam", {}).get("name", ""))
        away = normalize_name(api_match.get("awayTeam", {}).get("name", ""))

        key = (date, home, away)
        idx = index.get(key)
        if idx is None:
            # Also try swapped (some fixtures may list differently)
            idx = index.get((date, away, home))
        if idx is None:
            # Check if match already has a score (was manually entered or previously updated)
            already_scored = any(
                m.get("date") == date
                and {m.get("team1"), m.get("team2")} == {home, away}
                and "score" in m
                for m in matches
            )
            if not already_scored:
                print(f"  Skipped (not found in worldcup.json): {home} vs {away} ({date})")
            continue

        print(f"  Updating: {home} vs {away} ({date})")

        # Fetch detailed match data for goalscorers (rate-limit: 10 req/min on free tier)
        match_id = api_match["id"]
        try:
            detail = fetch_match_detail(match_id, api_key)
            time.sleep(6.1)  # stay within 10 req/min
        except Exception as e:
            print(f"    Warning: could not fetch detail for match {match_id}: {e}")
            detail = {}

        changed = update_match(matches[idx], api_match, detail)
        if changed:
            updated += 1
        else:
            print(f"    Warning: score not available yet for match {match_id}")

    if updated == 0:
        print("Nothing to update.")
        return

    if args.dry_run:
        print(f"\n[dry-run] Would update {updated} match(es). Result preview:")
        for m in matches:
            if "score" in m and "goals1" in m:
                print(f"  {m['team1']} {m['score']['ft'][0]}-{m['score']['ft'][1]} {m['team2']}")
    else:
        with open(JSON_PATH, "w") as f:
            json.dump(wc, f, indent=1, ensure_ascii=False)
            f.write("\n")
        print(f"\nSaved {updated} update(s) to {JSON_PATH}")


if __name__ == "__main__":
    main()
