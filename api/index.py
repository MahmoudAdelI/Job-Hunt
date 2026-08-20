"""
Vercel Serverless Entry Point
==============================
Thin wrapper that imports and exposes the FastAPI `app` instance.
Vercel's @vercel/python runtime detects the `app` variable automatically.
"""

import sys
import os

# Ensure the project root is on Python's path so we can import app.py and main.py
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app
