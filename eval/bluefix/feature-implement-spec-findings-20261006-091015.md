---
task: feature-implement-spec
kind: bluefix-ab
mode: findings
data_validity: void:timeout
fail_category: timeout
solve_seconds: 58.8
redteam_seconds: 138.4
bluefix_seconds: 600.0
timeout_budget: 600
verify_passed: 0
verify_failed: 0
verify_error: 0
verify_total: 0
eval_check: pass
---
# 蓝队 A/B — feature-implement-spec · mode=findings

- 时间:2026-10-06 09:23
- 红队输入:redteam-fresh-20261006-091015.md(实验内对本轮求解新跑)
- 蓝队失败类别:timeout
- 修复后校验:通过
- 耗时:求解 58.8s / 红队 138.4s / 蓝队 600.0s(预算 600s)
- 数据有效性:void:timeout

## 修复前解法 diff

```diff
--- util.py
+++ util.py
@@ -4,4 +4,6 @@
     moving_average([1, 2, 3, 4], 2) == [1.5, 2.5, 3.5]
     k 必须大于 0 且不超过 len(xs)，否则抛 ValueError。
     """
-    raise NotImplementedError
+    if k <= 0 or k > len(xs):
+        raise ValueError("k 必须大于 0 且不超过 len(xs)")
+    return [sum(xs[i:i + k]) / k for i in range(len(xs) - k + 1)]
```

## 蓝队输出

(蓝队调用失败:超时,600s 预算用尽)

## 独立自验门

蓝队未声明任何自验命令——按 v0.24.1 协议视为**自验缺失**,数据不可信。


## 修复后总 diff(蓝队改动 = 本节 − 上一节)

```diff
--- util.py
+++ util.py
@@ -4,4 +4,11 @@
     moving_average([1, 2, 3, 4], 2) == [1.5, 2.5, 3.5]
     k 必须大于 0 且不超过 len(xs)，否则抛 ValueError。
     """
-    raise NotImplementedError
+    # 长度公式 len(xs)-k+1 要求 k 为整数；整值浮点（如 2.0）归一为 int，
+    # 非整数 k 无法满足整数长度契约 → 一并入“否则”分支抛 ValueError。
+    if isinstance(k, float) and k.is_integer():
+        k = int(k)
+    if not isinstance(k, int) or k <= 0 or k > len(xs):
+        raise ValueError("k 必须大于 0 且不超过 len(xs)")
+    # 先在输入原生类型下做精确除法，再一次性转 float，兑现（float）契约。
+    return [float(sum(xs[i:i + k]) / k) for i in range(len(xs) - k + 1)]
```
