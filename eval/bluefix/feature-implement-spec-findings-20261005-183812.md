# 蓝队 A/B — feature-implement-spec · mode=findings

- 时间:2026-10-05 18:47
- 红队输入:redteam-fresh-20261005-183812.md(实验内对本轮求解新跑)
- 蓝队失败类别:timeout
- 修复后校验:通过
- 数据有效性:有效

## 修复前解法 diff

```diff
--- util.py
+++ util.py
@@ -4,4 +4,12 @@
     moving_average([1, 2, 3, 4], 2) == [1.5, 2.5, 3.5]
     k 必须大于 0 且不超过 len(xs)，否则抛 ValueError。
     """
-    raise NotImplementedError
+    if not isinstance(k, int) or k <= 0 or k > len(xs):
+        raise ValueError("k 必须大于 0 且不超过 len(xs)")
+
+    window_sum = sum(xs[:k])
+    result = [window_sum / k]
+    for i in range(k, len(xs)):
+        window_sum += xs[i] - xs[i - k]
+        result.append(window_sum / k)
+    return result
```

## 蓝队输出

(蓝队调用失败:超时 360s)

## 独立自验门

蓝队未声明任何自验命令——按 v0.24.1 协议视为**自验缺失**,数据不可信。


## 修复后总 diff(蓝队改动 = 本节 − 上一节)

```diff
--- util.py
+++ util.py
@@ -4,4 +4,16 @@
     moving_average([1, 2, 3, 4], 2) == [1.5, 2.5, 3.5]
     k 必须大于 0 且不超过 len(xs)，否则抛 ValueError。
     """
-    raise NotImplementedError
+    n = len(xs)
+    if k <= 0 or k > n:
+        raise ValueError("k 必须大于 0 且不超过 len(xs)")
+    k = int(k)
+    if k < 1:
+        raise ValueError("k 必须大于 0 且不超过 len(xs)")
+
+    window_sum = sum(xs[:k])
+    result = [window_sum / k]
+    for i in range(k, n):
+        window_sum += xs[i] - xs[i - k]
+        result.append(window_sum / k)
+    return result
```
