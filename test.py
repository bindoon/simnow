"""
策略名称：多均线共振箱体突破策略（MA_Resonance_Breakout）
测试标的：300750.XSHE 宁德时代（单股模式）

策略逻辑：
  买入条件（四者同时满足）：
    1. 金叉信号：两天前 MA5 < MA10，当日 MA5 > MA10（金叉确认）
    2. 多头排列：MA5 > MA10 > MA20 > MA60（趋势方向一致）
    3. 箱体突破：当日收盘价突破近 N 日最高价（动能释放）
    4. 成交量确认：当日成交量 > 90日均量 × 放量倍数（排除假突破）
  卖出条件（任一触发）：
    P0 动态止损：买入后跌破 entry_price - k × σ（60日波动率自适应）
    P1 趋势破坏：MA5 下穿 MA10（死叉，趋势方向逆转）
    P2 目标止盈：浮盈达到目标比例

图表说明（聚宽回测图表区）：
  价格图层：close / MA5 / MA10 / MA20 / MA60 / box_top(箱体上沿) / stop_line(动态止损)
  信号图层：signal > 0 = 买入日  signal < 0 = 卖出日（脉冲幅度 = 1% × 当日收盘）
  副图层：  vol_ratio（当日量 / 90日均量）

作者：Frank
数据源：聚宽（JoinQuant）
回测区间：2018-06-11 ~ 2024-12-31（宁德时代上市日起）
"""

import numpy as np
import pandas as pd
from jqdata import *


# ─────────────────────────────────────────────
# 参数区（改这里做敏感性测试）
# ─────────────────────────────────────────────
PARAMS = {
    "ma_short":  5,
    "ma_mid":   10,
    "ma_long":  20,
    "ma_trend": 60,

    "box_window":    20,      # 箱体观察窗口（日）
    "vol_ratio_min": 1.5,     # 放量最低倍数
    "vol_lookback":  90,      # 成交量均值回望期

    "sigma_lookback": 60,     # 动态止损：波动率回望期
    "sigma_k":        1.5,    # 动态止损：σ倍数

    "take_profit_pct": 0.15,  # 目标止盈比例（15%）

    "max_positions":   1,     # 单股测试固定为1
    "position_pct":   0.95,   # 每次买入动用资金比例

    "single_stock": "300750.XSHE",
}


# ─────────────────────────────────────────────
# 初始化
# ─────────────────────────────────────────────
def initialize(context):
    set_benchmark("000300.XSHG")        # 基准：沪深300，与大盘对比
    set_option("use_real_price", True)
    set_order_cost(
        OrderCost(
            open_tax=0,
            close_tax=0.001,            # 印花税 0.1%（卖出）
            open_commission=0.0003,
            close_commission=0.0003,
            min_commission=5,
        ),
        type="stock",
    )

    g.positions_entry  = {}
    g.stop_loss_prices = {}

    run_daily(strategy, "14:50")        # 尾盘执行，次日开盘成交


