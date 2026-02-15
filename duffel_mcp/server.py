#!/usr/bin/env python3
"""
Duffel MCP Server

This server provides tools to search for flights, retrieve offers, and create bookings
using the Duffel API. It enables LLMs to help users find and book flights through
a compliant MCP interface.

Features:
- Flight search with optimization strategies (cheapest, fastest, best)
- Weighted scoring algorithm for finding optimal flights
- MCP Resources for airlines and airports
- MCP Prompts for common booking scenarios
- Progress reporting and structured logging via Context
- Persistent HTTP client via lifespan management
- Proper isError flag handling for tool errors
- Multiple transport support (stdio, HTTP with SSE)
"""

import os
import sys
import json
import re
import logging
import uuid
from typing import Optional, List, Dict, Any, TypedDict, Tuple, Union
from enum import Enum
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
import httpx
from pydantic import BaseModel, Field, field_validator, ConfigDict
from fastmcp import FastMCP, Context

# Starlette for checkout HTTP routes
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse
from starlette.routing import Route, Mount
from starlette.requests import Request
from pathlib import Path
import uvicorn

# Configure logging to stderr (stdout is reserved for MCP protocol)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stderr
)
logger = logging.getLogger("duffel_mcp")

# Constants
API_BASE_URL = "https://api.duffel.com"
API_VERSION = "v2"
CHARACTER_LIMIT = 25000
DEFAULT_TIMEOUT = 30.0

# Get API key from environment
DUFFEL_API_KEY = os.getenv("DUFFEL_API_KEY_LIVE", "")

# HTTP client headers for Duffel API
def _get_http_headers() -> Dict[str, str]:
    """Get headers for Duffel API requests."""
    return {
        "Authorization": f"Bearer {DUFFEL_API_KEY}",
        "Duffel-Version": API_VERSION,
        "Accept": "application/json",
        "Accept-Encoding": "gzip"
    }

# Initialize the MCP server
mcp = FastMCP("duffel_mcp")


# ============================================================================
# Error Result Helper
# ============================================================================

class ToolError(Exception):
    """Custom exception for tool errors that should set isError=True."""
    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        self.message = message
        self.details = details or {}
        super().__init__(message)


def _create_error_result(error_message: str, ctx: Optional[Context] = None) -> str:
    """
    Create an error result string.

    Note: FastMCP handles isError flag automatically when exceptions are raised.
    For explicit error control, we raise ToolError which FastMCP catches.
    """
    if ctx:
        try:
            # Log the error via context if available
            # Note: ctx.log methods may not be available in all FastMCP versions
            pass
        except AttributeError:
            pass
    return error_message

# ============================================================================
# Enums
# ============================================================================

class ResponseFormat(str, Enum):
    """Output format for tool responses."""
    MARKDOWN = "markdown"
    JSON = "json"

class CabinClass(str, Enum):
    """Cabin class options for flights."""
    ECONOMY = "economy"
    PREMIUM_ECONOMY = "premium_economy"
    BUSINESS = "business"
    FIRST = "first"

class PassengerType(str, Enum):
    """Passenger type options."""
    ADULT = "adult"
    CHILD = "child"
    INFANT_WITHOUT_SEAT = "infant_without_seat"

class PaymentType(str, Enum):
    """Payment type options."""
    BALANCE = "balance"
    ARC_BSP_CASH = "arc_bsp_cash"

class OptimizationStrategy(str, Enum):
    """Flight optimization strategies."""
    NONE = "none"           # Return as-is from API
    CHEAPEST = "cheapest"   # Sort by price ascending
    FASTEST = "fastest"     # Sort by total duration ascending
    LEAST_STOPS = "least_stops"  # Sort by number of connections
    BEST = "best"           # Weighted score algorithm
    EARLIEST = "earliest"   # Sort by departure time
    LATEST = "latest"       # Sort by departure time descending

class DepartureTimePreference(str, Enum):
    """Preferred departure time windows."""
    MORNING = "morning"       # 6am - 12pm
    AFTERNOON = "afternoon"   # 12pm - 6pm
    EVENING = "evening"       # 6pm - 12am
    RED_EYE = "red_eye"       # 12am - 6am

# ============================================================================
# Structured Output Types
# ============================================================================

class FlightOfferSummary(TypedDict):
    """Summary of a flight offer for structured output."""
    id: str
    price: str
    currency: str
    duration_minutes: int
    stops: int
    airline: str
    departure_time: str
    arrival_time: str
    score: Optional[float]

class SearchResultSummary(TypedDict):
    """Summary of search results."""
    offer_request_id: str
    total_offers: int
    cheapest: Optional[FlightOfferSummary]
    fastest: Optional[FlightOfferSummary]
    best: Optional[FlightOfferSummary]

# ============================================================================
# Pydantic Models for Input Validation
# ============================================================================

class OptimizationWeights(BaseModel):
    """Weights for the 'best' flight scoring algorithm."""
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True)

    price: float = Field(
        default=0.4,
        ge=0,
        le=1,
        description="Weight for price factor (0-1). Higher = price matters more."
    )
    duration: float = Field(
        default=0.3,
        ge=0,
        le=1,
        description="Weight for flight duration (0-1). Higher = shorter flights preferred."
    )
    stops: float = Field(
        default=0.2,
        ge=0,
        le=1,
        description="Weight for number of stops (0-1). Higher = fewer stops preferred."
    )
    departure_time: float = Field(
        default=0.1,
        ge=0,
        le=1,
        description="Weight for departure time preference (0-1). Higher = time preference matters more."
    )

class TimeRange(BaseModel):
    """Time range for departure/arrival filtering."""
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True)

    from_time: str = Field(
        ...,
        alias="from",
        description="Earliest acceptable time in HH:MM format (e.g., '09:00')",
        pattern=r'^([0-1]?[0-9]|2[0-3]):[0-5][0-9]$'
    )
    to_time: str = Field(
        ...,
        alias="to",
        description="Latest acceptable time in HH:MM format (e.g., '17:00')",
        pattern=r'^([0-1]?[0-9]|2[0-3]):[0-5][0-9]$'
    )

    model_config = ConfigDict(populate_by_name=True)

class FlightSlice(BaseModel):
    """A flight slice representing one leg of a journey."""
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True)

    origin: str = Field(
        ...,
        description="Origin airport IATA code (e.g., 'JFK', 'LHR', 'SGN')",
        min_length=3,
        max_length=3
    )
    destination: str = Field(
        ...,
        description="Destination airport IATA code (e.g., 'LAX', 'CDG', 'KUL')",
        min_length=3,
        max_length=3
    )
    departure_date: str = Field(
        ...,
        description="Departure date in YYYY-MM-DD format (e.g., '2025-11-21')",
        pattern=r'^\d{4}-\d{2}-\d{2}$'
    )
    departure_time: Optional[TimeRange] = Field(
        default=None,
        description="Filter for departure time window (e.g., {'from': '09:00', 'to': '17:00'})"
    )
    arrival_time: Optional[TimeRange] = Field(
        default=None,
        description="Filter for arrival time window (e.g., {'from': '12:00', 'to': '20:00'})"
    )

class PassengerInput(BaseModel):
    """Passenger information for flight search."""
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True)

    type: Optional[PassengerType] = Field(
        None,
        description="Passenger type: 'adult', 'child', or 'infant_without_seat'"
    )
    age: Optional[int] = Field(
        None,
        description="Age of passenger (use instead of type for children/infants)",
        ge=0,
        le=120
    )

    @field_validator('age', 'type')
    @classmethod
    def validate_passenger(cls, v: Any, info) -> Any:
        """Ensure either type or age is provided, not both."""
        values = info.data
        if 'type' in values and 'age' in values:
            if values.get('type') is not None and values.get('age') is not None:
                raise ValueError("Specify either 'type' or 'age', not both")
        return v

class SearchFlightsInput(BaseModel):
    """Input model for searching flights."""
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True)

    slices: List[FlightSlice] = Field(
        ...,
        description="Flight slices: one for one-way, two for round-trip",
        min_length=1,
        max_length=4
    )
    passengers: List[PassengerInput] = Field(
        ...,
        description="List of passengers",
        min_length=1,
        max_length=9
    )
    cabin_class: Optional[CabinClass] = Field(
        default=CabinClass.ECONOMY,
        description="Cabin class preference"
    )
    max_connections: Optional[int] = Field(
        default=None,
        description="Maximum number of connections (0 for non-stop, 1 for max one stop)",
        ge=0,
        le=3
    )
    return_offers: bool = Field(
        default=True,
        description="Whether to return offers immediately in the response"
    )
    # Optimization parameters
    optimization: OptimizationStrategy = Field(
        default=OptimizationStrategy.NONE,
        description="How to optimize/sort results: 'cheapest', 'fastest', 'best', 'least_stops', 'earliest', 'latest'"
    )
    optimization_weights: Optional[OptimizationWeights] = Field(
        default=None,
        description="Custom weights for 'best' optimization. Defaults: price=0.4, duration=0.3, stops=0.2, departure_time=0.1"
    )
    preferred_departure_time: Optional[DepartureTimePreference] = Field(
        default=None,
        description="Preferred departure window: 'morning' (6am-12pm), 'afternoon' (12pm-6pm), 'evening' (6pm-12am), 'red_eye' (12am-6am)"
    )
    top_n: Optional[int] = Field(
        default=None,
        description="Return only top N results after optimization",
        ge=1,
        le=50
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' or 'json'"
    )

class GetOfferInput(BaseModel):
    """Input model for retrieving a single offer."""
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True)

    offer_id: str = Field(
        ...,
        description="The unique offer ID (e.g., 'off_00009htYpSCXrwaB9DnUm0')",
        min_length=10
    )
    return_available_services: bool = Field(
        default=False,
        description="Include available services (baggage, seats, etc.)"
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' or 'json'"
    )

class ListOffersInput(BaseModel):
    """Input model for listing offers from an offer request."""
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True)

    offer_request_id: str = Field(
        ...,
        description="The offer request ID from a previous search",
        min_length=10
    )
    limit: Optional[int] = Field(
        default=20,
        description="Maximum number of offers to return",
        ge=1,
        le=200
    )
    max_connections: Optional[int] = Field(
        default=None,
        description="Filter by maximum connections",
        ge=0,
        le=3
    )
    sort: Optional[str] = Field(
        default="total_amount",
        description="Sort by 'total_amount' or 'total_duration' (prefix with '-' for descending)"
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' or 'json'"
    )

class AnalyzeOffersInput(BaseModel):
    """Input for analyzing and ranking existing offers."""
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True)

    offer_request_id: str = Field(
        ...,
        description="Offer request ID from a previous search",
        min_length=10
    )
    optimization: OptimizationStrategy = Field(
        default=OptimizationStrategy.BEST,
        description="Optimization strategy: 'cheapest', 'fastest', 'best', 'least_stops', 'earliest', 'latest'"
    )
    optimization_weights: Optional[OptimizationWeights] = Field(
        default=None,
        description="Custom weights for 'best' optimization"
    )
    preferred_departure_time: Optional[DepartureTimePreference] = Field(
        default=None,
        description="Preferred departure time window"
    )
    top_n: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Number of top results to return"
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' or 'json'"
    )

class GetSeatMapInput(BaseModel):
    """Input model for retrieving seat maps."""
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True)

    offer_id: str = Field(
        ...,
        description="The offer ID to get seat maps for",
        min_length=10
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' or 'json'"
    )

class OrderPassenger(BaseModel):
    """Passenger details for creating an order."""
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True)

    id: str = Field(..., description="Passenger ID from the offer request")
    given_name: str = Field(..., description="First/given name", min_length=1)
    family_name: str = Field(..., description="Last/family name", min_length=1)
    born_on: str = Field(
        ...,
        description="Date of birth in YYYY-MM-DD format",
        pattern=r'^\d{4}-\d{2}-\d{2}$'
    )
    email: str = Field(..., description="Email address", pattern=r'^[\w\.-]+@[\w\.-]+\.\w+$')
    phone_number: str = Field(..., description="Phone number with country code (e.g., '+14155552671')")
    title: str = Field(..., description="Title: 'mr', 'ms', 'mrs', 'miss', 'dr'")
    gender: str = Field(..., description="Gender: 'm' or 'f'", pattern=r'^[mf]$')
    infant_passenger_id: Optional[str] = Field(
        default=None,
        description="ID of infant if this passenger is responsible for one"
    )

class Payment(BaseModel):
    """Payment information for an order."""
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True)

    type: PaymentType = Field(..., description="Payment type: 'balance' or 'arc_bsp_cash'")
    amount: str = Field(..., description="Payment amount (e.g., '100.00')")
    currency: str = Field(..., description="Currency code (e.g., 'USD', 'GBP')", min_length=3, max_length=3)

class ServiceSelection(BaseModel):
    """A service to add to the booking (e.g., seat selection, extra baggage)."""
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True)

    id: str = Field(
        ...,
        description="Service ID from seat map or available services (e.g., 'ase_00009htYpSCXrwaB9')"
    )
    quantity: int = Field(
        default=1,
        description="Quantity of service (usually 1 for seats)",
        ge=1
    )

class CreateOrderInput(BaseModel):
    """Input model for creating an order/booking."""
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True)

    selected_offers: List[str] = Field(
        ...,
        description="List containing exactly one offer ID to book",
        min_length=1,
        max_length=1
    )
    passengers: List[OrderPassenger] = Field(
        ...,
        description="Complete passenger details for all travelers",
        min_length=1,
        max_length=9
    )
    payments: List[Payment] = Field(
        ...,
        description="Payment information (required for instant orders)",
        min_length=1,
        max_length=1
    )
    services: Optional[List[ServiceSelection]] = Field(
        default=None,
        description="Optional services to add (seats, bags). Get IDs from duffel_get_seat_map"
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' or 'json'"
    )

class ListAirlinesInput(BaseModel):
    """Input model for listing airlines."""
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True)

    limit: Optional[int] = Field(
        default=50,
        description="Maximum number of airlines to return",
        ge=1,
        le=200
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' or 'json'"
    )

class FlexibleDateSearchInput(BaseModel):
    """Input for searching flights across a range of dates to find the best deal."""
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True)

    origin: str = Field(
        ...,
        description="Origin airport IATA code (e.g., 'SGN', 'JFK')",
        min_length=3,
        max_length=3
    )
    destination: str = Field(
        ...,
        description="Destination airport IATA code (e.g., 'KUL', 'LAX')",
        min_length=3,
        max_length=3
    )
    departure_date: str = Field(
        ...,
        description="Target departure date in YYYY-MM-DD format",
        pattern=r'^\d{4}-\d{2}-\d{2}$'
    )
    return_date: Optional[str] = Field(
        default=None,
        description="Target return date in YYYY-MM-DD format (omit for one-way)",
        pattern=r'^\d{4}-\d{2}-\d{2}$'
    )
    flexibility_days: int = Field(
        default=3,
        description="Number of days to search before and after target dates (+/- N days)",
        ge=1,
        le=7
    )
    passengers: List[PassengerInput] = Field(
        default=[PassengerInput(type=PassengerType.ADULT)],
        description="List of passengers (defaults to 1 adult)",
        min_length=1,
        max_length=9
    )
    cabin_class: Optional[CabinClass] = Field(
        default=CabinClass.ECONOMY,
        description="Cabin class preference"
    )
    max_connections: Optional[int] = Field(
        default=None,
        description="Maximum connections (0 for non-stop only)",
        ge=0,
        le=2
    )


class SearchPartialInput(BaseModel):
    """Input for partial offer requests (mix-and-match legs from different airlines)."""
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True)

    slices: List[FlightSlice] = Field(
        ...,
        description="Flight slices: must have at least 2 for mix-and-match (e.g., outbound + return)",
        min_length=2,
        max_length=4
    )
    passengers: List[PassengerInput] = Field(
        default=[PassengerInput(type=PassengerType.ADULT)],
        description="List of passengers (defaults to 1 adult)",
        min_length=1,
        max_length=9
    )
    cabin_class: Optional[CabinClass] = Field(
        default=CabinClass.ECONOMY,
        description="Cabin class preference"
    )
    max_connections: Optional[int] = Field(
        default=None,
        description="Maximum connections per slice (0 for non-stop only)",
        ge=0,
        le=3
    )
    top_n: Optional[int] = Field(
        default=5,
        description="Number of top options to show per leg",
        ge=1,
        le=20
    )


# ============================================================================
# Checkout Flow Models and Session Storage
# ============================================================================

class CheckoutPassenger(BaseModel):
    """Passenger details for checkout."""
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True)

    id: str = Field(..., description="Passenger ID from the offer")
    given_name: str = Field(..., description="First/given name (as on ID)", min_length=1)
    family_name: str = Field(..., description="Last/family name (as on ID)", min_length=1)
    born_on: str = Field(
        ...,
        description="Date of birth in YYYY-MM-DD format",
        pattern=r'^\d{4}-\d{2}-\d{2}$'
    )
    email: str = Field(..., description="Email address", pattern=r'^[\w\.-]+@[\w\.-]+\.\w+$')
    phone_number: str = Field(..., description="Phone number with country code (e.g., '+14155552671')")
    title: str = Field(..., description="Title: 'mr', 'ms', 'mrs', 'miss', 'dr'")
    gender: str = Field(..., description="Gender: 'm' or 'f'", pattern=r'^[mf]$')


class CreateCheckoutInput(BaseModel):
    """Input model for creating a checkout session."""
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True)

    offer_id: str = Field(
        ...,
        description="The offer ID to book (e.g., 'off_00009htYpSCXrwaB9DnUm0')",
        min_length=10
    )
    passengers: List[CheckoutPassenger] = Field(
        ...,
        description="Complete passenger details for all travelers",
        min_length=1,
        max_length=9
    )


class CheckoutSession(BaseModel):
    """Stored checkout session data."""
    session_id: str
    offer_id: str
    offer_data: Dict[str, Any]  # Cached offer details
    passengers: Optional[List[CheckoutPassenger]] = None  # Collected on checkout page
    payment_intent_id: str
    client_token: str
    amount: str
    currency: str
    created_at: datetime
    expires_at: datetime
    status: str = "pending"  # pending, passengers_collected, paid, confirmed, failed, expired
    order_id: Optional[str] = None
    booking_reference: Optional[str] = None


# Duffel Payments fee (approximately 2.9% for cards)
DUFFEL_PAYMENTS_FEE_PERCENT = 0.029

# Checkout session TTL (matches typical offer expiry)
CHECKOUT_SESSION_TTL_MINUTES = 30

# Base URL for checkout links (set via environment or auto-detect)
CHECKOUT_BASE_URL = os.getenv("CHECKOUT_BASE_URL", "")

# Redis URL for session storage (optional - falls back to in-memory)
REDIS_URL = os.getenv("REDIS_URL", "")

# Duffel Links configuration
DUFFEL_LINKS_LOGO_URL = os.getenv("DUFFEL_LINKS_LOGO_URL", "")
DUFFEL_LINKS_PRIMARY_COLOR = os.getenv("DUFFEL_LINKS_PRIMARY_COLOR", "#354640")  # Black sheep green
DUFFEL_LINKS_SUCCESS_URL = os.getenv("DUFFEL_LINKS_SUCCESS_URL", "")
DUFFEL_LINKS_FAILURE_URL = os.getenv("DUFFEL_LINKS_FAILURE_URL", "")
DUFFEL_LINKS_ABANDONMENT_URL = os.getenv("DUFFEL_LINKS_ABANDONMENT_URL", "")

