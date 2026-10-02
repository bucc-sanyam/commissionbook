"""Vercel's WSGI entrypoint; all state lives in PostgreSQL and private Storage."""

from app import app  # noqa: F401 – Vercel needs a top-level `app`
