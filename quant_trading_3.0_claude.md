# 量化交易系统 3.0 升级路线图

> 基于 2.0 版本现状，结合课程要求与 config.py 已落实内容，规划下一步优化方向。
> 数据源：Baostock | 市场：A股沪深 | 周期：日线，持仓1–2个月

---

## 现状快照（2.0 已完成）

| 模块 | 状态 | 说明 |
|---|---|---|
| 股票池初筛 | ✅ 已实现 | ST/停牌/流动性/新股过滤，tradestatus字段集成 |
| 市场环境判断 | ✅ 已实现 | MA20斜率，牛/震荡/熊三档仓位系数 |
| 四策略打标 | ✅ 已实现 | MA金叉、MACD、RSI、布林带 |
| 卖出双轨制 | ✅ 已实现 | P0硬止损+P1移动止盈+P2逻辑卖出+P3时间止损+P4目标止盈 |
| 资金管理 | ✅ 已实现 | 等权分配，8%基础仓，最多8只，20%现金保留 |
| 板块共振识别 | ⚠️ 框架有，RS计算待完善 | 能量偏离度定义尚未量化 |
| 单股历史回溯 | ⚠️ 有构想，未实现 | 需要按需触发的单股回测工具 |
| Walk-Forward验证 | ❌ 未实现 | 防过拟合的核心验证环节 |
| 策略报告 | ⚠️ 有模板 | 尚未对每个策略逐一填写 |

---

## 3.0 升级重点（按优先级排列）

---

### 🥇 优先级1：补全 Walk-Forward 验证（防过拟合）

**问题**：2.0 回测用同一段数据调参和验证，结论不可信。

**3.0 方案**：滚动样本外测试。

```
时间轴示意：

训练期          ← 用来调参
[2019.01 ─── 2022.12]

验证期（样本外）← 用调好的参数验证，绝不回头改参数
                  [2023.01 ─── 2024.06]

实盘期
                              [2024.07 → 至今]
```

**实现方式**：

```python
def walk_forward_test(data, train_years=3, test_months=6):
    """
    滚动样本外验证
    每次用 train_years 训练，往后推 test_months 验证
    滚动至数据末尾，汇总所有验证期的绩效
    """
    results = []
    # 每6个月向前滚一次
    for start in range(0, total_periods - train_window, step):
        train_data = data[start : start + train_window]
        test_data  = data[start + train_window : start + train_window + test_window]
        params     = optimize_params(train_data)   # 在训练集上找最优参数
        perf       = backtest(test_data, params)   # 在验证集上跑，不调参
        results.append(perf)
    return aggregate(results)  # 所有验证期合并后的真实表现
```

**产出**：一张"样本外夏普 vs 样本内夏普"对比表。差距 &gt;0.3 说明过拟合，需简化策略参数。

---

### 🥈 优先级2：完善 RS 板块强度计算

**问题**：2.0 的"能量偏离度"没有量化定义，代码写不出来。

**3.0 方案**：明确公式，可直接落地。

```python
def calc_sector_rs(sector_stocks_df, market_df, lookback=20):
    """
    RS 分数 = 行业超额收益 + 成交量偏离度
    """
    # 行业内股票的等权平均收益
    sector_return_20d = sector_stocks_df['close'].pct_change(lookback).mean()

    # 大盘同期收益
    market_return_20d = market_df['close'].pct_change(lookback).iloc[-1]

    # 超额收益
    alpha = sector_return_20d - market_return_20d

    # 成交量偏离度：行业近5日均量 / 行业60日均量
    vol_ratio = (
        sector_stocks_df['volume'].tail(5).mean().mean() /
        sector_stocks_df['volume'].tail(60).mean().mean()
    )

    # 归一化合并（超额收益权重0.7，量能权重0.3）
    rs_score = 0.7 * alpha + 0.3 * (vol_ratio - 1)
    return rs_score
```

**产出**：每日 Top 5 强势板块清单，与打标结果交叉，过滤出"强势板块内的共振股"。

---

### 🥉 优先级3：引入波动率加权仓位（资金管理升级）

**问题**：等权分配对高波动股票风险暴露过多。

**3.0 方案**：60日标准差加权，风险平价分配。

