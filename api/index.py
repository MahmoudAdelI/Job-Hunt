"""
Vercel Serverless Entry Point
==============================
Thin wrapper that imports and exposes the FastAPI `app` instance.
Vercel's @vercel/python runtime detects the `app` variable automatically.
"""

from app import app
