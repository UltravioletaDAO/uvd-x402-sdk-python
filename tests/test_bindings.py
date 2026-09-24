"""The binding of a payment has to outlive the process that minted it.

Ported from describe.net's ``tests/test_bindings_postgres.py``, which is the
contract: what is tested is not that a table keeps rows, it is that TWO
instances see the SAME binding, and the four conditions under which the
persisted key was accepted, one test or more each:

  (a) minting is a conditional insert plus a re-read: eight first requests at
      once for one payment leave with ONE key, and exactly one of them is new;
  (b) the key is random and stored, never derived from the header;
  (c) a store that cannot answer gives ``None``, never an exception (the seller
      then fails closed: ``tests/test_bindings_integrations.py``);
  (d) the row belongs to the PAYMENT and keeps its owner: another resource gets
      the owner back, at once or later, expired or not.

Every store runs the same tests: in memory (one process), Postgres over
``tests/binding_doubles.FakePostgres`` (tuple and dict rows), DynamoDB over
moto, and a real Postgres when ``UVD_TEST_POSTGRES_DSN`` names one, e.g.::

    docker run -d --rm --name uvd-pg -e POSTGRES_PASSWORD=pg -p 55432:5432 postgres:16
    export UVD_TEST_POSTGRES_DSN=postgresql://postgres:pg@127.0.0.1:55432/postgres
    pytest tests/test_bindings.py tests/test_bindings_integrations.py

(the live store creates ``POSTGRES_BINDINGS_SCHEMA`` and cleans its own rows).
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
from typing import Any, Callable

import pytest

from tests.binding_doubles import BrokenPool, FakePostgres, moto_dynamodb
from uvd_x402_sdk.bindings import (
    BINDING_WINDOW_CEILING_SECONDS,
    BINDING_WINDOW_FLOOR_SECONDS,
    POSTGRES_BINDINGS_SCHEMA,
    POSTGRES_STATEMENT_TIMEOUT_MS,
    Binding,
    DynamoDBBindingStore,
    InMemoryBindingStore,
    PostgresBindingStore,
    binding_window_seconds,
    payment_key,
    purchase_resource,
)
from uvd_x402_sdk.config import X402Config

PAYMENT = "test-binding-" + "0" * 8
A = "GET /reputation/wallet/0xaa"
B = "GET /reputation/wallet/0xbb"
LIVE_DSN = os.environ.get("UVD_TEST_POSTGRES_DSN")


class Backend:
    """One database and the stores over it: ``store()`` is another instance."""

    def __init__(
        self, store: Callable[[], Any], row: Callable[[str], dict[str, Any] | None]
    ) -> None:
        self.store = store
        self.row = row


def _memory() -> Backend:
    shared = InMemoryBindingStore()

    def row(key: str) -> dict[str, Any] | None:
        entry = shared._entries.get(key)
        if entry is None:
            return None
        deadline, resource, idempotency_key = entry
        return {"resource": resource, "idempotency_key": idempotency_key, "expires_at": deadline}

    return Backend(lambda: shared, row)


def _fake_postgres(dict_rows: bool) -> Backend:
    db = FakePostgres(dict_rows=dict_rows)
    return Backend(lambda: PostgresBindingStore(db), lambda key: db.rows.get(key))


def _live_postgres() -> Any:
    psycopg = pytest.importorskip("psycopg")
    pool_module = pytest.importorskip("psycopg_pool")
    from psycopg.rows import dict_row

    with psycopg.connect(LIVE_DSN, autocommit=True) as conn:
        conn.execute(POSTGRES_BINDINGS_SCHEMA)
    pool = pool_module.ConnectionPool(
        LIVE_DSN, min_size=1, max_size=10, open=False, timeout=5,
        kwargs={"row_factory": dict_row},
    )
    pool.open(wait=True, timeout=10)

    def row(key: str) -> dict[str, Any] | None:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM payment_bindings WHERE payment_key = %s", (key,))
            return cur.fetchone()

    def clean() -> None:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM payment_bindings WHERE payment_key LIKE 'test-binding-%%'")
        pool.close()

    return Backend(lambda: PostgresBindingStore(pool), row), clean


BACKENDS = ["memory", "postgres", "postgres-dict-rows", "dynamodb"] + (
    ["postgres-live"] if LIVE_DSN else []
)


@pytest.fixture(params=BACKENDS)
def backend(request, monkeypatch):
    kind = request.param
    if kind == "memory":
        yield _memory()
    elif kind == "postgres":
        yield _fake_postgres(dict_rows=False)
    elif kind == "postgres-dict-rows":
        yield _fake_postgres(dict_rows=True)
    elif kind == "postgres-live":
        live, clean = _live_postgres()
        try:
            yield live
        finally:
            clean()
    else:
        with moto_dynamodb(monkeypatch) as client:

            def row(key: str) -> dict[str, Any] | None:
                item = client().get_item(
                    TableName="payment_bindings", Key={"payment_key": {"S": key}},
                    ConsistentRead=True,
                ).get("Item")
                if not item:
                    return None
                return {name: next(iter(value.values())) for name, value in item.items()}

            yield Backend(lambda: DynamoDBBindingStore(client()), row)


def _at_once(backend: Backend, resources: list[str], ttl: float = 300) -> list[Binding | None]:
    seen: list[Binding | None] = []
    start = threading.Barrier(len(resources))

    def ask(resource: str) -> None:
        store = backend.store()  # another instance, the same database
        start.wait()
        seen.append(store.bind(PAYMENT, resource, ttl_seconds=ttl))

    threads = [threading.Thread(target=ask, args=(r,)) for r in resources]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return seen


def test_two_instances_see_the_same_binding(backend):
    """THE test. The second instance gets the first one's key, not a new one."""
    first = backend.store().bind(PAYMENT, A, ttl_seconds=300)
    assert first is not None and first.resource == A
    assert first.idempotency_key.startswith("x402-") and len(first.idempotency_key) == 69
    assert backend.store().bind(PAYMENT, A, ttl_seconds=300) == first

    # The contrast that gives the store its meaning: another process's memory.
    assert InMemoryBindingStore().bind(PAYMENT, A, 300).idempotency_key != first.idempotency_key


