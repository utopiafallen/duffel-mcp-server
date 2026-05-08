# Plan: Implement `duffel_get_stay_rates` (Tool 2)

## Overview

Add a single tool to `duffel_mcp/server.py` that fetches full room and rate details for a search result returned by `duffel_search_stays`. This is the second step in the 4-step Stays booking flow.

**API endpoint**: `POST stays/search_results/{search_result_id}/actions/fetch_all_rates` (empty body)

## What the tool does

- Takes a `search_result_id` (`srr_...`) from a previous `duffel_search_stays` call
- Calls the Duffel API to fetch all rooms and rates for that accommodation/date combination
- Returns formatted output with accommodation details, room options, bed configuration, rate options, cancellation policies, price breakdowns, and each rate's `rate_id` (needed for Tool 3: `duffel_book_stay`)

## New Pydantic Model

### `GetStayRatesInput`

Place after line 782 (after `SearchStaysInput`, before `CheckoutSession`).

```diff
--- a/duffel_mcp/server.py
+++ b/duffel_mcp/server.py
@@ -782,6 +782,15 @@ class SearchStaysInput(BaseModel):
         return self


+class GetStayRatesInput(BaseModel):
+    """Input model for fetching stay rates."""
+    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True)
+
+    search_result_id: str = Field(
+        ...,
+        description="Search result ID from duffel_search_stays (e.g., 'srr_0000ASVBuJVLdmqtZDJ4ca')",
+        pattern=r'^srr_'
+    )
+    response_format: ResponseFormat = Field(
+        default=ResponseFormat.MARKDOWN,
+        description="Output format: 'markdown' or 'json'"
+    )
+
+
 class CheckoutSession(BaseModel):
     """Stored checkout session data."""
```

**1 new model**, **0 validators** (pattern validation handles ID format).

## Tool Function

### `duffel_get_stay_rates`

Place immediately after `duffel_search_stays` at line 3136 (after the `except` block, before the blank line and next tool decorator).

