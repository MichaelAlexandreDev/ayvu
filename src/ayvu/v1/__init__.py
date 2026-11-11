"""The v1 runtime, quarantined until REF-18 retires it (D7 of docs/architecture/decisions/package-structure.md).

It is still the default when no model label is set. Only `composition` may import it, and v1 itself may import
any module. Each module here is public by itself, so this package exports nothing: no API is designed for code
that is going away.
"""