def test_b_the_key_is_random_and_no_header_is_stored(backend):
    """(b) The row keys on the payment's hash and the key does not come from it."""
    header = "eyJ4NDAyVmVyc2lvbiI6MX0="  # any header
    key = "test-binding-" + payment_key(header)[:24]
    store = backend.store()
    one = store.bind(key, A, 300)
    other = store.bind(PAYMENT, A, 300)
    assert one.idempotency_key != other.idempotency_key
    row = backend.row(key)
    assert header not in " ".join(str(v) for v in row.values())
    assert payment_key(header)[:24] not in row["idempotency_key"]


def test_d_another_resource_gets_the_owner_and_not_a_key_of_its_own(backend):
    """(d) The same payment for ANOTHER resource: the row does not change and
    the store answers the owner. Deciding that this is a 409 is the seller's."""
    store = backend.store()
    for_a = store.bind(PAYMENT, A, 300)
    for_b = store.bind(PAYMENT, B, 300)
    assert for_b == for_a
    assert backend.row(PAYMENT)["resource"] == A


def test_an_expired_binding_renews_the_key_and_not_the_owner(backend):
    """Past the window, the resend to the SAME resource gets another key (and the
    facilitator answers it 409 with the receipt): the ceiling of the bearer
    window. For the OTHER resource the payment still belongs to A."""
    store = backend.store()
    old = store.bind(PAYMENT, A, ttl_seconds=-1)
    assert store.bind(PAYMENT, B, ttl_seconds=300) == old, "expiry frees the payment"
    renewed = store.bind(PAYMENT, A, ttl_seconds=300)
    assert renewed.resource == A and renewed.idempotency_key != old.idempotency_key
    assert store.bind(PAYMENT, A, ttl_seconds=300) == renewed


def test_a_resend_does_not_extend_the_key(backend):
    store = backend.store()
    store.bind(PAYMENT, A, ttl_seconds=60)
    before = backend.row(PAYMENT)["expires_at"]
    store.bind(PAYMENT, A, ttl_seconds=3600)
    assert backend.row(PAYMENT)["expires_at"] == before, "the resend stretched the window"


def test_a_eight_first_requests_at_once_leave_with_one_key(backend):
    """(a) Two keys for one payment are two purchases for the facilitator: the
    second gets ``409 authorization_in_flight`` for a payment that is its own."""
    seen = _at_once(backend, [A] * 8)
    assert len(seen) == 8 and None not in seen
    assert len(set(seen)) == 1, f"{len(set(seen))} bindings for one payment"


