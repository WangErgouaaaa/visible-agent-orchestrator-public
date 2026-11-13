from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_registered_discovery_skills_match_their_canonical_sources() -> None:
    registry = json.loads(
        (ROOT / "contracts/companion-skills.registry.json").read_text(encoding="utf-8")
    )

    assert registry["skills"], "registry must contain at least one managed Skill"
    for skill in registry["skills"]:
        canonical = ROOT / skill["canonical_source"]
        assert canonical.is_file(), skill["id"]
        canonical_bytes = canonical.read_bytes()
        for discovery_path in skill["discovery_paths"]:
            discovery = ROOT / discovery_path
            assert discovery.is_file(), f"{skill['id']}: {discovery_path}"
            assert discovery.read_bytes() == canonical_bytes, (
                f"{skill['id']}: {discovery_path} differs from canonical source"
            )
