#!/usr/bin/env python3
"""
Sports Arbitrage Tracker - Backend Engine (arb_engine.py)

Strictly whitelisted bookmakers:
- fanduel
- betmgm
- caesars
- fanatics
- betrivers
- thescore

Calculates implied probabilities, best available odds across whitelisted bookies,
raw stakes, guaranteed profit, and ROI for head-to-head (h2h) sports markets.

Outputs:
- Lightweight JSON file: arbs.json (ROI >= 1.0%)
- Triggers NTFY.sh HTTP POST notifications (ROI >= 2.0%)
- Flags Palpable Errors with 'Palp Warning' (ROI > 4.0%)
"""

import json
import os
import sys
import urllib.request
import urllib.error
import argparse
import requests
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Tuple

# Strict whitelist of allowed sportsbooks (normalized lower-case string identifiers)
ALLOWED_BOOKMAKERS = {
    "fanduel",
    "betmgm",
    "caesars",
    "fanatics",
    "betrivers",
    "thescore"
}

# Bookmaker name mapping for normalization
BOOKMAKER_ALIASES = {
    "fanduel": "fanduel",
    "fanduel sportsbook": "fanduel",
    "betmgm": "betmgm",
    "caesars": "caesars",
    "caesars sportsbook": "caesars",
    "fanatics": "fanatics",
    "fanatics sportsbook": "fanatics",
    "betrivers": "betrivers",
    "thescore": "thescore",
    "thescore bet": "thescore",
    "the score": "thescore"
}

DEFAULT_NTFY_TOPIC = "arb_tracker_alerts"


def normalize_bookmaker_key(name: str) -> Optional[str]:
    """
    Normalizes a bookmaker string and returns the canonical key if whitelisted.
    Returns None if bookmaker is NOT in the strict whitelist.
    """
    if not name:
        return None
    cleaned = name.strip().lower()
    canonical = BOOKMAKER_ALIASES.get(cleaned, cleaned)
    if canonical in ALLOWED_BOOKMAKERS:
        return canonical
    return None


def american_to_decimal(american_odds: float) -> float:
    """
    Converts American odds (e.g. +150, -110) to Decimal odds (e.g. 2.50, 1.909).
    """
    if american_odds > 0:
        return round(1.0 + (american_odds / 100.0), 4)
    elif american_odds < 0:
        return round(1.0 + (100.0 / abs(american_odds)), 4)
    else:
        raise ValueError("American odds cannot be zero.")


def decimal_to_american(decimal_odds: float) -> int:
    """
    Converts Decimal odds (e.g. 2.50, 1.91) to American odds integer (e.g. +150, -110).
    """
    if decimal_odds >= 2.0:
        return int(round((decimal_odds - 1.0) * 100))
    elif decimal_odds > 1.0:
        return int(round(-100.0 / (decimal_odds - 1.0)))
    else:
        raise ValueError("Decimal odds must be greater than 1.0.")


def calculate_implied_probability(decimal_odds: float) -> float:
    """
    Calculates implied probability from Decimal odds.
    Implied Probability = 1 / Decimal Odds
    """
    if decimal_odds <= 1.0:
        raise ValueError("Decimal odds must be greater than 1.0 for implied prob.")
    return 1.0 / decimal_odds


def calculate_raw_stakes(
    total_stake: float, 
    implied_prob_1: float, 
    implied_prob_2: float, 
    total_implied_prob: float
) -> Tuple[float, float]:
    """
    Calculates exact raw stakes for 2 outcomes in a head-to-head market.
    Stake_i = Total_Stake * (Implied_Prob_i / Total_Implied_Prob)
    """
    stake_1 = total_stake * (implied_prob_1 / total_implied_prob)
    stake_2 = total_stake * (implied_prob_2 / total_implied_prob)
    return round(stake_1, 2), round(stake_2, 2)


