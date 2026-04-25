"""
jq_local/report.py

绩效报告：计算风险指标并绘制净值曲线图。
"""
from __future__ import annotations
import datetime
from typing import Dict, Optional

import numpy as np
import pandas as pd
import matplotlib
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

matplotlib.rcParams["font.sans-serif"] = ["PingFang SC", "Heiti TC",
                                           "SimHei", "Arial Unicode MS",
                                           "DejaVu Sans"]
matplotlib.rcParams["axes.unicode_minus"] = False


# ---------------------------------------------------------------------------
# 指标计算
# ---------------------------------------------------------------------------

def _max_drawdown(nav: pd.Series) -> float:
    roll_max = nav.cummax()
    drawdown = (nav - roll_max) / roll_max
    return float(drawdown.min())


def _sharpe(returns: pd.Series, risk_free: float = 0.03) -> float:
    excess = returns - risk_free / 252
    if excess.std() == 0:
        return 0.0
    return float(excess.mean() / excess.std() * np.sqrt(252))


def _annualized_return(total_ret: float, n_days: int) -> float:
    if n_days <= 0:
        return 0.0
    return float((1 + total_ret) ** (252 / n_days) - 1)


# ---------------------------------------------------------------------------
# 拉取基准净值
# ---------------------------------------------------------------------------

def _fetch_benchmark_nav(
    benchmark: str,
    dates: pd.DatetimeIndex,
    jq_auth_ok: bool = True,
) -> Optional[pd.Series]:
    try:
        import jqdatasdk as jq
        start = dates[0].date()
        end = dates[-1].date()
        df = jq.get_price(
            benchmark,
            start_date=start,
            end_date=end,
            fields=["close"],
            panel=False,
        )
        if df is None or df.empty:
            return None
        df = df.set_index("time")["close"]
        df.index = pd.to_datetime(df.index)
        # 对齐到回测交易日
        df = df.reindex(dates, method="ffill")
        df = df / df.iloc[0]  # 归一化到 1
        return df
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 主函数
# ---------------------------------------------------------------------------

def generate_report(
    nav_series: Dict[datetime.date, float],
    starting_cash: float,
    benchmark: str = "000300.XSHG",
    output_image: Optional[str] = "backtest_report.png",
):
    """
    根据净值序列生成绩效报告并保存图表。

    Parameters
    ----------
    nav_series   : {date: total_value} 字典
    starting_cash: 初始资金
    benchmark    : 比较基准代码
    output_image : 图片保存路径（None 则仅打印不保存）
    """
    if not nav_series:
        print("[report] 无净值数据，跳过报告生成。")
        return

    # 转为 Series
    s = pd.Series(nav_series).sort_index()
    s.index = pd.to_datetime(s.index)

    nav = s / starting_cash   # 归一化净值（起始=1）
    daily_ret = nav.pct_change().dropna()

    n_days = len(nav)
    total_ret = float(nav.iloc[-1] - 1)
    ann_ret = _annualized_return(total_ret, n_days)
    max_dd = _max_drawdown(nav)
    sharpe = _sharpe(daily_ret)

    win_days = int((daily_ret > 0).sum())
    win_rate = win_days / len(daily_ret) if len(daily_ret) > 0 else 0.0

    print("\n" + "=" * 60)
    print("  回测绩效摘要")
    print("=" * 60)
    print(f"  回测期间    : {s.index[0].date()} → {s.index[-1].date()}")
    print(f"  交易天数    : {n_days} 天")
    print(f"  初始资金    : {starting_cash:,.2f} 元")
    print(f"  终值        : {float(s.iloc[-1]):,.2f} 元")
    print(f"  总收益率    : {total_ret:.2%}")
    print(f"  年化收益率  : {ann_ret:.2%}")
    print(f"  最大回撤    : {max_dd:.2%}")
    print(f"  Sharpe 比率 : {sharpe:.3f}")
    print(f"  日胜率      : {win_rate:.2%}  (盈利 {win_days} / 总 {len(daily_ret)} 天)")
    print("=" * 60 + "\n")

    # -- 绘图 ---------------------------------------------------------------
    fig, axes = plt.subplots(2, 1, figsize=(14, 8),
                             gridspec_kw={"height_ratios": [3, 1]})
    fig.suptitle("本地回测报告", fontsize=14, fontweight="bold")

    ax_nav, ax_dd = axes

    # 策略净值
    ax_nav.plot(nav.index, nav.values, label="策略净值", color="#2196F3", linewidth=1.5)
    ax_nav.axhline(1.0, color="grey", linestyle="--", linewidth=0.8)

    # 基准净值
    bm_nav = _fetch_benchmark_nav(benchmark, nav.index)
    if bm_nav is not None:
        ax_nav.plot(bm_nav.index, bm_nav.values,
                    label=f"基准 ({benchmark})",
                    color="#FF9800", linewidth=1.2, alpha=0.8)

    ax_nav.set_ylabel("净值")
    ax_nav.legend(loc="upper left", fontsize=9)
    ax_nav.grid(True, alpha=0.3)
    ax_nav.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    ax_nav.xaxis.set_major_locator(mdates.MonthLocator(interval=3))

    # 回撤图
    roll_max = nav.cummax()
    drawdown = (nav - roll_max) / roll_max
    ax_dd.fill_between(drawdown.index, drawdown.values, 0,
                        alpha=0.4, color="#F44336", label="回撤")
    ax_dd.set_ylabel("回撤")
    ax_dd.set_ylim(bottom=min(drawdown.min() * 1.1, -0.01))
    ax_dd.grid(True, alpha=0.3)
    ax_dd.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    ax_dd.xaxis.set_major_locator(mdates.MonthLocator(interval=3))
    ax_dd.legend(loc="lower left", fontsize=9)

    # 在净值图上标注摘要
    summary_text = (
        f"年化收益: {ann_ret:.1%}  |  最大回撤: {max_dd:.1%}  |  "
        f"Sharpe: {sharpe:.2f}  |  总收益: {total_ret:.1%}"
    )
    ax_nav.text(
        0.01, 0.01, summary_text,
        transform=ax_nav.transAxes,
        fontsize=8, verticalalignment="bottom",
        bbox=dict(boxstyle="round,pad=0.3", facecolor="lightyellow", alpha=0.7),
    )

    plt.tight_layout()

    if output_image:
        fig.savefig(output_image, dpi=150, bbox_inches="tight")
        print(f"[report] 图表已保存：{output_image}")

    plt.show()
    plt.close(fig)