# ─────────────────────────────────────────────
# 每日主逻辑
# ─────────────────────────────────────────────
def strategy(context):
    stock = PARAMS["single_stock"]

    # 拉足够长的历史数据（一次拉取，所有指标共用）
    lookback = max(
        PARAMS["ma_trend"] + 5,
        PARAMS["box_window"] + 5,
        PARAMS["vol_lookback"] + 5,
    )
    df = attribute_history(
        stock, lookback, "1d",
        ["close", "high", "volume"],
        skip_paused=True,
    )
    if len(df) < lookback:
        return

    close  = df["close"]
    high   = df["high"]
    volume = df["volume"]

    # ── 均线 ──
    ma5  = close.rolling(PARAMS["ma_short"]).mean()
    ma10 = close.rolling(PARAMS["ma_mid"]).mean()
    ma20 = close.rolling(PARAMS["ma_long"]).mean()
    ma60 = close.rolling(PARAMS["ma_trend"]).mean()

    c0  = close.iloc[-1]
    m5  = ma5.iloc[-1]
    m10 = ma10.iloc[-1]
    m20 = ma20.iloc[-1]
    m60 = ma60.iloc[-1]

    # ── 箱体上沿（近N日最高，不含当日）──
    box_high = high.iloc[-(PARAMS["box_window"] + 1):-1].max()

    # ── 放量比 ──
    avg_vol   = volume.iloc[-PARAMS["vol_lookback"] - 1:-1].mean()
    vol_ratio = volume.iloc[-1] / avg_vol if avg_vol > 0 else 0

    # ── 止损线：无持仓时用 0.0 占位（record 不接受 None）──
    stop_price     = g.stop_loss_prices.get(stock, None)
    stop_line_plot = round(stop_price, 2) if stop_price else 0.0

    # ── signal_val 在买卖逻辑里赋值，最后统一 record 一次 ──
    # 买入 → +c0*0.01，卖出 → -c0*0.01，无信号 → 0.0
    signal_val = 0.0

    # ─────────────────────────────────────────
    # Step1：卖出检查（优先于买入）
    # ─────────────────────────────────────────
    in_position = (
        stock in context.portfolio.positions and
        context.portfolio.positions[stock].closeable_amount > 0
    )

    if in_position:
        pos         = context.portfolio.positions[stock]
        entry_price = g.positions_entry.get(stock, pos.avg_cost)
        pnl_pct     = (c0 - entry_price) / entry_price
        reason      = None

        # P0 动态止损
        if stop_price and c0 <= stop_price:
            reason = f"P0动态止损 止损={stop_price:.2f} 现价={c0:.2f}"

        # P1 死叉（趋势破坏）
        elif _is_dead_cross(ma5, ma10):
            reason = "P1死叉 MA5下穿MA10"

        # P2 目标止盈
        elif pnl_pct >= PARAMS["take_profit_pct"]:
            reason = f"P2止盈 浮盈={pnl_pct:.1%}"

        if reason:
            order_target(stock, 0)
            log.info(f"[卖出] {stock} | {reason} | 盈亏={pnl_pct:.1%}")
            g.positions_entry.pop(stock, None)
            g.stop_loss_prices.pop(stock, None)
            signal_val = round(-c0 * 0.01, 2)   # 卖出脉冲（负值）
            _do_record(c0, m5, m10, m20, m60, box_high, 0.0, vol_ratio, signal_val, close)
            return

    # ─────────────────────────────────────────
    # Step2：买入扫描
    # ─────────────────────────────────────────
    if in_position:
        return  # 已持仓不重复买

    if not _is_tradable(stock):
        return

    cond_cross = ma5.iloc[-3] < ma10.iloc[-3] and m5 > m10   # 金叉
    cond_align = m5 > m10 > m20 > m60                         # 多头排列
    cond_box   = c0 > box_high                                 # 箱体突破
    cond_vol   = vol_ratio >= PARAMS["vol_ratio_min"]          # 放量确认

    log.debug(
        f"[扫描] cross={cond_cross} align={cond_align} "
        f"box={cond_box}(c0={c0:.2f}>top={box_high:.2f}) "
        f"vol={cond_vol}({vol_ratio:.1f}x)"
    )

    if cond_cross and cond_align and cond_box and cond_vol:
        cash       = context.portfolio.cash
        buy_amount = context.portfolio.total_value * PARAMS["position_pct"]

        if cash >= buy_amount and cash >= 5000:
            order_value(stock, buy_amount)
            new_stop = _calc_stop_price(close, c0)

            g.positions_entry[stock]   = c0
            g.stop_loss_prices[stock]  = new_stop

            log.info(
                f"[买入] {stock} | 收盘={c0:.2f} | 箱顶={box_high:.2f} | "
                f"止损={new_stop:.2f}({(new_stop-c0)/c0:.1%}) | 放量={vol_ratio:.1f}x"
            )
            signal_val = round(c0 * 0.01, 2)    # 买入脉冲（正值）


    # ════════════════════════════════════════════════
    # 每日唯一一次 record（聚宽不支持同 key 多次 record）
    # close/MA线/箱体/止损线 → 价格轴；vol_ratio → 副图；signal → 信号轴
    # stop_line_plot 无持仓时为 0.0，与收盘价差距太大会压扁均线，
    # 可在聚宽图表设置里把 stop_line 拖到副图单独显示。
    # ════════════════════════════════════════════════
    _do_record(c0, m5, m10, m20, m60, box_high, stop_line_plot, vol_ratio, signal_val, close)


# ─────────────────────────────────────────────
# record 封装：所有值保证为 float，避免 NoneType 报错
# ─────────────────────────────────────────────
def _do_record(c0, m5, m10, m20, m60, box_high, stop_line, vol_ratio, signal, close_series):
    # 计算波动率 (20日收益率标准差)
    try:
        ret = close_series.pct_change().dropna().tail(20)
        volatility = ret.std() * 100  # 转为百分比
    except Exception:
        volatility = 0.0
    
    # 计算当日涨跌幅 (%)
    try:
        daily_return = (close_series.iloc[-1] - close_series.iloc[-2]) / close_series.iloc[-2] * 100
    except Exception:
        daily_return = 0.0
    
    record(
        close        = float(c0),
        volatility   = float(volatility),   # 波动率 (%)
        daily_return = float(daily_return), # 当日涨跌幅 (%)
        signal       = float(signal),       # >0 买入脉冲 / <0 卖出脉冲 / 0 无信号
    )


# ─────────────────────────────────────────────
# 动态止损线计算
# ─────────────────────────────────────────────
def _calc_stop_price(close_series, entry_price):
    """
    止损线 = 买入价 - k × (买入价 × 日收益率σ)
    波动大的股票自动给出更宽的止损空间
    """
    try:
        ret   = close_series.pct_change().dropna().tail(PARAMS["sigma_lookback"])
        sigma = ret.std()
        stop  = entry_price - PARAMS["sigma_k"] * entry_price * sigma
        floor = entry_price * 0.90      # 最多亏10%的绝对下限
        return round(max(stop, floor), 2)
    except Exception:
        return round(entry_price * 0.95, 2)


# ─────────────────────────────────────────────
# 死叉判断
# ─────────────────────────────────────────────
def _is_dead_cross(ma5, ma10):
    """昨日 MA5 > MA10，今日 MA5 < MA10 → 死叉"""
    try:
        return (
            ma5.iloc[-2] > ma10.iloc[-2] and
            ma5.iloc[-1] < ma10.iloc[-1]
        )
    except Exception:
        return False


# ─────────────────────────────────────────────
# 可交易判断
# ─────────────────────────────────────────────
def _is_tradable(stock):
    try:
        cd = get_current_data()
        return not cd[stock].paused and cd[stock].high_limit != cd[stock].low_limit
    except Exception:
        return False