def test_d_at_once_for_two_resources_there_is_one_owner(backend):
    """(d) racing: the same header asked for A and for B at once. One wins, and
    all eight see the same owner with the same key."""
    seen = _at_once(backend, [A, B] * 4)
    assert None not in seen
    assert len(set(seen)) == 1
    assert seen[0].resource in (A, B)


def test_a_eight_renewals_at_once_give_one_key(backend):
    """The renewal of an expired binding is conditional too: the
    ``expires_at <= now`` of the update makes it a single one."""
    old = backend.store().bind(PAYMENT, A, ttl_seconds=-1)
    seen = _at_once(backend, [A] * 8)
    assert len(set(seen)) == 1
    assert seen[0].idempotency_key != old.idempotency_key


def test_new_says_whether_the_payment_was_presented_before(backend):
    """The signal that decides 409 over 402 for a refused resend
    (``process_payment_bound``). With ``new`` always true (what an ``ON
    CONFLICT ... DO UPDATE ... RETURNING`` would give, ``rowcount`` 1 every
    time) a resend went back to a 402 with every other test green."""
    store = backend.store()
    assert store.bind(PAYMENT, A, 300).new is True, "the first presentation"
    assert store.bind(PAYMENT, A, 300).new is False, "the resend"
    assert store.bind(PAYMENT, B, 300).new is False, "the same payment for another resource"

    expired = PAYMENT + "-expired"
    assert store.bind(expired, A, ttl_seconds=-1).new is True
    renewed = store.bind(expired, A, 300)
    assert renewed.new is False, "renewing the key does not make the payment new"


def test_new_in_a_race_is_exactly_one(backend):
    seen = _at_once(backend, [A] * 8)
    assert sum(b.new for b in seen) == 1, [b.new for b in seen]


def test_new_in_a_renewal_race_is_none(backend):
    backend.store().bind(PAYMENT, A, ttl_seconds=-1)
    seen = _at_once(backend, [A] * 8)
    assert not any(b.new for b in seen), [b.new for b in seen]


def test_c_a_database_that_does_not_answer_is_none_and_not_a_500():
    assert PostgresBindingStore(BrokenPool()).bind(PAYMENT, A, ttl_seconds=300) is None


@pytest.mark.parametrize("statement", ["INSERT", "UPDATE", "SELECT resource"])
def test_c_a_statement_that_fails_is_none_and_nothing_is_kept(statement):
    """Any statement of the transaction failing: ``None``, and the INSERT before
    it is rolled back, so no binding exists that nobody was told about."""
    db = FakePostgres(fail_on=statement)
    assert PostgresBindingStore(db).bind(PAYMENT, A, ttl_seconds=300) is None
    assert db.rows == {}
    assert db.transactions[-1]["outcome"] == "rollback"


def test_c_every_postgres_bind_runs_under_the_statement_timeout():
    """The ceiling is set for the transaction BEFORE anything can wait on a
    lock. Its effect against a lock nobody releases is the live test below."""
    db = FakePostgres()
    PostgresBindingStore(db).bind(PAYMENT, A, ttl_seconds=300)
    (transaction,) = db.transactions
    assert transaction["statements"][0].startswith("SELECT set_config('statement_timeout'")
    assert transaction["settings"] == {"statement_timeout": f"{POSTGRES_STATEMENT_TIMEOUT_MS}ms"}
    assert transaction["outcome"] == "commit" and len(transaction["statements"]) == 4


def test_c_a_connection_in_autocommit_is_refused_because_it_loses_the_ceiling():
    """``set_config(..., true)`` lasts one transaction; in autocommit each
    statement is one, and the INSERT would wait on a lock with no ceiling."""
    db = FakePostgres(autocommit=True)
    assert PostgresBindingStore(db).bind(PAYMENT, A, ttl_seconds=300) is None
    assert db.rows == {} and db.transactions == []


