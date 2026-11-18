"""单一职责：从本地 fixtures 读取原始记录（离线摄取入口，不联网）。

source 可为：
- None / 省略：读取 config.FIXTURES；
- 路径（str/Path）：读取该 JSON 文件；
- list/tuple：视为已加载记录直接返回，便于测试构造离线场景。
"""
import json
from .config import FIXTURES


def load(source=None):
    if source is None:
        source = FIXTURES
    if isinstance(source, (list, tuple)):
        return list(source)
    with open(source, encoding="utf-8") as f:
        return json.load(f)