```diff
--- a/duffel_mcp/server.py
+++ b/duffel_mcp/server.py
@@ -3135,6 +3135,140 @@ async def duffel_search_stays(params: SearchStaysInput, ctx: Context) -> str:
         return _handle_api_error(e, ctx)


+@mcp.tool(
+    name="duffel_get_stay_rates",
+    annotations={
+        "title": "Get Stay Room Rates",
+        "readOnlyHint": True,
+        "destructiveHint": False,
+        "idempotentHint": True,
+        "openWorldHint": True
+    }
+)
+async def duffel_get_stay_rates(params: GetStayRatesInput, ctx: Context) -> str:
+    """
+    Fetch detailed room and rate options for a hotel search result.
+
+    Use this after duffel_search_stays to get full details about rooms,
+    bed types, cancellation policies, and all available rates for a
+    specific accommodation. Each rate has a unique rate_id that can be
+    used with duffel_book_stay to make a booking.
+
+    Args:
+        params (GetStayRatesInput): Validated input parameters containing:
+            - search_result_id (str): Search result ID from duffel_search_stays
+            - response_format (ResponseFormat): Output format
+        ctx (Context): MCP context for progress and logging
+
+    Returns:
+        str: Formatted room and rate details with rate_ids for booking
+
+    Examples:
+        - Use when: "Show me the room options for that hotel"
+        - Use when: "What rates are available for srr_xxx?"
+        - Use when: "I want to see cancellation policies and bed types"
+    """
+    try:
+        logger.info("Fetching rates for search result %s", params.search_result_id)
+
+        await ctx.report_progress(0.2, "Fetching room and rate details...")
+
+        response = await _make_api_request(
+            ctx,
+            f"stays/search_results/{params.search_result_id}/actions/fetch_all_rates",
+            method="POST"
+        )
+
+        await ctx.report_progress(0.7, "Formatting response...")
+
+        if params.response_format == ResponseFormat.JSON:
+            result = json.dumps(response, indent=2)
+            return _truncate_if_needed(result, "stay rates")
+
+        # Markdown format
+        data = response.get("data", {})
+        accommodation = data.get("accommodation", {})
+        lines = ["# Room and Rate Details\n"]
+
+        # Accommodation overview
+        lines.append("## Accommodation")
+        lines.append(f"- **Name**: {accommodation.get('name', 'N/A')}")
+        rating = accommodation.get('rating')
+        if rating:
+            lines.append(f"- **Rating**: {'★' * rating} ({rating}/5)")
+        else:
+            lines.append("- **Rating**: unrated")
+        review_score = accommodation.get('review_score')
+        review_count = accommodation.get('review_count')
+        if review_score is not None:
+            lines.append(f"- **Reviews**: {review_score}/10 ({review_count or 0} reviews)")
+        
+        location = accommodation.get("location", {})
+        address = location.get("address", {})
+        city = address.get("city_name", "")
+        region = address.get("region", "")
+        country = address.get("country_code", "")
+        loc_parts = [p for p in [city, region, country] if p]
+        if loc_parts:
+            lines.append(f"- **Location**: {', '.join(loc_parts)}")
+        
+        check_in_info = accommodation.get("check_in_information", {})
+        check_in_after = check_in_info.get("check_in_after_time")
+        check_in_before = check_in_info.get("check_in_before_time")
+        check_out_before = check_in_info.get("check_out_before_time")
+        if check_in_after or check_in_before:
+            lines.append(f"- **Check-in**: {check_in_after or 'N/A'} - {check_in_before or 'N/A'}")
+        if check_out_before:
+            lines.append(f"- **Check-out**: before {check_out_before}")
+        
+        key_collection = accommodation.get("key_collection", {})
+        key_instructions = key_collection.get("instructions")
+        if key_instructions:
+            lines.append(f"- **Key Collection**: {key_instructions}")
+        
+        brand = accommodation.get("brand", {})
+        chain = accommodation.get("chain", {})
+        if brand.get("name"):
+            lines.append(f"- **Brand**: {brand['name']}")
+        if chain.get("name"):
+            lines.append(f"- **Chain**: {chain['name']}")
+        
+        loyalty = accommodation.get("supported_loyalty_programme")
+        if loyalty:
+            lines.append(f"- **Loyalty Programme**: {loyalty.replace('_', ' ').title()}")
+
+        # Search result metadata
+        lines.append(f"\n## Stay Details")
+        lines.append(f"- **Check-in**: {data.get('check_in_date', 'N/A')}")
+        lines.append(f"- **Check-out**: {data.get('check_out_date', 'N/A')}")
+        expires_at = data.get("expires_at")
+        if expires_at:
+            lines.append(f"- **Rates expire**: {_format_datetime(expires_at)}")
+
+        # Room and rate details
+        rooms = accommodation.get("rooms", [])
+        if not rooms:
+            lines.append("\n## No room options available")
+            lines.append("No rooms are currently available for these dates.")
+        else:
+            lines.append(f"\n## Rooms ({len(rooms)})")
+            
+            for room_idx, room in enumerate(rooms, 1):
+                lines.append(f"\n### Room {room_idx}: {room.get('name', 'N/A')}")
+                
+                # Bed configuration
+                beds = room.get("beds", [])
+                if beds:
+                    bed_parts = []
+                    for bed in beds:
+                        count = bed.get("count", 1)
+                        btype = bed.get("type", "unknown").replace("_", " ")
+                        bed_parts.append(f"{count}x {btype}")
+                    if bed_parts:
+                        lines.append(f"- **Beds**: {', '.join(bed_parts)}")
+                
+                # Room photos (first URL only)
+                room_photos = room.get("photos", [])
+                if room_photos:
+                    lines.append(f"- **Photos**: {len(room_photos)} available (first: {room_photos[0].get('url', 'N/A')})")
+                
+                # Rates for this room
+                rates = room.get("rates", [])
+                if not rates:
+                    lines.append("- **Rates**: No rates available")
+                else:
+                    lines.append(f"- **Available rates**: {len(rates)}")
+                    
+                    for rate_idx, rate in enumerate(rates, 1):
+                        lines.append(f"\n#### Rate {rate_idx}")
+                        lines.append(f"- **Rate ID**: `{rate.get('id', 'N/A')}` (use with duffel_book_stay)")
+                        rate_name = rate.get("name", "N/A")
+                        lines.append(f"- **Name**: {rate_name}")
+                        
+                        # Board type
+                        board_type = rate.get("board_type", "")
+                        if board_type:
+                            lines.append(f"- **Board Type**: {_format_board_type(board_type)}")
+                        
+                        # Payment type
+                        payment_type = rate.get("payment_type", "")
+                        if payment_type:
+                            pt_label = "Pay now" if payment_type == "pay_now" else "Pay at hotel"
+                            lines.append(f"- **Payment**: {pt_label}")
+                        
+                        # Price breakdown
+                        total_amount = rate.get("total_amount", "0")
+                        total_currency = rate.get("total_currency", "USD")
+                        base_amount = rate.get("base_amount")
+                        tax_amount = rate.get("tax_amount")
+                        fee_amount = rate.get("fee_amount")
+                        due_at_accommodation = rate.get("due_at_accommodation_amount")
+                        
+                        lines.append(f"\n**Price Breakdown:**")
+                        lines.append(f"- **Total**: {_format_price(total_amount, total_currency)}")
+                        if base_amount:
+                            lines.append(f"  - Base rate: {_format_price(base_amount, total_currency)}")
+                        if tax_amount:
+                            lines.append(f"  - Taxes: {_format_price(tax_amount, total_currency)}")
+                        if fee_amount:
+                            lines.append(f"  - Fees: {_format_price(fee_amount, total_currency)}")
+                        if due_at_accommodation:
+                            lines.append(f"  - Due at hotel: {_format_price(due_at_accommodation, total_currency)}")
+                        
+                        # Cancellation policy
+                        cancellation = rate.get("cancellation_timeline", [])
+                        if cancellation:
+                            first_refund = cancellation[0]
+                            refund_amount = first_refund.get("refund_amount", "0")
+                            refund_before = first_refund.get("before", "")
+                            is_free = (float(refund_amount) == float(total_amount)) if refund_amount and total_amount else False
+                            cancel_label = "Free cancellation" if is_free else f"Partial refund ({_format_price(refund_amount, total_currency)})"
+                            lines.append(f"- **Cancellation**: {cancel_label} (before {_format_datetime(refund_before)})")
+                        else:
+                            lines.append(f"- **Cancellation**: Non-refundable")
+                        
+                        # Quantity available
+                        qty = rate.get("quantity_available")
+                        if qty is not None:
+                            lines.append(f"- **Availability**: {qty} rooms left")
+                        
+                        # Rate conditions
+                        conditions = rate.get("conditions", [])
+                        if conditions:
+                            lines.append(f"- **Conditions**:")
+                            for cond in conditions[:5]:  # Limit to 5 conditions
+                                title = cond.get("title", "")
+                                desc = cond.get("description", "")
+                                lines.append(f"  - **{title}**: {desc}")
+                        
+                        # Loyalty
+                        rate_loyalty = rate.get("supported_loyalty_programme")
+                        if rate_loyalty:
+                            required = rate.get("loyalty_programme_required", False)
+                            loyalty_label = f"{rate_loyalty.replace('_', ' ').title()}" 
+                            if required:
+                                loyalty_label += " (required)"
+                            lines.append(f"- **Loyalty**: {loyalty_label}")
+                        
+                        # Rate description
+                        rate_desc = rate.get("description")
+                        if rate_desc:
+                            lines.append(f"- **Description**: {rate_desc}")
+
+        await ctx.report_progress(1.0, "Done")
+
+        logger.info("Retrieved rates for search result %s", params.search_result_id)
+
+        result = "\n".join(lines)
+        return _truncate_if_needed(result, "stay rates")
+
+    except Exception as e:
+        return _handle_api_error(e, ctx)
+
 
 @mcp.tool(
     name="duffel_flexible_search",
```

