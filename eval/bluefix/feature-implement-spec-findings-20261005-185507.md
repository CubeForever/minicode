---
task: feature-implement-spec-findings
kind: legacy
data_validity: void:pre_invariant
note: 存量报告(早于 v0.25 不变量),仅作证据链,不进统计
---
# 蓝队 A/B — feature-implement-spec · mode=findings

- 时间:2026-10-05 19:02
- 红队输入:redteam-fresh-20261005-185507.md(实验内对本轮求解新跑)
- 蓝队失败类别:无
- 修复后校验:通过
- 数据有效性:有效

## 修复前解法 diff

```diff
--- util.py
+++ util.py
@@ -4,4 +4,11 @@
     moving_average([1, 2, 3, 4], 2) == [1.5, 2.5, 3.5]
     k 必须大于 0 且不超过 len(xs)，否则抛 ValueError。
     """
-    raise NotImplementedError
+    if k <= 0 or k > len(xs):
+        raise ValueError("k 必须大于 0 且不超过 len(xs)")
+    window_sum = sum(xs[:k])
+    result = [window_sum / k]
+    for i in range(k, len(xs)):
+        window_sum += xs[i] - xs[i - k]
+        result.append(window_sum / k)
+    return result
```

## 蓝队输出

## 修复报告 — `util.py` `moving_average`

### 0. 契约清单(docstring 逐条)

docstring 共 5 条承诺,核对结果:

| # | 契约承诺 | 状态 | 依据 |
|---|---|---|---|
| C1 | 返回长度 `len(xs)-k+1` 的列表 | **已兑现** | 首元素 1 + 循环 `len(xs)-k` 次 = `len(xs)-k+1`;实测 n=10,k=3→8;k=5,n=5→1 |
| C2 | 元素类型为 **float** | **未兑现→已修** | `window_sum/k` 对 int/float/bool 出 float,但对 **Decimal 返回 Decimal、Fraction 返回 Fraction**;红队未覆盖此路径。已用 `float()` 包裹两处 |
| C3 | 是"滑动平均值"(语义) | **无法验证** | 增量法对 int/Decimal/Fraction 为精确(实测 Decimal 漂移 0.0);对 float 有漂移,但 docstring **无精度容差**承诺,无法判定违约 |
| C4 | 示例 `moving_average([1,2,3,4],2)==[1.5,2.5,3.5]` | **已兑现** | 修复前后均成立 |
| C5 | `k<=0` 或 `k>len(xs)` 抛 `ValueError` | **已兑现** | k=0/k<0/k>len/空 xs 四例均抛 ValueError |

### 1. 红队发现 → 核对结论 → 改动

**红队发现(仅一条,标"可选/不阻断")**:增量累加 `window_sum += xs[i]-xs[i-k]` 在长序列大跨度浮点上漂移,与逐窗口 `sum(window)/k` 不一致(util.py:12),实测 max 偏差 1.40e-7。

- **核对结论:成立**(漂移真实)。我用 `n=100000, k=5000, ±1e9, seed=0` 独立复现得 **1.397e-07**,与红队吻合。
- **但红队证伪理由不完整**(正是工作要求第 1 条警告):它只跑了 int/float 路径就讨论"无害",**从未验证 Decimal/Fraction 路径**——而该路径上 (a) 漂移根本不存在(精确算术,实测 0.0),(b) 真正的违约是**元素类型不是 float**(见下)。
- **改动**:漂移本身**未修**。理由:契约 C3 无精度容差 → 无法验证为违约;红队自身判"可选/不阻断"正确;逐窗口重算为 O(n·k) 性能回退,无契约依据。`float()` 对 float 为恒等,**未改变漂移**(实测仍 1.397e-07)。

### 2. 红队遗漏的同类问题(本报告最重要部分)

**遗漏:整条"元素类型 float"契约(C2)无人提过。** 红队聚焦数值精度,完全漏了 docstring 第 2 行"列表(float)"这条类型约束——恰是工作要求第 0 条点名的"元素类型约束"层。

- 现象:`moving_average([Decimal('1'),Decimal('2'),Decimal('3')],2)` 修复前返回 `[Decimal('1.5'), Decimal('2.5')]`;Fraction 返回 `[Fraction(3,2), Fraction(5,2)]`。docstring 承诺 float → **违约**。
- **举一反三(同类实例)**:产出 float 的位置有 **两处**——`util.py:10`(首窗口)与 `util.py:13`(滑动窗口),均未保证 float,两处一并修。
- 改动:`util.py:10` `[window_sum / k]` → `[float(window_sum / k)]`;`util.py:13` `result.append(window_sum / k)` → `result.append(float(window_sum / k))`。

