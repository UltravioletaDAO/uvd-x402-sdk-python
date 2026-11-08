"""Keep the release archive and token summary consistent with the README links."""

import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HISTORY_HEADING = b"## Releases 0.1.0 - 0.84.0 (moved from the README)\n"
# git show 0a1511a1:README.md, lines 3224-3639, including the final LF.
# This is the original 416-line archive, not a hash generated from CHANGELOG.md.
HISTORY_SHA256 = "22ee058fc8e94e324391bca7eaa821a0f0d91a49c5fe8cf2c37831bc5fd3cdb0"


def test_changelog_preserves_original_readme_release_history():
    changelog = (ROOT / "CHANGELOG.md").read_bytes()
    assert changelog.count(HISTORY_HEADING) == 1, "missing or duplicated release archive"
    section = changelog.split(HISTORY_HEADING, 1)[1].split(b"\n## ", 1)[0]
    first_release = b"### v0.84.0 (2026-09-16)\n"
    assert first_release in section, "archive must start with the original v0.84.0 entry"
    history = first_release + section.split(first_release, 1)[1]
    assert hashlib.sha256(history).hexdigest() == HISTORY_SHA256, "release archive bytes changed"


def test_readme_stablecoin_summary_links_to_network_coverage():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    feature = next(
        line for line in readme.splitlines() if line.startswith("- **6 Stablecoins**")
    )
    # AUSD is also on Solana and Sui; the summary must not restrict the tokens to EVM.
    assert "(evm chains)" not in " ".join(feature.lower().split())
    assert "[Supported Tokens](#supported-tokens)" in feature
