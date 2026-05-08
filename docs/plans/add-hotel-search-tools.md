# Plan: Add Hotel Search Tools to Duffel MCP Server

## Overview

Add 3 new MCP tools to `duffel_mcp/server.py` to cover the Duffel Stays booking flow, following the existing flight tool patterns. The Stays API is a 4-step sequential flow — each step returns an ID needed for the next.

**Prerequisite**: User must request access to Duffel Stays separately from the flight API. Same `DUFFEL_API_KEY_LIVE`, but Stays permissions are not enabled by default.

## Duffel Stays API Endpoints

| Step | Method | Endpoint | Returns |
|------|--------|----------|---------|
| 1. Search | `POST` | `/stays/search` | `search_result_id` per accommodation |
| 2. Fetch Rates | `POST` | `/stays/search_results/{id}/actions/fetch_all_rates` | Full room + rate details with `rate_id` |
| 3. Create Quote | `POST` | `/stays/quotes` | `quote_id` (price lock) |
| 4. Create Booking | `POST` | `/stays/bookings` | `booking_id`, confirmation |

## Tool 1: `duffel_search_stays` ✅ DONE

**Commit**: `5269138` — "Add duffel_search_stays tool and fix 7 existing bugs"

**Status**: Implemented and validated (12 tools loaded, syntax OK).

### What was implemented

- **4 new Pydantic models** (`server.py:636-772`): `StaysGuestInput`, `StaysLocationInput`, `StaysOptimizationWeights`, `SearchStaysInput`
- **Tool function** (`server.py:2854-3139`): `duffel_search_stays` with full markdown + JSON output
- **Client-side star rating filter**: unrated properties treated as 1-star; `min_rating`/`max_rating` applied after API response
- **4 optimization strategies** (all client-side):
  - `cheapest`: sort by `cheapest_rate_total_amount` ascending
  - `best_reviewed`: confidence-weighted `review_score × log(review_count + 1)` descending
  - `most_reviewed`: `review_count` descending
  - `recommended` (default): normalized weighted composite (price=0.3, rating=0.2, review_score=0.3, review_count=0.2)
- **Input**: geographic location (lat/lon + radius), `check_in_date`, `num_nights`, guests, rooms, cancellation/payment filters
- **Output**: markdown capped at 30 results with name, rating, reviews, price, amenities, and `search_result_id`; JSON returns full response

### Bugs fixed alongside implementation

1. Merged duplicate `TimeRange` model_config into single `ConfigDict`
2. Replaced hardcoded `$` currency in seat map rows with actual currency
3. Replaced hardcoded `$` in flexible search output (6 occurrences)
4. Replaced deprecated `datetime.utcnow()` with `datetime.now(timezone.utc)` (6 occurrences)
5. Removed 3 redundant `import re` inside function bodies
6. Removed redundant `from datetime import` inside `duffel_flexible_search`
7. Narrowed bare `except:` in `_handle_api_error` to specific exceptions

## Tool 2: `duffel_get_stay_rates`

**Input model**: `GetStayRatesInput`

### Fields

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `search_result_id` | `str` | Yes | Pattern: `^srr_` |
| `response_format` | `ResponseFormat` | No | Default MARKDOWN |

### Implementation

- **Endpoint**: `POST stays/search_results/{search_result_id}/actions/fetch_all_rates` (empty body)
- Returns full room options with rate details: room name, bed type, board type, cancellation policy, price breakdown (base/tax/fee/total), and `rate_id`
- Markdown format: per-room summary with rate options and their `rate_id`
- JSON format: raw API response
- Follow the `duffel_get_offer` pattern (line 1993)

## Tool 3: `duffel_book_stay`

**Input model**: `BookStayInput`

### Fields

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `rate_id` | `str` | Yes | Pattern: `^rat_` |
| `email` | `str` | Yes | Email validation |
| `phone_number` | `str` | Yes | E.164 format |
| `guests` | `List[StayGuestDetailInput]` | Yes | `given_name`, `family_name`, optional `born_on` |
| `special_requests` | `str` | No | Forwarded to hotel |
| `loyalty_programme_account_number` | `str` | No | Only if quote supports loyalty |

### Implementation

- **Two-step internal flow**:
  1. `POST stays/quotes` with `rate_id` → get `quote_id` (confirms availability + locks price)
  2. `POST stays/bookings` with `quote_id` + guest details → booking confirmation
- Payment uses Duffel balance by default (omit `payment` object). Card payment requires a 3D Secure session ID — out of scope for now.
- Return booking confirmation with: reference, dates, hotel details, key collection instructions
- Follow the `duffel_create_order` pattern (line 2178)

## New Pydantic Models

Place after line 634 (after `CreateCheckoutInput`, before `CheckoutSession`):

### ✅ Already implemented (`server.py:636-772`)

