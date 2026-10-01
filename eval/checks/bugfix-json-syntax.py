"""checker: config.json 可被 json.load 解析且字段内容完整。"""
import json
import sys
from pathlib import Path

sandbox = Path(sys.argv[1])
cfg = json.loads((sandbox / "config.json").read_text(encoding="utf-8"))
assert cfg["name"] == "demo"
assert cfg["version"] == "1.0"
assert cfg["debug"] is True
assert cfg["tags"] == ["a", "b"]
print("OK")
