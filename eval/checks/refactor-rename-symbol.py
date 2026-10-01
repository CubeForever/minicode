"""checker: calc_total 已改名 invoice_total 且调用点同步。"""
import sys
from pathlib import Path

sandbox = Path(sys.argv[1])
sys.path.insert(0, str(sandbox))
import utils  # noqa: E402
import main   # noqa: E402

assert hasattr(utils, "invoice_total"), "utils.invoice_total 不存在"
assert not hasattr(utils, "calc_total"), "旧名 calc_total 仍存在"
assert main.run() == 6
src_u = (sandbox / "utils.py").read_text(encoding="utf-8")
src_m = (sandbox / "main.py").read_text(encoding="utf-8")
assert "calc_total" not in src_u and "calc_total" not in src_m, "源码里仍有旧名"
print("OK")