## Helper Function

### `_format_board_type`

A small helper to convert board type snake_case values to human-readable labels. Place near the existing `_format_price` function (~line 1039).

```diff
--- a/duffel_mcp/server.py
+++ b/duffel_mcp/server.py
@@ -1041,6 +1041,22 @@ def _format_price(amount: str, currency: str) -> str:
     return f"{currency} {amount}"


+def _format_board_type(board_type: str) -> str:
+    """Convert board type snake_case to human-readable label."""
+    board_type_labels = {
+        "room_only": "Room only",
+        "breakfast": "Breakfast included",
+        "half_board": "Half board (breakfast + dinner)",
+        "full_board": "Full board (all meals)",
+        "all_inclusive": "All inclusive",
+    }
+    return board_type_labels.get(board_type, board_type.replace("_", " ").title())
+
+
 def _format_datetime(dt_str: str) -> str:
     """Format ISO datetime to human-readable format."""
```

**Note**: The Duffel API docs reference these board types. If an unknown type appears, falls back to `board_type.replace("_", " ").title()`.

## Summary of Changes

| File | Change | Lines added | Lines removed |
|------|--------|-------------|---------------|
| `duffel_mcp/server.py` | `GetStayRatesInput` model | ~15 | 0 |
| `duffel_mcp/server.py` | `_format_board_type` helper | ~16 | 0 |
| `duffel_mcp/server.py` | `duffel_get_stay_rates` tool | ~140 | 0 |
| **Total** | | **~171** | **0** |

## Key Design Decisions

1. **Empty POST body**: The API takes a `POST` with no request body. No `json_data` parameter needed — `_make_api_request` sends the POST without a body, which is valid HTTP for this endpoint.

2. **Markdown structure**: Accommodation overview → stay dates → rooms → rates per room. Each rate shows its `rate_id` prominently (needed for Tool 3).

3. **Cancellation timeline**: Only shows the first (most generous) cancellation window, since the full timeline can be verbose. The "free cancellation" label is computed by comparing `refund_amount` to `total_amount`.

4. **Board type formatting**: Small lookup dict for known types; fallback to title-cased snake_case for unknowns.

5. **Payment type**: Maps `pay_now` → "Pay now", everything else → "Pay at hotel".

6. **Conditions limited to 5**: Rate conditions can be numerous; showing first 5 prevents excessive output.

7. **No client-side filtering/sorting**: Unlike Tool 1, this is a detail-fetch tool — shows all data as-is. The caller already selected the accommodation via search.

8. **`_truncate_if_needed(result, "stay rates")`**: Uses the section key for consistent truncation messaging.

## Validation

After implementation:
```bash
uv run python -c "import asyncio, duffel_mcp.server; m = duffel_mcp.server.mcp; tools = asyncio.run(m._local_provider._list_tools()); print(f'{len(tools)} tools loaded')"
```
Expected output: **13 tools** (12 existing + 1 new).

## Edge Cases Handled

- Empty rooms list → "No room options available" message
- Room with no rates → "No rates available" per room
- Missing optional fields (description, conditions, loyalty) → skipped silently
- `rating = null` → shows "unrated" with no stars
- Cancellation timeline empty → shows "Non-refundable"
- `quantity_available = null` → availability line omitted
