"""Точка входа для сборки exe (PyInstaller) и для запуска из исходников: python run_agent.py"""
import sys

from lanai_agent.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
