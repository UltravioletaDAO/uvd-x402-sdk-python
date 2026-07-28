"""
Live traffic stream client for x402 SDK (``GET /events``, Server-Sent Events).

The facilitator emits one event per operation it handles, so you can render or
react to live traffic without polling and without scraping logs.

Example:
    >>> from uvd_x402_sdk.events import TrafficEventStream
    >>>
    >>> with TrafficEventStream() as stream:
    ...     for event in stream:
    ...         print(event.kind, event.network, event.ok, event.tx)

    >>> # Only settlements on the chains you care about. The facilitator has NO
    >>> # server-side filter by network, so this is applied here, client-side.
    >>> with TrafficEventStream(networks=["base", "polygon"], kinds=["settle"]) as s:
    ...     for event in s:
    ...         print(event.tx)

    >>> # Async, for an event loop
    >>> async with TrafficEventStream() as stream:
    ...     async for event in stream:
    ...         print(event.network)

Three properties of this stream decide how you should use it:

**It is lossy by design.** The facilitator will never slow down or fail a payment
to keep an observer in sync, so an event you were not connected for, or one that
arrived while you were behind, is simply gone. Treat it as a live hint and use
the chain as the source of truth. In particular, absence of events is NOT
evidence that nothing happened.

**Failed operations are not published.** Only operations that resolved emit an
event, so ``ok=False`` means "resolved and came back negative", never "blew up".
A stream that looks healthy is not proof that the rail is.

**Admission is bounded.** The endpoint is public and unauthenticated, so it sheds
with HTTP 503 and a ``Retry-After`` once too many subscribers are connected, and
returns 404 when the operator has disabled it. Both surface as
:class:`~uvd_x402_sdk.exceptions.FacilitatorError` with the status code intact.
"""

import json
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, Iterable, Iterator, List, Optional, Sequence

import httpx
from pydantic import BaseModel

from uvd_x402_sdk.exceptions import FacilitatorError

#: Default facilitator, matching the rest of the SDK.
DEFAULT_FACILITATOR_URL = "https://facilitator.ultravioletadao.xyz"

#: Operations the facilitator publishes. `settle` carries a transaction hash;
#: `verify` never does, because nothing has settled yet.
EVENT_KINDS = ("verify", "settle")

#: The stream sends a `:keepalive` comment on this cadence, so a read timeout
#: must comfortably exceed it or the connection dies on an idle rail.
KEEPALIVE_INTERVAL_SECONDS = 15.0


class TrafficEvent(BaseModel):
    """One facilitator operation, as seen by an observer.

    Optional fields are *omitted* by the facilitator rather than sent as null,
    and they are all absent when the operator runs the stream in `minimal`
    detail mode. Never assume ``payer`` or ``amount`` is present.
    """

    #: Unix epoch **milliseconds**, UTC. Note: milliseconds, not seconds.
    ts: int
    #: ``"verify"`` or ``"settle"``.
    kind: str
    #: The facilitator's canonical network slug, the same one ``/supported``
    #: uses. Beware: this is the *canonical* name, which is not always the
    #: alias you are allowed to send. ``skale`` is accepted on the way in but
    #: this field always says ``skale-base``. Match on the canonical form.
    network: str
    #: Did the operation succeed? False means it resolved negative, not that it
    #: errored — errors are not published at all.
    ok: bool
    payer: Optional[str] = None
    #: Present on ``settle``, absent on ``verify``.
    tx: Optional[str] = None
    amount: Optional[str] = None
    asset: Optional[str] = None

    @property
    def timestamp(self) -> datetime:
        """:attr:`ts` as a timezone-aware UTC datetime."""
        return datetime.fromtimestamp(self.ts / 1000, tz=timezone.utc)


def _sse_events(lines: Iterable[str]) -> Iterator[Dict[str, str]]:
    """Turn a stream of SSE lines into ``{event, data}`` dicts.

    Implements the parts of the Server-Sent Events wire format the facilitator
    uses: ``event:``/``data:`` fields, a blank line to dispatch, and ``:``
    comments for keepalives. ``id:`` and ``retry:`` are accepted and ignored
    rather than treated as an error, so a future facilitator that starts
    sending them does not break this parser.
    """
    event_name = ""
    data_lines: List[str] = []

    for raw in lines:
        line = raw.rstrip("\r")

        if not line:
            # Blank line dispatches. A dispatch with no data is a no-op per spec.
            if data_lines:
                yield {"event": event_name or "message", "data": "\n".join(data_lines)}
            event_name = ""
            data_lines = []
            continue

        if line.startswith(":"):
            # Comment. This is what a keepalive looks like — deliberately silent.
            continue

        field, _, value = line.partition(":")
        # A single leading space after the colon is part of the framing, not the value.
        if value.startswith(" "):
            value = value[1:]

        if field == "event":
            event_name = value
        elif field == "data":
            data_lines.append(value)
        # id / retry: accepted and ignored.


