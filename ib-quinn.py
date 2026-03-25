"""
ib-quinn.py — thin launcher

The spec defines the entry point as ib-quinn.py.
Since Python module names can't contain hyphens,
this file imports and runs the real module from ib_quinn.py.
"""

from ib_quinn import main

if __name__ == "__main__":
    main()
