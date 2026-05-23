"""Pytest bootstrap — ensure project root is on sys.path so tests can import
the app modules (bot_engine, schemas, routes, ...) without packaging.
"""

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