def _raise_for_stream_status(response: httpx.Response, body: str) -> None:
    """Translate the two rejections this endpoint has into a typed error."""
    status = response.status_code
    if status == 404:
        raise FacilitatorError(
            message=(
                "GET /events is disabled on this facilitator "
                "(operator set X402_EVENTS_ENABLED=false)"
            ),
            status_code=status,
            response_body=body,
        )
    if status == 503:
        retry_after = response.headers.get("retry-after", "unknown")
        raise FacilitatorError(
            message=(
                f"GET /events is at subscriber capacity; retry after {retry_after}s. "
                "This is admission control, not an outage."
            ),
            status_code=status,
            response_body=body,
        )
    raise FacilitatorError(
        message=f"GET /events failed: {status}",
        status_code=status,
        response_body=body,
    )


class TrafficEventStream:
    """Subscribe to the facilitator's live traffic stream.

    Usable synchronously (``with`` + ``for``) or asynchronously (``async with``
    + ``async for``). Both yield :class:`TrafficEvent`.

    Args:
        base_url: Facilitator to subscribe to.
        networks: If given, only yield events whose ``network`` is in this set.
            Applied **client-side**: the facilitator has no per-network filter,
            so every event still crosses the wire. Matched against the canonical
            slug, so use ``skale-base``, not ``skale``.
        kinds: If given, only yield these operations (``verify`` / ``settle``).
        connect_timeout: Timeout for establishing the connection. There is
            deliberately no read timeout — an idle rail is normal, and the
            keepalive is what proves the connection is alive.
        headers: Extra headers, for a facilitator deployment that gates the
            stream behind authorization.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_FACILITATOR_URL,
        *,
        networks: Optional[Sequence[str]] = None,
        kinds: Optional[Sequence[str]] = None,
        connect_timeout: float = 10.0,
        headers: Optional[Dict[str, str]] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.networks = {n.lower() for n in networks} if networks else None
        self.kinds = {k.lower() for k in kinds} if kinds else None
        self.headers = {"Accept": "text/event-stream", **(headers or {})}
        # read=None: the stream is long-lived and idle between events by nature.
        # Anything finite here would kill a healthy connection on a quiet rail.
        self._timeout = httpx.Timeout(
            connect=connect_timeout, read=None, write=connect_timeout, pool=connect_timeout
        )
        self._client: Optional[httpx.Client] = None
        self._aclient: Optional[httpx.AsyncClient] = None

    @property
    def url(self) -> str:
        return f"{self.base_url}/events"

    def _wanted(self, event: TrafficEvent) -> bool:
        if self.kinds is not None and event.kind.lower() not in self.kinds:
            return False
        if self.networks is not None and event.network.lower() not in self.networks:
            return False
        return True

    def _to_event(self, frame: Dict[str, str]) -> Optional[TrafficEvent]:
        """Parse one SSE frame, skipping anything malformed.

        A single unparseable frame must not tear down a long-lived stream: the
        connection is worth more than the message.
        """
        try:
            payload: Any = json.loads(frame["data"])
        except (ValueError, KeyError):
            return None
        if not isinstance(payload, dict):
            return None
        # The SSE event name is authoritative for `kind`; the JSON body carries
        # it too, and they agree, but the framing is the contract.
        payload.setdefault("kind", frame.get("event", ""))
        try:
            return TrafficEvent(**payload)
        except Exception:
            return None

    # -- sync ---------------------------------------------------------------

    def __enter__(self) -> "TrafficEventStream":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def __iter__(self) -> Iterator[TrafficEvent]:
        """Yield events until the connection ends or the caller stops iterating."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.Client(timeout=self._timeout)
        with self._client.stream("GET", self.url, headers=self.headers) as response:
            if response.status_code != 200:
                _raise_for_stream_status(response, response.read().decode("utf-8", "replace"))
            for frame in _sse_events(response.iter_lines()):
                event = self._to_event(frame)
                if event is not None and self._wanted(event):
                    yield event

    # -- async --------------------------------------------------------------

    async def __aenter__(self) -> "TrafficEventStream":
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._aclient is not None:
            await self._aclient.aclose()
            self._aclient = None

    async def __aiter__(self) -> AsyncIterator[TrafficEvent]:
        if self._aclient is None or self._aclient.is_closed:
            self._aclient = httpx.AsyncClient(timeout=self._timeout)
        async with self._aclient.stream("GET", self.url, headers=self.headers) as response:
            if response.status_code != 200:
                body = (await response.aread()).decode("utf-8", "replace")
                _raise_for_stream_status(response, body)
            buffer: List[str] = []
            async for line in response.aiter_lines():
                buffer.append(line)
                # Dispatch frame-by-frame rather than buffering the whole stream:
                # it never ends, so batching would mean never yielding.
                if line == "":
                    for frame in _sse_events(buffer):
                        event = self._to_event(frame)
                        if event is not None and self._wanted(event):
                            yield event
                    buffer = []
