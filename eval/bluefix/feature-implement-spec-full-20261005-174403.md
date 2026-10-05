# 蓝队 A/B — feature-implement-spec · mode=full

- 时间:2026-10-05 17:58
- 红队输入:redteam-fresh-20261005-174403.md(实验内对本轮求解新跑)
- 蓝队失败类别:timeout
- 修复后校验:通过

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

(蓝队调用失败:超时 300s)

## 独立自验门

蓝队未声明任何自验命令——按 v0.24.1 协议视为**自验缺失**,数据不可信。


## 修复后总 diff(蓝队改动 = 本节 − 上一节)

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
