"""checker: moving_average 规格符合 docstring。"""
import sys
from pathlib import Path

sandbox = Path(sys.argv[1])
sys.path.insert(0, str(sandbox))
from util import moving_average  # noqa: E402

assert moving_average([1, 2, 3, 4], 2) == [1.5, 2.5, 3.5]
assert moving_average([5], 1) == [5.0]
assert moving_average([1, 2], 2) == [1.5]
try:
    moving_average([1], 2)
except ValueError:
    pass
else:
    raise AssertionError("k > len(xs) 应抛 ValueError")
try:
    moving_average([1], 0)
except ValueError:
    pass
else:
    raise AssertionError("k=0 应抛 ValueError")
print("OK")