```python
import numpy as np

def calc_position_size(capital, candidates_df, sigma_lookback=60,
                       base_pct=0.08, max_pct=0.15, market_coef=1.0):
    """
    波动率加权仓位计算
    capital        : 可用资金
    candidates_df  : 候选股价格数据
    sigma_lookback : 计算波动率的回望天数
    market_coef    : 市场环境系数（牛1.0/震荡0.5/熊0.0）
    """
    sigmas = {}
    for code, prices in candidates_df.groupby('code')['close']:
        ret = prices.pct_change().dropna().tail(sigma_lookback)
        sigmas[code] = ret.std()  # 日收益率标准差

    # 权重 = 1/σ，归一化
    inv_sigma = {k: 1/v for k, v in sigmas.items()}
    total = sum(inv_sigma.values())
    weights = {k: v / total for k, v in inv_sigma.items()}

    # 最终仓位（受上下限约束）
    positions = {}
    for code, w in weights.items():
        raw_pct = w * market_coef
        clamped_pct = min(max(raw_pct, 0.02), max_pct)
        positions[code] = capital * clamped_pct

    return positions
```

**对比实验**：在回测框架中同时跑等权 vs 波动率加权，对比夏普比率差异，用数据决定是否切换。

---

### 4️⃣ 优先级4：完善每个策略的报告填写

按老师要求，每个策略都需要一份完整报告，内容包括：

| 报告章节 | 核心内容 | 2.0现状 |
|---|---|---|
| 策略描述 | 类型、理念、数学公式 | 部分完成 |
| 参数敏感性测试 | 对比不同参数组合的夏普/回撤 | ❌ 未完成 |
| 期望值分析 | E(R) = 胜率×盈利 − 败率×亏损 | ❌ 未计算 |
| 卖出方案对比 | 固定5%/10% vs 浮动σ方案 | ❌ 未对比 |
| 分市场环境拆解 | 牛/震荡/熊各自胜率 | ❌ 未拆解 |
| 资金容量估算 | 策略规模上限 | ❌ 未估算 |

**建议**：以"均线金叉"策略为起点，先完整填一份，形成模板，其余3个策略依样复制。

---

### 5️⃣ 优先级5：夏普比率的正确计算方式

**A股特殊性**：无风险利率通常取10年期国债收益率，当前约2.3%/年。

```python
def calc_sharpe(daily_returns, risk_free_annual=0.023):
    """
    夏普比率 = (年化收益 - 无风险利率) / 年化波动率
    """
    rf_daily = (1 + risk_free_annual) ** (1/252) - 1
    excess   = daily_returns - rf_daily
    sharpe   = excess.mean() / excess.std() * np.sqrt(252)  # 年化
    return round(sharpe, 2)

def calc_calmar(annual_return, max_drawdown):
    """
    卡玛比率 = 年化收益 / |最大回撤|
    """
    return round(annual_return / abs(max_drawdown), 2)
```

---

### 6️⃣ 优先级6：财报窗口期与节假日的系统化处理

**2.0现状**：财报窗口用硬编码函数标记，但尚未集成进回测框架和实盘扫描。

**3.0方案**：在信号生成阶段加入窗口期权重衰减，而非硬过滤（避免错过窗口期内的强势个股）。

```python
def signal_confidence(signal_score, date, in_earnings_window):
    """
    财报窗口期内，将信号置信度降低，而非完全屏蔽
    """
    if in_earnings_window:
        return signal_score * 0.6  # 窗口期信号降权60%
    return signal_score
```

---

## 3.0 开发路线建议

```
阶段一（1–2周）：数据与基础设施
  └─ 完善 RS 板块强度计算，产出每日强势板块清单

阶段二（2–3周）：Walk-Forward 验证框架
  └─ 用现有4个策略跑样本外验证，检查是否过拟合

阶段三（3–4周）：填写完整策略报告
  └─ 均线金叉优先，完整走一遍敏感性测试→期望值→卖出方案对比流程

阶段四（4–6周）：波动率加权仓位
  └─ 对比等权 vs σ加权，用数据决策

阶段五（持续）：实盘模拟
  └─ 满足：样本外夏普 > 1.2 且最大回撤 < 15%，才进入实盘
```

---

## 新增术语速查表

| 术语 | 公式/定义 | 本系统参考值 |
|---|---|---|
| 期望值 E(R) | 胜率×均盈 − 败率×均亏 | 目标 > 0 |
| 方差 σ² | E[(R − μ)²] | 越小越稳定 |
| 标准差 σ | √方差 | 60日滚动计算 |
| 夏普比率 | (年化收益 − 无风险利率) / 年化σ | 目标 > 1.2 |
| 卡玛比率 | 年化收益 / \|最大回撤\| | 目标 > 1.0 |
| 盈亏比 | 平均盈利 / 平均亏损 | 目标 > 1.5 |
| 胜率 | 盈利笔数 / 总笔数 | 参考 50–55% |
| 等权分配 | 每只股票分配相同资金 | 当前使用 |
| 波动率加权 | 权重 = 1/σ，归一化 | 3.0 目标 |
| Walk-Forward | 训练集调参，样本外验证 | 3.0 必做 |

---

*版本：3.0 规划稿 | 基于 2.0 + config.py + 课程笔记 | 2026年3月*
