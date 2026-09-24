"""The databases behind the binding stores, in process: nothing touches a network.

``FakePostgres`` is a ``payment_bindings`` table behind the shape
``PostgresBindingStore`` takes (``psycopg_pool.ConnectionPool``): ``connection()``
is ``with conn:`` as psycopg_pool writes it (commit on a normal exit, rollback on
an exception), ``cursor()``, ``%s`` parameters, ``rowcount``, ``fetchone()``.

It RESPECTS THE TRANSACTION: a connection takes the table at its first statement
and holds it until commit or rollback, statements work on a private copy, and
only a commit publishes it. Transactions are therefore serialised, which is
stricter than Postgres's READ COMMITTED; the subtlety condition (a) relies on (an
INSERT that collides with one in flight waits for it, then the re-read in
ANOTHER statement sees the winner) is what the live tests of
``tests/test_bindings.py`` check against a real Postgres (``UVD_TEST_POSTGRES_DSN``).

It EVALUATES the SQL it is sent rather than recognising the store's statements:
``INSERT ... VALUES ... ON CONFLICT (col) DO NOTHING``, ``UPDATE ... SET ... WHERE``,
``SELECT cols FROM ... WHERE`` and ``DELETE FROM ... WHERE``, with ``%s``,
``NOW()`` and ``NOW() + MAKE_INTERVAL(secs => %s)`` as values and ``AND`` of
comparisons as conditions. A statement it does not understand raises, so a
change to the store's SQL is red here until the double learns it, never
silently green. ``NOW()`` is ``clock()``, epoch seconds.

``moto_dynamodb()`` is moto's DynamoDB (it evaluates condition expressions
itself), with fake keys passed explicitly and the AWS config files pointed at
nothing, so no real credential is ever read. Its requests run one at a time
(``_AtomicRequests``): that is DynamoDB's guarantee for a conditional write,
and moto alone does not give it across threads.
"""
from __future__ import annotations

import copy
import re
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Callable

from uvd_x402_sdk.bindings import DYNAMODB_BINDINGS_KEY_SCHEMA

_SET_CONFIG = re.compile(r"SELECT set_config\('(\w+)', %s, (true|false)\)")
_INSERT = re.compile(
    r"INSERT INTO (\w+) \(([^)]*)\) VALUES \((.*)\) ON CONFLICT \((\w+)\) DO NOTHING", re.S
)
_UPDATE = re.compile(r"UPDATE (\w+) SET (.*?) WHERE (.*)", re.S)
_SELECT = re.compile(r"SELECT (.*?) FROM (\w+) WHERE (.*)", re.S)
_DELETE = re.compile(r"DELETE FROM (\w+) WHERE (.*)", re.S)
_INTERVAL = re.compile(r"NOW\(\) \+ MAKE_INTERVAL\(secs => %s\)")
_CONDITION = re.compile(r"(\w+) (<=|>=|=|<|>) (.+)")
_OPERATORS: dict[str, Callable[[Any, Any], bool]] = {
    "=": lambda a, b: a == b,
    "<=": lambda a, b: a <= b,
    ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b,
    ">": lambda a, b: a > b,
}


def _split(text: str) -> list[str]:
    """Split on the commas outside parentheses."""
    parts, depth, current = [], 0, ""
    for char in text:
        if char == "," and depth == 0:
            parts.append(current.strip())
            current = ""
            continue
        depth += char == "("
        depth -= char == ")"
        current += char
    parts.append(current.strip())
    return parts


