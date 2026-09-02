"""EV Deal Pipeline data layer."""

from .db import SCHEMA_VERSION, connect, create_schema

__all__ = ["SCHEMA_VERSION", "connect", "create_schema"]
