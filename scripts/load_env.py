#!/usr/bin/env python3
"""
加载项目环境变量脚本 - 本地模式从 .env 文件加载
使用方式: python load_env.py
"""
import os
import sys

# 将 src/ 加入路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

try:
    from dotenv import load_dotenv
    env_file = os.path.join(os.path.dirname(__file__), "..", ".env")
    if os.path.exists(env_file):
        load_dotenv(env_file)
        print(f"# Loaded .env from {env_file}", file=sys.stderr)
    else:
        print(f"# Warning: .env file not found at {env_file}", file=sys.stderr)
except ImportError:
    print("# Error: python-dotenv not installed. Run: uv pip install python-dotenv", file=sys.stderr)
    sys.exit(1)
