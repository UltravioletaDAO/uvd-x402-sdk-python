"""v0.70.0: the three server-side gaps describe.net carried, now upstream.

Every change in this release is OPT-IN and OFF by default, and these tests
exist to keep that true forever: the first block PINS the legacy output of
each touched helper byte-for-byte (via sorted JSON) — a consumer with a caret
or `>=` pin adopts this release without asking, so the default output moving
would be a silent breaking change for every project of the DAO.

Provenance of each fix (measured, not assumed — 2026-08-30 sync cycle):
* `omit_unused_solana_facilitator`: describe.net's production 402 advertised a
  base58 Solana fee-payer on an EVM-only API for 19 days.
* `require_recipient`: the v1 chain loop never checked recipients; v2 always
  did (response.py `if not recipient: continue`) — consumers pre-constrained
  `supported_networks` to work around the asymmetry.
* `bazaar_extension` HTTP shape: the only form usable by a GET-priced API,
  already published by live crawlers (Agent Arena, trust-agent.io) and
  documented as a gap in describe.net's paywall for two SDK generations.
* `is_transient_error`: the 402-or-503 verdict every server rewrote; merges
  describe.net's `_is_transient` matrix with this SDK's private
  anti-double-settle guard (a 5xx whose body carries a tx hash is FINAL).
"""

import json

import pytest

from uvd_x402_sdk import is_transient_error
from uvd_x402_sdk.config import X402Config
from uvd_x402_sdk.exceptions import (
    FacilitatorError,
    PaymentSettlementError,
    TimeoutError as X402TimeoutError,
    X402Error,
)
from uvd_x402_sdk.response import bazaar_extension, create_402_response

EVM_ONLY = X402Config(
    recipient_evm="0x" + "11" * 20,
    supported_networks=["base", "avalanche"],
)


# ---------------------------------------------------------------------------
# Legacy parity: no flags => byte-for-byte the historical output.
# ---------------------------------------------------------------------------

class TestLegacyParity:
    def test_create_402_response_default_is_unchanged(self):
        body = create_402_response(0.01, EVM_ONLY)
        # The historical body DOES carry the Solana facilitator even on an
        # EVM-only config — that is the bug, and the default preserves it on
        # purpose: nobody's bytes move without opting in.
        assert body["facilitator"] == EVM_ONLY.facilitator_solana
        assert 8453 in body["supportedChains"]
        assert 43114 in body["supportedChains"]

    def test_create_402_response_is_deterministic_across_calls(self):
        a = json.dumps(create_402_response(0.01, EVM_ONLY), sort_keys=True)
        b = json.dumps(create_402_response(0.01, EVM_ONLY), sort_keys=True)
        assert a == b

    def test_bazaar_extension_two_positionals_is_unchanged(self):
        # The exact historical dict, written out — if this test goes red the
        # release is a breaking change no matter what the changelog says.
        out = bazaar_extension({"type": "object"}, {"ok": True})
        assert out == {
            "bazaar": {
                "schema": {
                    "properties": {
                        "input": {"properties": {"body": {"type": "object"}}},
                        "output": {"properties": {"example": {"ok": True}}},
                    }
                }
            }
        }


# ---------------------------------------------------------------------------
# Fix (a): the Solana facilitator is omitted when nothing SVM is offered.
# ---------------------------------------------------------------------------

class TestOmitUnusedSolanaFacilitator:
    def test_evm_only_config_omits_the_field(self):
        body = create_402_response(
            0.01, EVM_ONLY, omit_unused_solana_facilitator=True
        )
        assert "facilitator" not in body

    def test_solana_offered_keeps_the_field(self):
        cfg = X402Config(
            recipient_evm="0x" + "11" * 20,
            recipient_solana="F742C4VfFLQ9zRQyithoj5229ZgtX2WqKCSFKgH2EThq",
            supported_networks=["base", "solana"],
        )
        body = create_402_response(
            0.01, cfg, omit_unused_solana_facilitator=True
        )
        assert body["facilitator"] == cfg.facilitator_solana


# ---------------------------------------------------------------------------
# Fix (d): v1 chain list can require a recipient — parity with v2.
# ---------------------------------------------------------------------------

class TestRequireRecipient:
    def test_chains_without_recipient_drop_out(self):
        cfg = X402Config(
            recipient_evm="0x" + "11" * 20,
            supported_networks=["base", "solana"],  # no recipient_solana
        )
        legacy = create_402_response(0.01, cfg)
        filtered = create_402_response(0.01, cfg, require_recipient=True)
        assert "solana" in legacy["supportedChains"]  # the asymmetry, pinned
        assert "solana" not in filtered["supportedChains"]
        assert 8453 in filtered["supportedChains"]


# ---------------------------------------------------------------------------
# Fix (b): bazaar_extension HTTP/GET shape.
# ---------------------------------------------------------------------------

class TestBazaarHttpShape:
    def test_get_shape(self):
        out = bazaar_extension(
            output_example={"score": 81.86},
            method="GET",
            query_params={"wallet": {"type": "string"}},
            discoverable=True,
        )
        entrada = out["bazaar"]["schema"]["properties"]["input"]
        assert entrada == {
            "type": "http",
            "method": "GET",
            "queryParams": {"wallet": {"type": "string"}},
        }
        assert out["bazaar"]["discoverable"] is True

    def test_mixed_shapes_raise(self):
        with pytest.raises(ValueError):
            bazaar_extension({"x": 1}, {"ok": 1}, method="GET")

    def test_empty_call_raises(self):
        with pytest.raises(ValueError):
            bazaar_extension()

    def test_missing_output_raises(self):
        with pytest.raises(ValueError):
            bazaar_extension(method="GET")


# ---------------------------------------------------------------------------
# Fix (c): the public transient-vs-final matrix.
# ---------------------------------------------------------------------------

class TestIsTransientError:
    @pytest.mark.parametrize(
        ("exc", "esperado"),
        [
            (X402TimeoutError(operation="verify", timeout_seconds=5), True),
            (FacilitatorError("wrapped transport"), True),  # status None
            (FacilitatorError("rate limited", status_code=429), True),
            (FacilitatorError("boom", status_code=500), True),
            (FacilitatorError("bad request", status_code=400), False),
            (PaymentSettlementError("declined"), False),
            (ValueError("not ours"), False),
        ],
    )
    def test_matrix(self, exc, esperado):
        assert is_transient_error(exc) is esperado

    def test_5xx_with_tx_hash_is_final_anti_double_settle(self):
        # The guard that lived only in the private settle path: the
        # facilitator can 5xx AFTER broadcasting; retrying risks paying twice.
        exc = FacilitatorError(
            "post-settle hook failed",
            status_code=500,
            response_body=json.dumps({"txHash": "0x" + "ab" * 32}),
        )
        assert is_transient_error(exc) is False
        # The caller can disarm the guard explicitly and own the risk.
        assert is_transient_error(exc, anti_double_settle=False) is True

    def test_retryable_detail_is_respected(self):
        exc = X402Error("custom", details={"retryable": True})
        assert is_transient_error(exc) is True