def test_a_dynamodb_put_whose_answer_was_lost_is_still_the_first_presentation(monkeypatch):
    """botocore retries a put whose answer it lost; the retry fails its own
    condition. The item carries the key minted here and our ``created_at``: it
    is still this request's first presentation, never a "presented before"
    that would turn a plain refusal into a 409."""
    with moto_dynamodb(monkeypatch) as client:
        real = client()

        class LostAnswer:
            def put_item(self, **kwargs: Any) -> Any:
                real.put_item(**kwargs)  # applied...
                error = RuntimeError("the retry failed its condition")
                error.response = {"Error": {"Code": "ConditionalCheckFailedException"}}
                raise error  # ...and what reaches the caller is the retry

            def __getattr__(self, name: str) -> Any:
                return getattr(real, name)

        minted = "x402-" + "3" * 64
        seen = DynamoDBBindingStore(LostAnswer(), new_key=lambda: minted).bind(PAYMENT, A, 300)
        assert seen == Binding(A, minted) and seen.new is True
        # A put of ANOTHER request that won is still not ours.
        other = DynamoDBBindingStore(LostAnswer()).bind(PAYMENT, A, 300)
        assert other == seen and other.new is False


def test_c_a_dynamodb_that_does_not_answer_is_none():
    class Unreachable:
        def put_item(self, **_kwargs: Any) -> None:
            raise ConnectionError("Could not connect to the endpoint URL")

    assert DynamoDBBindingStore(Unreachable()).bind(PAYMENT, A, ttl_seconds=300) is None


def test_c_a_dynamodb_table_that_does_not_exist_is_none(monkeypatch):
    with moto_dynamodb(monkeypatch) as client:
        store = DynamoDBBindingStore(client(), table_name="no_such_table")
        assert store.bind(PAYMENT, A, ttl_seconds=300) is None


def test_c_a_refused_dynamodb_renewal_is_none_not_the_owner(monkeypatch):
    """Only a FAILED CONDITION moves on to the next step; any other refusal of
    the update (throttling here) fails closed instead of reading on."""
    with moto_dynamodb(monkeypatch) as client:
        real = client()
        DynamoDBBindingStore(real).bind(PAYMENT, A, ttl_seconds=-1)

        class Throttled:
            def put_item(self, **kwargs: Any) -> Any:
                return real.put_item(**kwargs)

            def update_item(self, **_kwargs: Any) -> Any:
                error = RuntimeError("throttled")
                error.response = {"Error": {"Code": "ProvisionedThroughputExceededException"}}
                raise error

            def get_item(self, **kwargs: Any) -> Any:
                return real.get_item(**kwargs)

        assert DynamoDBBindingStore(Throttled()).bind(PAYMENT, A, ttl_seconds=300) is None


def test_the_dynamodb_clock_is_the_process_clock(monkeypatch):
    """The one difference with Postgres, pinned: DynamoDB has no server clock a
    condition can read. A process whose clock runs ahead renews a key early;
    it never changes the owner nor mints a second first presentation."""
    with moto_dynamodb(monkeypatch) as client:
        now = time.time()
        first = DynamoDBBindingStore(client(), clock=lambda: now).bind(PAYMENT, A, 300)
        same = DynamoDBBindingStore(client(), clock=lambda: now + 299).bind(PAYMENT, A, 300)
        ahead = DynamoDBBindingStore(client(), clock=lambda: now + 301).bind(PAYMENT, A, 300)
        other = DynamoDBBindingStore(client(), clock=lambda: now + 10_000).bind(PAYMENT, B, 300)
    assert same == first
    assert ahead.resource == A and ahead.idempotency_key != first.idempotency_key
    assert not ahead.new
    assert other == ahead


def test_the_sweep_deletes_only_the_expired():
    db = FakePostgres()
    store = PostgresBindingStore(db)
    alive = store.bind(PAYMENT + "-alive", A, ttl_seconds=300)
    store.bind(PAYMENT + "-dead", A, ttl_seconds=-1)
    assert store.sweep() == 1
    assert {k: r["idempotency_key"] for k, r in db.rows.items()} == {
        PAYMENT + "-alive": alive.idempotency_key
    }


def test_the_table_name_is_an_identifier_or_nothing():
    with pytest.raises(ValueError):
        PostgresBindingStore(FakePostgres(), table="payment_bindings; DROP TABLE orders")
    db = FakePostgres(table="shop_payment_bindings")
    assert PostgresBindingStore(db, table="shop_payment_bindings").bind(PAYMENT, A, 300).new
    # A table the migration never created: the store fails closed.
    assert PostgresBindingStore(db).bind(PAYMENT, A, 300) is None


