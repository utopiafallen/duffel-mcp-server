# Smarter Search & Scoring Design

Three improvements to make the Duffel MCP server find better flights: city-code aware instructions, partial offer requests for mix-and-match legs, and layover quality scoring.

## 1. City-Code Aware Instructions

### Problem
When a user says "flights from New York", the LLM might search `JFK` specifically, missing cheaper options from `EWR` or `LGA`. The Duffel API already supports city IATA codes (`NYC` returns results from all three airports), but nothing tells the LLM to use them.

### Design
Update the `duffel://instructions` resource to include:
- A table of common city codes (NYC, LON, PAR, TYO, CHI, WAS, MIL, ROM, etc.)
- Guidance: "When the user doesn't specify a particular airport, use the city IATA code instead of a specific airport code"
- Example: "User says 'flights from London' → use `LON` (covers LHR, LGW, STN, LTN, SEN)"

**Files modified:** `server.py` — the `flight_search_instructions()` resource function (~line 1177)

**No new API calls.** The Duffel API already handles city codes natively.

### City Code Reference Table

| City | Code | Airports |
|------|------|----------|
| New York | NYC | JFK, EWR, LGA |
| London | LON | LHR, LGW, STN, LTN, SEN |
| Paris | PAR | CDG, ORY |
| Tokyo | TYO | NRT, HND |
| Chicago | CHI | ORD, MDW |
| Washington DC | WAS | IAD, DCA, BWI |
| Milan | MIL | MXP, LIN |
| Rome | ROM | FCO, CIA |
| Moscow | MOW | SVO, DME, VKO |
| Stockholm | STO | ARN, BMA |
| Seoul | SEL | ICN, GMP |
| Shanghai | SHA | PVG, SHA |
| Buenos Aires | BUE | EZE, AEP |
| Sao Paulo | SAO | GRU, CGH |
| Ho Chi Minh City | SGN | SGN (single airport, but city code still works) |

## 2. Partial Offer Requests (Mix-and-Match Legs)

### Problem
The current `duffel_search_flights` tool returns bundled round-trip offers where both legs come from the same airline/booking. Often, flying airline A outbound and airline B on the return is significantly cheaper. Google Flights does this — Duffel supports it via `POST /air/partial_offer_requests`.

### Design
Add a new MCP tool `duffel_search_partial` that:
1. Calls `POST /air/partial_offer_requests` with the same slice/passenger structure
2. Returns offers grouped **per slice** instead of bundled round-trips
3. Shows the cheapest combination by adding up per-leg prices
4. Highlights savings vs the cheapest bundled round-trip (if the LLM has one from a prior search)

### API Details
- Endpoint: `POST /air/partial_offer_requests`
- Same request body as regular offer requests (slices, passengers, cabin_class, max_connections)
- Response differs: offers contain `partial_offers` grouped by slice, each with its own price
- One API call (same cost as regular search)

### New Components

**Input model: `SearchPartialInput`**
```python
class SearchPartialInput(BaseModel):
    slices: List[FlightSlice]  # reuse existing
    passengers: List[PassengerInput]  # reuse existing
    cabin_class: Optional[CabinClass] = CabinClass.ECONOMY
    max_connections: Optional[int] = None
    top_n: Optional[int] = Field(default=5, ge=1, le=20)
```

**Tool: `duffel_search_partial`**
- Calls partial offer request endpoint
- Groups results by slice
- For each slice, shows top N cheapest options with airline, times, stops, baggage
- Shows cheapest total combination (sum of cheapest per-leg)
- Response format: markdown with per-leg sections and a "Best Combination" summary