class FakePostgres:
    """One ``payment_bindings`` table, as a pool. See the module docstring."""

    def __init__(
        self,
        *,
        table: str = "payment_bindings",
        dict_rows: bool = False,
        clock: Callable[[], float] = time.time,
        fail_on: str | None = None,
        autocommit: bool = False,
    ) -> None:
        self.table = table
        self.dict_rows = dict_rows
        self.clock = clock
        #: A statement that starts with this raises, as a database error would.
        self.fail_on = fail_on
        #: What ``conn.autocommit`` reports (psycopg 3's attribute).
        self.autocommit = autocommit
        #: payment_key -> the row, with ``expires_at`` in epoch seconds.
        self.rows: dict[str, dict[str, Any]] = {}
        #: Every transaction that ended: its statements and how it ended.
        self.transactions: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    @contextmanager
    def connection(self) -> Iterator[_Connection]:
        conn = _Connection(self)
        try:
            yield conn
        except BaseException:
            conn.rollback()
            raise
        conn.commit()


class _Connection:
    def __init__(self, db: FakePostgres) -> None:
        self._db = db
        self._working: dict[str, dict[str, Any]] | None = None
        self._statements: list[str] = []
        self._settings: dict[str, Any] = {}

    @property
    def autocommit(self) -> bool:
        return self._db.autocommit

    @contextmanager
    def cursor(self) -> Iterator[_Cursor]:
        yield _Cursor(self)

    def _begin(self) -> dict[str, dict[str, Any]]:
        if self._working is None:
            self._db._lock.acquire()
            self._working = copy.deepcopy(self._db.rows)
        return self._working

    def _end(self, outcome: str) -> None:
        if self._working is None:
            return
        if outcome == "commit":
            self._db.rows = self._working
        self._db.transactions.append(
            {"statements": self._statements, "settings": self._settings, "outcome": outcome}
        )
        self._working, self._statements, self._settings = None, [], {}
        self._db._lock.release()

    def commit(self) -> None:
        self._end("commit")

    def rollback(self) -> None:
        self._end("rollback")


class _Cursor:
    def __init__(self, conn: _Connection) -> None:
        self._conn = conn
        self.rowcount = -1
        self._result: list[Any] = []

    def fetchone(self) -> Any:
        return self._result.pop(0) if self._result else None

    def execute(self, sql: str, params: tuple = ()) -> None:
        db = self._conn._db
        rows = self._conn._begin()
        self._conn._statements.append(sql)
        if db.fail_on is not None and sql.startswith(db.fail_on):
            raise RuntimeError(f"the database refused: {sql[:40]}")
        values = iter(params)
        now = db.clock()
        self._result = []

        match = _SET_CONFIG.fullmatch(sql)
        if match:
            self._conn._settings[match.group(1)] = next(values)
            self._result = [self._row({"set_config": self._conn._settings[match.group(1)]})]
            return
        match = _INSERT.fullmatch(sql)
        if match:
            table, columns, row_values, conflict = match.groups()
            self._check_table(table)
            pairs = zip(_split(columns), _split(row_values))
            row = {column: self._value(expr, values, now) for column, expr in pairs}
            self._no_params_left(values, sql)
            if row[conflict] in rows:
                self.rowcount = 0
                return
            rows[row[conflict]] = {**row, "created_at": now}
            self.rowcount = 1
            return
        match = _UPDATE.fullmatch(sql)
        if match:
            table, assignments, where = match.groups()
            self._check_table(table)
            changes = {}
            for assignment in _split(assignments):
                column, _, expr = assignment.partition(" = ")
                changes[column.strip()] = self._value(expr, values, now)
            matching = self._where(rows, where, values, now)
            self._no_params_left(values, sql)
            for key in matching:
                rows[key].update(changes)
            self.rowcount = len(matching)
            return
        match = _SELECT.fullmatch(sql)
        if match:
            columns, table, where = match.groups()
            self._check_table(table)
            matching = self._where(rows, where, values, now)
            self._no_params_left(values, sql)
            names = _split(columns)
            self._result = [self._row({c: rows[k][c] for c in names}) for k in matching]
            self.rowcount = len(self._result)
            return
        match = _DELETE.fullmatch(sql)
        if match:
            table, where = match.groups()
            self._check_table(table)
            matching = self._where(rows, where, values, now)
            self._no_params_left(values, sql)
            for key in matching:
                del rows[key]
            self.rowcount = len(matching)
            return
        raise AssertionError(f"FakePostgres does not understand: {sql!r}")

    def _row(self, row: dict[str, Any]) -> Any:
        return dict(row) if self._conn._db.dict_rows else tuple(row.values())

    def _check_table(self, table: str) -> None:
        if table != self._conn._db.table:
            raise RuntimeError(f'relation "{table}" does not exist')

    @staticmethod
    def _no_params_left(values: Iterator[Any], sql: str) -> None:
        leftover = next(values, _NOTHING)
        assert leftover is _NOTHING, f"more parameters than %s in {sql!r}"

    @staticmethod
    def _value(expr: str, values: Iterator[Any], now: float) -> Any:
        expr = expr.strip()
        if expr == "%s":
            return next(values)
        if expr == "NOW()":
            return now
        if _INTERVAL.fullmatch(expr):
            return now + float(next(values))
        raise AssertionError(f"FakePostgres does not understand the value {expr!r}")

    def _where(
        self, rows: dict[str, dict[str, Any]], where: str, values: Iterator[Any], now: float
    ) -> list[str]:
        conditions = []
        for condition in where.split(" AND "):
            match = _CONDITION.fullmatch(condition.strip())
            assert match, f"FakePostgres does not understand the condition {condition!r}"
            column, operator, expr = match.groups()
            conditions.append((column, _OPERATORS[operator], self._value(expr, values, now)))
        return [
            key
            for key, row in rows.items()
            if all(test(row[column], value) for column, test, value in conditions)
        ]


