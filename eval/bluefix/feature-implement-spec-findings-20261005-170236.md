---
task: feature-implement-spec-findings
kind: legacy
data_validity: void:pre_invariant
note: 存量报告(早于 v0.25 不变量),仅作证据链,不进统计
---
# 蓝队 A/B — feature-implement-spec · mode=findings

- 时间:2026-10-05 17:06
- 红队输入:feature-implement-spec-20261005-163610.md(findings 模式)
- 蓝队失败类别:无
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

自验全部通过。无回归,文档示例仍自洽。

---

## 修复报告

### 逐条核对

**红队发现** — 非整数 `k` 通过校验后在 `range()` 抛 `TypeError` 而非文档承诺的 `ValueError`(`util.py:7-8`)
- **核对结论:成立(但红队覆盖不完整)**
- 复现:`moving_average([1,2,3,4], 2.0)` / `2.5` → `TypeError: 'float' object cannot be interpreted as an integer`,确系从 `range(len(xs) - k + 1)` 泄漏,违反文档「否则抛 ValueError」。
- 红队只验证了 int/float;其结论方向正确,但覆盖面不足(见下)。
- **改动**:`util.py:10-11` 在数值比较前加 `if not isinstance(k, numbers.Integral): raise ValueError("k 必须是整数")`;并据实收紧文档 `util.py:8`(「k 必须是正整数且不超过 len(xs)」)。

### 红队遗漏的同类问题(举一反三)

同一根因(校验只做数值比较、无类型守卫)下,红队漏掉一大批实例,现已一并处理:

| 类型 k | 原行为 | 修复后 | 红队是否覆盖 |
|---|---|---|---|
| `Decimal('2')`(整数值!) | TypeError | ValueError | ❌ 遗漏 |
| `Decimal('2.5')` | TypeError | ValueError | ❌ 遗漏 |
| `Fraction(2,1)`(整数值!) | TypeError | ValueError | ❌ 遗漏 |
| `Fraction(5,2)` | TypeError | ValueError | ❌ 遗漏 |
| `complex(2,0)` | TypeError(在 `<=` 比较) | ValueError | ❌ 遗漏 |
| `str "2"` | TypeError(在 `<=` 比较) | ValueError | ❌ 遗漏 |
| `None` / `list [1]` | TypeError(在 `<=` 比较) | ValueError | ❌ 遗漏 |

要点:红队举例 `2.0`/`2.5` 给人「只是小数 k 的问题」的错觉;实际连**整数值的 Decimal/Fraction**也会炸(因为 `range()` 不接受这些类型),且 complex/str/None 在**校验行本身**就抛 TypeError。修复用 `numbers.Integral` 守卫一次性覆盖全部——这度量出红队漏了约 7 类同根实例。bool 是 int 子类、与 int 等价,继续放行(无 TypeError、无需处理)。

### 自验结果

直接执行验证矩阵(项目无测试文件,以此为准):
- **正常路径不回归**:int 1/2/4、bool True → 均正常返回;文档示例 `moving_average([1,2,3,4], 2) == [1.5, 2.5, 3.5]` 断言通过。
- **红队发现**:float 2.0/2.5 → 现抛 `ValueError` ✓
- **红队遗漏同类**:Decimal/Fraction/complex/str/None/list → 现均抛 `ValueError` ✓
- **数值边界**:int 0/-1/5、bool False → 仍抛 `ValueError`(「k 必须大于 0 且不超过 len(xs)」)✓
- 全部结果与文档承诺一致,无 `TypeError` 泄漏。

## 修复后总 diff(蓝队改动 = 本节 − 上一节)

```diff
--- util.py
+++ util.py
@@ -1,7 +1,14 @@
+import numbers
+
+
 def moving_average(xs, k):
     """返回长度为 len(xs) - k + 1 的滑动平均值列表（float）。
 
     moving_average([1, 2, 3, 4], 2) == [1.5, 2.5, 3.5]
-    k 必须大于 0 且不超过 len(xs)，否则抛 ValueError。
+    k 必须是正整数且不超过 len(xs)，否则抛 ValueError。
     """
-    raise NotImplementedError
+    if not isinstance(k, numbers.Integral):
+        raise ValueError("k 必须是整数")
+    if k <= 0 or k > len(xs):
+        raise ValueError("k 必须大于 0 且不超过 len(xs)")
+    return [sum(xs[i:i + k]) / k for i in range(len(xs) - k + 1)]
```
