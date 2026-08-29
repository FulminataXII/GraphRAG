from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]

# Pulled forward from BO-12's own list (BUILD_ORDER.md: "test_images_pinned_by_digest — no
# `latest` / `main-latest` in any compose file") at explicit request, after `arizephoenix/
# phoenix:latest` silently moved underneath an already-migrated Postgres schema and crashed
# with PhoenixMigrationError — a floating tag means the exact same `docker compose up` pulls
# different bytes over time. `latest`/`main-latest` are the two floating conventions this repo
# has actually used; a bare tag with no `:` at all (Docker's own default) is covered too.
_FLOATING_TAGS: frozenset[str] = frozenset({"latest", "main-latest"})


def test_no_crlf_in_repo() -> None:
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    tracked = [line for line in result.stdout.splitlines() if line]

    offenders = []
    for rel_path in tracked:
        path = REPO_ROOT / rel_path
        if not path.is_file():
            continue
        try:
            content = path.read_bytes()
        except OSError:
            continue
        if b"\r\n" in content:
            offenders.append(rel_path)

    assert offenders == [], f"CRLF line endings found in: {offenders}"


def _image_tag(image: str) -> tuple[str | None, bool]:
    """(tag, has_digest) for a Docker image reference, tolerant of an optional
    `registry[:port]/` prefix — a colon before the last `/` is a registry port, not a tag."""
    has_digest = "@sha256:" in image
    without_digest = image.split("@", 1)[0]
    last_segment = without_digest.rsplit("/", 1)[-1]
    tag = last_segment.split(":", 1)[1] if ":" in last_segment else None
    return tag, has_digest


def test_images_pinned_by_digest() -> None:
    """No compose file may reference an image by a floating tag (`latest`, `main-latest`, or
    no tag at all — Docker's own implicit default is `latest`). A locally `build:`-only
    service (no `image:` key, e.g. `otel-collector`) is exempt — nothing is pulled for it.

    `arizephoenix/phoenix:latest` silently moved to an image whose migrations didn't match an
    already-provisioned Postgres schema (PhoenixMigrationError) — the exact same `docker
    compose up` pulled different bytes on two different days. Pinning by digest (or at minimum
    a fixed, non-floating tag) makes that impossible: the file always names the same bytes.
    """
    offenders: list[str] = []
    checked_any = False
    for path in sorted(REPO_ROOT.glob("docker-compose*.yml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for name, spec in (doc.get("services") or {}).items():
            image = spec.get("image")
            if image is None:
                continue  # built locally — nothing to pin
            checked_any = True
            tag, has_digest = _image_tag(image)
            if has_digest:
                continue
            if tag is None or tag in _FLOATING_TAGS:
                offenders.append(f"{path.name}:{name} -> {image!r}")

    assert checked_any, "scan found no `image:` keys at all across docker-compose*.yml"
    assert not offenders, "floating/unpinned image tag(s) found:\n" + "\n".join(offenders)
