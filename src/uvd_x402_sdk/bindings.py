"""
The purchase binding of a payment, persisted BEFORE the facilitator is called.

A seller that settles in a short-lived process (AWS Lambda above all) has one
failure mode :class:`~uvd_x402_sdk.client.X402Client` cannot close by itself:
the process dies between the settle and whatever it does next (delivering,
recording the order). The facilitator settled; the buyer got nothing and, as
every 503 of this SDK asks, presents the SAME ``X-PAYMENT`` again. That resend
lands in another process, which knows nothing of the first one.

With the facilitator's receipt rail (x402-rs 2.39.0, ``docs/
facilitator-receipts.md``, "Replays of an admitted authorization") the original
answer of an admitted payment goes back ONLY to the ``Idempotency-Key`` that
admitted it. A fresh key per request, the SDK's default since 0.89.0, gets
``409 authorization_already_settled`` there: the buyer paid and the seller can
no longer deliver. The key has to outlive the process that minted it, and that
is what a :class:`BindingStore` is: one row per payment, written before the
facilitator is called, so it exists however the rest fails.

Ported from describe.net's paywall, where it runs in production, with the same
semantics and the four conditions under which the persisted key was accepted:

(a) Minting is a conditional insert followed by a re-read. Two first requests
    for one payment, at the same time, leave with the same key: two keys for
    one payment are two purchases for the facilitator, and the second one gets
    ``409 authorization_in_flight`` for a payment that is its own.
(b) The key is random and stored (:func:`~uvd_x402_sdk.client.new_idempotency_key`),
    never derived from the ``X-PAYMENT``: whoever holds the payment could
    recompute a derived key, and holding the payment is exactly what the
    facilitator refuses to accept as a binding. The row keys on the payment's
    hash (:func:`payment_key`); the header itself, a bearer credential, is
    never stored.
(c) A store that cannot read or mint answers ``None`` and never raises, and the
    seller then FAILS CLOSED: 503, the facilitator is not called, nothing is
    charged or delivered. Never "go on without a key": a charge whose key was
    not stored is one the resend cannot recover.
(d) The row is per PAYMENT and keeps the resource that owns it, the first one
    it was presented for. The same header for another resource gets that owner
    back, and the seller answers 409 without calling the facilitator.

The row keys on the header's bytes, so the same signed authorization encoded
another way (other JSON whitespace, a character base64 decoding drops) is a new
row and a first presentation. That is why a first presentation keeps the
client's guard of a fresh key (:class:`~uvd_x402_sdk.client._Binding`): a
replay under a key minted for THIS request, before any attempt of this request
ended without a verdict, is another request's purchase and is refused, as
without a store. Against x402-rs 2.36 to 2.38, which replayed to any resend of
the same terms whatever its key, that guard is what keeps "one payment buys one
resource" for a re-encoded header; 2.39.0 refuses such a resend itself.

``expires_at`` is the ceiling of the bearer window for the KEY, never for the
owner. The facilitator's receipts do not expire: while the key lives, a resend
of the same ``X-PAYMENT`` to the same resource recovers the purchase (a replay,
not a second charge); once it expires, the resend gets a new key and the
facilitator answers 409 with the receipt. A resend never extends a live key, so
a caller resending every few minutes cannot stretch the window forever.

:func:`process_payment_bound` is the seller's side of the rule, and what the
FastAPI and Lambda integrations run when they get a ``binding_store``. Without
one they behave exactly as before.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Protocol
from urllib.parse import quote

from uvd_x402_sdk.client import X402Client, _undelivered_response, new_idempotency_key
from uvd_x402_sdk.config import X402Config
from uvd_x402_sdk.exceptions import (
    PAYMENT_ALREADY_USED,
    PAYMENT_PRESENTED_BEFORE,
    PAYMENT_STORE_UNAVAILABLE,
    PaymentBindingError,
    X402Error,
)
from uvd_x402_sdk.models import PaymentResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# The window of a key, derived and not picked by hand.
#
# Floor 300 s. The SDK waits up to `verify_timeout` (30 s) and then up to
# `settle_timeout` (55 s): 85 s for ONE attempt at worst. A common HTTP client
# gives up at 60 s and retries, so two or three rounds are already ~255 s. A
# window shorter than the buyer's retry horizon would expire right before the
# resend it exists for.
#
# Ceiling 1800 s. While the key lives, whoever holds the X-PAYMENT reads that
# resource again. With Ethereum L1's settle timeout (900 s) the formula would
# give 2790 s; the ceiling keeps a timeout change from minting a long-lived
# bearer token without anyone noticing.
# ---------------------------------------------------------------------------
BINDING_WINDOW_FLOOR_SECONDS = 300
BINDING_WINDOW_CEILING_SECONDS = 1800
_BINDING_RETRY_FACTOR = 3

#: How long one statement of :class:`PostgresBindingStore` may wait (a lock, a
#: hung database) before giving up, which is failing closed. Measured by
#: describe.net against a local Postgres, 300 calls: ``bind`` p50 0.70 ms, max
#: 7.01 ms. A primary-key read and write on a small table; what reaches 1000 ms
#: is a pathology, not load, and without a ceiling it eats the Lambda's budget
#: and the buyer sees API Gateway's empty 5xx instead of the 503 that says what
#: to do.
POSTGRES_STATEMENT_TIMEOUT_MS = 1000

#: Ceiling on live entries of :class:`InMemoryBindingStore`, oldest out first.
MAX_IN_MEMORY_BINDINGS = 10_000

#: The table :class:`PostgresBindingStore` expects. The schema belongs to the
#: seller's migrations, not to the payment path: apply it there, BEFORE the code
#: that uses it, because a store without its table fails closed and every paid
#: request answers 503. ``expires_at`` expires the KEY, never the owner, and the
#: index is only for :meth:`PostgresBindingStore.sweep`, which nothing calls.
POSTGRES_BINDINGS_SCHEMA = """\
CREATE TABLE IF NOT EXISTS payment_bindings (
    payment_key      TEXT PRIMARY KEY,
    resource         TEXT        NOT NULL,
    idempotency_key  TEXT        NOT NULL,
    expires_at       TIMESTAMPTZ NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS payment_bindings_expires_at
    ON payment_bindings (expires_at);
"""

#: The key schema of the table :class:`DynamoDBBindingStore` expects, in the
#: shape ``create_table`` takes (``client.create_table(TableName=...,
#: BillingMode="PAY_PER_REQUEST", **DYNAMODB_BINDINGS_KEY_SCHEMA)``); write the
#: same in Terraform. Do NOT enable DynamoDB TTL on ``expires_at``: it would
#: delete the row, and the row is also the payment's owner and the proof it was
#: presented before (see :meth:`PostgresBindingStore.sweep`).
DYNAMODB_BINDINGS_KEY_SCHEMA: dict[str, Any] = {
    "AttributeDefinitions": [{"AttributeName": "payment_key", "AttributeType": "S"}],
    "KeySchema": [{"AttributeName": "payment_key", "KeyType": "HASH"}],
}

_SQL_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?")


def payment_key(x_payment_header: str) -> str:
    """The fingerprint of a PAYMENT, the key of its binding: sha256 of the header.

    Computed over the header exactly as it arrived: a buyer's resend repeats it
    byte for byte, and normalising it first would unhook the binding from the
    resend it exists for. The header itself is a signed authorization, money to
    the bearer; only recognising it is needed, and its hash does that.
    """
    return hashlib.sha256(x_payment_header.encode("utf-8")).hexdigest()


#: What stays literal in the path of a resource. Everything else is
#: percent-encoded, ``%``, ``?``, ``#`` and space included, so two paths never
#: meet in one string: ``/gen/cats%23x`` (a path whose handler sees ``cats#x``)
#: can no longer read as ``/gen/cats``.
_PATH_SAFE = "/!$&'()*+,;=:@-._~"
#: The query is kept as received; only what could end it (a space, a ``#``)
#: and non-ASCII are encoded.
_QUERY_SAFE = "!$&'()*+,;=:@/?-._~%"


def purchase_resource(method: str, path: str, query: str = "", body: bytes = b"") -> str:
    """What one request buys, as a binding compares it: ``"GET /path?query"``.

    ``path`` is the path the application routes on (decoded, as ASGI's
    ``scope["path"]``), and it is percent-encoded here so that the string is
    unambiguous: a decoded ``#`` or ``?`` inside a path segment cannot make one
    path read as another, nor move text into the query. A non-empty body enters
    as its sha256 (``" sha256:<hex>"``): two POSTs to the same path with
    different bodies are two purchases, and with the body left out the same
    ``X-PAYMENT`` presented with another body would get the facilitator's replay
    of the first one and be delivered for free. A buyer's resend repeats the
    request byte for byte, so it maps to the same resource.

    Build it from what the application serves, never from a URL rebuilt out of
    headers (``Host`` included): one derivation per deployment, because two that
    build this string differently disagree about which resource owns a payment.
    What it leaves out is not part of the purchase: an answer that depends on
    the caller (``Authorization``, cookies) is served again, within the window,
    to whoever resends the same request with the same ``X-PAYMENT``.
    """
    resource = f"{method.upper()} {quote(path, safe=_PATH_SAFE)}"
    if query:
        resource += f"?{quote(query, safe=_QUERY_SAFE)}"
    if body:
        resource += f" sha256:{hashlib.sha256(body).hexdigest()}"
    return resource


def binding_window_seconds(config: X402Config) -> int:
    """How long a key lives: ``(verify_timeout + settle_timeout) * 3``, clamped
    to [:data:`BINDING_WINDOW_FLOOR_SECONDS`, :data:`BINDING_WINDOW_CEILING_SECONDS`]."""
    derived = int((config.verify_timeout + config.settle_timeout) * _BINDING_RETRY_FACTOR)
    return min(BINDING_WINDOW_CEILING_SECONDS, max(BINDING_WINDOW_FLOOR_SECONDS, derived))


@dataclass(frozen=True)
class Binding:
    """The binding of one payment: which resource it belongs to, and its key.

    ``resource`` is the OWNER of the payment, the first resource it was
    presented for, not necessarily the one being requested. Whether the two
    match is the seller's decision (:func:`process_payment_bound`), not the
    store's nor the facilitator's.
    """

    resource: str
    idempotency_key: str
    #: Was this payment presented here for the FIRST time by this request? The
    #: only thing a seller knows by itself about whether the payment could have
    #: moved before: if the binding already existed, an earlier attempt reached
    #: the facilitator. Out of equality on purpose: two reads of one binding are
    #: the same binding, whoever makes them.
    new: bool = field(default=False, compare=False)


class BindingStore(Protocol):
    """Where the binding of each payment lives.

    ``bind`` returns the payment's binding: the existing one (live, or with a
    renewed key when it expired and the resource is the same) or a new one
    with a random key. ``None`` when it could neither read nor mint, and then
    the seller fails closed (503 without calling the facilitator). It never
    raises.
    """

    def bind(self, payment_key: str, resource: str, ttl_seconds: float) -> Binding | None:
        ...


class InMemoryBindingStore:
    """Bindings in the process's memory.

    RIGHT FOR ONE PROCESS (a local server, one container), NOT ENOUGH for
    Lambda or several replicas: a resend that lands in another instance or
    after a cold start finds no binding and gets a new key, and the
    facilitator's replay does not go back to it. For those, a shared store
    (:class:`PostgresBindingStore`, :class:`DynamoDBBindingStore`).

    It takes a lock: two requests with the same header running at once in a
    thread pool that minted TWO keys would be two purchases for the
    facilitator.

    Expiry renews the KEY, never the owner: an expired binding is not pruned,
    because what says "this payment belongs to resource A" has to keep saying
    so after the window. Only the entry ceiling removes one, oldest first. The
    clock is ``monotonic``: a change of the system time can neither expire nor
    extend a key.
    """

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        max_entries: int = MAX_IN_MEMORY_BINDINGS,
        new_key: Callable[[], str] = new_idempotency_key,
    ) -> None:
        self._clock = clock
        self._max_entries = max_entries
        self._new_key = new_key
        self._entries: dict[str, tuple[float, str, str]] = {}
        self._lock = threading.Lock()

    def bind(self, payment_key: str, resource: str, ttl_seconds: float) -> Binding | None:
        with self._lock:
            now = self._clock()
            entry = self._entries.get(payment_key)
            if entry is not None:
                deadline, owner, key = entry
                if owner != resource or deadline > now:
                    return Binding(owner, key)
                # Expired and for the same resource: a new key, the same owner.
                key = self._new_key()
                self._entries[payment_key] = (now + ttl_seconds, owner, key)
                return Binding(owner, key)
            if len(self._entries) >= self._max_entries:
                oldest = min(self._entries, key=lambda k: self._entries[k][0])
                del self._entries[oldest]
            key = self._new_key()
            self._entries[payment_key] = (now + ttl_seconds, resource, key)
            return Binding(resource, key, new=True)


def _checked_table(table: str) -> str:
    if not isinstance(table, str) or not _SQL_IDENTIFIER.fullmatch(table):
        raise ValueError(f"table must be a plain SQL identifier, got {table!r}")
    return table


class PostgresBindingStore:
    """Bindings in Postgres: a payment's key outlives the process that minted it.

    ``pool`` is a psycopg 3 pool (``psycopg_pool.ConnectionPool``) or anything
    with the same shape: ``pool.connection()`` a context manager giving a
    connection, ``conn.cursor()`` a context manager, ``%s`` parameters,
    ``conn.commit()``. Tuple rows and ``dict_row`` both work. Use the pool the
    app already has, with a bounded wait for a connection (``timeout=``): a
    store that waits for ever fails the Lambda instead of failing closed. The
    table is :data:`POSTGRES_BINDINGS_SCHEMA`, applied by the app's migrations.

    Where each condition lives (see the module docstring):

    (a) ``INSERT ... ON CONFLICT DO NOTHING`` and a ``SELECT`` in ANOTHER
        statement. Under READ COMMITTED each statement takes its own snapshot,
        and an INSERT that collides with one in flight WAITS for the other to
        commit before doing nothing, so the re-read always sees the winning
        row. (A ``SELECT`` inside the same statement, a CTE, would have the
        snapshot from before the other commit and see nothing.)
    (b) The key comes from ``new_key`` and is stored.
    (c) Any failure is logged and answers ``None``; every statement runs under
        :data:`POSTGRES_STATEMENT_TIMEOUT_MS` (``statement_timeout`` for this
        transaction), so a lock that is never released fails closed in time. A
        connection in autocommit would lose that ceiling, so it is refused the
        same way.
    (d) One row per ``payment_key`` with its owner; a renewal only rewrites the
        key of an EXPIRED row for the SAME resource, and the ``expires_at <=
        NOW()`` of that ``UPDATE`` makes two renewals at once a single one.

    The clock is Postgres's (``NOW()``), the only one shared by every instance:
    a skewed container can neither expire nor extend another's key.
    """

    def __init__(
        self,
        pool: Any,
        table: str = "payment_bindings",
        *,
        new_key: Callable[[], str] = new_idempotency_key,
        statement_timeout_ms: int = POSTGRES_STATEMENT_TIMEOUT_MS,
    ) -> None:
        self._pool = pool
        self._table = _checked_table(table)
        self._new_key = new_key
        self._statement_timeout_ms = statement_timeout_ms

    def bind(self, payment_key: str, resource: str, ttl_seconds: float) -> Binding | None:
        t = self._table
        key = self._new_key()
        try:
            with self._pool.connection() as conn, conn.cursor() as cur:
                if getattr(conn, "autocommit", False):
                    # `set_config(..., true)` lasts one transaction, and in
                    # autocommit every statement is its own: the ceiling of (c)
                    # would silently not apply to the INSERT. Refused, loudly.
                    raise RuntimeError(
                        "PostgresBindingStore needs connections that are not in autocommit"
                    )
                # `set_config(..., true)` and not `SET LOCAL`: psycopg 3 sends
                # parameters to the server, and `SET` does not take a `$1`.
                cur.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (f"{self._statement_timeout_ms}ms",),
                )
                # (a) Mint: only if the payment has no binding.
                cur.execute(
                    f"INSERT INTO {t} (payment_key, resource, idempotency_key, expires_at)"  # noqa: S608
                    " VALUES (%s, %s, %s, NOW() + MAKE_INTERVAL(secs => %s))"
                    " ON CONFLICT (payment_key) DO NOTHING",
                    (payment_key, resource, key, float(ttl_seconds)),
                )
                # A row inserted here = the first presentation of this payment.
                new = cur.rowcount == 1
                # Renew the key of an EXPIRED binding of the SAME resource. The
                # `expires_at <= NOW()` makes two renewals at once one: the
                # second waits for the row lock, re-checks the condition on the
                # renewed row and updates nothing.
                cur.execute(
                    f"UPDATE {t} SET idempotency_key = %s,"  # noqa: S608
                    " expires_at = NOW() + MAKE_INTERVAL(secs => %s)"
                    " WHERE payment_key = %s AND resource = %s AND expires_at <= NOW()",
                    (key, float(ttl_seconds), payment_key, resource),
                )
                # (a) Re-read: the winning row, whoever wrote it.
                cur.execute(
                    f"SELECT resource, idempotency_key FROM {t} WHERE payment_key = %s",  # noqa: S608
                    (payment_key,),
                )
                row = cur.fetchone()
                conn.commit()
        except Exception:
            # (c) Not raised on purpose: `None` is the signal to fail closed.
            logger.exception("bindings: could not read or mint the payment's binding")
            return None
        if row is None:
            return None
        if isinstance(row, dict):
            return Binding(row["resource"], row["idempotency_key"], new=new)
        return Binding(row[0], row[1], new=new)

    def sweep(self) -> int:
        """Delete expired bindings. Returns how many.

        NOT HARMLESS, and nothing in this SDK calls it. A row holds two
        guarantees and deleting it breaks both:

        1. "this payment belongs to resource A" (condition (d)): without the
           row the payment reaches the facilitator again for any resource, and
           the rule depends again on the facilitator tying its replay to the
           key;
        2. "this payment was presented here before" (:attr:`Binding.new`):
           without the row, the resend of a payment that moved is a "first
           presentation" again, and an opaque refusal of it is a 402 over a
           payment that was charged.

        Whoever schedules it decides first how long a row is worth: never less
        than the authorization's ``validBefore`` (or leave a tombstone with the
        ``payment_key``).
        """
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(f"DELETE FROM {self._table} WHERE expires_at <= NOW()")  # noqa: S608
            count = int(cur.rowcount)
            conn.commit()
        return count


def _conditional_check_failed(exc: Exception) -> bool:
    """Is ``exc`` botocore's ``ConditionalCheckFailedException``? Read off the
    error response, so this module never imports botocore."""
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    error = response.get("Error")
    return isinstance(error, dict) and error.get("Code") == "ConditionalCheckFailedException"


def _dynamodb_number(value: float) -> dict[str, str]:
    return {"N": f"{value:.6f}"}


class DynamoDBBindingStore:
    """Bindings in DynamoDB: the same semantics as :class:`PostgresBindingStore`.

    ``client`` is a boto3 DynamoDB client (``boto3.client("dynamodb")``). Give
    it bounded timeouts and few retries, which is condition (c) here: a store
    that waits for ever fails the Lambda instead of failing closed::

        boto3.client("dynamodb", config=botocore.config.Config(
            connect_timeout=1, read_timeout=1, retries={"max_attempts": 2}))

    The table has ``payment_key`` (string) as its only key
    (:data:`DYNAMODB_BINDINGS_KEY_SCHEMA`); items also carry ``resource``,
    ``idempotency_key``, ``expires_at`` and ``created_at`` (epoch seconds).

    The conditions, each one conditional write atomic on its item:

    (a) ``PutItem`` if ``attribute_not_exists(payment_key)``: exactly one first
        request wins and is :attr:`Binding.new`; the others fall through to the
        renewal and then to a ``ConsistentRead`` of the winning item.
    (b) The key comes from ``new_key`` and is stored.
    (c) Any failure other than a failed condition is logged and answers
        ``None``. A put that was applied but whose answer was lost, and whose
        retry then failed its condition, is still recognised as this request's
        first presentation (the re-read item carries the key minted here).
    (d) The renewal is ``UpdateItem`` if the stored resource is this one AND
        ``expires_at <= now``: two renewals at once are one, and another
        resource never takes the row.

    THE ONE DIFFERENCE, and it is the clock. DynamoDB has no server clock a
    condition can read, so ``now`` is this process's wall clock (``clock``).
    An instance whose clock runs ahead can renew a key before its window ends:
    a resend then carries a key the facilitator did not admit and gets 409
    with the receipt, the same as a resend after the window. It cannot charge
    twice nor change the owner.
    """

    def __init__(
        self,
        client: Any,
        table_name: str = "payment_bindings",
        *,
        new_key: Callable[[], str] = new_idempotency_key,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._client = client
        self._table = table_name
        self._new_key = new_key
        self._clock = clock

    def bind(self, payment_key: str, resource: str, ttl_seconds: float) -> Binding | None:
        key = self._new_key()
        now = self._clock()
        item_key = {"payment_key": {"S": payment_key}}
        try:
            try:
                # (a) Mint: only if the payment has no binding.
                self._client.put_item(
                    TableName=self._table,
                    Item={
                        **item_key,
                        "resource": {"S": resource},
                        "idempotency_key": {"S": key},
                        "expires_at": _dynamodb_number(now + ttl_seconds),
                        "created_at": _dynamodb_number(now),
                    },
                    ConditionExpression="attribute_not_exists(#pk)",
                    ExpressionAttributeNames={"#pk": "payment_key"},
                )
                return Binding(resource, key, new=True)
            except Exception as exc:
                if not _conditional_check_failed(exc):
                    raise
            try:
                # (d) Renew the key of an EXPIRED binding of the SAME resource.
                self._client.update_item(
                    TableName=self._table,
                    Key=item_key,
                    UpdateExpression="SET #key = :key, #expires = :expires",
                    ConditionExpression="#resource = :resource AND #expires <= :now",
                    ExpressionAttributeNames={
                        "#key": "idempotency_key",
                        "#expires": "expires_at",
                        "#resource": "resource",
                    },
                    ExpressionAttributeValues={
                        ":key": {"S": key},
                        ":expires": _dynamodb_number(now + ttl_seconds),
                        ":resource": {"S": resource},
                        ":now": _dynamodb_number(now),
                    },
                )
                return Binding(resource, key)
            except Exception as exc:
                if not _conditional_check_failed(exc):
                    raise
            # (a) Re-read: the winning item, whoever wrote it.
            item = self._client.get_item(
                TableName=self._table, Key=item_key, ConsistentRead=True
            ).get("Item")
        except Exception:
            # (c) Not raised on purpose: `None` is the signal to fail closed.
            logger.exception("bindings: could not read or mint the payment's binding")
            return None
        if not item:
            return None
        # A put of OURS whose answer was lost and whose retry (botocore retries)
        # failed its own condition: the item carries the key minted here and our
        # `created_at`, which a renewal never writes. Still the first presentation.
        stored_key = item["idempotency_key"]["S"]
        created = item.get("created_at", {}).get("N")
        ours = stored_key == key and created is not None and Decimal(created) == Decimal(
            _dynamodb_number(now)["N"]
        )
        return Binding(item["resource"]["S"], stored_key, new=ours)


def check_binding_config(config: X402Config) -> None:
    """Refuse a binding store on a client that sends no ``Idempotency-Key``.

    The store's whole value is the key it carries to the facilitator; with
    ``send_idempotency_key=False`` the client would drop it and the resend
    would recover nothing, silently. Raises ``ValueError``; the integrations
    call it when they are built, so a misconfigured seller fails at start-up,
    not at its first sale.
    """
    if not config.send_idempotency_key:
        raise ValueError(
            "a binding store needs X402Config.send_idempotency_key=True: the key it "
            "stores is what recovers the payment"
        )


_STORE_UNAVAILABLE_MESSAGE = (
    "The seller could not record this payment and did not call the facilitator. If this "
    "same X-PAYMENT was presented before, that attempt may have charged it: present the "
    "SAME X-PAYMENT again later, and do not sign another one."
)
_ALREADY_USED_MESSAGE = (
    "This X-PAYMENT was already presented for another resource, and one payment buys one "
    "resource: nothing was charged or delivered with it here. Do not sign another payment "
    "to recover the one you made: present the same X-PAYMENT to the request you signed it "
    "for. This resource is another purchase: request it without X-PAYMENT and follow its 402."
)
_PRESENTED_BEFORE_MESSAGE = (
    "This X-PAYMENT was already presented to this seller and that attempt may have charged "
    "it, so this refusal does not mean nothing moved: do not sign another one. Check the "
    "payment before paying again."
)


def process_payment_bound(
    client: X402Client,
    store: BindingStore,
    x_payment_header: str,
    expected_amount_usd: Decimal,
    resource: str,
    *,
    ttl_seconds: float | None = None,
    receipt_context: str | None = None,
) -> PaymentResult:
    """:meth:`X402Client.process_payment` under the payment's persisted binding.

    In this order, which is the guarantee:

    0. The header is parsed (:meth:`X402Client.extract_payload`, no I/O): a
       header that is not a payment is refused as it always was and never
       reaches the store.
    1. ``store.bind(payment_key(x_payment_header), resource, ttl)``, BEFORE the
       facilitator is called (``ttl`` defaults to
       :func:`binding_window_seconds`). ``None``, or a store that raised:
       :class:`~uvd_x402_sdk.exceptions.PaymentBindingError`
       ``payment_store_unavailable`` and the facilitator is not called.
    2. A binding owned by another resource:
       ``payment_already_used``, and the facilitator is not called.
    3. The verify and the settle of ``process_payment`` under the binding's
       key: the same key on ``/verify``, ``/settle`` and every resend, in any
       process. On a resend (the binding is not :attr:`Binding.new`) a replay
       under it is this purchase's own: delivered, and reported as
       ``idempotent_replayed``. On a first presentation the key was minted for
       this request and never sent, so the client keeps the guard of a fresh
       key: a replay reaching it before any attempt of its own ended without a
       verdict is another request's purchase, refused as without a store.
    4. A refusal of a payment this seller had seen before (the binding is not
       :attr:`Binding.new`) that would otherwise be a rejection (a 402):
       ``payment_presented_before``, because the earlier attempt reached the
       facilitator and may have moved the payment. Every other failure is
       raised unchanged.

    The receipt rail makes step 3 recover a payment whose process died after
    the settle. On a network without receipts the facilitator's settle cache
    does the same only if its ``/verify`` does not refuse the used
    authorization first (EVM ``/verify`` simulates the transfer): then step 4
    answers 409 instead of 402, and the payment is not delivered.

    Within the window a byte-identical resend runs the caller's handler again
    (``idempotent_replayed`` is true): a handler with side effects, or whose
    answer depends on the caller's identity, must check it.

    Raises:
        PaymentBindingError: Steps 1, 2 and 4.
        ValueError: ``client`` does not send an ``Idempotency-Key``
            (:func:`check_binding_config`).
        X402Error: Whatever ``process_payment`` raised otherwise.
    """
    check_binding_config(client.config)
    payload = client.extract_payload(x_payment_header)
    ttl = binding_window_seconds(client.config) if ttl_seconds is None else ttl_seconds
    try:
        binding = store.bind(payment_key(x_payment_header), resource, ttl)
    except Exception:
        # The protocol says `bind` never raises; one that does fails closed too.
        logger.exception("bindings: the store raised instead of answering None")
        binding = None
    if binding is None:
        logger.error("bindings: no binding for %s, not charging", resource)
        raise PaymentBindingError(PAYMENT_STORE_UNAVAILABLE, _STORE_UNAVAILABLE_MESSAGE)
    if binding.resource != resource:
        logger.warning("bindings: a payment bought for another resource presented for %s", resource)
        raise PaymentBindingError(PAYMENT_ALREADY_USED, _ALREADY_USED_MESSAGE)
    logger.info(
        "Processing payment: network=%s, amount=$%s, %s presentation",
        payload.network, expected_amount_usd, "first" if binding.new else "repeated",
    )
    handling = client._binding(payload, binding.idempotency_key, None, receipt_context)
    # Only an earlier handling can have admitted the payment under this key.
    handling.brought = not binding.new
    try:
        return client._handle_payment(
            payload, expected_amount_usd, None, asset=None, eip712_domain=None,
            token_decimals=None, binding=handling,
        )
    except X402Error as exc:
        if binding.new or _undelivered_response(exc) is not None:
            raise
        raise PaymentBindingError(
            PAYMENT_PRESENTED_BEFORE, _PRESENTED_BEFORE_MESSAGE, cause=exc
        ) from exc
