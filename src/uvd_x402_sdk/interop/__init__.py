"""The stack's interop contract in Python. The contract itself is ``interop/``
at the root of this repository: Markdown, JSON Schemas, fixtures and vectors.

Today this package holds the offline reference runner of its conformance suite,
:mod:`uvd_x402_sdk.interop.conformance`, and its command line::

    python -m uvd_x402_sdk.interop check [--interop DIR] [MANIFEST ...]

Nothing is imported here on purpose: the runner needs ``jsonschema`` (the
``interop`` extra), and ``import uvd_x402_sdk`` must keep working without it.
"""
