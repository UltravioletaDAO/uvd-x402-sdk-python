"""Public evidence never carries the chat text, its author or the viewer's ids."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("eth_account")

from tests.reputation_doubles import SUBJECT, fixed_account  # noqa: E402
from uvd_x402_sdk.reputation import (  # noqa: E402
    EvidenceRejected,
    canonical_json,
    commitment,
    content_hash,
    evidence_document,
    message_ref,
    new_salt,
)

TEXT = "una idea brillante para el stream"
SALT = bytes.fromhex("ab" * 32)


def build(**overrides):
    args = dict(
        network="base",
        agent_id=18896,
        rater=fixed_account().address,
        subject=SUBJECT,
        value=90,
        tag1="credits_roll:idea",
        tag2="credits-roll|moment",
        endpoint="https://www.twitch.tv/somechannel",
        created_at=1_790_000_000,
        salt=SALT,
        private={
            "moment_ref": message_ref("credits-roll:v1", "s", "m"),
            "user_id": "40912334",
            "login": "some_viewer",
            "message": TEXT,
        },
        issuer={"name": "Credits Roll", "product": "credits-roll"},
    )
    args.update(overrides)
    return evidence_document(**args)


def test_the_text_and_the_ids_are_only_commitments():
    built = build()
    body = built.body.decode("utf-8")
    for value in (TEXT, "40912334", "some_viewer"):
        assert value not in body
    assert set(built.document["commitments"]) == {"moment_ref", "user_id", "login", "message"}
    assert built.document["subject"] == {
        "wallet": SUBJECT,
        "walletBinding": "self_declared_unverified",
    }
    assert built.content_hash == content_hash(built.body)


def test_destroying_the_salt_unlinks_the_commitments():
    one, two = build(salt=new_salt()), build(salt=new_salt())
    assert one.document["commitments"]["login"] != two.document["commitments"]["login"]
    assert build().document["commitments"] == build().document["commitments"]


@pytest.mark.parametrize(
    "key",
    [
        "message",
        "Message",
        "MESSAGE",
        "chat_text",
        "chatText",
        "author",
        "Author-Name",
        "user_name",
        "username",
        "login",
        "LOGIN",
        "displayName",
        "user-id",
        "viewerId",
        "viewer_login",
        "transcript",
        "quote",
        "comment",
        "email",
        "nick",
        "msgBody",
        "userAgent",
    ],
)
def test_a_private_key_anywhere_is_refused(key):
    for context in ({key: "x"}, {"nested": {key: "x"}}, {"list": [{key: "x"}]}):
        with pytest.raises(EvidenceRejected) as caught:
            build(context=context)
        assert caught.value.code == "private_field"


@pytest.mark.parametrize(
    "key",
    ["momentType", "contentHash", "contextual", "network", "streamTitle", "category", "textured"],
)
def test_public_keys_pass(key):
    build(context={key: "x"})


@pytest.mark.parametrize("where", ["tag1", "tag2", "endpoint"])
def test_the_text_riding_along_in_a_public_field_is_refused(where):
    with pytest.raises(EvidenceRejected) as caught:
        build(**{where: "x|" + TEXT})
    assert caught.value.code == "private_value_leaked"


def test_the_text_in_the_issuer_or_a_context_value_is_refused():
    for overrides in ({"issuer": {"name": TEXT}}, {"context": {"note": "said: " + TEXT}}):
        with pytest.raises(EvidenceRejected) as caught:
            build(**overrides)
        assert caught.value.code == "private_value_leaked"


def test_a_short_private_value_is_not_searched_for():
    # "ab" would match half the document; values under 8 characters rely on the key check.
    build(private={"moment_ref": "r" * 64, "login": "ab"}, tag2="ab|cd")


def test_floats_are_refused_because_they_are_not_canonical():
    with pytest.raises(EvidenceRejected) as caught:
        build(context={"score": 0.5})
    assert caught.value.code == "float_in_evidence"


@pytest.mark.parametrize(
    "overrides,code",
    [
        (dict(private={}), "private"),
        (dict(salt=b"short"), "salt"),
        (dict(private={"Bad Label": "x"}), "commitment_label"),
        (dict(private={"login": 5}), "commitment_value"),
        (dict(created_at=1.5), "created_at"),
        (dict(subject="nope"), "address"),
        (dict(agent_id=True), "agent_id"),
    ],
)
def test_the_other_refusals(overrides, code):
    with pytest.raises(EvidenceRejected) as caught:
        build(**overrides)
    assert caught.value.code == code


def test_canonical_json_is_karma_hellos():
    value = {"b": [1, "ñ"], "a": {"z": None, "y": True}}
    karma_hello = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    assert canonical_json(value) == karma_hello


def test_commitment_is_hmac_sha256():
    import hashlib
    import hmac

    assert (
        commitment(SALT, "login", "x")
        == "0x" + hmac.new(SALT, b"login|x", hashlib.sha256).hexdigest()
    )
