"""命令行入口（打包用）。"""
import os
import sys

if getattr(sys, "frozen", False):
    sys.path.insert(0, os.path.dirname(sys.executable))
else:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nhc_tool.__main__ import main  # noqa: E402

if __name__ == "__main__":
    main()
