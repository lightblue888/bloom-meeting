# -*- coding: utf-8 -*-
"""本机起一份中继（测访客页用）：密钥文件和状态目录都用临时的，端口 8787。
    python -X utf8 relay/run_local_relay.py <测试用 livekit.env> <状态目录>"""
import os
import runpy
import sys
from pathlib import Path

os.environ["BLOOM_ENV"] = sys.argv[1]
os.environ["STATE_DIRECTORY"] = sys.argv[2]
sys.argv = [sys.argv[0]]
runpy.run_path(str(Path(__file__).with_name("token_server.py")), run_name="__main__")