**新路径重过契约(第 4 条)**:`float(window_sum/k)` 新路径审计:
- C1 长度:循环结构未动 → 兑现;
- C2 float:int/float/Decimal/Fraction/bool 现均出 float → **兑现**;(complex 会抛 TypeError,但 complex 从未在契约范围内,且原本就非 float,非回退)
- C3 语义:float 漂移不变;Decimal/Fraction 精确后转 float → 无法验证(无容差),无回退;
- C4 示例:`float(1.5)==1.5` → 兑现;C5:守卫未动 → 兑现。
- **无任何原已兑现契约因重写失效**(未把直接求和改滚动和之类的精度退化)。

### 3. 自验命令清单(独立重跑,均退出 0)

- 自验: `python -c "import util; from decimal import Decimal; from fractions import Fraction; r=util.moving_average([Decimal('1'),Decimal('2'),Decimal('3')],2); assert all(isinstance(x,float) for x in r) and r==[1.5,2.5], r; r2=util.moving_average([Fraction(1),Fraction(2),Fraction(3)],2); assert all(isinstance(x,float) for x in r2) and r2==[1.5,2.5], r2; print('C2 float OK')"`
- 自验: `python -c "import util; assert util.moving_average([1,2,3,4],2)==[1.5,2.5,3.5]; print('C4 example OK')"`
- 自验: `python -c "import util; assert len(util.moving_average(list(range(10)),3))==8 and len(util.moving_average(list(range(5)),5))==1; print('C1 length OK')"`
- 自验: `python -c "import util; ok=True
for xs,k in [([1,2],0),([1,2],3),([1,2],-1),([],1)]:
 try: util.moving_average(xs,k); ok=False
 except ValueError: pass
assert ok; print('C5 ValueError OK')"`
- 自验: `python -c "import util,random; random.seed(0); xs=[random.uniform(-1e9,1e9) for _ in range(100000)]; inc=util.moving_average(xs,5000); rec=[sum(xs[i:i+5000])/5000 for i in range(len(xs)-5000+1)]; d=max(abs(a-b) for a,b in zip(inc,rec)); assert d>0; print('float drift (no-tolerance spec, non-blocking):', format(d,'.3e'))"`

---

**小结**:红队仅报告 1 条(漂移,标可选/不阻断,核对成立但非契约违约,未修)。契约驱动额外暴露红队漏掉的整条 C2「float 元素类型」契约——Decimal/Fraction 路径完全未覆盖——已修 `util.py:10` 与 `util.py:13` 两处同类实例并自验通过。红队漏报率:1 条真实契约违约被遗漏(漏报 100%)。

## 独立自验门(评测脚本重跑,非蓝队自述)
- [PASS] `python -c "import util; from decimal import Decimal; from fractions import Fraction; r=util.moving_average([Decimal('1'),Decimal('2'),Decimal('3')],2); assert all(isinstance(x,float) for x in r) and r==[1.5,2.5], r; r2=util.moving_average([Fraction(1),Fraction(2),Fraction(3)],2); assert all(isinstance(x,float) for x in r2) and r2==[1.5,2.5], r2; print('C2 float OK')"`
- [PASS] `python -c "import util; assert util.moving_average([1,2,3,4],2)==[1.5,2.5,3.5]; print('C4 example OK')"`
- [PASS] `python -c "import util; assert len(util.moving_average(list(range(10)),3))==8 and len(util.moving_average(list(range(5)),5))==1; print('C1 length OK')"`
- [PASS] `python -c "import util; ok=True
for xs,k in [([1,2],0),([1,2],3),([1,2],-1),([],1)]:
 try: util.moving_average(xs,k); ok=False
 except ValueError: pass
assert ok; print('C5 ValueError OK')"`
- [PASS] `python -c "import util,random; random.seed(0); xs=[random.uniform(-1e9,1e9) for _ in range(100000)]; inc=util.moving_average(xs,5000); rec=[sum(xs[i:i+5000])/5000 for i in range(len(xs)-5000+1)]; d=max(abs(a-b) for a,b in zip(inc,rec)); assert d>0; print('float drift (no-tolerance spec, non-blocking):', format(d,'.3e'))"`

独立复验:5/5 通过

## 修复后总 diff(蓝队改动 = 本节 − 上一节)

```diff
--- util.py
+++ util.py
@@ -4,4 +4,11 @@
     moving_average([1, 2, 3, 4], 2) == [1.5, 2.5, 3.5]
     k 必须大于 0 且不超过 len(xs)，否则抛 ValueError。
     """
-    raise NotImplementedError
+    if k <= 0 or k > len(xs):
+        raise ValueError("k 必须大于 0 且不超过 len(xs)")
+    window_sum = sum(xs[:k])
+    result = [float(window_sum / k)]
+    for i in range(k, len(xs)):
+        window_sum += xs[i] - xs[i - k]
+        result.append(float(window_sum / k))
+    return result
```