def test_the_in_memory_store_evicts_the_oldest_past_its_ceiling():
    store = InMemoryBindingStore(max_entries=2)
    first = store.bind("one", A, 300)
    store.bind("two", A, 300)
    store.bind("three", A, 300)
    assert store.bind("one", A, 300) != first, "the oldest was evicted"


def test_the_window_is_derived_and_clamped():
    assert binding_window_seconds(X402Config(recipient_evm="0x" + "1" * 40)) == 300
    assert BINDING_WINDOW_FLOOR_SECONDS == 300 and BINDING_WINDOW_CEILING_SECONDS == 1800
    long = X402Config(recipient_evm="0x" + "1" * 40, verify_timeout=100, settle_timeout=200)
    assert binding_window_seconds(long) == 900
    l1 = X402Config(recipient_evm="0x" + "1" * 40, settle_timeout=900)
    assert binding_window_seconds(l1) == 1800


def test_the_resource_is_one_string_per_request_and_never_two_requests_in_one():
    """The path is encoded, so a decoded ``#`` or ``?`` inside a segment cannot
    make one path read as another or move text into the query."""
    assert purchase_resource("GET", "/gen/cats#other") != purchase_resource("GET", "/gen/cats")
    assert purchase_resource("GET", "/gen/cats#") != purchase_resource("GET", "/gen/cats")
    assert purchase_resource("GET", "/a?b") != purchase_resource("GET", "/a", "b")
    assert purchase_resource("GET", "/a b") != purchase_resource("GET", "/a", " b")
    # A query cannot pose as a body: its space is encoded.
    digest = hashlib.sha256(b"y").hexdigest()
    with_body = purchase_resource("POST", "/a", "x=", b"y")
    assert purchase_resource("POST", "/a", f"x= sha256:{digest}") != with_body
    assert purchase_resource("GET", "/a%2Fb") != purchase_resource("GET", "/a/b")
    assert purchase_resource("GET", "/gen/café") == purchase_resource("GET", "/gen/café")


def test_the_resource_is_method_path_query_and_body():
    assert purchase_resource("get", "/paid") == "GET /paid"
    assert purchase_resource("GET", "/paid", "id=1") == "GET /paid?id=1"
    one = purchase_resource("POST", "/buy", "", b'{"pixel":1}')
    assert one.startswith("POST /buy sha256:") and len(one) == len("POST /buy sha256:") + 64
    assert one != purchase_resource("POST", "/buy", "", b'{"pixel":2}')
    assert payment_key("abc") == payment_key("abc") != payment_key("abd")


# -- against a real Postgres only ---------------------------------------------


@pytest.mark.skipif(not LIVE_DSN, reason="UVD_TEST_POSTGRES_DSN is not set")
def test_live_c_a_hung_lock_fails_closed_within_the_ceiling():
    """(c): another transaction minted this payment and does not commit. The
    INSERT waits for its lock; without the ceiling it would wait for ever (or
    until the Lambda dies). With it, ``bind`` gives up and answers ``None``."""
    psycopg = pytest.importorskip("psycopg")
    live, clean = _live_postgres()
    hung = psycopg.connect(LIVE_DSN)
    # The clock that releases the lock: without the ceiling `bind` would wait
    # for this and leave with a binding, which the asserts below catch, instead
    # of hanging the suite.
    release = threading.Timer(POSTGRES_STATEMENT_TIMEOUT_MS / 1000 + 3.0, hung.rollback)
    try:
        with hung.cursor() as cur:
            cur.execute(
                "INSERT INTO payment_bindings (payment_key, resource, idempotency_key, expires_at)"
                " VALUES (%s, %s, %s, NOW() + INTERVAL '300 seconds')",
                (PAYMENT, A, "x402-" + "7" * 64),
            )
        release.start()
        started = time.monotonic()
        seen = live.store().bind(PAYMENT, A, ttl_seconds=300)
        took = time.monotonic() - started
    finally:
        release.cancel()
        hung.rollback()
        hung.close()
        clean()
    assert seen is None
    assert took < POSTGRES_STATEMENT_TIMEOUT_MS / 1000 + 1.0, f"took {took:.2f} s"
