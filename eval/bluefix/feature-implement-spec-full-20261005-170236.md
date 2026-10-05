# 蓝队 A/B — feature-implement-spec · mode=full

- 时间:2026-10-05 17:10
- 红队输入:feature-implement-spec-20261005-163610.md(full 模式)
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

全仓仅 `util.py` 一个文件、一个函数、一处 `range()` 调用点,无其他同类实例。修复完成并自验通过。

---

## 修复报告

### 一、逐条核对红队发现

| 红队发现 | 核对结论 | 改动 |
|---|---|---|
| **[可选] 非整数 `k` 通过校验后在 `range()` 抛 `TypeError` 而非承诺的 `ValueError`** | **成立**(已复现:`k=2.5`/`2.0` → `TypeError: 'float' object cannot be interpreted as an integer`)。红队定级 [可选] 偏低:docstring 明确承诺"否则抛 ValueError",非整数 `k` 属无效 `k`,应落入该承诺,而非未承诺的 `TypeError`。 | `util.py:7-8` 新增 `if not isinstance(k, int): raise ValueError("k 必须为整数")`,前置拦截。 |

### 红队 [证伪] 项复核

| 红队 [证伪] 项 | 我的复核 |
|---|---|
| 空输入必抛 ValueError | 成立 ✓(空列表 `len=0`,任何 `k>0` 命中 `k>len`) |
| `k==len` 不越界 | 成立 ✓(`range(1)` → 单元素全窗) |
| Py2 整数除法风险 | 成立 ✓(Py3 真除法,且 `k` 经校验为 int,`int/int→float`) |
| `float()` 包装无害 | **部分推翻**:红队评审的 diff 含 `float(sum(...))`,但**实际文件并无 `float()`**(只有 `sum(...)/k`)。结论方向对(Py3 `/` 已得 float,无 `float()` 也满足"返回 float"契约),但红队是对着不存在的代码片段做证伪。 |
| 无调用点/测试可破坏 | 成立 ✓(grep 全仓仅命中定义与文档示例) |
| 错误路径无半成品/副作用 | 成立 ✓ |
| 整数溢出 / 注入 / 并发 | 成立 ✓(纯函数) |

### 二、红队遗漏的同类问题(本报告最重要部分)

红队只验证了 `float` 路径就升格结论,以下**同类"通过数值比较、卡在 `range()` 抛 TypeError"**的实例均未覆盖,我用同一前置校验一并修复:

| 遗漏项 | 复现(修复前) | 红队是否覆盖 |
|---|---|---|
| **`Decimal` 型 `k`**(`Decimal('2')`、`Decimal('2.5')`) | `TypeError: 'decimal.Decimal' object cannot be interpreted as an integer` | ❌ 漏报 |
| **`Fraction` 型 `k`**(`Fraction(5,2)`=2.5 等) | `TypeError: 'Fraction' object cannot be interpreted as an integer` | ❌ 漏报 |
| **非数值 `k`**(`'2'`/`None`/`[2]`) | `TypeError: '<=' not supported between instances of ...`(在 `k<=0` 比较处) | ❌ 漏报 |

> 红队"明确排查过且无问题的领域"里列了边界条件却**未覆盖类型维度**,是其系统性盲点。`isinstance(k, int)` 前置校验一次性覆盖以上全部,统一抛 `ValueError`,完整兑现"否则抛 ValueError"契约。

**`bool` 子类处理**:`isinstance(True, int)` 为 True,故 `k=True`→按 `k=1` 正常工作、`k=False`→`k<=0` 抛 ValueError,未被误杀,无需特判。

**已评估但判定不适用(超范围)**:`xs` 非序列时 `len(xs)` 抛 `TypeError`。docstring 契约只对 `k` 承诺 ValueError,未对 `xs` 类型承诺特定异常,且红队未标记、示例默认 `xs` 为列表——属不同类别,不在本次修复范围。

### 三、自验结果

修复后运行 24 项断言,**24 passed / 0 failed**,覆盖:
- 正常用例(示例 `k=2`→`[1.5,2.5,3.5]`、`k==len`→`[2.5]`、`k=1`、负元素、混合符号、单元素、返回类型为 `float`)✓
- `bool` 不被误杀(`k=True` 工作、`k=False` 抛 ValueError)✓
- 原契约范围校验回归(`k=0`/`-1`/`>len`/空列表 均抛 ValueError)✓
- 红队发现(`k=2.5`/`2.0`/`0.0`/`-1.5` 均抛 ValueError)✓
- 举一反三(`Decimal`/`Fraction`/`str`/`None`/`list` 均抛 ValueError)✓

最终代码 `util.py:1-11`:
```python
def moving_average(xs, k):
    """返回长度为 len(xs) - k + 1 的滑动平均值列表（float）。

    moving_average([1, 2, 3, 4], 2) == [1.5, 2.5, 3.5]
    k 必须大于 0 且不超过 len(xs)，否则抛 ValueError。
    """
    if not isinstance(k, int):
        raise ValueError("k 必须为整数")
    if k <= 0 or k > len(xs):
        raise ValueError("k 必须大于 0 且不超过 len(xs)")
    return [sum(xs[i:i + k]) / k for i in range(len(xs) - k + 1)]
```

## 修复后总 diff(蓝队改动 = 本节 − 上一节)

```diff
--- util.py
+++ util.py
@@ -4,4 +4,8 @@
     moving_average([1, 2, 3, 4], 2) == [1.5, 2.5, 3.5]
     k 必须大于 0 且不超过 len(xs)，否则抛 ValueError。
     """
-    raise NotImplementedError
+    if not isinstance(k, int):
+        raise ValueError("k 必须为整数")
+    if k <= 0 or k > len(xs):
+        raise ValueError("k 必须大于 0 且不超过 len(xs)")
+    return [sum(xs[i:i + k]) / k for i in range(len(xs) - k + 1)]
```