# Scanner/Attack Protection (enabled by default)
SCANNER_PROTECTION_ENABLED = os.getenv("SCANNER_PROTECTION_ENABLED", "true").lower() == "true"


# ============================================================================
# Session Storage (Redis with in-memory fallback)
# ============================================================================

class SessionStore:
    """Abstract session storage with Redis and in-memory implementations."""

    def __init__(self):
        self._redis_client = None
        self._memory_store: Dict[str, CheckoutSession] = {}
        self._use_redis = False

        if REDIS_URL:
            try:
                import redis
                self._redis_client = redis.from_url(
                    REDIS_URL,
                    decode_responses=True,
                    socket_connect_timeout=5
                )
                # Test connection
                self._redis_client.ping()
                self._use_redis = True
                logger.info("Redis session storage connected: %s", REDIS_URL.split("@")[-1] if "@" in REDIS_URL else REDIS_URL)
            except Exception as e:
                logger.warning("Redis connection failed, using in-memory storage: %s", str(e))
                self._use_redis = False

        if not self._use_redis:
            logger.info("Using in-memory session storage")

    def _session_key(self, session_id: str) -> str:
        """Generate Redis key for a session."""
        return f"checkout_session:{session_id}"

    def _serialize_session(self, session: CheckoutSession) -> str:
        """Serialize session to JSON for Redis storage."""
        data = session.model_dump()
        # Convert datetime objects to ISO format strings
        data["created_at"] = session.created_at.isoformat()
        data["expires_at"] = session.expires_at.isoformat()
        return json.dumps(data)

    def _deserialize_session(self, data: str) -> CheckoutSession:
        """Deserialize session from JSON."""
        parsed = json.loads(data)
        # Convert ISO strings back to datetime
        parsed["created_at"] = datetime.fromisoformat(parsed["created_at"])
        parsed["expires_at"] = datetime.fromisoformat(parsed["expires_at"])
        # Convert passenger dicts back to CheckoutPassenger objects
        parsed["passengers"] = [CheckoutPassenger(**p) for p in parsed["passengers"]] if parsed.get("passengers") else None
        return CheckoutSession(**parsed)

    def save(self, session: CheckoutSession) -> None:
        """Save a checkout session."""
        if self._use_redis:
            try:
                ttl_seconds = CHECKOUT_SESSION_TTL_MINUTES * 60
                self._redis_client.setex(
                    self._session_key(session.session_id),
                    ttl_seconds,
                    self._serialize_session(session)
                )
                logger.debug("Session saved to Redis: %s", session.session_id)
            except Exception as e:
                logger.error("Redis save failed, falling back to memory: %s", str(e))
                self._memory_store[session.session_id] = session
        else:
            self._memory_store[session.session_id] = session

    def get(self, session_id: str) -> Optional[CheckoutSession]:
        """Get a checkout session, checking for expiry."""
        if self._use_redis:
            try:
                data = self._redis_client.get(self._session_key(session_id))
                if data:
                    session = self._deserialize_session(data)
                    # Redis TTL handles expiry, but double-check
                    if session.expires_at < datetime.utcnow():
                        self.delete(session_id)
                        return None
                    return session
                return None
            except Exception as e:
                logger.error("Redis get failed: %s", str(e))
                return self._memory_store.get(session_id)
        else:
            session = self._memory_store.get(session_id)
            if session and session.expires_at < datetime.utcnow():
                session.status = "expired"
                del self._memory_store[session_id]
                return None
            return session

    def update(self, session: CheckoutSession) -> None:
        """Update an existing session (same as save, preserves TTL in Redis)."""
        if self._use_redis:
            try:
                # Get remaining TTL
                ttl = self._redis_client.ttl(self._session_key(session.session_id))
                if ttl > 0:
                    self._redis_client.setex(
                        self._session_key(session.session_id),
                        ttl,
                        self._serialize_session(session)
                    )
                else:
                    # Session expired, save with new TTL
                    self.save(session)
                logger.debug("Session updated in Redis: %s", session.session_id)
            except Exception as e:
                logger.error("Redis update failed: %s", str(e))
                self._memory_store[session.session_id] = session
        else:
            self._memory_store[session.session_id] = session

    def delete(self, session_id: str) -> None:
        """Delete a checkout session."""
        if self._use_redis:
            try:
                self._redis_client.delete(self._session_key(session_id))
            except Exception as e:
                logger.error("Redis delete failed: %s", str(e))
        if session_id in self._memory_store:
            del self._memory_store[session_id]


# Global session store instance
session_store = SessionStore()

# ============================================================================
# Shared Utility Functions
# ============================================================================

