"""Shared pytest environment for the workbench test suite."""

import os

# QMT 桥接在生产环境默认开启,但单元测试必须与本地 QMT 客户端完全隔离:
# 这里统一关闭,任何测试需要验证桥接行为时,请在用例内显式替换
# provider.qmt 为鸭子类型的假对象并置 provider.qmt_enabled = True
# (见 tests/test_qmt_provider.py)。
os.environ["XUNLONG_QMT_ENABLED"] = "0"
os.environ["XUNLONG_QMT_KLINE"] = "0"
