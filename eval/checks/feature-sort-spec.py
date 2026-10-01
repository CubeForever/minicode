"""checker: sort_scores 排序规格。"""
import sys
from pathlib import Path

sandbox = Path(sys.argv[1])
sys.path.insert(0, str(sandbox))
from scores import sort_scores  # noqa: E402

rows = [("b", 2), ("a", 2), ("c", 1)]
r = sort_scores(rows)
assert r == [("a", 2), ("b", 2), ("c", 1)], r
assert rows == [("b", 2), ("a", 2), ("c", 1)], "入参未被修改"
assert sort_scores([]) == []
print("OK")