**Response structure:**
```markdown
# Partial Offer Search (Mix & Match)

## Outbound: NYC → LON (Dec 24)
| # | Price | Airline | Departure | Arrival | Stops | Baggage |
|...|.......|.........|...........|.........|.......|.........|
| 1 | $245  | Norse   | 18:00     | 06:30+1 | 0     | Carry-on only |
| 2 | $312  | BA      | 21:00     | 09:15+1 | 0     | 1x checked |

## Return: LON → NYC (Dec 31)
| # | Price | Airline | Departure | Arrival | Stops | Baggage |
|...|.......|.........|...........|.........|.......|.........|
| 1 | $198  | Norse   | 10:00     | 13:30   | 0     | Carry-on only |
| 2 | $287  | Virgin  | 11:30     | 14:45   | 0     | 1x checked |

## Best Combination: $443 (Norse out + Norse return)
## Best Mixed: $532 (BA out + Norse return) — bags included both ways
```

**Files modified:** `server.py`
- New `SearchPartialInput` model (~15 lines)
- New `duffel_search_partial` tool function (~150 lines)

**Update `duffel://instructions`** to add:
```
| "Mix and match airlines" | `duffel_search_partial` |
| "Cheapest round-trip combo" | `duffel_search_partial` |
```

## 3. Layover Quality Scoring

### Problem
The current `calculate_flight_score()` treats all connections equally — a 45-minute tight connection scores the same as a comfortable 2-hour layover, and a 7-hour wait scores the same as both. The `_count_stops()` helper just counts stops without considering their quality.

### Design
Add a `_layover_quality_score()` helper that evaluates connection times, then integrate it into `calculate_flight_score()`.

### Scoring Logic

```python
def _layover_quality_score(offer: Dict[str, Any]) -> float:
    """Score layover quality (0-1, higher is better). Direct flights = 1.0."""
```

**Per-connection scoring (0-1):**

| Connection Time | Score | Reason |
|----------------|-------|--------|
| < 45 min | 0.2 | High misconnect risk |
| 45-60 min | 0.5 | Tight but possible |
| 60-90 min | 0.9 | Comfortable minimum |
| 90 min - 3 hr | 1.0 | Ideal |
| 3-5 hr | 0.7 | Getting long |
| 5-8 hr | 0.4 | Unpleasant wait |
| > 8 hr | 0.2 | Overnight/excessive |

**Implementation:**
- Extract connection times by comparing `arriving_at` of segment N with `departing_at` of segment N+1 within each slice
- Average the per-connection scores across all connections in the offer
- Direct flights (no connections) get 1.0

### Red-Eye Default Penalty

When no `preferred_departure_time` is specified, apply a small penalty for departures between midnight and 5 AM:

```python
# In _calculate_time_preference_score():
if not preference:
    # Small red-eye penalty instead of flat 0.5
    if 0 <= hour < 5:
        return 0.3  # Red-eye penalty
    return 0.5  # Neutral for normal hours
```

### Integration into `calculate_flight_score()`

Add layover quality as a sub-component of the existing `duration` weight rather than adding a new top-level weight. This avoids changing the `OptimizationWeights` model (backward compatible).

```python
# Current:
duration_score = 1 - _normalize(duration, min(durations), max(durations))

# New: blend duration with layover quality (70/30 split)
raw_duration_score = 1 - _normalize(duration, min(durations), max(durations))
layover_score = _layover_quality_score(offer)
duration_score = 0.7 * raw_duration_score + 0.3 * layover_score
```

This means the `duration` weight (default 0.3) now accounts for both total travel time AND connection quality, without requiring users to learn new weights.

**Files modified:** `server.py`
- New `_layover_quality_score()` function (~30 lines)
- Modified `calculate_flight_score()` (~3 lines changed)
- Modified `_calculate_time_preference_score()` (~3 lines changed)

## Implementation Order

1. **Layover quality scoring** — pure logic, no API changes, immediately improves "best" results
2. **City-code instructions** — text-only change, immediate value
3. **Partial offer requests** — new tool, most code, highest value for cost-conscious users

## Summary

| Feature | New API Calls | Lines Changed | Risk |
|---------|--------------|---------------|------|
| City-code instructions | 0 | ~30 (instructions text) | None |
| Partial offer requests | 0 (replaces regular search) | ~170 (new tool) | Low — new tool, doesn't change existing |
| Layover quality scoring | 0 | ~40 (new helper + integration) | Low — backward compatible scoring |
| **Total** | **0 additional** | **~240** | **Low** |
