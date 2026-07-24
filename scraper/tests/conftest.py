"""
Shared fixtures.
"""
import sys
from pathlib import Path

# Make scraper package importable from project root
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
