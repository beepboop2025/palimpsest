"""Portable, offline-verifiable evidence capsules."""

from .capsule import (  # noqa: F401
    CANONICALIZATION,
    SPEC_VERSION,
    CapsuleError,
    build_capsule,
    canonical_bytes,
    content_sha256,
    load_capsule,
    verify_capsule,
)
