"""
run_backtest.py —— 本地 JoinQuant 策略回测入口

使用方法
--------
1. 填写下方的账号、密码（jqdatasdk 购买后收到的凭据）。
2. 调整 START_DATE / END_DATE / CAPITAL 等参数。
3. 运行：python run_backtest.py
"""
import sys
import os
import argparse

# ============================================================
# ✏️  在此填写配置
# ============================================================

JQ_USERNAME = "15658059081"   # jqdatasdk 账号（手机号）
JQ_PASSWORD = "Qianchen_0"   # jqdatasdk 密码

DEFAULT_STRATEGY_FILE = "strategys/low_price.py"   # 默认策略文件路径

START_DATE = "2025-01-02"       # 回测开始日期（基本面数据从2025-01起有效；账号权限至2025-12-29）
END_DATE   = "2025-09-30"       # 回测结束日期
CAPITAL    = 1_000_000.0        # 初始资金（元）

OUTPUT_IMAGE = "backtest_report.png"   # 绩效图片保存路径

# ============================================================

from jq_local.engine import BacktestEngine
from jq_local.report import generate_report


def main():
    parser = argparse.ArgumentParser(description="本地 JoinQuant 策略回测")
    parser.add_argument(
        "strategy",
        nargs="?",
        default=DEFAULT_STRATEGY_FILE,
        help=f"策略文件路径（默认：{DEFAULT_STRATEGY_FILE}）",
    )
    args = parser.parse_args()

    engine = BacktestEngine(
        strategy_path=args.strategy,
        start_date=START_DATE,
        end_date=END_DATE,
        capital=CAPITAL,
        jq_username=JQ_USERNAME,
        jq_password=JQ_PASSWORD,
    )

    try:
        nav_series = engine.run()
    except RuntimeError as e:
        print(f"\n[ERROR] {e}")
        sys.exit(1)

    generate_report(
        nav_series=nav_series,
        starting_cash=CAPITAL,
        benchmark=engine._state.benchmark,
        output_image=OUTPUT_IMAGE,
    )


if __name__ == "__main__":
    main()