_NOTHING = object()


class BrokenPool:
    """A database that does not answer: every connection attempt raises."""

    def connection(self) -> Any:
        raise RuntimeError("the database does not answer")


class _AtomicRequests:
    """A boto3 client whose requests run one at a time.

    DynamoDB applies each conditional write atomically: two requests on one
    item never both pass a condition only one of them could pass. moto does
    not: its ``update_item`` reads the item, evaluates the condition and writes
    without a lock (``moto/dynamodb/models/__init__.py``, moto 5.2), so two
    threads can both renew one expired binding, which DynamoDB never allows.
    One lock per request models the service's guarantee and nothing more:
    each request atomic, the three requests of one ``bind`` still interleaved
    with other threads' requests.
    """

    def __init__(self, client: Any, lock: threading.Lock) -> None:
        self._client = client
        self._lock = lock

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._client, name)
        if not callable(attribute):
            return attribute

        def request(*args: Any, **kwargs: Any) -> Any:
            with self._lock:
                return attribute(*args, **kwargs)

        return request


@contextmanager
def moto_dynamodb(monkeypatch: Any, table: str = "payment_bindings") -> Iterator[Callable[[], Any]]:
    """moto's DynamoDB with the bindings table; yields a factory of clients
    (one per "instance", all on the same table)."""
    import pytest

    moto = pytest.importorskip("moto")
    boto3 = pytest.importorskip("boto3")
    monkeypatch.setenv("AWS_CONFIG_FILE", "/nonexistent/uvd-x402-sdk-tests")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/nonexistent/uvd-x402-sdk-tests")
    for name in ("AWS_PROFILE", "AWS_SESSION_TOKEN", "AWS_SECURITY_TOKEN"):
        monkeypatch.delenv(name, raising=False)

    requests = threading.Lock()

    def client() -> Any:
        return _AtomicRequests(
            boto3.client(
                "dynamodb",
                region_name="us-east-1",
                aws_access_key_id="testing",
                aws_secret_access_key="testing",
            ),
            requests,
        )

    with moto.mock_aws():
        client().create_table(
            TableName=table, BillingMode="PAY_PER_REQUEST", **DYNAMODB_BINDINGS_KEY_SCHEMA
        )
        yield client
