"""Portable, offline-verifiable evidence capsules."""

from .capsule import (  # noqa: F401
    CANONICALIZATION,
    IJSON_SAFE_INTEGER,
    SPEC_VERSION,
    CapsuleError,
    build_capsule,
    canonical_bytes,
    content_sha256,
    load_capsule,
    strict_json_loads,
    verify_capsule,
)
