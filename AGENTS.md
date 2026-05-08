# AGENTS.md

## Structure

Everything lives in `duffel_mcp/server.py` (~4900 lines, 11 tools, 4 resources, 3 prompts). No other Python modules. Package config (`pyproject.toml`) is inside `duffel_mcp/`, **not** at repo root.

## Commands

```bash
# Install (must run from duffel_mcp/ or use --directory)
uv pip install -e duffel_mcp/

# Run stdio (default, for MCP clients like Claude Desktop)
python duffel_mcp/server.py

# Run SSE (web deployment, includes checkout routes)
python duffel_mcp/server.py --transport sse --host 0.0.0.0 --port 8080

# Quick validation
python -c "import duffel_mcp.server; print(f'{len(duffel_mcp.server.mcp._tool_manager._tools)} tools loaded')"

# Docker (SSE on port 8080)
docker build -t duffel-mcp . && docker run -p 8080:8080 -e DUFFEL_API_KEY_LIVE=xxx duffel-mcp
```

## Environment

| Variable | Required | Purpose |
|---|---|---|
| `DUFFEL_API_KEY_LIVE` | Yes | API key with flight + payment permissions |
| `CHECKOUT_BASE_URL` | No | Base URL for self-hosted checkout pages |
| `REDIS_URL` | No | Session persistence (falls back to in-memory) |

## Key Patterns

- **All logging goes to stderr** — stdout is reserved for the MCP stdio protocol
- **`ToolError` exception** sets the MCP `isError` flag; don't swallow it
- **Character limit 25000** — responses are truncated by `_truncate_if_needed()`
- **FastMCP SSE**: use `mcp.http_app(transport="sse")` (not `sse_app()`, which is deprecated)
- **`duffel_search_partial` requires 2+ slices** — round-trip/multi-city only, NOT one-way
- SSE transport combines MCP routes with Starlette checkout routes via `Mount` in `create_combined_app()`

## Booking Flows

1. **Duffel Links** (`duffel_get_booking_link`) — recommended; Duffel-hosted page handles everything
2. **Legacy Checkout** (`duffel_create_checkout`) — self-hosted, requires Duffel Payments enabled and `CHECKOUT_BASE_URL`

Static checkout HTML assets are in `duffel_mcp/static/`.

## No Test/Lint Infrastructure

There are no unit tests, linters, or type checkers. Validation is manual via the quick-import command above or the test scripts (`test_api_key.py`, `test_payments.py`).

## Deployment

Dockerfile hardcodes SSE transport on port 8080. Railway uses `railway.json` (DOCKERFILE builder). Render uses `render.yaml` (Python runtime, `$PORT` env). Fly.io uses `fly.toml` (port 8000).