async def _make_api_request(
    ctx: Context,
    endpoint: str,
    method: str = "GET",
    params: Optional[Dict[str, Any]] = None,
    json_data: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Make an authenticated request to the Duffel API."""
    if not DUFFEL_API_KEY:
        logger.error("API key not configured")
        raise ToolError("DUFFEL_API_KEY_LIVE environment variable is not set")

    headers = _get_http_headers()
    if json_data:
        headers["Content-Type"] = "application/json"

    logger.debug("API request: %s /%s", method, endpoint)

    async with httpx.AsyncClient(
        base_url=API_BASE_URL,
        timeout=DEFAULT_TIMEOUT
    ) as client:
        response = await client.request(
            method,
            f"/{endpoint}",
            headers=headers,
            params=params,
            json=json_data
        )

        logger.debug("API response: %s %s", response.status_code, endpoint)
        response.raise_for_status()
        return response.json()

def _handle_api_error(e: Exception, ctx: Optional[Context] = None) -> str:
    """Format API errors consistently and log them."""
    error_message = ""

    if isinstance(e, ToolError):
        error_message = e.message
        logger.error("Tool error: %s", error_message)
    elif isinstance(e, httpx.HTTPStatusError):
        status = e.response.status_code
        try:
            error_data = e.response.json()
            if "errors" in error_data and error_data["errors"]:
                error = error_data["errors"][0]
                message = error.get("message", "Unknown error")
                code = error.get("code", "unknown")
                error_message = f"Error ({status}): {message} [Code: {code}]"
                logger.error("API error %s: %s [%s]", status, message, code)
        except:
            pass

        if not error_message:
            if status == 401:
                error_message = "Error: Authentication failed. Please check your API key has the required permissions."
            elif status == 403:
                error_message = "Error: Permission denied. Your API key lacks the required permissions for this operation."
            elif status == 404:
                error_message = "Error: Resource not found. Please check the ID is correct."
            elif status == 422:
                error_message = "Error: Validation failed. Please check your input parameters."
            elif status == 429:
                error_message = "Error: Rate limit exceeded. Please wait before making more requests."
            else:
                error_message = f"Error: API request failed with status {status}"
            logger.error("HTTP error %s: %s", status, error_message)
    elif isinstance(e, httpx.TimeoutException):
        error_message = "Error: Request timed out. Please try again."
        logger.error("Request timeout: %s", str(e))
    elif isinstance(e, ValueError):
        error_message = f"Error: {str(e)}"
        logger.error("Validation error: %s", str(e))
    else:
        error_message = f"Error: Unexpected error occurred: {type(e).__name__}: {str(e)}"
        logger.exception("Unexpected error: %s", str(e))

    return error_message

def _format_price(amount: str, currency: str) -> str:
    """Format price consistently."""
    return f"{currency} {amount}"

def _format_datetime(dt_str: str) -> str:
    """Format ISO datetime to human-readable format."""
    try:
        dt = datetime.fromisoformat(dt_str.replace('Z', '+00:00'))
        return dt.strftime("%Y-%m-%d %H:%M %Z").strip()
    except:
        return dt_str

def _truncate_if_needed(content: str, data_description: str = "results") -> str:
    """Truncate content if it exceeds CHARACTER_LIMIT."""
    if len(content) > CHARACTER_LIMIT:
        truncated = content[:CHARACTER_LIMIT]
        truncated += f"\n\n**[Truncated]** Response exceeded {CHARACTER_LIMIT} characters. Use filters or pagination to see more {data_description}."
        return truncated
    return content

# ============================================================================
# Flight Optimization Helpers
# ============================================================================

def _parse_duration_minutes(offer: Dict[str, Any]) -> int:
    """Parse total duration from an offer in minutes."""
    total_minutes = 0
    for slice_data in offer.get("slices", []):
        duration_str = slice_data.get("duration", "PT0H0M")
        # Parse ISO 8601 duration (e.g., "PT2H30M")
        match = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?', duration_str)
        if match:
            hours = int(match.group(1) or 0)
            minutes = int(match.group(2) or 0)
            total_minutes += hours * 60 + minutes
    return total_minutes

def _extract_baggage_info(offer: Dict[str, Any]) -> str:
    """Extract baggage allowance info from offer.

    Returns a concise string like "✅ 1x checked bag" or "⚠️ Carry-on only"
    """
    checked_bags = 0
    carry_on = 0

    for slice_data in offer.get("slices", []):
        for segment in slice_data.get("segments", []):
            for passenger in segment.get("passengers", []):
                for baggage in passenger.get("baggages", []):
                    bag_type = baggage.get("type", "")
                    quantity = baggage.get("quantity", 0)
                    if bag_type == "checked":
                        checked_bags = max(checked_bags, quantity)
                    elif bag_type == "carry_on":
                        carry_on = max(carry_on, quantity)

    if checked_bags > 0:
        return f"✅ {checked_bags}x checked bag included"
    elif carry_on > 0:
        return "⚠️ Carry-on only (checked bags extra)"
    else:
        return "⚠️ Baggage info not available"

def _extract_fare_conditions(offer: Dict[str, Any]) -> Dict[str, str]:
    """Extract fare conditions (change/refund policies) from offer.

    Returns dict with 'change' and 'refund' policy strings.
    """
    conditions = offer.get("conditions", {})
    result = {"change": "Unknown", "refund": "Unknown"}

    # Change policy
    change = conditions.get("change_before_departure")
    if change:
        if change.get("allowed"):
            penalty = change.get("penalty_amount")
            currency = change.get("penalty_currency", "")
            if penalty and float(penalty) > 0:
                result["change"] = f"✅ Changes allowed ({currency} {penalty} fee)"
            else:
                result["change"] = "✅ Free changes"
        else:
            result["change"] = "❌ No changes allowed"

    # Refund policy
    refund = conditions.get("refund_before_departure")
    if refund:
        if refund.get("allowed"):
            penalty = refund.get("penalty_amount")
            currency = refund.get("penalty_currency", "")
            if penalty and float(penalty) > 0:
                result["refund"] = f"✅ Refundable ({currency} {penalty} fee)"
            else:
                result["refund"] = "✅ Fully refundable"
        else:
            result["refund"] = "❌ Non-refundable"

    return result

def _format_fare_conditions_brief(offer: Dict[str, Any]) -> str:
    """Format fare conditions as a brief one-liner."""
    conditions = _extract_fare_conditions(offer)
    parts = []
    if "✅" in conditions["refund"]:
        parts.append("Refundable")
    else:
        parts.append("Non-refundable")
    if "✅" in conditions["change"]:
        parts.append("Changeable")
    return " | ".join(parts) if parts else "Conditions unknown"

def _layover_quality_score(offer: Dict[str, Any]) -> float:
    """Score layover quality (0-1, higher is better). Direct flights = 1.0."""
    connection_scores = []
    for slice_data in offer.get("slices", []):
        segments = slice_data.get("segments", [])
        for i in range(len(segments) - 1):
            try:
                arr = datetime.fromisoformat(segments[i].get("arriving_at", "").replace("Z", "+00:00"))
                dep = datetime.fromisoformat(segments[i + 1].get("departing_at", "").replace("Z", "+00:00"))
                conn_minutes = (dep - arr).total_seconds() / 60
            except (ValueError, TypeError):
                connection_scores.append(0.5)
                continue

            if conn_minutes < 45:
                score = 0.2
            elif conn_minutes < 60:
                score = 0.5
            elif conn_minutes < 90:
                score = 0.9
            elif conn_minutes <= 180:
                score = 1.0
            elif conn_minutes <= 300:
                score = 0.7
            elif conn_minutes <= 480:
                score = 0.4
            else:
                score = 0.2
            connection_scores.append(score)

    if not connection_scores:
        return 1.0  # Direct flight
    return sum(connection_scores) / len(connection_scores)


def _count_stops(offer: Dict[str, Any]) -> int:
    """Count total stops across all slices."""
    total_stops = 0
    for slice_data in offer.get("slices", []):
        segments = slice_data.get("segments", [])
        total_stops += max(0, len(segments) - 1)
    return total_stops

def _get_departure_hour(offer: Dict[str, Any]) -> int:
    """Get the departure hour of the first segment."""
    try:
        slices = offer.get("slices", [])
        if slices:
            segments = slices[0].get("segments", [])
            if segments:
                departing_at = segments[0].get("departing_at", "")
                dt = datetime.fromisoformat(departing_at.replace('Z', '+00:00'))
                return dt.hour
    except:
        pass
    return 12  # Default to noon

def _normalize(value: float, min_val: float, max_val: float) -> float:
    """Normalize value to 0-1 range."""
    if max_val == min_val:
        return 0.5
    return (value - min_val) / (max_val - min_val)

def _calculate_time_preference_score(hour: int, preference: Optional[str]) -> float:
    """Score departure time based on preference (0-1, higher is better)."""
    if not preference:
        if 0 <= hour < 5:
            return 0.3  # Red-eye penalty
        return 0.5  # Neutral

    ranges = {
        "morning": (6, 12),
        "afternoon": (12, 18),
        "evening": (18, 24),
        "red_eye": (0, 6)
    }

    preferred_start, preferred_end = ranges.get(preference, (0, 24))
    if preferred_start <= hour < preferred_end:
        return 1.0  # Perfect match

    # Calculate distance from preferred range
    distance = min(abs(hour - preferred_start), abs(hour - preferred_end))
    return max(0, 1 - (distance / 12))  # Decay over 12 hours

def calculate_flight_score(
    offer: Dict[str, Any],
    all_offers: List[Dict[str, Any]],
    weights: OptimizationWeights,
    preferred_departure: Optional[str] = None
) -> float:
    """
    Calculate normalized score (0-100, higher is better).

    Normalization: Each factor is normalized to 0-1 range relative to all offers,
    then weighted and combined.
    """
    # Extract values
    price = float(offer.get("total_amount", 0))
    duration = _parse_duration_minutes(offer)
    stops = _count_stops(offer)
    departure_hour = _get_departure_hour(offer)

    # Get min/max for normalization
    prices = [float(o.get("total_amount", 0)) for o in all_offers]
    durations = [_parse_duration_minutes(o) for o in all_offers]
    stops_list = [_count_stops(o) for o in all_offers]

    # Normalize (invert so lower is better becomes higher score)
    price_score = 1 - _normalize(price, min(prices), max(prices)) if prices else 0.5
    raw_duration_score = 1 - _normalize(duration, min(durations), max(durations)) if durations else 0.5
    layover_score = _layover_quality_score(offer)
    duration_score = 0.7 * raw_duration_score + 0.3 * layover_score
    stops_score = 1 - _normalize(stops, min(stops_list), max(stops_list)) if stops_list else 0.5
    time_score = _calculate_time_preference_score(departure_hour, preferred_departure)

    # Weighted combination
    total = (
        weights.price * price_score +
        weights.duration * duration_score +
        weights.stops * stops_score +
        weights.departure_time * time_score
    )

    return round(total * 100, 2)

def _optimize_offers(
    offers: List[Dict[str, Any]],
    strategy: OptimizationStrategy,
    weights: Optional[OptimizationWeights] = None,
    preferred_departure: Optional[str] = None,
    top_n: Optional[int] = None
) -> List[Dict[str, Any]]:
    """Apply optimization strategy to offers and return sorted list."""
    if not offers or strategy == OptimizationStrategy.NONE:
        return offers[:top_n] if top_n else offers

    if weights is None:
        weights = OptimizationWeights()

    # Calculate scores for all offers if using BEST strategy
    if strategy == OptimizationStrategy.BEST:
        for offer in offers:
            offer["_score"] = calculate_flight_score(offer, offers, weights, preferred_departure)

    # Sort based on strategy
    if strategy == OptimizationStrategy.CHEAPEST:
        sorted_offers = sorted(offers, key=lambda x: float(x.get("total_amount", 0)))
    elif strategy == OptimizationStrategy.FASTEST:
        sorted_offers = sorted(offers, key=lambda x: _parse_duration_minutes(x))
    elif strategy == OptimizationStrategy.LEAST_STOPS:
        sorted_offers = sorted(offers, key=lambda x: _count_stops(x))
    elif strategy == OptimizationStrategy.EARLIEST:
        sorted_offers = sorted(offers, key=lambda x: _get_departure_hour(x))
    elif strategy == OptimizationStrategy.LATEST:
        sorted_offers = sorted(offers, key=lambda x: _get_departure_hour(x), reverse=True)
    elif strategy == OptimizationStrategy.BEST:
        sorted_offers = sorted(offers, key=lambda x: x.get("_score", 0), reverse=True)
    else:
        sorted_offers = offers

    return sorted_offers[:top_n] if top_n else sorted_offers

def _get_offer_summary(offer: Dict[str, Any]) -> FlightOfferSummary:
    """Extract summary from an offer."""
    slices = offer.get("slices", [])
    first_segment = slices[0].get("segments", [{}])[0] if slices else {}
    last_slice = slices[-1] if slices else {}
    last_segment = last_slice.get("segments", [{}])[-1] if last_slice else {}

    return FlightOfferSummary(
        id=offer.get("id", ""),
        price=offer.get("total_amount", "0"),
        currency=offer.get("total_currency", "USD"),
        duration_minutes=_parse_duration_minutes(offer),
        stops=_count_stops(offer),
        airline=offer.get("owner", {}).get("name", "Unknown"),
        departure_time=first_segment.get("departing_at", ""),
        arrival_time=last_segment.get("arriving_at", ""),
        score=offer.get("_score")
    )

# ============================================================================
# MCP Resources
# ============================================================================

@mcp.resource("duffel://airlines")
async def list_airlines_resource(ctx: Context) -> str:
    """
    List of all available airlines for booking through Duffel.

    Returns airline names, IATA codes, and logos for reference when
    searching for flights or filtering results.
    """
    try:
        logger.debug("Resource request: duffel://airlines")
        response = await _make_api_request(ctx, "air/airlines", params={"limit": "200"})
        logger.info("Airlines resource: returned %d airlines", len(response.get("data", [])))
        return json.dumps(response, indent=2)
    except Exception as e:
        logger.error("Airlines resource error: %s", str(e))
        return json.dumps({"error": str(e)})

@mcp.resource("duffel://airlines/{iata_code}")
async def get_airline_resource(iata_code: str, ctx: Context) -> str:
    """
    Details for a specific airline by IATA code.

    Use this to get information about a particular airline including
    their name, logo, and conditions of carriage.
    """
    try:
        logger.debug("Resource request: duffel://airlines/%s", iata_code)
        # The Duffel API uses airline IDs, not IATA codes directly
        # We need to search for the airline by IATA code
        response = await _make_api_request(ctx, "air/airlines", params={"limit": "200"})
        airlines = response.get("data", [])
        for airline in airlines:
            if airline.get("iata_code", "").upper() == iata_code.upper():
                logger.info("Found airline %s: %s", iata_code, airline.get("name", "Unknown"))
                return json.dumps({"data": airline}, indent=2)
        logger.warning("Airline not found: %s", iata_code)
        return json.dumps({"error": f"Airline with IATA code '{iata_code}' not found"})
    except Exception as e:
        logger.error("Airline resource error: %s", str(e))
        return json.dumps({"error": str(e)})

@mcp.resource("duffel://places/{query}")
async def search_places_resource(query: str, ctx: Context) -> str:
    """
    Search for airports and cities by name or code.

    Use this to find IATA codes for airports when the user provides
    a city name or partial airport code.
    """
    try:
        logger.debug("Resource request: duffel://places/%s", query)
        response = await _make_api_request(
            ctx,
            "air/places/suggestions",
            params={"query": query}
        )
        logger.info("Places search '%s': returned %d results", query, len(response.get("data", [])))
        return json.dumps(response, indent=2)
    except Exception as e:
        logger.error("Places resource error: %s", str(e))
        return json.dumps({"error": str(e)})

@mcp.resource("duffel://instructions")
def flight_search_instructions() -> str:
    """
    Instructions for the AI travel agent on how to search effectively.
    Includes current date/time for context.
    """
    # Get current date/time for the AI
    now = datetime.utcnow()
    current_date = now.strftime("%Y-%m-%d")
    current_time = now.strftime("%H:%M UTC")
    current_year = now.year
    current_month = now.strftime("%B")

    return f"""# AI Travel Agent Guidelines

## IMPORTANT: Current Date & Time

**Today's Date: {current_date}**
**Current Time: {current_time}**
**Current Year: {current_year}**

When users mention months like "February" or "next month", always use {current_year} or later.
Flight searches must use dates AFTER {current_date}.

Example: If user says "February" and today is November {current_year}, search for February {current_year + 1}.

## Be a Smart Agent, Not a Questionnaire

You are a helpful travel agent. Don't interrogate the customer with many questions.
Instead, be proactive: search comprehensively and present findings with smart advice.

### Only Ask When Truly Needed:
- **Departure city** - if not clear from context (but infer from their location if known)
- **Approximate dates** - if not mentioned at all
- **Trip length** - only if they said round-trip but didn't mention return

### DON'T Ask About (Just Handle It):
- Luggage: Search and NOTE in results if baggage isn't included
- Connections: Show both options and note "direct" vs "1 stop (2hr layover)"
- Time of day: Show a range of options
- Cabin class: Default to economy unless they mention otherwise
- Airline preferences: Show what's available, note if budget carrier

## City Codes: Search All Airports at Once

When the user mentions a city (not a specific airport), use the **city IATA code** to search all airports:

| City | Code | Airports Covered |
|------|------|-----------------|
| New York | NYC | JFK, EWR, LGA |
| London | LON | LHR, LGW, STN, LTN, SEN |
| Paris | PAR | CDG, ORY |
| Tokyo | TYO | NRT, HND |
| Chicago | CHI | ORD, MDW |
| Washington DC | WAS | IAD, DCA, BWI |
| Milan | MIL | MXP, LIN |
| Rome | ROM | FCO, CIA |
| Seoul | SEL | ICN, GMP |
| Shanghai | SHA | PVG, SHA |
| Buenos Aires | BUE | EZE, AEP |
| Sao Paulo | SAO | GRU, CGH |
| Stockholm | STO | ARN, BMA |
| Moscow | MOW | SVO, DME, VKO |

**Examples:**
- User says "flights from New York" → search with origin `NYC`
- User says "flights from JFK" → search with origin `JFK` (they specified)
- User says "flights to London" → search with destination `LON`

This finds cheaper options the user might miss if you only search one airport.

## Proactive Search Strategy

When user wants the "cheapest" or "best deal":
1. **Search multiple dates automatically** (+/- 3 days from target)
2. **Compare and present** the best options found
3. **Advise** on trade-offs ("Flying Dec 23 instead of Dec 24 saves $85")

## Smart Result Presentation

For each option, include relevant warnings inline:

**Good Example:**
"✈️ **$301** - Malaysia Airlines (MH751)
- Dec 24: SGN 11:00 → KUL 14:10 (direct, 2h10m)
- Dec 26: KUL 17:10 → SGN 18:10 (direct, 2h)
- ✅ 20kg checked bag included"

"✈️ **$189** - AirAsia (AK857)
- Dec 24: SGN 08:30 → KUL 11:45 (direct, 2h15m)
- Dec 26: KUL 19:00 → SGN 20:05 (direct, 2h5m)
- ⚠️ Carry-on only - checked bag +$35 each way"

"✈️ **$156** - VietJet + Firefly
- Dec 24: SGN 06:00 → KUL 15:30 (1 stop, 5h layover in SIN)
- ⚠️ Long layover, separate tickets, bags not included"

## Key Behaviors

1. **Infer intelligently** - Use context about the user
2. **Search broadly** - Multiple dates, multiple airlines
3. **Advise clearly** - Note trade-offs, hidden costs, long layovers
4. **Be concise** - Present findings, don't ask unnecessary questions
5. **Recommend** - "I'd suggest the Malaysia Airlines option - only $45 more but includes bags and better times"

## When to Ask vs Infer

| Situation | Action |
|-----------|--------|
| User says "cheapest to Paris" | Search +/- 3 days, present options |
| User says "Christmas in Tokyo" | Infer Dec 24-26ish, ask how many days |
| User mentions budget | Prioritize price, warn about fees |
| User mentions "quick trip" | Note total travel times prominently |
| User is vague about dates | Ask for approximate timeframe |

## Trip Type: One-Way vs Round-Trip

**Infer trip type from context — don't always ask:**

| Signal | Infer As | Example |
|--------|----------|---------|
| "flight to X" (no return mentioned) | **One-way** | "Flight from NYC to LAX on March 5" |
| "trip to X", "going to X and back" | **Round-trip** | "Trip to London next week" |
| "visiting for N days/weeks" | **Round-trip** (infer return date) | "Visiting Paris for 5 days starting March 10" → return March 15 |
| "moving to", "relocating" | **One-way** | "Moving to Berlin in April" |
| Mentions return date | **Round-trip** | "NYC to LAX Dec 20, back Dec 27" |

**When unclear:** Default to round-trip and ask for trip length ("How many days are you staying?"), NOT "Is this one-way or round-trip?"

## Tool Selection Guide

**Step 1: What does the user want?**

```
User request
├─ "Book a flight" / "Ready to book"
│   └─ duffel_get_booking_link
├─ "Details on offer off_xxx"
│   └─ duffel_get_offer
├─ "Seat map"
│   └─ duffel_get_seat_map
└─ Searching for flights? → Step 2
```

**Step 2: Is price the priority?**

```
Price-focused? ("cheapest", "best deal", "most affordable", "budget")
├─ YES, and dates are flexible
│   └─ duffel_flexible_search (searches +/- 3 days automatically)
│      Works for both one-way and round-trip
├─ YES, and it's a round-trip
│   └─ duffel_search_partial (mix-and-match airlines per leg)
│      Often finds cheaper combos than bundled round-trips
└─ NO (specific date, "find me flights", comparing options)
    └─ duffel_search_flights
       Works for one-way (1 slice), round-trip (2 slices), multi-city (3+ slices)
```

**Step 3: Can you combine tools?**

For the most thorough price search on round-trips:
1. `duffel_flexible_search` → find cheapest dates
2. `duffel_search_partial` → check if mixing airlines saves more on those dates

| User Request | Tool | Why |
|--------------|------|-----|
| "Cheapest flight to X" | `duffel_flexible_search` | Auto-searches +/- 3 days |
| "One-way to LAX on March 5" | `duffel_search_flights` (1 slice) | Specific date, one-way |
| "Round-trip NYC-LON, Dec 20-27" | `duffel_search_flights` (2 slices) | Specific dates |
| "Cheapest round-trip to Tokyo" | `duffel_flexible_search` then `duffel_search_partial` | Flexible dates + mix airlines |
| "Mix airlines for best price" | `duffel_search_partial` | Explicit mix-and-match request |
| "Multi-city: NYC→LON→PAR→NYC" | `duffel_search_flights` (3 slices) | Multi-city itinerary |
| "Best deal around Christmas" | `duffel_flexible_search` | Vague dates, price focus |
| "Compare options for next Friday" | `duffel_search_flights` with `best` optimization | Specific date, wants comparison |

## Key Tools

### duffel_flexible_search
Searches +/- N days automatically. Works for **one-way** (omit return_date) and **round-trip**.
Best for: price-focused searches with date flexibility.

### duffel_search_partial
Mix-and-match airlines per leg. **Round-trip and multi-city only** (requires 2+ slices).
Best for: finding the cheapest airline combination when the user doesn't care about same-airline booking.

### duffel_search_flights
General-purpose search. Works for **one-way, round-trip, and multi-city**.
Best for: specific dates, comparing options, or when the user just says "find me flights."

## Booking Flow - IMPORTANT

When the user wants to book:
1. Use `duffel_get_booking_link` to get a branded booking page URL
2. Give them the booking link
3. They search, select, enter details, and pay ON THE BOOKING PAGE
4. DO NOT ask for their name, DOB, email, phone in chat - the booking page handles everything!

**Good response:**
"Here's your booking link: [URL]
This opens a professional booking page where you can search flights, enter your details, and pay securely."

**Bad response (DON'T DO THIS):**
"To book, I'll need your full name, date of birth, email, and phone number..."

## Optimization Strategies (for duffel_search_flights)

- `cheapest`: Find lowest price
- `fastest`: Shortest total travel time
- `best`: Balanced score (good for general "find me flights")
- `least_stops`: Prioritize direct flights
- `earliest`/`latest`: Time-of-day preference

## IMPORTANT: API Rate Limits

The Duffel API has rate limits. If you make requests too quickly, you'll get a 429 error.

**Rate Limit Guidelines:**
- **Window**: 60 seconds
- **Best Practice**: Add a brief pause (1-2 seconds) between consecutive API calls
- **If rate limited**: Wait 60 seconds before retrying
- **Flexible search**: This makes multiple API calls internally - use it sparingly

**When doing multiple searches:**
1. Prefer `duffel_flexible_search` over multiple `duffel_search_flights` calls
2. If you must do sequential searches, wait 1-2 seconds between them
3. If you get a rate limit error (429), pause for 60 seconds then retry

**Example - searching multiple routes:**
```
Search 1: SGN -> KUL
[wait 1-2 seconds]
Search 2: SGN -> BKK
[wait 1-2 seconds]
Search 3: SGN -> SIN
```

This prevents rate limit errors and ensures smooth operation.
"""

# ============================================================================
# MCP Prompts
# ============================================================================

@mcp.prompt("book_round_trip")
def book_round_trip_prompt(
    origin: str = "JFK",
    destination: str = "LHR",
    departure_date: str = "2025-01-15",
    return_date: str = "2025-01-22",
    passengers: str = "1 adult"
) -> str:
    """
    Smart workflow for booking a round-trip flight.
    Be helpful, not interrogative. Search proactively and advise.
    """
    return f"""# Round-Trip Flight Booking

## Trip: {origin} → {destination}
- Outbound: {departure_date}
- Return: {return_date}
- Passengers: {passengers}

## Your Approach

### 1. Search Proactively
- Search the requested dates immediately
- If looking for best deal, also search +/- 2-3 days and compare
- Use `optimization: "best"` for balanced results

### 2. Present Options with Advice
Show top options with inline notes:
- Price and what's included (bags, changes)
- Flight times and duration
- Direct vs connections (note layover length if long)
- Budget carrier warnings if applicable

### 3. Make a Recommendation
"Based on your trip, I'd recommend [option] because..."

### 4. Only Ask If Truly Needed
- Missing departure city → ask
- Vague dates ("sometime in December") → ask for range
- Everything else → search and advise

### 5. Book When Ready
When they're ready to book, use `duffel_get_booking_link` to get a branded booking page.
The booking page handles everything - search, selection, passenger details, and payment.

Just say: "Here's your booking link: [URL]. You can search, compare, and book directly on that page."
"""

@mcp.prompt("find_cheapest")
def find_cheapest_prompt(
    destination: str = "destination",
    trip_type: str = "round-trip"
) -> str:
    """
    Find the cheapest flight - search proactively across multiple dates.
    """
    return f"""# Find Cheapest Flight to {destination}

## Your Mission
Find the absolute best deal. Search proactively, advise on trade-offs.

## Search Strategy (Do This Automatically)

1. **Search multiple dates** - Don't just search one date. Search +/- 3 days:
   - If user wants Dec 24-26, also check Dec 22-27 departures/returns
   - Track the cheapest option found across all searches

2. **Use `optimization: "cheapest"`** for all searches

3. **Compare and advise**:
   - "Cheapest overall: $156 on Dec 22-25"
   - "Your preferred dates (Dec 24-26): $301"
   - "Savings: $145 by flying 2 days earlier"

## Present Results With Context

For each option, note:
- ✅ What's good (included bags, direct flight, good times)
- ⚠️ What to watch out for (no bags, long layover, 5am departure, budget carrier)

Example:
"**$156** - VietJet (Dec 22-25)
⚠️ Basic fare: +$35/bag each way, 6:00am departure
Actual cost with 1 bag: ~$226"

"**$301** - Malaysia Airlines (Dec 24-26)
✅ 20kg bag included, reasonable 11am departure
Better value if you're checking luggage"

## Only Ask If Needed
- No departure city mentioned → ask where they're flying from
- Very vague dates ("sometime next month") → ask for a target week
- Otherwise → just search and present findings

## Make a Recommendation
End with: "My recommendation: [option] because [reason]"
"""

@mcp.prompt("compare_options")
def compare_options_prompt(
    offer_request_id: str = "orq_xxxxx",
    criteria: str = "price, duration, stops"
) -> str:
    """
    Compare multiple flight options from a search.

    This prompt helps users analyze and compare different
    flight options to make the best choice.
    """
    return f"""# Compare Flight Options

Analyze and compare offers from search: `{offer_request_id}`

## Comparison Criteria
{criteria}

## Analysis Steps

1. **Retrieve All Offers**
   Use `duffel_analyze_offers` with:
   - `offer_request_id: "{offer_request_id}"`
   - `optimization: "best"`
   - `top_n: 10`

2. **Create Comparison Table**
   For each option, show:
   | Offer | Price | Duration | Stops | Departure | Score |
   |-------|-------|----------|-------|-----------|-------|

3. **Highlight Trade-offs**
   - Cheapest vs Fastest
   - Non-stop vs Connection savings
   - Early/Late departures

4. **Recommendation**
   Based on the user's priorities, recommend:
   - Best overall value
   - Budget option
   - Convenience option

## Decision Factors
- Price difference percentage
- Time saved vs cost
- Layover quality (duration, airport)
- Airline reputation
- Baggage policies
"""

# ============================================================================
# Tool Implementations
# ============================================================================

@mcp.tool(
    name="duffel_search_flights",
    annotations={
        "title": "Search Flights",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True
    }
)
async def duffel_search_flights(params: SearchFlightsInput, ctx: Context) -> str:
    """
    Search for flights by creating an offer request in the Duffel API.

    SMART AGENT TIPS:
    - For "cheapest" requests: Search multiple date combinations (+/- 3 days)
      and compare results. Don't just search one date.
    - Present results with context: Note if bags aren't included, if there's
      a long layover, or if it's a budget carrier with extra fees.
    - Make recommendations based on the trade-offs you find.
    - Only ask the user questions if truly needed (missing origin, vague dates).

    This tool searches for available flights based on itinerary (origin, destination, dates),
    passenger information, and preferences like cabin class. It supports optimization
    strategies to find the cheapest, fastest, or best overall flights.

    Args:
        params (SearchFlightsInput): Validated input parameters containing:
            - slices (List[FlightSlice]): Flight segments with optional time filters:
                - origin, destination, departure_date (required)
                - departure_time: TimeRange {'from': 'HH:MM', 'to': 'HH:MM'} (optional)
                - arrival_time: TimeRange {'from': 'HH:MM', 'to': 'HH:MM'} (optional)
            - passengers (List[PassengerInput]): Passenger list (max 9)
            - cabin_class (Optional[CabinClass]): Cabin preference (default: economy)
            - max_connections (Optional[int]): Max connections (0=non-stop)
            - return_offers (bool): Return offers immediately (default: true)
            - optimization (OptimizationStrategy): Sort strategy (cheapest, fastest, best, etc.)
            - optimization_weights (OptimizationWeights): Custom weights for 'best' strategy
            - preferred_departure_time (DepartureTimePreference): Preferred departure window
            - top_n (Optional[int]): Return only top N results
            - response_format (ResponseFormat): 'markdown' or 'json'
        ctx (Context): MCP context for progress reporting and logging

    Returns:
        str: Formatted response containing flight offers and search details

    Examples:
        - "Find flights from SGN to KUL departing Dec 24, returning Dec 26"
        - "Search cheapest business class flights NYC to LON" (search multiple dates!)
        - "Find morning flights departing after 9am" (use departure_time filter)
        - "Find flights arriving before 8pm" (use arrival_time filter)
    """
    try:
        await ctx.report_progress(0.1, "Validating search parameters...")

        # Log search request
        route_summary = " -> ".join([f"{s.origin}->{s.destination}" for s in params.slices])
        logger.info(
            "Flight search: %s, %d passengers, %s class, optimization=%s",
            route_summary,
            len(params.passengers),
            params.cabin_class.value,
            params.optimization.value
        )

        # Build request payload
        slices_data = []
        for slice_data in params.slices:
            slice_obj = {
                "origin": slice_data.origin.upper(),
                "destination": slice_data.destination.upper(),
                "departure_date": slice_data.departure_date
            }
            # Add time filters if specified
            if slice_data.departure_time:
                slice_obj["departure_time"] = {
                    "from": slice_data.departure_time.from_time,
                    "to": slice_data.departure_time.to_time
                }
            if slice_data.arrival_time:
                slice_obj["arrival_time"] = {
                    "from": slice_data.arrival_time.from_time,
                    "to": slice_data.arrival_time.to_time
                }
            slices_data.append(slice_obj)

        request_data = {
            "data": {
                "slices": slices_data,
                "passengers": [
                    {"type": p.type.value} if p.type else {"age": p.age}
                    for p in params.passengers
                ],
                "cabin_class": params.cabin_class.value
            }
        }

        if params.max_connections is not None:
            request_data["data"]["max_connections"] = params.max_connections

        query_params = {
            "return_offers": "true" if params.return_offers else "false"
        }

        await ctx.report_progress(0.3, "Searching for flights...")

        response = await _make_api_request(
            ctx,
            "air/offer_requests",
            method="POST",
            params=query_params,
            json_data=request_data
        )

        await ctx.report_progress(0.7, "Processing offers...")

        data = response.get("data", {})
        offers = data.get("offers", [])

        # Apply optimization
        if offers and params.optimization != OptimizationStrategy.NONE:
            preferred_dep = params.preferred_departure_time.value if params.preferred_departure_time else None
            offers = _optimize_offers(
                offers,
                params.optimization,
                params.optimization_weights,
                preferred_dep,
                params.top_n
            )
            data["offers"] = offers

        await ctx.report_progress(0.9, "Formatting results...")

        # Format response
        if params.response_format == ResponseFormat.JSON:
            result = json.dumps(response, indent=2)
            return _truncate_if_needed(result, "offers")

        # Markdown format
        lines = ["# Flight Search Results\n"]

        # Search details
        lines.append("## Search Criteria")
        lines.append(f"- **Offer Request ID**: `{data.get('id', 'N/A')}`")
        lines.append(f"- **Cabin Class**: {data.get('cabin_class', 'N/A').replace('_', ' ').title()}")
        lines.append(f"- **Passengers**: {len(data.get('passengers', []))}")
        if params.optimization != OptimizationStrategy.NONE:
            lines.append(f"- **Optimization**: {params.optimization.value}")

        # Slices
        lines.append("\n### Itinerary")
        for i, slice_info in enumerate(data.get("slices", []), 1):
            origin = slice_info.get("origin", {})
            destination = slice_info.get("destination", {})
            lines.append(f"{i}. **{origin.get('iata_code', 'N/A')}** -> **{destination.get('iata_code', 'N/A')}** on {slice_info.get('departure_date', 'N/A')}")

        # Offers
        if offers:
            lines.append(f"\n## Available Offers ({len(offers)} found)\n")
            display_count = min(len(offers), params.top_n or 20)

            for i, offer in enumerate(offers[:display_count], 1):
                score_str = f" | Score: {offer.get('_score', 'N/A')}" if offer.get('_score') else ""
                lines.append(f"### Offer {i}: {_format_price(offer.get('total_amount', '0'), offer.get('total_currency', 'USD'))}{score_str}")
                lines.append(f"- **Offer ID**: `{offer.get('id', 'N/A')}`")
                lines.append(f"- **Owner**: {offer.get('owner', {}).get('name', 'N/A')}")
                lines.append(f"- **Duration**: {_parse_duration_minutes(offer)} minutes")
                lines.append(f"- **Stops**: {_count_stops(offer)}")
                lines.append(f"- **Baggage**: {_extract_baggage_info(offer)}")
                lines.append(f"- **Fare**: {_format_fare_conditions_brief(offer)}")

                for j, slice_data in enumerate(offer.get("slices", []), 1):
                    segments = slice_data.get("segments", [])
                    if segments:
                        duration = slice_data.get("duration", "N/A")
                        lines.append(f"  - Slice {j}: {len(segments)} segment(s), Duration: {duration}")

                lines.append("")

            if len(offers) > display_count:
                lines.append(f"\n*Showing {display_count} of {len(offers)} offers. Use duffel_list_offers or increase top_n for more.*")
        else:
            lines.append("\n## No offers available")
            lines.append("No flights found matching your criteria. Try adjusting dates or removing restrictions.")

        await ctx.report_progress(1.0, "Search complete")

        logger.info("Search completed: %d offers found", len(offers))

        result = "\n".join(lines)
        return _truncate_if_needed(result, "offers")

    except Exception as e:
        return _handle_api_error(e, ctx)

@mcp.tool(
    name="duffel_analyze_offers",
    annotations={
        "title": "Analyze Flight Offers",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
async def duffel_analyze_offers(params: AnalyzeOffersInput, ctx: Context) -> str:
    """
    Analyze and rank flight offers from a previous search.

    Use this after duffel_search_flights to find the best options based on
    user preferences. Supports multiple optimization strategies and custom
    weighting for the 'best' algorithm.

    Args:
        params (AnalyzeOffersInput): Validated input parameters containing:
            - offer_request_id (str): Offer request ID from previous search
            - optimization (OptimizationStrategy): Strategy to apply
            - optimization_weights (OptimizationWeights): Custom weights for 'best'
            - preferred_departure_time (DepartureTimePreference): Time preference
            - top_n (int): Number of results to return (default: 5)
            - response_format (ResponseFormat): Output format
        ctx (Context): MCP context for progress and logging

    Returns:
        str: Ranked and analyzed flight offers with scores

    Examples:
        - Use when: "Analyze the search results and find the best value"
        - Use when: "Compare the top 5 cheapest flights from the last search"
        - Use when: "Find flights that balance price and duration"
    """
    try:
        logger.info(
            "Analyzing offers for request %s with strategy=%s, top_n=%d",
            params.offer_request_id,
            params.optimization.value,
            params.top_n
        )

        await ctx.report_progress(0.2, "Fetching offers from search...")

        # Fetch all offers from the offer request
        response = await _make_api_request(
            ctx,
            "air/offers",
            params={
                "offer_request_id": params.offer_request_id,
                "limit": "200"  # Get all for analysis
            }
        )

        offers = response.get("data", [])

        if not offers:
            logger.warning("No offers found for request %s", params.offer_request_id)
            return "No offers found for this offer request ID. The search may have expired."

        await ctx.report_progress(0.5, f"Analyzing {len(offers)} offers...")

        # Apply optimization
        preferred_dep = params.preferred_departure_time.value if params.preferred_departure_time else None
        optimized = _optimize_offers(
            offers,
            params.optimization,
            params.optimization_weights,
            preferred_dep,
            params.top_n
        )

        await ctx.report_progress(0.8, "Formatting analysis...")

        if params.response_format == ResponseFormat.JSON:
            result = {
                "offer_request_id": params.offer_request_id,
                "total_analyzed": len(offers),
                "optimization": params.optimization.value,
                "top_offers": [_get_offer_summary(o) for o in optimized]
            }
            return json.dumps(result, indent=2)

        # Markdown format
        lines = ["# Flight Offer Analysis\n"]
        lines.append(f"- **Offer Request**: `{params.offer_request_id}`")
        lines.append(f"- **Total Offers Analyzed**: {len(offers)}")
        lines.append(f"- **Optimization Strategy**: {params.optimization.value}")
        if params.optimization_weights and params.optimization == OptimizationStrategy.BEST:
            w = params.optimization_weights
            lines.append(f"- **Weights**: Price={w.price}, Duration={w.duration}, Stops={w.stops}, Time={w.departure_time}")

        # Summary stats
        prices = [float(o.get("total_amount", 0)) for o in offers]
        durations = [_parse_duration_minutes(o) for o in offers]
        lines.append(f"\n## Market Overview")
        lines.append(f"- **Price Range**: {min(prices):.2f} - {max(prices):.2f} {offers[0].get('total_currency', 'USD')}")
        lines.append(f"- **Duration Range**: {min(durations)} - {max(durations)} minutes")

        lines.append(f"\n## Top {len(optimized)} Offers\n")

        for i, offer in enumerate(optimized, 1):
            score = offer.get("_score")
            score_str = f" (Score: **{score}**/100)" if score else ""
            lines.append(f"### #{i}: {_format_price(offer.get('total_amount', '0'), offer.get('total_currency', 'USD'))}{score_str}")
            lines.append(f"- **Offer ID**: `{offer.get('id', 'N/A')}`")
            lines.append(f"- **Airline**: {offer.get('owner', {}).get('name', 'N/A')}")
            lines.append(f"- **Duration**: {_parse_duration_minutes(offer)} minutes")
            lines.append(f"- **Stops**: {_count_stops(offer)}")

            for j, slice_data in enumerate(offer.get("slices", []), 1):
                segments = slice_data.get("segments", [])
                if segments:
                    first_seg = segments[0]
                    last_seg = segments[-1]
                    lines.append(f"  - **Slice {j}**: {first_seg.get('origin', {}).get('iata_code', '?')} -> {last_seg.get('destination', {}).get('iata_code', '?')}")
                    lines.append(f"    - Depart: {_format_datetime(first_seg.get('departing_at', 'N/A'))}")
                    lines.append(f"    - Arrive: {_format_datetime(last_seg.get('arriving_at', 'N/A'))}")

            lines.append("")

        await ctx.report_progress(1.0, "Analysis complete")

        logger.info("Analysis completed: %d offers analyzed, top %d returned", len(offers), len(optimized))

        result = "\n".join(lines)
        return _truncate_if_needed(result, "offers")

    except Exception as e:
        return _handle_api_error(e, ctx)

@mcp.tool(
    name="duffel_get_offer",
    annotations={
        "title": "Get Single Offer",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
async def duffel_get_offer(params: GetOfferInput, ctx: Context) -> str:
    """
    Retrieve the latest details for a specific flight offer.

    This tool fetches up-to-date information for a specific offer using its ID.
    It's recommended to call this before booking to ensure the offer is still
    valid and to get the latest pricing.

    Args:
        params (GetOfferInput): Validated input parameters
        ctx (Context): MCP context for progress reporting

    Returns:
        str: Formatted offer details including pricing, itinerary, and conditions
    """
    try:
        logger.info("Fetching offer details for %s", params.offer_id)
        await ctx.report_progress(0.2, "Fetching offer details...")

        query_params = {}
        if params.return_available_services:
            query_params["return_available_services"] = "true"

        response = await _make_api_request(
            ctx,
            f"air/offers/{params.offer_id}",
            method="GET",
            params=query_params
        )

        await ctx.report_progress(0.8, "Formatting response...")

        if params.response_format == ResponseFormat.JSON:
            result = json.dumps(response, indent=2)
            return _truncate_if_needed(result)

        # Markdown format
        data = response.get("data", {})
        lines = ["# Flight Offer Details\n"]

        # Basic info
        lines.append("## Overview")
        lines.append(f"- **Offer ID**: `{data.get('id', 'N/A')}`")
        lines.append(f"- **Total Price**: **{_format_price(data.get('total_amount', '0'), data.get('total_currency', 'USD'))}**")
        lines.append(f"- **Airline**: {data.get('owner', {}).get('name', 'N/A')}")
        lines.append(f"- **Baggage**: {_extract_baggage_info(data)}")

        # Fare conditions (refund/change policies)
        fare_conditions = _extract_fare_conditions(data)
        lines.append(f"- **Refund Policy**: {fare_conditions['refund']}")
        lines.append(f"- **Change Policy**: {fare_conditions['change']}")

        lines.append(f"- **Expires**: {_format_datetime(data.get('expires_at', 'N/A'))}")
        lines.append(f"- **Live Mode**: {'Yes' if data.get('live_mode') else 'No'}")

        # Itinerary
        lines.append("\n## Itinerary")
        for i, slice_data in enumerate(data.get("slices", []), 1):
            lines.append(f"\n### Slice {i}")
            lines.append(f"- **Duration**: {slice_data.get('duration', 'N/A')}")

            for j, segment in enumerate(slice_data.get("segments", []), 1):
                lines.append(f"\n#### Segment {j}")
                lines.append(f"- **Flight**: {segment.get('marketing_carrier', {}).get('name', 'N/A')} {segment.get('marketing_carrier_flight_number', 'N/A')}")
                lines.append(f"- **Aircraft**: {segment.get('aircraft', {}).get('name', 'N/A')}")
                lines.append(f"- **Departure**: {segment.get('origin', {}).get('iata_code', 'N/A')} at {_format_datetime(segment.get('departing_at', 'N/A'))}")
                lines.append(f"- **Arrival**: {segment.get('destination', {}).get('iata_code', 'N/A')} at {_format_datetime(segment.get('arriving_at', 'N/A'))}")
                lines.append(f"- **Duration**: {segment.get('duration', 'N/A')}")

        # Passengers
        passengers = data.get("passengers", [])
        if passengers:
            lines.append(f"\n## Passengers ({len(passengers)})")
            for p in passengers:
                lines.append(f"- **ID**: `{p.get('id', 'N/A')}` - {p.get('type', 'N/A').replace('_', ' ').title()}")

        await ctx.report_progress(1.0, "Done")

        logger.info("Retrieved offer %s: %s %s", params.offer_id, data.get('total_currency', 'USD'), data.get('total_amount', '0'))

        result = "\n".join(lines)
        return _truncate_if_needed(result)

    except Exception as e:
        return _handle_api_error(e, ctx)

@mcp.tool(
    name="duffel_list_offers",
    annotations={
        "title": "List Offers from Request",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
async def duffel_list_offers(params: ListOffersInput, ctx: Context) -> str:
    """
    List all flight offers from a specific offer request with filtering and sorting.

    This tool retrieves offers from a previous search (offer request), with support
    for pagination, filtering by connections, and sorting by price or duration.

    Args:
        params (ListOffersInput): Validated input parameters
        ctx (Context): MCP context for progress reporting

    Returns:
        str: Formatted list of flight offers with filtering applied
    """
    try:
        logger.info("Listing offers for request %s (limit=%d, sort=%s)", params.offer_request_id, params.limit, params.sort)
        await ctx.report_progress(0.2, "Fetching offers...")

        query_params = {
            "offer_request_id": params.offer_request_id,
            "limit": str(params.limit)
        }

        if params.max_connections is not None:
            query_params["max_connections"] = str(params.max_connections)

        if params.sort:
            query_params["sort"] = params.sort

        response = await _make_api_request(
            ctx,
            "air/offers",
            method="GET",
            params=query_params
        )

        await ctx.report_progress(0.8, "Formatting results...")

        if params.response_format == ResponseFormat.JSON:
            result = json.dumps(response, indent=2)
            return _truncate_if_needed(result, "offers")

        # Markdown format
        data = response.get("data", [])
        lines = ["# Flight Offers\n"]
        lines.append(f"Found {len(data)} offer(s)\n")

        if not data:
            lines.append("No offers found matching your criteria.")
            return "\n".join(lines)

        for i, offer in enumerate(data, 1):
            lines.append(f"## Offer {i}: {_format_price(offer.get('total_amount', '0'), offer.get('total_currency', 'USD'))}")
            lines.append(f"- **ID**: `{offer.get('id', 'N/A')}`")
            lines.append(f"- **Airline**: {offer.get('owner', {}).get('name', 'N/A')}")

            for j, slice_data in enumerate(offer.get("slices", []), 1):
                segments = slice_data.get("segments", [])
                connections = len(segments) - 1
                duration = slice_data.get("duration", "N/A")

                if segments:
                    origin = segments[0].get("origin", {}).get("iata_code", "N/A")
                    destination = segments[-1].get("destination", {}).get("iata_code", "N/A")
                    lines.append(f"  - **Slice {j}**: {origin} -> {destination}")
                    lines.append(f"    - Segments: {len(segments)}, Connections: {connections}, Duration: {duration}")

            lines.append("")

        await ctx.report_progress(1.0, "Done")

        logger.info("Listed %d offers for request %s", len(data), params.offer_request_id)

        result = "\n".join(lines)
        return _truncate_if_needed(result, "offers")

    except Exception as e:
        return _handle_api_error(e, ctx)

@mcp.tool(
    name="duffel_create_order",
    annotations={
        "title": "Create Flight Booking",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True
    }
)
async def duffel_create_order(params: CreateOrderInput, ctx: Context) -> str:
    """
    Create a flight booking (order) with passenger and payment details.

    This tool creates a confirmed flight booking for a selected offer. It requires
    complete passenger information (names, DOB, contact details) and payment information.
    This operation charges the payment method and confirms the booking with the airline.

    Optionally, you can add services like seat selection. Get seat service IDs from
    duffel_get_seat_map and include them in the services parameter.

    Args:
        params (CreateOrderInput): Validated input parameters containing:
            - selected_offers: List with one offer ID
            - passengers: Complete passenger details
            - payments: Payment information
            - services (optional): Seat selections or other services from duffel_get_seat_map
        ctx (Context): MCP context for progress reporting

    Returns:
        str: Formatted order confirmation with booking reference and details
    """
    try:
        logger.info(
            "Creating order for offer %s with %d passengers",
            params.selected_offers[0] if params.selected_offers else "unknown",
            len(params.passengers)
        )
        await ctx.report_progress(0.1, "Validating booking details...")

        request_data = {
            "data": {
                "selected_offers": params.selected_offers,
                "payments": [
                    {
                        "type": p.type.value,
                        "amount": p.amount,
                        "currency": p.currency.upper()
                    }
                    for p in params.payments
                ],
                "passengers": [
                    {
                        "id": p.id,
                        "given_name": p.given_name,
                        "family_name": p.family_name,
                        "born_on": p.born_on,
                        "email": p.email,
                        "phone_number": p.phone_number,
                        "title": p.title,
                        "gender": p.gender,
                        **({"infant_passenger_id": p.infant_passenger_id} if p.infant_passenger_id else {})
                    }
                    for p in params.passengers
                ]
            }
        }

        # Add services (seat selection, extra baggage, etc.) if specified
        if params.services:
            request_data["data"]["services"] = [
                {"id": s.id, "quantity": s.quantity}
                for s in params.services
            ]

        await ctx.report_progress(0.3, "Creating booking...")

        response = await _make_api_request(
            ctx,
            "air/orders",
            method="POST",
            json_data=request_data
        )

        await ctx.report_progress(0.9, "Confirming booking...")

        if params.response_format == ResponseFormat.JSON:
            result = json.dumps(response, indent=2)
            return _truncate_if_needed(result)

        # Markdown format
        data = response.get("data", {})
        lines = ["# Booking Confirmed\n"]

        lines.append("## Order Details")
        lines.append(f"- **Order ID**: `{data.get('id', 'N/A')}`")
        lines.append(f"- **Booking Reference**: **{data.get('booking_reference', 'N/A')}**")
        lines.append(f"- **Total Amount**: **{_format_price(data.get('total_amount', '0'), data.get('total_currency', 'USD'))}**")
        lines.append(f"- **Status**: {data.get('booking_status', {}).get('status', 'N/A').title()}")
        lines.append(f"- **Created**: {_format_datetime(data.get('created_at', 'N/A'))}")

        # Passengers
        passengers = data.get("passengers", [])
        if passengers:
            lines.append(f"\n## Passengers ({len(passengers)})")
            for p in passengers:
                name = f"{p.get('given_name', '')} {p.get('family_name', '')}".strip()
                lines.append(f"- **{name}** ({p.get('type', 'N/A')})")

        # Slices
        slices = data.get("slices", [])
        if slices:
            lines.append("\n## Flight Itinerary")
            for i, slice_data in enumerate(slices, 1):
                lines.append(f"\n### Slice {i}")
                for j, segment in enumerate(slice_data.get("segments", []), 1):
                    lines.append(f"**Segment {j}**: {segment.get('marketing_carrier', {}).get('name', 'N/A')} {segment.get('marketing_carrier_flight_number', 'N/A')}")
                    lines.append(f"  - {segment.get('origin', {}).get('iata_code', 'N/A')} -> {segment.get('destination', {}).get('iata_code', 'N/A')}")
                    lines.append(f"  - Departs: {_format_datetime(segment.get('departing_at', 'N/A'))}")
                    lines.append(f"  - Arrives: {_format_datetime(segment.get('arriving_at', 'N/A'))}")

        lines.append("\n---")
        lines.append("**Important**: Please save your booking reference. You may receive confirmation emails from the airline.")

        await ctx.report_progress(1.0, "Booking complete")

        logger.info(
            "Order created successfully: ID=%s, Reference=%s, Amount=%s %s",
            data.get('id', 'N/A'),
            data.get('booking_reference', 'N/A'),
            data.get('total_currency', 'USD'),
            data.get('total_amount', '0')
        )

        result = "\n".join(lines)
        return _truncate_if_needed(result)

    except Exception as e:
        return _handle_api_error(e, ctx)

def _infer_seat_position(designator: str, aisles: int, all_letters_in_cabin: List[str]) -> str:
    """
    Infer seat position (window/aisle/middle) from designator and cabin layout.

    Args:
        designator: Seat designator like "14A" or "7F"
        aisles: Number of aisles in the cabin (1 or 2)
        all_letters_in_cabin: Sorted list of all seat letters in this cabin

    Returns:
        Position string: "Window", "Aisle", "Middle", or "Unknown"
    """
    import re
    match = re.match(r'^(\d+)([A-Z])$', designator.upper())
    if not match:
        return "Unknown"

    letter = match.group(2)

    if not all_letters_in_cabin:
        # Fallback: use common patterns
        # Single aisle (3-3): A=window, B=middle, C=aisle | D=aisle, E=middle, F=window
        # Single aisle (2-2): A=window, B=aisle | C=aisle, D=window
        # Twin aisle (3-3-3): A=window, B=middle, C=aisle | D=middle, E=middle, F=middle | G=aisle, H=middle, J=window
        if letter in ['A', 'K', 'L']:
            return "Window"
        elif letter in ['C', 'D', 'G', 'H'] and aisles >= 1:
            return "Aisle"
        elif letter in ['B', 'E', 'F', 'J']:
            return "Middle" if aisles == 2 or letter == 'B' else "Window" if letter == 'F' else "Middle"
        return "Unknown"

    # Use actual cabin layout
    sorted_letters = sorted(all_letters_in_cabin)
    if letter not in sorted_letters:
        return "Unknown"

    idx = sorted_letters.index(letter)
    total = len(sorted_letters)

    if aisles == 1:
        # Single aisle: typically splits cabin in half
        # First and last letters are windows
        # Letters adjacent to middle are aisles
        if idx == 0 or idx == total - 1:
            return "Window"
        mid = total // 2
        if idx == mid - 1 or idx == mid:
            return "Aisle"
        return "Middle"
    elif aisles == 2:
        # Twin aisle (wide-body): typically 3-3-3 or 2-4-2 or 3-4-3
        if idx == 0 or idx == total - 1:
            return "Window"
        # Estimate aisle positions (roughly at 1/3 and 2/3)
        third = total // 3
        if idx == third or idx == third - 1 or idx == 2 * third or idx == 2 * third + 1:
            return "Aisle"
        return "Middle"

    return "Unknown"


def _parse_seat_row_number(designator: str) -> int:
    """Extract row number from seat designator."""
    import re
    match = re.match(r'^(\d+)', designator)
    return int(match.group(1)) if match else 0


@mcp.tool(
    name="duffel_get_seat_map",
    annotations={
        "title": "Get Seat Map",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
async def duffel_get_seat_map(params: GetSeatMapInput, ctx: Context) -> str:
    """
    Retrieve seat maps for a specific flight offer with layout analysis.

    This tool fetches the seat map layout for each flight segment in an offer,
    showing available seats organized by row. It analyzes the cabin layout to
    determine seat positions (window/aisle/middle) and helps identify adjacent
    seats for groups traveling together.

    Args:
        params (GetSeatMapInput): Input with offer_id and response_format
        ctx (Context): MCP context for progress reporting

    Returns:
        str: Formatted seat map showing:
        - Cabin layout (number of aisles, seat configuration)
        - Available seats organized by row
        - Seat position (window/aisle/middle)
        - Prices and any restrictions
        - Adjacent seat suggestions for groups

    Note:
        - Not all airlines support seat selection
        - Seats must be selected during booking (duffel_create_order)
        - Use the seat service ID when creating an order to select a seat
    """
    try:
        logger.info("Fetching seat map for offer %s", params.offer_id)
        await ctx.report_progress(0.2, "Fetching seat maps...")

        response = await _make_api_request(
            ctx,
            "air/seat_maps",
            method="GET",
            params={"offer_id": params.offer_id}
        )

        await ctx.report_progress(0.7, "Processing seat map data...")

        if params.response_format == ResponseFormat.JSON:
            result = json.dumps(response, indent=2)
            return _truncate_if_needed(result)

        # Markdown format
        seat_maps = response.get("data", [])
        lines = ["# Seat Maps\n"]

        if not seat_maps:
            lines.append("**No seat maps available for this offer.**")
            lines.append("\nThis airline may not support seat selection through the API.")
            return "\n".join(lines)

        lines.append(f"Found seat maps for {len(seat_maps)} flight segment(s)\n")

        for i, seat_map in enumerate(seat_maps, 1):
            segment_id = seat_map.get("segment_id", "N/A")
            slice_id = seat_map.get("slice_id", "N/A")
            lines.append(f"## Segment {i}")
            lines.append(f"- **Segment ID**: `{segment_id}`")

            cabins = seat_map.get("cabins", [])
            for cabin in cabins:
                cabin_class = cabin.get("cabin_class", "unknown").replace("_", " ").title()
                aisles = cabin.get("aisles", 1)
                deck = cabin.get("deck", 0)
                wings = cabin.get("wings", {})

                lines.append(f"\n### {cabin_class} Class")

                # Describe aircraft layout
                aisle_desc = "single-aisle" if aisles == 1 else "twin-aisle (wide-body)"
                lines.append(f"- **Layout**: {aisle_desc} aircraft")
                if deck == 1:
                    lines.append(f"- **Deck**: Upper deck")

                # Collect all seat data with full context
                rows_data = cabin.get("rows", [])
                all_seats = []  # All seats in cabin (available or not)
                available_seats = []  # Only available seats
                all_letters = set()  # Track all seat letters for position inference

                for row_idx, row in enumerate(rows_data):
                    sections = row.get("sections", [])
                    section_count = len(sections)

                    for section_idx, section in enumerate(sections):
                        elements = section.get("elements", [])
                        for element in elements:
                            if element.get("type") == "seat":
                                designator = element.get("designator", "?")
                                # Extract letter for layout analysis
                                import re
                                letter_match = re.search(r'([A-Z])$', designator.upper())
                                if letter_match:
                                    all_letters.add(letter_match.group(1))

                                seat_name = element.get("name", "")
                                disclosures = element.get("disclosures", [])
                                services = element.get("available_services", [])

                                seat_info = {
                                    "designator": designator,
                                    "row_num": _parse_seat_row_number(designator),
                                    "section_idx": section_idx,
                                    "section_count": section_count,
                                    "name": seat_name,
                                    "disclosures": disclosures,
                                    "available": len(services) > 0,
                                    "services": services
                                }
                                all_seats.append(seat_info)

                                if services:
                                    for service in services:
                                        available_seats.append({
                                            **seat_info,
                                            "price": float(service.get("total_amount", "0")),
                                            "currency": service.get("total_currency", "USD"),
                                            "service_id": service.get("id", ""),
                                            "passenger_id": service.get("passenger_id", "")
                                        })

                # Infer positions for all available seats
                sorted_letters = sorted(all_letters)
                for seat in available_seats:
                    seat["position"] = _infer_seat_position(seat["designator"], aisles, sorted_letters)

                if not available_seats:
                    lines.append("- **Available Seats**: 0 (all seats taken or not available)")
                    continue

                # Summary stats
                prices = sorted(set(s["price"] for s in available_seats))
                currency = available_seats[0]["currency"]
                window_seats = [s for s in available_seats if s["position"] == "Window"]
                aisle_seats = [s for s in available_seats if s["position"] == "Aisle"]
                middle_seats = [s for s in available_seats if s["position"] == "Middle"]

                lines.append(f"- **Available Seats**: {len(available_seats)} total")
                lines.append(f"  - Window: {len(window_seats)}, Aisle: {len(aisle_seats)}, Middle: {len(middle_seats)}")

                if len(prices) == 1:
                    lines.append(f"- **Price**: {currency} {prices[0]:.2f}")
                else:
                    lines.append(f"- **Price Range**: {currency} {min(prices):.2f} - {currency} {max(prices):.2f}")

                # Group seats by row for layout visualization
                rows_with_seats = {}
                for seat in available_seats:
                    row_num = seat["row_num"]
                    if row_num not in rows_with_seats:
                        rows_with_seats[row_num] = []
                    rows_with_seats[row_num].append(seat)

                # Find adjacent seat pairs/groups (seats in same row)
                adjacent_groups = []
                for row_num, row_seats in sorted(rows_with_seats.items()):
                    if len(row_seats) >= 2:
                        # Sort by letter to find truly adjacent seats
                        sorted_row = sorted(row_seats, key=lambda x: x["designator"])
                        # Check if letters are consecutive (adjacent)
                        for j in range(len(sorted_row) - 1):
                            s1, s2 = sorted_row[j], sorted_row[j + 1]
                            letter1 = s1["designator"][-1]
                            letter2 = s2["designator"][-1]
                            # Check if same section (no aisle between)
                            if s1["section_idx"] == s2["section_idx"]:
                                # Check if letters are adjacent
                                if ord(letter2) - ord(letter1) == 1:
                                    adjacent_groups.append((s1, s2))

                # Show seat layout by row
                lines.append("\n#### Seats by Row")
                lines.append("| Row | Seats (Position) | Prices | Notes |")
                lines.append("|-----|------------------|--------|-------|")

                shown_rows = 0
                for row_num in sorted(rows_with_seats.keys()):
                    if shown_rows >= 15:  # Limit rows shown
                        remaining = len(rows_with_seats) - shown_rows
                        lines.append(f"| ... | *{remaining} more rows available* | | |")
                        break

                    row_seats = sorted(rows_with_seats[row_num], key=lambda x: x["designator"])
                    seats_str = ", ".join([f"{s['designator']} ({s['position'][0]})" for s in row_seats])
                    prices_str = ", ".join([f"${s['price']:.0f}" for s in row_seats])

                    # Collect notes
                    notes = set()
                    for s in row_seats:
                        if s["name"]:
                            notes.add(s["name"])
                        notes.update(s["disclosures"][:1])  # Just first disclosure
                    notes_str = ", ".join(list(notes)[:2]) if notes else "-"
                    if len(notes_str) > 25:
                        notes_str = notes_str[:22] + "..."

                    lines.append(f"| {row_num} | {seats_str} | {prices_str} | {notes_str} |")
                    shown_rows += 1

                # Suggest adjacent seats for groups
                if adjacent_groups:
                    lines.append("\n#### 👥 Adjacent Seat Pairs (for groups)")
                    lines.append("These seats are next to each other with no aisle between:\n")

                    shown_pairs = 0
                    for s1, s2 in sorted(adjacent_groups, key=lambda x: x[0]["price"] + x[1]["price"])[:5]:
                        total_price = s1["price"] + s2["price"]
                        lines.append(f"- **{s1['designator']} + {s2['designator']}**: {currency} {total_price:.2f} total ({s1['position']} + {s2['position']})")
                        lines.append(f"  - Service IDs: `{s1['service_id']}`, `{s2['service_id']}`")
                        shown_pairs += 1

                    if len(adjacent_groups) > 5:
                        lines.append(f"\n*{len(adjacent_groups) - 5} more adjacent pairs available*")
                else:
                    lines.append("\n*No adjacent seat pairs currently available*")

                # Position legend
                lines.append("\n**Position Key**: W=Window, A=Aisle, M=Middle")

            lines.append("")

        lines.append("\n## How to Select Seats")
        lines.append("When booking with `duffel_create_order`, include the seat service ID(s) in the `services` parameter:")
        lines.append("```json")
        lines.append('{')
        lines.append('  "services": [')
        lines.append('    {"id": "ase_xxx_passenger1", "quantity": 1},')
        lines.append('    {"id": "ase_xxx_passenger2", "quantity": 1}')
        lines.append('  ]')
        lines.append('}')
        lines.append("```")

        await ctx.report_progress(1.0, "Done")

        logger.info("Retrieved seat maps for offer %s: %d segments", params.offer_id, len(seat_maps))

        result = "\n".join(lines)
        return _truncate_if_needed(result)

    except Exception as e:
        return _handle_api_error(e, ctx)

@mcp.tool(
    name="duffel_list_airlines",
    annotations={
        "title": "List Airlines",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
async def duffel_list_airlines(params: ListAirlinesInput, ctx: Context) -> str:
    """
    List available airlines in the Duffel API.

    This tool retrieves information about airlines that can be booked through Duffel,
    including their names, IATA codes, and logos.

    Args:
        params (ListAirlinesInput): Validated input parameters
        ctx (Context): MCP context for progress reporting

    Returns:
        str: Formatted list of airlines with codes and names
    """
    try:
        logger.info("Listing airlines (limit=%d)", params.limit)
        await ctx.report_progress(0.3, "Fetching airlines...")

        query_params = {"limit": str(params.limit)}

        response = await _make_api_request(
            ctx,
            "air/airlines",
            method="GET",
            params=query_params
        )

        await ctx.report_progress(0.8, "Formatting results...")

        if params.response_format == ResponseFormat.JSON:
            result = json.dumps(response, indent=2)
            return _truncate_if_needed(result, "airlines")

        # Markdown format
        data = response.get("data", [])
        lines = ["# Airlines\n"]
        lines.append(f"Showing {len(data)} airline(s)\n")

        for airline in data:
            name = airline.get("name", "N/A")
            iata = airline.get("iata_code", "N/A")
            lines.append(f"- **{name}** ({iata})")

        await ctx.report_progress(1.0, "Done")

        logger.info("Listed %d airlines", len(data))

        result = "\n".join(lines)
        return _truncate_if_needed(result, "airlines")

    except Exception as e:
        return _handle_api_error(e, ctx)

@mcp.tool(
    name="duffel_flexible_search",
    annotations={
        "title": "Flexible Date Flight Search",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True
    }
)
async def duffel_flexible_search(params: FlexibleDateSearchInput, ctx: Context) -> str:
    """
    Search for the cheapest flights across a range of dates.

    USE THIS TOOL when the user wants the "cheapest", "most affordable", or "best deal"
    on flights. It automatically searches multiple date combinations to find the
    lowest price, then compares and presents the best options.

    This is better than regular search when:
    - User wants the cheapest option and has some date flexibility
    - User says "around Christmas" or "sometime in December"
    - User prioritizes price over specific dates

    Args:
        params: Contains origin, destination, target dates, and flexibility_days (+/- N days to search)

    Returns:
        Comparison of cheapest options found across all searched dates, with recommendations.
    """
    from datetime import datetime, timedelta

    try:
        logger.info(
            "Flexible search: %s->%s, target %s (return: %s), +/-%d days",
            params.origin, params.destination, params.departure_date,
            params.return_date or "one-way", params.flexibility_days
        )

        await ctx.report_progress(0.1, "Preparing date combinations...")

        # Parse target dates
        target_dep = datetime.strptime(params.departure_date, "%Y-%m-%d")
        target_ret = datetime.strptime(params.return_date, "%Y-%m-%d") if params.return_date else None
        is_roundtrip = target_ret is not None

        # Generate date combinations to search
        date_combinations = []
        flex = params.flexibility_days

        if is_roundtrip:
            # For round-trip: search combinations of departure and return dates
            # Focus on key combinations to avoid too many API calls
            for dep_offset in range(-flex, flex + 1):
                dep_date = target_dep + timedelta(days=dep_offset)
                # Keep same trip length, just shift dates
                ret_date = target_ret + timedelta(days=dep_offset)
                date_combinations.append((dep_date, ret_date))

            # Also try original departure with flexible return
            for ret_offset in [-2, -1, 1, 2]:
                if ret_offset != 0:
                    ret_date = target_ret + timedelta(days=ret_offset)
                    if ret_date > target_dep:
                        date_combinations.append((target_dep, ret_date))
        else:
            # For one-way: just search different departure dates
            for dep_offset in range(-flex, flex + 1):
                dep_date = target_dep + timedelta(days=dep_offset)
                date_combinations.append((dep_date, None))

        # Remove duplicates
        date_combinations = list(set(date_combinations))
        total_searches = len(date_combinations)

        await ctx.report_progress(0.2, f"Searching {total_searches} date combinations...")

        # Search each date combination
        all_results = []
        headers = _get_http_headers()
        headers["Content-Type"] = "application/json"

        async with httpx.AsyncClient(base_url=API_BASE_URL, timeout=DEFAULT_TIMEOUT) as client:
            for i, (dep_date, ret_date) in enumerate(date_combinations):
                progress = 0.2 + (0.6 * (i / total_searches))
                dep_str = dep_date.strftime("%Y-%m-%d")
                ret_str = ret_date.strftime("%Y-%m-%d") if ret_date else None

                await ctx.report_progress(progress, f"Searching {dep_str}...")

                # Build slices
                slices = [{"origin": params.origin, "destination": params.destination, "departure_date": dep_str}]
                if ret_str:
                    slices.append({"origin": params.destination, "destination": params.origin, "departure_date": ret_str})

                # Build passengers
                passengers = []
                for p in params.passengers:
                    if p.type:
                        passengers.append({"type": p.type.value})
                    elif p.age is not None:
                        passengers.append({"age": p.age})
                    else:
                        passengers.append({"type": "adult"})

                request_data = {
                    "data": {
                        "slices": slices,
                        "passengers": passengers,
                        "cabin_class": params.cabin_class.value if params.cabin_class else "economy"
                    }
                }

                if params.max_connections is not None:
                    request_data["data"]["max_connections"] = params.max_connections

                try:
                    response = await client.post(
                        "/air/offer_requests",
                        headers=headers,
                        json=request_data,
                        params={"return_offers": "true"}
                    )
                    response.raise_for_status()
                    data = response.json()

                    offers = data.get("data", {}).get("offers", [])
                    if offers:
                        # Get the cheapest offer for this date combo
                        cheapest_offer = min(offers, key=lambda o: float(o.get("total_amount", "999999")))
                        all_results.append({
                            "departure_date": dep_str,
                            "return_date": ret_str,
                            "price": float(cheapest_offer.get("total_amount", 0)),
                            "currency": cheapest_offer.get("total_currency", "USD"),
                            "offer_id": cheapest_offer.get("id"),
                            "airline": cheapest_offer.get("owner", {}).get("name", "Unknown"),
                            "slices": cheapest_offer.get("slices", []),
                            "baggage": _extract_baggage_info(cheapest_offer),
                            "fare_conditions": _format_fare_conditions_brief(cheapest_offer),
                            "is_target_date": (dep_date == target_dep and (ret_date == target_ret or ret_date is None))
                        })
                except Exception as e:
                    logger.warning("Search failed for %s: %s", dep_str, str(e))
                    continue

        await ctx.report_progress(0.9, "Analyzing results...")

        if not all_results:
            return "No flights found for any of the searched dates. Try different airports or a wider date range."

        # Sort by price
        all_results.sort(key=lambda x: x["price"])

        # Find cheapest and target date price
        cheapest = all_results[0]
        target_result = next((r for r in all_results if r["is_target_date"]), None)

        # Build response
        lines = ["# Flexible Date Search Results\n"]
        lines.append(f"**Route**: {params.origin} → {params.destination}")
        if is_roundtrip:
            lines.append(f"**Target Dates**: {params.departure_date} to {params.return_date}")
        else:
            lines.append(f"**Target Date**: {params.departure_date}")
        lines.append(f"**Searched**: +/- {params.flexibility_days} days ({len(all_results)} combinations with results)\n")

        # Cheapest overall
        lines.append("## 🏆 Cheapest Option Found\n")
        lines.append(f"**{cheapest['currency']} {cheapest['price']:.2f}** - {cheapest['airline']}")
        lines.append(f"- Depart: {cheapest['departure_date']}")
        if cheapest['return_date']:
            lines.append(f"- Return: {cheapest['return_date']}")
        lines.append(f"- {cheapest.get('baggage', 'Baggage info not available')}")
        lines.append(f"- {cheapest.get('fare_conditions', 'Conditions unknown')}")
        lines.append(f"- Offer ID: `{cheapest['offer_id']}`")

        # Add flight details
        for j, slice_data in enumerate(cheapest.get('slices', [])):
            segments = slice_data.get('segments', [])
            if segments:
                first_seg = segments[0]
                last_seg = segments[-1]
                dep_time = first_seg.get('departing_at', '')[:16].replace('T', ' ')
                arr_time = last_seg.get('arriving_at', '')[:16].replace('T', ' ')
                stops = len(segments) - 1
                stop_text = "direct" if stops == 0 else f"{stops} stop(s)"
                lines.append(f"- Flight {j+1}: {dep_time} → {arr_time} ({stop_text})")

        # Compare to target date
        if target_result and target_result != cheapest:
            savings = target_result['price'] - cheapest['price']
            lines.append(f"\n## 📅 Your Target Dates ({params.departure_date})\n")
            lines.append(f"**{target_result['currency']} {target_result['price']:.2f}** - {target_result['airline']}")
            lines.append(f"\n💰 **Savings**: Flying on {cheapest['departure_date']} instead saves **${savings:.2f}**")

        # Top 5 options
        lines.append("\n## All Options (sorted by price)\n")
        lines.append("| Dates | Price | Airline | vs Target |")
        lines.append("|-------|-------|---------|-----------|")

        target_price = target_result['price'] if target_result else cheapest['price']
        for r in all_results[:10]:
            dates = r['departure_date']
            if r['return_date']:
                dates += f" - {r['return_date']}"
            diff = r['price'] - target_price
            diff_str = f"+${diff:.0f}" if diff > 0 else f"-${abs(diff):.0f}" if diff < 0 else "target"
            marker = " 🏆" if r == cheapest else ""
            lines.append(f"| {dates} | ${r['price']:.2f}{marker} | {r['airline']} | {diff_str} |")

        # Recommendation
        lines.append("\n## 💡 Recommendation\n")
        if cheapest['is_target_date']:
            lines.append(f"Great news! Your target dates are already the cheapest option at **${cheapest['price']:.2f}**.")
        elif target_result:
            savings = target_result['price'] - cheapest['price']
            if savings > 20:
                lines.append(f"Consider flying **{cheapest['departure_date']}** instead of {params.departure_date} to save **${savings:.2f}**.")
            else:
                lines.append(f"Your target dates are close to the best price. Only ${savings:.2f} difference.")
        else:
            lines.append(f"The cheapest option is **${cheapest['price']:.2f}** on {cheapest['departure_date']}.")

        await ctx.report_progress(1.0, "Done")
        logger.info("Flexible search complete: found %d options, cheapest $%.2f", len(all_results), cheapest['price'])

        return "\n".join(lines)

    except Exception as e:
        logger.error("Flexible search error: %s", str(e))
        return _handle_api_error(e, ctx)


@mcp.tool(
    name="duffel_search_partial",
    annotations={
        "title": "Mix & Match Flight Search",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True
    }
)
async def duffel_search_partial(params: SearchPartialInput, ctx: Context) -> str:
    """
    Search for flights with mix-and-match legs from different airlines.

    USE THIS TOOL when the user wants the cheapest round-trip combination and is open
    to flying different airlines on each leg. This uses Duffel's partial offer request
    API which returns per-leg pricing, allowing you to combine the cheapest outbound
    from one airline with the cheapest return from another.

    This is better than regular search when:
    - User wants the absolute cheapest round-trip
    - User is flexible about airlines
    - User says "mix and match" or "different airlines each way"

    Args:
        params: Slices (2+ for round-trip/multi-city), passengers, cabin class, connection limits

    Returns:
        Per-leg options with prices, plus the cheapest combined total.
    """
    try:
        route_summary = " -> ".join([f"{s.origin}->{s.destination}" for s in params.slices])
        logger.info("Partial offer search: %s, %d passengers", route_summary, len(params.passengers))

        await ctx.report_progress(0.1, "Preparing partial offer request...")

        # Build request payload
        slices_data = []
        for slice_data in params.slices:
            slice_obj = {
                "origin": slice_data.origin.upper(),
                "destination": slice_data.destination.upper(),
                "departure_date": slice_data.departure_date
            }
            if slice_data.departure_time:
                slice_obj["departure_time"] = {
                    "from": slice_data.departure_time.from_time,
                    "to": slice_data.departure_time.to_time
                }
            if slice_data.arrival_time:
                slice_obj["arrival_time"] = {
                    "from": slice_data.arrival_time.from_time,
                    "to": slice_data.arrival_time.to_time
                }
            slices_data.append(slice_obj)

        passengers_data = [
            {"type": p.type.value} if p.type else {"age": p.age}
            for p in params.passengers
        ]

        request_data = {
            "data": {
                "slices": slices_data,
                "passengers": passengers_data,
                "cabin_class": params.cabin_class.value if params.cabin_class else "economy"
            }
        }

        if params.max_connections is not None:
            request_data["data"]["max_connections"] = params.max_connections

        await ctx.report_progress(0.3, "Searching for mix-and-match options...")

        response = await _make_api_request(
            ctx,
            "air/partial_offer_requests",
            method="POST",
            json_data=request_data
        )

        await ctx.report_progress(0.7, "Analyzing per-leg options...")

        data = response.get("data", {})
        offers = data.get("offers", [])

        if not offers:
            return "No partial offers found. Try adjusting dates, airports, or removing connection limits."

        # Group partial offers by slice
        # Each offer has slices, and each slice has segments with pricing
        slice_options: Dict[int, List[Dict[str, Any]]] = {}
        for i in range(len(params.slices)):
            slice_options[i] = []

        for offer in offers:
            offer_slices = offer.get("slices", [])
            total_amount = float(offer.get("total_amount", "0"))
            currency = offer.get("total_currency", "USD")
            airline = offer.get("owner", {}).get("name", "Unknown")
            offer_id = offer.get("id", "")

            # For partial offers, extract per-slice info
            for i, offer_slice in enumerate(offer_slices):
                if i >= len(params.slices):
                    break

                segments = offer_slice.get("segments", [])
                if not segments:
                    continue

                first_seg = segments[0]
                last_seg = segments[-1]
                dep_time = first_seg.get("departing_at", "")[:16].replace("T", " ")
                arr_time = last_seg.get("arriving_at", "")[:16].replace("T", " ")
                stops = len(segments) - 1
                duration = offer_slice.get("duration", "N/A")

                slice_options[i].append({
                    "offer_id": offer_id,
                    "price": total_amount,
                    "currency": currency,
                    "airline": airline,
                    "departure": dep_time,
                    "arrival": arr_time,
                    "stops": stops,
                    "duration": duration,
                    "baggage": _extract_baggage_info(offer),
                })

        # Sort each slice's options by price
        for i in slice_options:
            slice_options[i].sort(key=lambda x: x["price"])

        await ctx.report_progress(0.9, "Formatting results...")

        # Build response
        lines = ["# Mix & Match Flight Search\n"]
        lines.append(f"**Route**: {route_summary}")
        lines.append(f"**Passengers**: {len(params.passengers)}")
        lines.append(f"**Total offers analyzed**: {len(offers)}\n")

        # Per-slice tables
        slice_cheapest = []
        for i, slice_input in enumerate(params.slices):
            options = slice_options.get(i, [])
            leg_label = f"{slice_input.origin.upper()} → {slice_input.destination.upper()} ({slice_input.departure_date})"
            lines.append(f"## Leg {i + 1}: {leg_label}\n")

            if not options:
                lines.append("No options found for this leg.\n")
                continue

            # Deduplicate by offer_id (take first/cheapest)
            seen_offers = set()
            unique_options = []
            for opt in options:
                if opt["offer_id"] not in seen_offers:
                    seen_offers.add(opt["offer_id"])
                    unique_options.append(opt)

            display_count = min(len(unique_options), params.top_n or 5)
            lines.append("| # | Price | Airline | Departure | Arrival | Stops | Baggage |")
            lines.append("|---|-------|---------|-----------|---------|-------|---------|")

            for j, opt in enumerate(unique_options[:display_count], 1):
                stop_text = "direct" if opt["stops"] == 0 else f"{opt['stops']} stop(s)"
                lines.append(
                    f"| {j} | {opt['currency']} {opt['price']:.2f} | {opt['airline']} | "
                    f"{opt['departure']} | {opt['arrival']} | {stop_text} | {opt['baggage']} |"
                )

            lines.append("")

            if unique_options:
                slice_cheapest.append(unique_options[0])

        # Best combination
        if len(slice_cheapest) == len(params.slices):
            total_cheapest = sum(opt["price"] for opt in slice_cheapest)
            currency = slice_cheapest[0]["currency"]
            lines.append("## 🏆 Cheapest Combination\n")
            lines.append(f"**Total: {currency} {total_cheapest:.2f}**\n")
            for i, opt in enumerate(slice_cheapest):
                lines.append(f"- Leg {i + 1}: {opt['airline']} at {currency} {opt['price']:.2f}")

            # Check if all legs are same airline
            airlines = set(opt["airline"] for opt in slice_cheapest)
            if len(airlines) > 1:
                lines.append(f"\n💡 **Mix & match**: Different airlines per leg for the best price.")
            else:
                lines.append(f"\n✈️ **Same airline** ({airlines.pop()}) is cheapest for all legs.")

        lines.append(f"\n*Use offer IDs with `duffel_get_offer` for full details, or `duffel_get_booking_link` to book.*")

        await ctx.report_progress(1.0, "Done")
        logger.info("Partial search complete: %d offers, %d slices", len(offers), len(params.slices))

        result = "\n".join(lines)
        return _truncate_if_needed(result, "partial offers")

    except Exception as e:
        return _handle_api_error(e, ctx)


# ============================================================================
# Checkout Flow - Payment Intent Helpers
# ============================================================================

async def _create_payment_intent(amount: str, currency: str) -> Dict[str, Any]:
    """Create a Duffel Payment Intent."""
    headers = _get_http_headers()
    headers["Content-Type"] = "application/json"

    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
        response = await client.post(
            f"{API_BASE_URL}/payments/payment_intents",
            headers=headers,
            json={"data": {"amount": amount, "currency": currency}}
        )
        response.raise_for_status()
        return response.json()["data"]


async def _confirm_payment_intent(payment_intent_id: str) -> Dict[str, Any]:
    """Confirm a Payment Intent after successful card collection."""
    headers = _get_http_headers()

    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
        response = await client.post(
            f"{API_BASE_URL}/payments/payment_intents/{payment_intent_id}/actions/confirm",
            headers=headers
        )
        response.raise_for_status()
        return response.json()["data"]


async def _get_offer_details(offer_id: str) -> Dict[str, Any]:
    """Fetch offer details from Duffel API."""
    headers = _get_http_headers()

    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
        response = await client.get(
            f"{API_BASE_URL}/air/offers/{offer_id}",
            headers=headers
        )
        response.raise_for_status()
        return response.json()["data"]


async def _create_duffel_links_session(
    reference: str,
    markup_amount: Optional[str] = None,
    markup_rate: Optional[str] = None,
    currency: str = "USD"
) -> Dict[str, Any]:
    """
    Create a Duffel Links session for hosted flight search and booking.

    Args:
        reference: Unique reference for this user/session
        markup_amount: Fixed markup amount (e.g., "5.00")
        markup_rate: Percentage markup as decimal (e.g., "0.05" for 5%)
        currency: Currency code (default USD)

    Returns:
        Duffel Links session data including the booking URL
    """
    headers = _get_http_headers()
    headers["Content-Type"] = "application/json"

    # Build session data
    session_data: Dict[str, Any] = {
        "reference": reference,
        "traveller_currency": currency,
        "flights": {"enabled": True},
        "stays": {"enabled": False},
    }

    # Add branding if configured
    if DUFFEL_LINKS_LOGO_URL:
        session_data["logo_url"] = DUFFEL_LINKS_LOGO_URL
    if DUFFEL_LINKS_PRIMARY_COLOR:
        session_data["primary_color"] = DUFFEL_LINKS_PRIMARY_COLOR

    # Add redirect URLs if configured
    if DUFFEL_LINKS_SUCCESS_URL:
        session_data["success_url"] = DUFFEL_LINKS_SUCCESS_URL
    if DUFFEL_LINKS_FAILURE_URL:
        session_data["failure_url"] = DUFFEL_LINKS_FAILURE_URL
    if DUFFEL_LINKS_ABANDONMENT_URL:
        session_data["abandonment_url"] = DUFFEL_LINKS_ABANDONMENT_URL

    # Add markup if specified
    if markup_amount:
        session_data["markup_amount"] = markup_amount
        session_data["markup_currency"] = currency
    if markup_rate:
        session_data["markup_rate"] = markup_rate

    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
        response = await client.post(
            f"{API_BASE_URL}/links/sessions",
            headers=headers,
            json={"data": session_data}
        )
        response.raise_for_status()
        return response.json()["data"]


async def _create_order_with_balance(
    offer_id: str,
    passengers: List[CheckoutPassenger],
    amount: str,
    currency: str
) -> Dict[str, Any]:
    """Create an order using balance payment (after payment intent confirmation)."""
    headers = _get_http_headers()
    headers["Content-Type"] = "application/json"

    order_data = {
        "data": {
            "selected_offers": [offer_id],
            "payments": [{
                "type": "balance",
                "amount": amount,
                "currency": currency
            }],
            "passengers": [
                {
                    "id": p.id,
                    "given_name": p.given_name,
                    "family_name": p.family_name,
                    "born_on": p.born_on,
                    "email": p.email,
                    "phone_number": p.phone_number,
                    "title": p.title,
                    "gender": p.gender
                }
                for p in passengers
            ]
        }
    }

    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
        response = await client.post(
            f"{API_BASE_URL}/air/orders",
            headers=headers,
            json=order_data
        )
        response.raise_for_status()
        return response.json()["data"]


def _calculate_payment_amount(offer_amount: float, currency: str) -> str:
    """Calculate amount to charge including Duffel Payments fee."""
    # Amount = offer_total / (1 - fee_percent)
    # This ensures after Duffel takes their fee, we have enough to cover the offer
    charge_amount = offer_amount / (1 - DUFFEL_PAYMENTS_FEE_PERCENT)
    # Round up to 2 decimal places
    return f"{charge_amount:.2f}"


def _get_session(session_id: str) -> Optional[CheckoutSession]:
    """Get a checkout session from the session store."""
    return session_store.get(session_id)


# ============================================================================
# Checkout Flow - HTML Templates
# ============================================================================

CHECKOUT_HTML_TEMPLATE = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Complete Your Booking</title>
    <script src="https://assets.duffel.com/components/3.5.0/duffel-payments.js"></script>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            padding: 20px;
        }}
        .container {{
            max-width: 600px;
            margin: 0 auto;
            background: white;
            border-radius: 16px;
            box-shadow: 0 20px 60px rgba(0,0,0,0.3);
            overflow: hidden;
        }}
        .header {{
            background: #1a1a2e;
            color: white;
            padding: 24px;
            text-align: center;
        }}
        .header h1 {{ font-size: 24px; margin-bottom: 8px; }}
        .header p {{ opacity: 0.8; font-size: 14px; }}
        .flight-summary {{
            padding: 24px;
            border-bottom: 1px solid #eee;
        }}
        .flight-card {{
            background: #f8f9fa;
            border-radius: 12px;
            padding: 16px;
            margin-bottom: 16px;
        }}
        .flight-route {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 12px;
        }}
        .airport {{ text-align: center; }}
        .airport-code {{ font-size: 28px; font-weight: bold; color: #1a1a2e; }}
        .airport-city {{ font-size: 12px; color: #666; }}
        .flight-arrow {{
            flex: 1;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 0 16px;
        }}
        .flight-arrow::before {{
            content: '';
            flex: 1;
            height: 2px;
            background: linear-gradient(90deg, #667eea, #764ba2);
        }}
        .flight-arrow::after {{
            content: '✈';
            font-size: 20px;
            margin-left: -10px;
        }}
        .flight-details {{ font-size: 13px; color: #666; }}
        .flight-details span {{ margin-right: 16px; }}
        .price-section {{
            padding: 24px;
            background: #f8f9fa;
            text-align: center;
        }}
        .price-label {{ font-size: 14px; color: #666; margin-bottom: 4px; }}
        .price {{ font-size: 36px; font-weight: bold; color: #1a1a2e; }}
        .price-currency {{ font-size: 18px; }}
        .passengers {{
            padding: 24px;
            border-bottom: 1px solid #eee;
        }}
        .passengers h3 {{ margin-bottom: 12px; color: #1a1a2e; }}
        .passenger {{
            display: flex;
            align-items: center;
            padding: 8px 0;
            border-bottom: 1px solid #f0f0f0;
        }}
        .passenger:last-child {{ border-bottom: none; }}
        .passenger-icon {{
            width: 32px; height: 32px;
            background: #667eea;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            color: white;
            margin-right: 12px;
            font-size: 14px;
        }}
        .payment-section {{
            padding: 24px;
        }}
        .payment-section h3 {{ margin-bottom: 16px; color: #1a1a2e; }}
        duffel-payments {{
            display: block;
            margin-bottom: 16px;
        }}
        .error-message {{
            background: #fee;
            color: #c00;
            padding: 12px;
            border-radius: 8px;
            margin-bottom: 16px;
            display: none;
        }}
        .loading {{
            text-align: center;
            padding: 40px;
            color: #666;
        }}
        .spinner {{
            width: 40px;
            height: 40px;
            border: 4px solid #f0f0f0;
            border-top-color: #667eea;
            border-radius: 50%;
            animation: spin 1s linear infinite;
            margin: 0 auto 16px;
        }}
        @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
        .secure-badge {{
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 16px;
            background: #f8f9fa;
            font-size: 12px;
            color: #666;
        }}
        .secure-badge svg {{ margin-right: 8px; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>✈️ Complete Your Booking</h1>
            <p>Secure payment powered by Duffel</p>
        </div>

        <div class="flight-summary">
            {flight_cards}
        </div>

        <div class="price-section">
            <div class="price-label">Total Amount</div>
            <div class="price">
                <span class="price-currency">{currency}</span> {amount}
            </div>
        </div>

        <div class="passengers">
            <h3>👥 Passengers</h3>
            {passengers_html}
        </div>

        <div class="payment-section">
            <h3>💳 Payment Details</h3>
            <div id="error-message" class="error-message"></div>
            <duffel-payments id="duffel-payments"></duffel-payments>
        </div>

        <div class="secure-badge">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
                <path d="M12 1L3 5v6c0 5.55 3.84 10.74 9 12 5.16-1.26 9-6.45 9-12V5l-9-4zm0 10.99h7c-.53 4.12-3.28 7.79-7 8.94V12H5V6.3l7-3.11v8.8z"/>
            </svg>
            Your payment is secure and encrypted
        </div>
    </div>

    <script>
        const sessionId = "{session_id}";
        const clientToken = "{client_token}";

        // Initialize the Duffel Payments component
        const paymentsElement = document.getElementById("duffel-payments");
        paymentsElement.render({{
            paymentIntentClientToken: clientToken,
            debug: {debug_mode}
        }});

        // Handle successful payment
        paymentsElement.addEventListener("onSuccessfulPayment", async () => {{
            document.querySelector('.payment-section').innerHTML = `
                <div class="loading">
                    <div class="spinner"></div>
                    <p>Processing your booking...</p>
                </div>
            `;

            try {{
                const response = await fetch(`/checkout/${{sessionId}}/confirm`, {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }}
                }});

                const result = await response.json();

                if (result.success) {{
                    window.location.href = `/checkout/${{sessionId}}/success`;
                }} else {{
                    document.getElementById('error-message').textContent = result.error || 'Booking failed. Please contact support.';
                    document.getElementById('error-message').style.display = 'block';
                }}
            }} catch (err) {{
                document.getElementById('error-message').textContent = 'An error occurred. Please try again.';
                document.getElementById('error-message').style.display = 'block';
            }}
        }});

        // Handle failed payment
        paymentsElement.addEventListener("onFailedPayment", (event) => {{
            const errorMessage = event.detail?.message || 'Payment failed. Please try again.';
            document.getElementById('error-message').textContent = errorMessage;
            document.getElementById('error-message').style.display = 'block';
        }});
    </script>
</body>
</html>'''


SUCCESS_HTML_TEMPLATE = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Booking Confirmed!</title>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #11998e 0%, #38ef7d 100%);
            min-height: 100vh;
            padding: 20px;
            display: flex;
            align-items: center;
            justify-content: center;
        }}
        .container {{
            max-width: 500px;
            background: white;
            border-radius: 16px;
            box-shadow: 0 20px 60px rgba(0,0,0,0.3);
            overflow: hidden;
            text-align: center;
        }}
        .success-header {{
            background: #1a1a2e;
            color: white;
            padding: 40px 24px;
        }}
        .check-icon {{
            width: 80px;
            height: 80px;
            background: #38ef7d;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            margin: 0 auto 20px;
            font-size: 40px;
        }}
        .success-header h1 {{ font-size: 28px; margin-bottom: 8px; }}
        .booking-ref {{
            padding: 24px;
            background: #f8f9fa;
        }}
        .booking-ref-label {{ font-size: 14px; color: #666; margin-bottom: 8px; }}
        .booking-ref-code {{
            font-size: 32px;
            font-weight: bold;
            color: #1a1a2e;
            letter-spacing: 4px;
            font-family: monospace;
        }}
        .details {{
            padding: 24px;
        }}
        .detail-item {{
            display: flex;
            justify-content: space-between;
            padding: 12px 0;
            border-bottom: 1px solid #f0f0f0;
        }}
        .detail-item:last-child {{ border-bottom: none; }}
        .detail-label {{ color: #666; }}
        .detail-value {{ font-weight: 600; color: #1a1a2e; }}
        .flight-info {{
            background: #f8f9fa;
            padding: 24px;
            margin: 0 24px 24px;
            border-radius: 12px;
        }}
        .flight-route {{
            font-size: 24px;
            font-weight: bold;
            color: #1a1a2e;
            margin-bottom: 8px;
        }}
        .flight-date {{ color: #666; }}
        .email-notice {{
            padding: 16px 24px;
            background: #e8f5e9;
            color: #2e7d32;
            font-size: 14px;
        }}
        .footer {{
            padding: 24px;
            color: #666;
            font-size: 13px;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="success-header">
            <div class="check-icon">✓</div>
            <h1>Booking Confirmed!</h1>
            <p>Your flight has been booked successfully</p>
        </div>

        <div class="booking-ref">
            <div class="booking-ref-label">Booking Reference</div>
            <div class="booking-ref-code">{booking_reference}</div>
        </div>

        <div class="flight-info">
            <div class="flight-route">{route}</div>
            <div class="flight-date">{dates}</div>
        </div>

        <div class="details">
            <div class="detail-item">
                <span class="detail-label">Order ID</span>
                <span class="detail-value">{order_id}</span>
            </div>
            <div class="detail-item">
                <span class="detail-label">Total Paid</span>
                <span class="detail-value">{currency} {amount}</span>
            </div>
            <div class="detail-item">
                <span class="detail-label">Passengers</span>
                <span class="detail-value">{passenger_count}</span>
            </div>
        </div>

        <div class="email-notice">
            📧 A confirmation email has been sent to your registered email address.
        </div>

        <div class="footer">
            <p>Please save your booking reference for check-in.</p>
            <p>You may receive additional emails directly from the airline.</p>
        </div>
    </div>
</body>
</html>'''


ERROR_HTML_TEMPLATE = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Checkout Error</title>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #ff416c 0%, #ff4b2b 100%);
            min-height: 100vh;
            padding: 20px;
            display: flex;
            align-items: center;
            justify-content: center;
        }}
        .container {{
            max-width: 400px;
            background: white;
            border-radius: 16px;
            box-shadow: 0 20px 60px rgba(0,0,0,0.3);
            padding: 40px;
            text-align: center;
        }}
        .error-icon {{
            width: 80px;
            height: 80px;
            background: #ff416c;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            margin: 0 auto 20px;
            font-size: 40px;
            color: white;
        }}
        h1 {{ color: #1a1a2e; margin-bottom: 16px; }}
        p {{ color: #666; line-height: 1.6; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="error-icon">✕</div>
        <h1>{title}</h1>
        <p>{message}</p>
    </div>
</body>
</html>'''


PASSENGER_FORM_HTML_TEMPLATE = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Enter Passenger Details</title>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            padding: 20px;
        }}
        .container {{
            max-width: 600px;
            margin: 0 auto;
            background: white;
            border-radius: 16px;
            box-shadow: 0 20px 60px rgba(0,0,0,0.3);
            overflow: hidden;
        }}
        .header {{
            background: #1a1a2e;
            color: white;
            padding: 24px;
            text-align: center;
        }}
        .header h1 {{ font-size: 24px; margin-bottom: 8px; }}
        .header p {{ opacity: 0.8; font-size: 14px; }}
        .steps {{
            display: flex;
            justify-content: center;
            padding: 16px;
            background: #f8f9fa;
            border-bottom: 1px solid #eee;
        }}
        .step {{
            display: flex;
            align-items: center;
            color: #999;
            font-size: 14px;
        }}
        .step.active {{ color: #667eea; font-weight: 600; }}
        .step-number {{
            width: 24px;
            height: 24px;
            border-radius: 50%;
            background: #ddd;
            display: flex;
            align-items: center;
            justify-content: center;
            margin-right: 8px;
            font-size: 12px;
        }}
        .step.active .step-number {{ background: #667eea; color: white; }}
        .step-divider {{ width: 40px; height: 2px; background: #ddd; margin: 0 16px; }}
        .flight-summary {{
            padding: 24px;
            border-bottom: 1px solid #eee;
        }}
        .flight-card {{
            background: #f8f9fa;
            border-radius: 12px;
            padding: 16px;
            margin-bottom: 16px;
        }}
        .flight-route {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 12px;
        }}
        .airport {{ text-align: center; }}
        .airport-code {{ font-size: 28px; font-weight: bold; color: #1a1a2e; }}
        .airport-city {{ font-size: 12px; color: #666; }}
        .flight-arrow {{
            flex: 1;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 0 16px;
        }}
        .flight-arrow::before {{
            content: '';
            flex: 1;
            height: 2px;
            background: linear-gradient(90deg, #667eea, #764ba2);
        }}
        .flight-arrow::after {{
            content: '✈';
            font-size: 20px;
            margin-left: -10px;
        }}
        .flight-details {{ font-size: 13px; color: #666; }}
        .flight-details span {{ margin-right: 16px; }}
        .price-badge {{
            display: inline-block;
            background: #667eea;
            color: white;
            padding: 8px 16px;
            border-radius: 20px;
            font-weight: 600;
            margin-top: 12px;
        }}
        .form-section {{
            padding: 24px;
        }}
        .form-section h3 {{
            margin-bottom: 20px;
            color: #1a1a2e;
            display: flex;
            align-items: center;
        }}
        .passenger-form {{
            border: 1px solid #eee;
            border-radius: 12px;
            padding: 20px;
            margin-bottom: 16px;
        }}
        .passenger-form h4 {{
            font-size: 14px;
            color: #667eea;
            margin-bottom: 16px;
        }}
        .form-row {{
            display: flex;
            gap: 12px;
            margin-bottom: 16px;
        }}
        .form-group {{
            flex: 1;
        }}
        .form-group label {{
            display: block;
            font-size: 12px;
            font-weight: 600;
            color: #666;
            margin-bottom: 4px;
        }}
        .form-group input, .form-group select {{
            width: 100%;
            padding: 10px 12px;
            border: 1px solid #ddd;
            border-radius: 8px;
            font-size: 14px;
            transition: border-color 0.2s;
        }}
        .form-group input:focus, .form-group select:focus {{
            outline: none;
            border-color: #667eea;
        }}
        .form-group.small {{ flex: 0 0 100px; }}
        .submit-btn {{
            width: 100%;
            padding: 16px;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            border: none;
            border-radius: 8px;
            font-size: 16px;
            font-weight: 600;
            cursor: pointer;
            transition: transform 0.2s, box-shadow 0.2s;
        }}
        .submit-btn:hover {{
            transform: translateY(-2px);
            box-shadow: 0 4px 12px rgba(102, 126, 234, 0.4);
        }}
        .submit-btn:disabled {{
            opacity: 0.6;
            cursor: not-allowed;
            transform: none;
        }}
        .error-message {{
            background: #fee;
            color: #c00;
            padding: 12px;
            border-radius: 8px;
            margin-bottom: 16px;
            display: none;
        }}
        .required {{ color: #c00; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>✈️ Complete Your Booking</h1>
            <p>Step 1 of 2 - Enter passenger details</p>
        </div>

        <div class="steps">
            <div class="step active">
                <div class="step-number">1</div>
                <span>Passengers</span>
            </div>
            <div class="step-divider"></div>
            <div class="step">
                <div class="step-number">2</div>
                <span>Payment</span>
            </div>
        </div>

        <div class="flight-summary">
            {flight_cards}
            <div class="price-badge">{currency} {amount}</div>
        </div>

        <form id="passenger-form" class="form-section">
            <h3>👥 Passenger Information</h3>
            <div id="error-message" class="error-message"></div>

            {passenger_forms}

            <button type="submit" class="submit-btn">Continue to Payment →</button>
        </form>
    </div>

    <script>
        const sessionId = "{session_id}";
        const passengerIds = {passenger_ids_json};

        document.getElementById('passenger-form').addEventListener('submit', async (e) => {{
            e.preventDefault();

            const btn = e.target.querySelector('.submit-btn');
            btn.disabled = true;
            btn.textContent = 'Processing...';

            const errorDiv = document.getElementById('error-message');
            errorDiv.style.display = 'none';

            // Collect passenger data
            const passengers = passengerIds.map((id, index) => ({{
                id: id,
                given_name: document.getElementById(`given_name_${{index}}`).value.trim(),
                family_name: document.getElementById(`family_name_${{index}}`).value.trim(),
                born_on: document.getElementById(`born_on_${{index}}`).value,
                email: document.getElementById(`email_${{index}}`).value.trim(),
                phone_number: document.getElementById(`phone_${{index}}`).value.trim(),
                title: document.getElementById(`title_${{index}}`).value,
                gender: document.getElementById(`gender_${{index}}`).value
            }}));

            // Validate
            for (let i = 0; i < passengers.length; i++) {{
                const p = passengers[i];
                if (!p.given_name || !p.family_name || !p.born_on || !p.email || !p.phone_number) {{
                    errorDiv.textContent = `Please fill in all fields for Passenger ${{i + 1}}`;
                    errorDiv.style.display = 'block';
                    btn.disabled = false;
                    btn.textContent = 'Continue to Payment →';
                    return;
                }}
                // Basic email validation
                if (!p.email.includes('@')) {{
                    errorDiv.textContent = `Invalid email address for Passenger ${{i + 1}}`;
                    errorDiv.style.display = 'block';
                    btn.disabled = false;
                    btn.textContent = 'Continue to Payment →';
                    return;
                }}
            }}

            try {{
                const response = await fetch(`/checkout/${{sessionId}}/passengers`, {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ passengers }})
                }});

                const result = await response.json();

                if (result.success) {{
                    window.location.reload();
                }} else {{
                    errorDiv.textContent = result.error || 'Failed to save passenger details';
                    errorDiv.style.display = 'block';
                    btn.disabled = false;
                    btn.textContent = 'Continue to Payment →';
                }}
            }} catch (err) {{
                errorDiv.textContent = 'An error occurred. Please try again.';
                errorDiv.style.display = 'block';
                btn.disabled = false;
                btn.textContent = 'Continue to Payment →';
            }}
        }});
    </script>
</body>
</html>'''


# ============================================================================
# Checkout Flow - HTTP Route Handlers
# ============================================================================

def _build_flight_cards_html(offer_data: Dict[str, Any]) -> str:
    """Build HTML for flight cards from offer data."""
    cards = []
    for i, slice_data in enumerate(offer_data.get("slices", [])):
        segments = slice_data.get("segments", [])
        if not segments:
            continue

        first_seg = segments[0]
        last_seg = segments[-1]

        origin = first_seg.get("origin", {})
        destination = last_seg.get("destination", {})

        origin_code = origin.get("iata_code", "???")
        origin_city = origin.get("city_name", origin.get("name", ""))
        dest_code = destination.get("iata_code", "???")
        dest_city = destination.get("city_name", destination.get("name", ""))

        dep_time = first_seg.get("departing_at", "")[:16].replace("T", " ")
        arr_time = last_seg.get("arriving_at", "")[:16].replace("T", " ")

        stops = len(segments) - 1
        stops_text = "Direct" if stops == 0 else f"{stops} stop{'s' if stops > 1 else ''}"

        duration = slice_data.get("duration", "")

        airline = first_seg.get("marketing_carrier", {}).get("name", "")
        flight_num = first_seg.get("marketing_carrier_flight_number", "")

        card = f'''
        <div class="flight-card">
            <div class="flight-route">
                <div class="airport">
                    <div class="airport-code">{origin_code}</div>
                    <div class="airport-city">{origin_city}</div>
                </div>
                <div class="flight-arrow"></div>
                <div class="airport">
                    <div class="airport-code">{dest_code}</div>
                    <div class="airport-city">{dest_city}</div>
                </div>
            </div>
            <div class="flight-details">
                <span>🛫 {dep_time}</span>
                <span>🛬 {arr_time}</span>
                <span>⏱️ {duration}</span>
                <span>🔄 {stops_text}</span>
            </div>
            <div class="flight-details" style="margin-top: 8px;">
                <span>✈️ {airline} {flight_num}</span>
            </div>
        </div>
        '''
        cards.append(card)

    return "\n".join(cards)


def _build_passengers_html(passengers: List[CheckoutPassenger]) -> str:
    """Build HTML for passengers list."""
    items = []
    for p in passengers:
        items.append(f'''
        <div class="passenger">
            <div class="passenger-icon">{p.given_name[0].upper()}</div>
            <span>{p.title.title()}. {p.given_name} {p.family_name}</span>
        </div>
        ''')
    return "\n".join(items)


def _build_passenger_forms_html(passenger_ids: List[str]) -> str:
    """Build HTML for passenger input forms."""
    forms = []
    for i, passenger_id in enumerate(passenger_ids):
        passenger_type = "Adult" if i == 0 else f"Passenger {i + 1}"
        forms.append(f'''
        <div class="passenger-form">
            <h4>✈️ {passenger_type}</h4>
            <div class="form-row">
                <div class="form-group small">
                    <label>Title <span class="required">*</span></label>
                    <select id="title_{i}" required>
                        <option value="mr">Mr</option>
                        <option value="ms">Ms</option>
                        <option value="mrs">Mrs</option>
                        <option value="miss">Miss</option>
                        <option value="dr">Dr</option>
                    </select>
                </div>
                <div class="form-group">
                    <label>First Name <span class="required">*</span></label>
                    <input type="text" id="given_name_{i}" placeholder="As on ID/passport" required>
                </div>
                <div class="form-group">
                    <label>Last Name <span class="required">*</span></label>
                    <input type="text" id="family_name_{i}" placeholder="As on ID/passport" required>
                </div>
            </div>
            <div class="form-row">
                <div class="form-group">
                    <label>Date of Birth <span class="required">*</span></label>
                    <input type="date" id="born_on_{i}" required>
                </div>
                <div class="form-group small">
                    <label>Gender <span class="required">*</span></label>
                    <select id="gender_{i}" required>
                        <option value="m">Male</option>
                        <option value="f">Female</option>
                    </select>
                </div>
            </div>
            <div class="form-row">
                <div class="form-group">
                    <label>Email <span class="required">*</span></label>
                    <input type="email" id="email_{i}" placeholder="email@example.com" required>
                </div>
                <div class="form-group">
                    <label>Phone <span class="required">*</span></label>
                    <input type="tel" id="phone_{i}" placeholder="+1234567890" required>
                </div>
            </div>
        </div>
        ''')
    return "\n".join(forms)


async def checkout_page(request: Request) -> HTMLResponse:
    """Render the checkout page with Duffel payment component."""
    session_id = request.path_params.get("session_id")
    session = _get_session(session_id)

    if not session:
        return HTMLResponse(
            ERROR_HTML_TEMPLATE.format(
                title="Session Expired",
                message="This checkout session has expired or is invalid. Please start a new booking."
            ),
            status_code=404
        )

    if session.status == "confirmed":
        return RedirectResponse(url=f"/checkout/{session_id}/success")

    if session.status not in ("pending", "paid", "passengers_collected"):
        return HTMLResponse(
            ERROR_HTML_TEMPLATE.format(
                title="Checkout Unavailable",
                message=f"This checkout session is {session.status}. Please start a new booking."
            ),
            status_code=400
        )

    # Get passenger IDs from offer data
    offer_passengers = session.offer_data.get("passengers", [])
    passenger_ids = [p.get("id", f"pas_{i}") for i, p in enumerate(offer_passengers)]

    # If passengers haven't been collected yet, show the passenger form
    if not session.passengers:
        flight_cards = _build_flight_cards_html(session.offer_data)
        passenger_forms = _build_passenger_forms_html(passenger_ids)

        html = PASSENGER_FORM_HTML_TEMPLATE.format(
            session_id=session_id,
            flight_cards=flight_cards,
            passenger_forms=passenger_forms,
            passenger_ids_json=json.dumps(passenger_ids),
            currency=session.currency,
            amount=session.amount
        )
        return HTMLResponse(html)

    # Passengers collected - show payment page
    flight_cards = _build_flight_cards_html(session.offer_data)
    passengers_html = _build_passengers_html(session.passengers)

    # Check if we're in debug/test mode
    debug_mode = "true" if not session.offer_data.get("live_mode", True) else "false"

    html = CHECKOUT_HTML_TEMPLATE.format(
        session_id=session_id,
        client_token=session.client_token,
        flight_cards=flight_cards,
        passengers_html=passengers_html,
        currency=session.currency,
        amount=session.amount,
        debug_mode=debug_mode
    )

    return HTMLResponse(html)


async def save_passengers(request: Request) -> JSONResponse:
    """Save passenger details to the checkout session."""
    session_id = request.path_params.get("session_id")
    session = _get_session(session_id)

    if not session:
        return JSONResponse({"success": False, "error": "Session expired"}, status_code=404)

    if session.passengers:
        return JSONResponse({"success": True, "message": "Passengers already saved"})

    try:
        body = await request.json()
        passengers_data = body.get("passengers", [])

        if not passengers_data:
            return JSONResponse({"success": False, "error": "No passenger data provided"}, status_code=400)

        # Validate and convert to CheckoutPassenger objects
        passengers = []
        for p in passengers_data:
            try:
                passenger = CheckoutPassenger(
                    id=p.get("id", ""),
                    given_name=p.get("given_name", ""),
                    family_name=p.get("family_name", ""),
                    born_on=p.get("born_on", ""),
                    email=p.get("email", ""),
                    phone_number=p.get("phone_number", ""),
                    title=p.get("title", "mr"),
                    gender=p.get("gender", "m")
                )
                passengers.append(passenger)
            except Exception as e:
                return JSONResponse({"success": False, "error": f"Invalid passenger data: {str(e)}"}, status_code=400)

        # Update session
        session.passengers = passengers
        session.status = "passengers_collected"
        session_store.update(session)

        logger.info("Passengers saved for session %s: %d passengers", session_id, len(passengers))

        return JSONResponse({"success": True})

    except Exception as e:
        logger.exception("Error saving passengers: %s", str(e))
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


async def confirm_checkout(request: Request) -> JSONResponse:
    """Confirm payment and create the booking."""
    session_id = request.path_params.get("session_id")
    session = _get_session(session_id)

    if not session:
        return JSONResponse({"success": False, "error": "Session expired"}, status_code=404)

    if session.status == "confirmed":
        return JSONResponse({
            "success": True,
            "order_id": session.order_id,
            "booking_reference": session.booking_reference
        })

    if session.status not in ("pending", "passengers_collected"):
        return JSONResponse({"success": False, "error": f"Invalid session status: {session.status}"}, status_code=400)

    # Check that passengers have been collected
    if not session.passengers:
        return JSONResponse({"success": False, "error": "Passengers not yet collected"}, status_code=400)

    try:
        # 1. Confirm the payment intent
        logger.info("Confirming payment intent: %s", session.payment_intent_id)
        payment_result = await _confirm_payment_intent(session.payment_intent_id)

        if payment_result.get("status") != "succeeded":
            session.status = "failed"
            session_store.update(session)
            return JSONResponse({"success": False, "error": "Payment confirmation failed"}, status_code=400)

        session.status = "paid"
        session_store.update(session)
        logger.info("Payment confirmed, creating order for offer: %s", session.offer_id)

        # 2. Create the order using balance payment
        # The payment intent tops up our balance, so we pay with balance
        order_result = await _create_order_with_balance(
            session.offer_id,
            session.passengers,
            session.offer_data.get("total_amount", session.amount),
            session.offer_data.get("total_currency", session.currency)
        )

        session.status = "confirmed"
        session.order_id = order_result.get("id")
        session.booking_reference = order_result.get("booking_reference")
        session_store.update(session)

        logger.info(
            "Order created: ID=%s, Reference=%s",
            session.order_id,
            session.booking_reference
        )

        return JSONResponse({
            "success": True,
            "order_id": session.order_id,
            "booking_reference": session.booking_reference
        })

    except httpx.HTTPStatusError as e:
        logger.error("Checkout confirmation failed: %s", e.response.text)
        session.status = "failed"
        session_store.update(session)
        try:
            error_data = e.response.json()
            error_msg = error_data.get("errors", [{}])[0].get("message", "Booking failed")
        except:
            error_msg = "Booking failed. Please contact support."
        return JSONResponse({"success": False, "error": error_msg}, status_code=500)
    except Exception as e:
        logger.exception("Checkout confirmation error: %s", str(e))
        session.status = "failed"
        session_store.update(session)
        return JSONResponse({"success": False, "error": "An unexpected error occurred"}, status_code=500)


async def success_page(request: Request) -> HTMLResponse:
    """Render the success page after booking."""
    session_id = request.path_params.get("session_id")
    session = _get_session(session_id)

    if not session:
        return HTMLResponse(
            ERROR_HTML_TEMPLATE.format(
                title="Session Not Found",
                message="This checkout session could not be found."
            ),
            status_code=404
        )

    if session.status != "confirmed":
        return RedirectResponse(url=f"/checkout/{session_id}")

    # Build route string
    slices = session.offer_data.get("slices", [])
    if slices:
        first_slice = slices[0]
        segments = first_slice.get("segments", [])
        if segments:
            origin = segments[0].get("origin", {}).get("iata_code", "???")
            dest = segments[-1].get("destination", {}).get("iata_code", "???")
            route = f"{origin} → {dest}"
            if len(slices) > 1:
                route += f" → {origin}"  # Round trip
        else:
            route = "Flight Details"
    else:
        route = "Flight Details"

    # Build dates string
    dates_parts = []
    for slice_data in slices:
        segments = slice_data.get("segments", [])
        if segments:
            dep_date = segments[0].get("departing_at", "")[:10]
            if dep_date:
                dates_parts.append(dep_date)
    dates = " - ".join(dates_parts) if dates_parts else ""

    html = SUCCESS_HTML_TEMPLATE.format(
        booking_reference=session.booking_reference or "N/A",
        order_id=session.order_id or "N/A",
        route=route,
        dates=dates,
        currency=session.currency,
        amount=session.amount,
        passenger_count=len(session.passengers)
    )

    return HTMLResponse(html)


# Static file serving
STATIC_DIR = Path(__file__).parent / "static"


async def serve_logo(request: Request) -> FileResponse:
    """Serve the logo file."""
    logo_path = STATIC_DIR / "logo.svg"
    if logo_path.exists():
        return FileResponse(logo_path, media_type="image/svg+xml")
    return JSONResponse({"error": "Logo not found"}, status_code=404)


async def serve_static(request: Request) -> FileResponse:
    """Serve static files."""
    filename = request.path_params.get("filename", "")
    # Security: prevent directory traversal
    if ".." in filename or filename.startswith("/"):
        return JSONResponse({"error": "Invalid path"}, status_code=400)

    file_path = (STATIC_DIR / filename).resolve()
    if not file_path.is_relative_to(STATIC_DIR.resolve()):
        return JSONResponse({"error": "Invalid path"}, status_code=400)
    if file_path.exists() and file_path.is_file():
        # Determine media type
        suffix = file_path.suffix.lower()
        media_types = {
            ".svg": "image/svg+xml",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".ico": "image/x-icon",
            ".css": "text/css",
            ".js": "application/javascript",
        }
        media_type = media_types.get(suffix, "application/octet-stream")
        return FileResponse(file_path, media_type=media_type)
    return JSONResponse({"error": "File not found"}, status_code=404)


# Define checkout routes
checkout_routes = [
    Route("/logo.svg", serve_logo, methods=["GET"]),
    Route("/static/{filename:path}", serve_static, methods=["GET"]),
    Route("/checkout/{session_id}", checkout_page, methods=["GET"]),
    Route("/checkout/{session_id}/passengers", save_passengers, methods=["POST"]),
    Route("/checkout/{session_id}/confirm", confirm_checkout, methods=["POST"]),
    Route("/checkout/{session_id}/success", success_page, methods=["GET"]),
]


# ============================================================================
# Checkout Flow - MCP Tool
# ============================================================================

@mcp.tool(
    name="duffel_create_checkout",
    annotations={
        "title": "Create Checkout Session",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True
    }
)
async def duffel_create_checkout(params: CreateCheckoutInput, ctx: Context) -> str:
    """
    Create a checkout session with a payment link for completing a flight booking.

    This tool creates a secure checkout page where the customer can enter their
    payment details. After successful payment, the booking is automatically confirmed.

    Use this instead of duffel_create_order when you want the customer to pay
    via credit/debit card through a secure web form.

    Args:
        params (CreateCheckoutInput): Contains:
            - offer_id: The flight offer to book
            - passengers: Complete passenger details (name, DOB, contact info)

    Returns:
        A checkout URL that the customer can visit to complete payment.
        The URL is valid for 30 minutes.

    Example:
        After finding a flight offer, create a checkout:
        - offer_id: "off_00009htYpSCXrwaB9DnUm0"
        - passengers: [{id, given_name, family_name, born_on, email, phone_number, title, gender}]
    """
    try:
        await ctx.report_progress(0.1, "Fetching offer details...")

        # 1. Fetch the current offer to get pricing and validate it's still available
        offer_data = await _get_offer_details(params.offer_id)

        offer_amount = float(offer_data.get("total_amount", 0))
        offer_currency = offer_data.get("total_currency", "USD")

        if offer_amount <= 0:
            return "Error: Invalid offer amount. The offer may have expired."

        # Validate passenger count matches offer
        offer_passengers = offer_data.get("passengers", [])
        if len(params.passengers) != len(offer_passengers):
            return f"Error: Passenger count mismatch. Offer has {len(offer_passengers)} passenger(s), but {len(params.passengers)} provided."

        await ctx.report_progress(0.3, "Creating payment intent...")

        # 2. Calculate payment amount (including Duffel fee buffer)
        payment_amount = _calculate_payment_amount(offer_amount, offer_currency)

        # 3. Create the payment intent
        payment_intent = await _create_payment_intent(payment_amount, offer_currency)

        await ctx.report_progress(0.6, "Creating checkout session...")

        # 4. Create and store the checkout session
        session_id = str(uuid.uuid4())
        now = datetime.utcnow()

        session = CheckoutSession(
            session_id=session_id,
            offer_id=params.offer_id,
            offer_data=offer_data,
            passengers=params.passengers,
            payment_intent_id=payment_intent["id"],
            client_token=payment_intent["client_token"],
            amount=payment_amount,
            currency=offer_currency,
            created_at=now,
            expires_at=now + timedelta(minutes=CHECKOUT_SESSION_TTL_MINUTES),
            status="pending"
        )

        session_store.save(session)

        await ctx.report_progress(0.9, "Generating checkout URL...")

        # 5. Generate the checkout URL
        if CHECKOUT_BASE_URL:
            checkout_url = f"{CHECKOUT_BASE_URL}/checkout/{session_id}"
        else:
            # For local development, provide instructions
            checkout_url = f"/checkout/{session_id}"

        logger.info(
            "Checkout session created: %s for offer %s, amount %s %s",
            session_id, params.offer_id, offer_currency, payment_amount
        )

        await ctx.report_progress(1.0, "Checkout ready")

        # Format response
        lines = ["# Checkout Session Created\n"]
        lines.append("## Flight Summary")
        lines.append(f"- **Price**: {offer_currency} {offer_amount:.2f}")
        lines.append(f"- **Payment Amount**: {offer_currency} {payment_amount} (includes processing fee)")
        lines.append(f"- **Passengers**: {len(params.passengers)}")

        # Add flight details
        for i, slice_data in enumerate(offer_data.get("slices", []), 1):
            segments = slice_data.get("segments", [])
            if segments:
                first_seg = segments[0]
                last_seg = segments[-1]
                origin = first_seg.get("origin", {}).get("iata_code", "?")
                dest = last_seg.get("destination", {}).get("iata_code", "?")
                dep_time = first_seg.get("departing_at", "")[:16].replace("T", " ")
                lines.append(f"- **Flight {i}**: {origin} → {dest} on {dep_time}")

        lines.append(f"\n## 💳 Payment Link")
        lines.append(f"\n**[Click here to complete payment]({checkout_url})**")
        lines.append(f"\n`{checkout_url}`")
        lines.append(f"\n⏰ This link expires in {CHECKOUT_SESSION_TTL_MINUTES} minutes.")
        lines.append("\n---")
        lines.append("The customer should open this link to enter their payment details securely.")
        lines.append("Once payment is complete, the booking will be confirmed automatically.")

        return "\n".join(lines)

    except httpx.HTTPStatusError as e:
        logger.error("Failed to create checkout: %s", e.response.text)
        try:
            error_data = e.response.json()
            error_msg = error_data.get("errors", [{}])[0].get("message", str(e))
        except:
            error_msg = str(e)
        return f"Error creating checkout: {error_msg}"
    except Exception as e:
        logger.exception("Checkout creation error: %s", str(e))
        return f"Error: {str(e)}"


@mcp.tool(
    name="duffel_get_booking_link",
    annotations={
        "title": "Get Booking Link",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True
    }
)
async def duffel_get_booking_link(
    reference: Optional[str] = None,
    currency: str = "USD",
    *,
    ctx: Context
) -> str:
    """
    Get a branded booking link where customers can search and book flights.

    This uses Duffel Links - a hosted booking interface. The customer gets a
    professional booking page where they can:
    - Search for flights
    - Select their preferred option
    - Enter passenger details
    - Complete payment

    No need to collect any personal info in chat!

    Args:
        reference: Optional user/session reference for tracking (auto-generated if not provided)
        currency: Currency for prices (default: USD)
        ctx: MCP context

    Returns:
        A booking URL for the customer to complete their flight booking.
    """
    try:
        await ctx.report_progress(0.2, "Creating booking session...")

        # Generate reference if not provided
        if not reference:
            reference = f"booking_{uuid.uuid4().hex[:12]}"

        # Create Duffel Links session
        session_data = await _create_duffel_links_session(
            reference=reference,
            currency=currency
        )

        await ctx.report_progress(0.8, "Booking link ready...")

        booking_url = session_data.get("url", "")
        session_id = session_data.get("id", "")

        if not booking_url:
            return "Error: Failed to get booking URL from Duffel Links"

        logger.info(
            "Duffel Links session created: %s (ref: %s)",
            session_id, reference
        )

        await ctx.report_progress(1.0, "Done")

        # Format response
        lines = [
            f"**Book your flight: {booking_url}**",
            "",
            "This link opens a professional booking page where you can:",
            "- Search and compare flights",
            "- Enter passenger details",
            "- Complete secure payment",
            "",
            "The link expires in 24 hours.",
            f"Reference: {reference}"
        ]

        return "\n".join(lines)

    except httpx.HTTPStatusError as e:
        logger.error("Failed to create booking link: %s", e.response.text)
        try:
            error_data = e.response.json()
            error_msg = error_data.get("errors", [{}])[0].get("message", str(e))
        except:
            error_msg = str(e)
        return f"Error creating booking link: {error_msg}"
    except Exception as e:
        logger.exception("Booking link creation error: %s", str(e))
        return f"Error: {str(e)}"


# ============================================================================
# Main Entry Point
# ============================================================================

# Known vulnerability scanner paths to block immediately
SCANNER_PATHS = {
    ".php", ".asp", ".aspx", ".jsp", ".action", ".do", ".cgi",
    "/wp-", "/wordpress", "/admin", "/phpmyadmin", "/mysql",
    "/Public/", "/common/template", "/wap/api", "/leftDao",
    "/getConfig", "/.env", "/.git", "/config", "/backup",
    "/shell", "/cmd", "/exec", "/eval", "/system",
}

# Known malicious user agents
SCANNER_USER_AGENTS = {
    "zgrab", "masscan", "nmap", "nikto", "sqlmap", "dirbuster",
    "gobuster", "wfuzz", "nuclei", "httpx", "curl/", "python-requests",
}


class ScannerProtectionMiddleware:
    """Starlette middleware to block vulnerability scanners and attack traffic."""

    def __init__(self, app):
        self.app = app
        self._blocked_ips: Dict[str, float] = {}  # IP -> block expiry timestamp
        self._scanner_hits: Dict[str, int] = {}  # IP -> scanner path hit count
        self.BLOCK_DURATION = 300  # 5 minutes
        self.SCANNER_BLOCK_THRESHOLD = 3  # Block after 3 scanner-like requests

    def _get_client_ip(self, request: Request) -> str:
        """Extract client IP from request, handling proxies."""
        # Check X-Forwarded-For header (Railway/proxy)
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            # Take the first IP in the chain
            return forwarded.split(",")[0].strip()
        # Fall back to direct connection
        if request.client:
            return request.client.host
        return "unknown"

    def _is_blocked(self, ip: str) -> bool:
        """Check if an IP is currently blocked."""
        if ip in self._blocked_ips:
            if datetime.utcnow().timestamp() < self._blocked_ips[ip]:
                return True
            else:
                # Block expired
                del self._blocked_ips[ip]
                self._scanner_hits.pop(ip, None)
        return False

    def _block_ip(self, ip: str):
        """Block an IP address."""
        expiry = datetime.utcnow().timestamp() + self.BLOCK_DURATION
        self._blocked_ips[ip] = expiry
        logger.warning("Blocked IP %s for %d seconds due to abuse", ip, self.BLOCK_DURATION)

    def _is_scanner_path(self, path: str) -> bool:
        """Check if the path looks like a vulnerability scanner probe."""
        path_lower = path.lower()
        return any(scanner in path_lower for scanner in SCANNER_PATHS)

    def _is_scanner_ua(self, user_agent: str) -> bool:
        """Check if the user agent looks like a scanner."""
        if not user_agent:
            return False
        ua_lower = user_agent.lower()
        return any(scanner in ua_lower for scanner in SCANNER_USER_AGENTS)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        client_ip = self._get_client_ip(request)
        path = request.url.path
        user_agent = request.headers.get("user-agent", "")

        # Check if IP is blocked
        if self._is_blocked(client_ip):
            logger.debug("Rejected request from blocked IP: %s", client_ip)
            response = JSONResponse(
                {"error": "Too many requests. Please try again later."},
                status_code=429
            )
            await response(scope, receive, send)
            return

        # Block scanner user agents immediately
        if self._is_scanner_ua(user_agent):
            logger.warning("Blocked scanner user agent from %s: %s", client_ip, user_agent[:50])
            self._block_ip(client_ip)
            response = JSONResponse({"error": "Forbidden"}, status_code=403)
            await response(scope, receive, send)
            return

        # Detect and block vulnerability scanners by path
        if self._is_scanner_path(path):
            self._scanner_hits[client_ip] = self._scanner_hits.get(client_ip, 0) + 1
            logger.warning("Scanner path detected from %s: %s (hits: %d)",
                         client_ip, path, self._scanner_hits[client_ip])

            if self._scanner_hits[client_ip] >= self.SCANNER_BLOCK_THRESHOLD:
                self._block_ip(client_ip)
                response = JSONResponse({"error": "Forbidden"}, status_code=403)
                await response(scope, receive, send)
                return

            # Return 404 but don't process further
            response = JSONResponse({"error": "Not found"}, status_code=404)
            await response(scope, receive, send)
            return

        # Process the request normally - Duffel's own rate limiting will apply
        await self.app(scope, receive, send)


def create_combined_app(mcp_app):
    """Create a Starlette app that combines MCP SSE routes with checkout routes."""
    # The checkout routes are served at /checkout/*
    # The MCP SSE routes are served at /sse and /messages (mounted at root)
    routes = checkout_routes + [
        Mount("/", app=mcp_app),
    ]
    app = Starlette(routes=routes)

    # Add scanner protection middleware (blocks vulnerability scanners)
    if SCANNER_PROTECTION_ENABLED:
        logger.info("Scanner protection enabled - blocking vulnerability scanners")
        return ScannerProtectionMiddleware(app)

    return app


def main():
    """Run the Duffel MCP server with configurable transport."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Duffel MCP Server - Flight search and booking via MCP"
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="stdio",
        help="Transport type: 'stdio' (default) for CLI, 'sse' for HTTP Server-Sent Events"
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host for SSE transport (default: 127.0.0.1)"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port for SSE transport (default: 8000)"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging"
    )

    args = parser.parse_args()

    # Configure logging level
    if args.debug:
        logging.getLogger("duffel_mcp").setLevel(logging.DEBUG)
        logger.debug("Debug logging enabled")

    logger.info("Starting Duffel MCP server with transport: %s", args.transport)

    if args.transport == "stdio":
        # Standard stdio transport for CLI tools
        mcp.run()
    elif args.transport == "sse":
        # HTTP with Server-Sent Events for web deployments
        # Combined with checkout routes for payment flow
        logger.info("SSE server with checkout starting on http://%s:%d", args.host, args.port)
        logger.info("Checkout pages available at http://%s:%d/checkout/{session_id}", args.host, args.port)

        # Get the MCP SSE app and combine with checkout routes
        mcp_sse_app = mcp.http_app(transport="sse")
        combined_app = create_combined_app(mcp_sse_app)

        uvicorn.run(
            combined_app,
            host=args.host,
            port=args.port,
            log_level="info" if not args.debug else "debug"
        )


if __name__ == "__main__":
    main()