def send_ntfy_alert(arb: Dict[str, Any], ntfy_topic: str) -> bool:
    """
    Fires an HTTP POST request to NTFY.sh for an arbitrage opportunity with ROI >= 2.0%.
    """
    url = f"https://ntfy.sh/{ntfy_topic}"
    roi = arb["roi_percent"]
    event = arb["event"]
    sport = arb["sport"]
    palp_warning = arb.get("palp_warning")
    
    outcomes_str = "\n".join([
        f"  • {o['name']}: {o['bookmaker'].upper()} @ {o['odds_decimal']} ({o['odds_american_str']}) | Stake: ${o['raw_stake']:.2f}"
        for o in arb["outcomes"]
    ])
    
    warning_header = " ⚠️ PALP WARNING!" if palp_warning else ""
    title = f"🚨 Arbitrage Opportunity: {roi:.2f}% ROI{warning_header}"
    
    body = (
        f"Event: {event} ({sport})\n"
        f"ROI: {roi:.2f}%\n"
        f"Guaranteed Profit: ${arb['guaranteed_profit']:.2f} (on ${arb['total_stake']:.2f} stake)\n"
        f"Outcomes:\n{outcomes_str}\n"
    )
    if palp_warning:
        body += f"\nNote: {palp_warning} (ROI > 4.0% - Risk of voided odds)."

    req = urllib.request.Request(
        url,
        data=body.encode("utf-8"),
        headers={
            "Title": title,
            "Tags": "moneybag,chart_with_upwards_trend" if not palp_warning else "warning,moneybag",
            "Priority": "high" if roi >= 4.0 else "default"
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status == 200:
                print(f"[NTFY ALERT SENT] Posted alert to ntfy.sh/{ntfy_topic} for {event}")
                return True
            else:
                print(f"[NTFY WARNING] Server returned status code {resp.status}")
                return False
    except Exception as e:
        print(f"[NTFY ERROR] Failed to send alert to ntfy.sh/{ntfy_topic}: {e}")
        return False


def process_event(event_data: Dict[str, Any], total_stake: float = 100.0) -> Optional[Dict[str, Any]]:
    """
    Processes a single game/event object.
    Strictly filters for whitelisted bookmakers and calculates head-to-head arbs.
    Returns arb dict if ROI >= 1.0%, else None.
    """
    event_name = event_data.get("event", "Unknown Event")
    sport = event_data.get("sport", "Unknown Sport")
    event_id = event_data.get("id", "evt_" + str(hash(event_name)))
    commence_time = event_data.get("commence_time", datetime.now(timezone.utc).isoformat())
    market_type = event_data.get("market", "h2h")

    if market_type != "h2h":
        return None  # Only process head-to-head markets

    bookmakers_raw = event_data.get("bookmakers", [])

    # Filter bookmakers STRICTLY by whitelist
    filtered_bookmakers = []
    for b in bookmakers_raw:
        bookie_name = b.get("key") or b.get("title", "")
        canonical_key = normalize_bookmaker_key(bookie_name)
        if canonical_key:
            # Look for outcomes inside markets if not directly available (The Odds API structure)
            outcomes = b.get("outcomes")
            if not outcomes:
                for market in b.get("markets", []):
                    if market.get("key") == "h2h":
                        outcomes = market.get("outcomes", [])
                        break
            
            filtered_bookmakers.append({
                "canonical_key": canonical_key,
                "display_title": b.get("title", canonical_key.title()),
                "outcomes": outcomes or []
            })

    if not filtered_bookmakers:
        return None

    # Collect the best decimal odds for each outcome across whitelisted books
    best_outcomes: Dict[str, Dict[str, Any]] = {}

    for b in filtered_bookmakers:
        bookie_key = b["canonical_key"]
        bookie_title = b["display_title"]
        for o in b["outcomes"]:
            name = o.get("name", "").strip()
            if not name:
                continue

            decimal_odds = o.get("price")
            american_odds = o.get("american")

            if decimal_odds is None and american_odds is not None:
                decimal_odds = american_to_decimal(american_odds)
            elif decimal_odds is not None and american_odds is None:
                american_odds = decimal_to_american(decimal_odds)

            if decimal_odds is None or decimal_odds <= 1.0:
                continue

            if name not in best_outcomes or decimal_odds > best_outcomes[name]["decimal_odds"]:
                best_outcomes[name] = {
                    "name": name,
                    "decimal_odds": decimal_odds,
                    "american_odds": american_odds,
                    "bookmaker": bookie_key,
                    "bookmaker_title": bookie_title
                }

    # Head-to-head requires exactly 2 outcomes
    if len(best_outcomes) != 2:
        return None

    outcomes_list = list(best_outcomes.values())
    o1 = outcomes_list[0]
    o2 = outcomes_list[1]

    # Calculate implied probabilities
    p1 = calculate_implied_probability(o1["decimal_odds"])
    p2 = calculate_implied_probability(o2["decimal_odds"])

    total_implied_prob = p1 + p2

    # Arbitrage ROI (%) = ((1 / total_implied_prob) - 1) * 100
    if total_implied_prob >= 1.0:
        return None  # No arbitrage opportunity

    roi_percent = round(((1.0 / total_implied_prob) - 1.0) * 100.0, 2)

    if roi_percent < 1.0:
        return None  # Below 1.0% ROI threshold

    # Calculate raw stakes
    raw_stake_1, raw_stake_2 = calculate_raw_stakes(total_stake, p1, p2, total_implied_prob)

    # Expected payout & guaranteed profit
    payout = round(total_stake / total_implied_prob, 2)
    profit = round(payout - total_stake, 2)

    # Check for Palp Warning (ROI > 4.0%)
    palp_warning = "Palp Warning" if roi_percent > 4.0 else None

    # Formatted american odds strings (e.g. "+150", "-110")
    def format_american(odds: Optional[int]) -> str:
        if odds is None:
            return ""
        return f"+{odds}" if odds > 0 else str(odds)

    arb_item = {
        "id": event_id,
        "sport": sport,
        "event": event_name,
        "commence_time": commence_time,
        "market": "h2h",
        "roi_percent": roi_percent,
        "implied_prob_total": round(total_implied_prob, 4),
        "total_stake": total_stake,
        "guaranteed_payout": payout,
        "guaranteed_profit": profit,
        "palp_warning": palp_warning,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "outcomes": [
            {
                "name": o1["name"],
                "bookmaker": o1["bookmaker"],
                "bookmaker_title": o1["bookmaker_title"],
                "odds_decimal": o1["decimal_odds"],
                "odds_american": o1["american_odds"],
                "odds_american_str": format_american(o1["american_odds"]),
                "implied_prob": round(p1, 4),
                "raw_stake": raw_stake_1,
                "payout": round(raw_stake_1 * o1["decimal_odds"], 2)
            },
            {
                "name": o2["name"],
                "bookmaker": o2["bookmaker"],
                "bookmaker_title": o2["bookmaker_title"],
                "odds_decimal": o2["decimal_odds"],
                "odds_american": o2["american_odds"],
                "odds_american_str": format_american(o2["american_odds"]),
                "implied_prob": round(p2, 4),
                "raw_stake": raw_stake_2,
                "payout": round(raw_stake_2 * o2["decimal_odds"], 2)
            }
        ]
    }

    return arb_item

def fetch_live_odds(sport_key: str = "basketball_nba", market: str = "h2h") -> List[Dict[str, Any]]:
    """
    Fetches live odds from The Odds API for specific markets.
    """
    api_key = os.environ.get("ODDS_API_KEY")
    if not api_key:
        raise ValueError("ODDS_API_KEY environment variable not set.")
    
    url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds"
    params = {
        "apiKey": api_key,
        "regions": "us",
        "markets": market,
        "oddsFormat": "decimal",
    }
    
    response = requests.get(url, params=params)
    if response.status_code != 200:
        raise Exception(f"Failed to fetch odds: {response.text}")
    
    return response.json()


def run_engine(
    sport: str = "basketball_nba", 
    market: str = "h2h",
    output_file: str = "arbs.json", 
    total_stake: float = 100.0, 
    ntfy_topic: Optional[str] = DEFAULT_NTFY_TOPIC,
    enable_ntfy: bool = True
) -> List[Dict[str, Any]]:
    """
    Executes the arbitrage engine on live odds data.
    """
    print(f"--- Running Sports Arbitrage Tracker (Live) ---")
    print(f"Sport: {sport}, Market: {market}")
    events = fetch_live_odds(sport, market)
    
    arbs: List[Dict[str, Any]] = []

    print(f"Strict Whitelist: {', '.join(sorted(ALLOWED_BOOKMAKERS))}")
    print(f"Processing {len(events)} events...")

    for event in events:
        # Standardize The Odds API event format to our engine format
        standardized_bookmakers = []
        for b in event.get("bookmakers", []):
            # Find the market matching the requested market key
            for m in b.get("markets", []):
                if m.get("key") == market:
                    standardized_bookmakers.append({
                        "key": b.get("key"),
                        "title": b.get("title"),
                        "outcomes": [
                            # Map {name, description, price} -> {name, price}
                            # Combine player name and description for unique ID
                            {"name": f"{o.get('name')} - {o.get('description')}", "price": o.get("price")}
                            for o in m.get("outcomes", [])
                        ]
                    })
        
        standardized_event = {
            "id": event.get("id"),
            "sport": event.get("sport_title"),
            "event": f"{event.get('home_team')} vs {event.get('away_team')}",
            "commence_time": event.get("commence_time"),
            "market": market,
            "bookmakers": standardized_bookmakers
        }
        
        arb = process_event(standardized_event, total_stake=total_stake)
        if arb:
            arbs.append(arb)
            roi = arb["roi_percent"]
            palp = f" [{arb['palp_warning']}]" if arb.get("palp_warning") else ""
            print(f"[FOUND ARB] {arb['event']} - ROI: {roi:.2f}%{palp} | Profit: ${arb['guaranteed_profit']:.2f}")

            if enable_ntfy and roi >= 2.0 and ntfy_topic:
                send_ntfy_alert(arb, ntfy_topic)

    arbs.sort(key=lambda x: x["roi_percent"], reverse=True)

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(arbs, f, indent=2)

    print(f"Saved {len(arbs)} arbitrage opportunities (>= 1.0% ROI) to {output_file}")
    return arbs


def main():
    parser = argparse.ArgumentParser(description="Sports Arbitrage Tracker Engine (Live)")
    parser.add_argument("--sport", default="basketball_nba", help="Sport key for The Odds API")
    parser.add_argument("--market", default="h2h", help="Market key for The Odds API (e.g., h2h, player_points)")
    parser.add_argument("--output", default="arbs.json", help="Path to output JSON file for arbs")
    parser.add_argument("--stake", type=float, default=100.0, help="Base total stake amount")
    parser.add_argument("--ntfy-topic", default=DEFAULT_NTFY_TOPIC, help="NTFY.sh topic name")
    parser.add_argument("--no-ntfy", action="store_true", help="Disable NTFY.sh alerts")

    args = parser.parse_args()

    run_engine(
        sport=args.sport,
        market=args.market,
        output_file=args.output,
        total_stake=args.stake,
        ntfy_topic=args.ntfy_topic,
        enable_ntfy=not args.no_ntfy
    )


if __name__ == "__main__":
    main()




