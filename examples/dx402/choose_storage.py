"""Ask a facilitator where it can store evidence, then choose.

Run:  python examples/dx402/choose_storage.py

Ask instead of assuming. What exists depends on the DEPLOYMENT -- a facilitator
without a Pinata credential offers only `s3` -- and you may be pointed at one
that is not ours. A hardcoded list in your code is a promise somebody else has
to keep.
"""

from uvd_x402_sdk import available_backends


def main() -> None:
    backends = available_backends("https://facilitator.ultravioletadao.xyz")

    if not backends:
        print("this facilitator does not run DX402 (or is unreachable)")
        print("discovery returns [] rather than raising -- it must never be a gate")
        return

    print(f"{'id':16}{'retention':12}{'deletable':11}{'public':8}status")
    for b in backends:
        status = "available" if b["enabled"] else f"off: {b.get('disabledReason', '')}"
        print(
            f"{b['id']:16}{b['retention']:12}"
            f"{'yes' if b['revocable'] else 'NO':11}"
            f"{'yes' if b['public'] else 'no':8}{status}"
        )

    print()
    print("The two fields that matter when choosing:")
    print()
    print("  revocable=NO  the `retentionUntil` in the SIGNED receipt cannot be")
    print("                honoured. On public IPFS, unpinning removes the")
    print("                facilitator's copy, not the network's. Irreversible.")
    print()
    print("  public=yes    anyone resolves the bytes without the facilitator.")
    print("                That is the point of asking for it -- and the cost.")
    print()

    usable = [b for b in backends if b["enabled"]]
    if usable:
        print(f"anchor_evidence(..., storage={usable[0]['id']!r})")
    print()
    print("Ask for one that is not offered and you get an error naming it --")
    print("never a quiet anchor somewhere you did not choose.")


if __name__ == "__main__":
    main()