```python
class StaysGuestInput(BaseModel):
    """Guest type for stay search."""
    type: str = Field(..., pattern=r"^(adult|child)$")
    age: Optional[int] = Field(None, ge=0, le=17)  # required for child

class StaysLocationInput(BaseModel):
    """Geographic location for stay search."""
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    radius: int = Field(default=5, ge=1, le=50)

class StaysOptimizationWeights(BaseModel):
    """Weights for the 'recommended' optimization strategy."""
    price: float = Field(default=0.3, ge=0, le=1)
    rating: float = Field(default=0.2, ge=0, le=1)
    review_score: float = Field(default=0.3, ge=0, le=1)
    review_count: float = Field(default=0.2, ge=0, le=1)

class SearchStaysInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True)
    location: StaysLocationInput = Field(...)  # required
    check_in_date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    num_nights: int = Field(..., ge=1, le=99)  # check-out computed from this
    guests: List[StaysGuestInput] = Field(..., min_length=1)
    rooms: int = Field(default=1, ge=1, le=10)
    free_cancellation_only: bool = False
    instant_payment: Optional[bool] = None
    min_rating: Optional[int] = Field(None, ge=1, le=5)  # client-side filter
    max_rating: Optional[int] = Field(None, ge=1, le=5)  # client-side filter
    optimization: str = "recommended"  # cheapest, best_reviewed, most_reviewed, recommended
    optimization_weights: Optional[StaysOptimizationWeights] = None
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)

    @model_validator(mode='after')
    def validate_rating_range(self) -> 'SearchStaysInput':
        ...  # enforce min_rating <= max_rating when both set
```

### ⏳ Still needed (for tools 2-3)

```python
class StayGuestDetailInput(BaseModel):
    """Guest details required at booking time."""
    given_name: str
    family_name: str
    born_on: Optional[str] = None  # YYYY-MM-DD
    user_id: Optional[str] = None  # Duffel customer ID

class GetStayRatesInput(BaseModel):
    search_result_id: str = Field(..., pattern=r"^srr_")
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)

class BookStayInput(BaseModel):
    rate_id: str = Field(..., pattern=r"^rat_")
    email: str = Field(..., pattern=r"^[^@]+@[^@]+\.[^@]+$")
    phone_number: str = Field(..., pattern=r"^\+")
    guests: List[StayGuestDetailInput] = Field(..., min_length=1)
    special_requests: Optional[str] = None
    loyalty_programme_account_number: Optional[str] = None
```

## Placement in `server.py`

| Addition | Location | Status |
|----------|----------|--------|
| New input models (4) | After line 634 (`server.py:636-772`) | ✅ Done |
| `duffel_search_stays` tool | After `duffel_list_airlines` (`server.py:2854-3139`) | ✅ Done |
| `duffel_get_stay_rates` tool | Immediately after search stays | ⏳ Pending |
| `duffel_book_stay` tool | Immediately after get stay rates | ⏳ Pending |

## Key Differences from Flight Tools

1. **Location-based search**: Hotels use lat/lon + radius, not airport codes. The existing `duffel://places/{query}` resource only returns airport IATA codes — it cannot resolve city names to coordinates. The LLM must provide lat/lon from its own knowledge or the user must supply it directly.
2. **Two-step booking**: Quote creation is required before booking (flights skip this step). `duffel_book_stay` handles both calls internally with progress reporting at each step.
3. **Client-side optimization**: Hotel search always sorts by default (`recommended`). Other strategies: `cheapest`, `best_reviewed` (confidence-weighted: score × log(count+1)), `most_reviewed`. All sorting is client-side since the API has no ordering parameter.
4. **Guest details collected at booking time**: Unlike flights where passenger IDs come from the offer request, stays requires collecting guest names during `duffel_book_stay`.
5. **Stays requires separate API access**: Add a docstring note that `DUFFEL_API_KEY_LIVE` must have Stays permissions enabled (separate opt-in from Duffel at duffel.com/contact-us).

## Validation

After all tools are implemented, verify with:

```bash
# Should report 14 tools (11 existing + 3 new)
uv run python -c "import asyncio, duffel_mcp.server; m = duffel_mcp.server.mcp; tools = asyncio.run(m._local_provider._list_tools()); print(f'{len(tools)} tools loaded')"
```

Test with Duffel's test hotel in London (lat 51.5071, lon -0.1416) using a sandbox API key. Verify both markdown and JSON response formats for each tool.

**Note**: Tool 1 (`duffel_search_stays`) is already validated at 12 tools. Tools 2-3 will bring the total to 14.

## Out of Scope (Future)

- `duffel_cancel_stay_booking` — cancellation endpoint exists (`POST stays/bookings/{id}/actions/cancel`)
- `duffel_list_stay_bookings` — listing existing bookings (`GET stays/bookings`)
- `duffel_get_stay_booking` — get single booking detail (`GET stays/bookings/{id}`)
- Negotiated rates support — preview feature, requires enterprise setup
- Loyalty programme deep integration — optional enhancement
- Duffel Links for stays — if/when Duffel supports it
