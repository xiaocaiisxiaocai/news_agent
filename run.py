#!/usr/bin/env python3
"""
启动入口：python run.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from web.app import run
if __name__ == "__main__":
    run()
