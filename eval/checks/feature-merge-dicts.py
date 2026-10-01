"""checker: deep_merge 递归合并语义 + 不修改入参。"""
import sys
from pathlib import Path

sandbox = Path(sys.argv[1])
sys.path.insert(0, str(sandbox))
from merge import deep_merge  # noqa: E402

a = {"x": {"p": 1}, "y": 1}
b = {"x": {"q": 2}, "z": 3}
r = deep_merge(a, b)
assert r == {"x": {"p": 1, "q": 2}, "y": 1, "z": 3}, r
assert a == {"x": {"p": 1}, "y": 1}          # 入参未被修改
assert b == {"x": {"q": 2}, "z": 3}
assert deep_merge({"x": 1}, {"x": 2}) == {"x": 2}   # 非 dict 直接覆盖
print("OK")
