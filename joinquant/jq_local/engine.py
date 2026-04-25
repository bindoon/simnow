"""
jq_local/engine.py

BacktestEngine —— 主回测引擎。

流程
----
1. 认证 jqdatasdk
2. exec(strategy.py) 把策略代码加载到兼容命名空间中
3. 调用 initialize(context)
4. 按交易日循环：
   a. 更新 context.current_dt
   b. 结算上一日的 pending orders（取当日 open 价格成交，扣手续费）
   c. 更新所有持仓的当日收盘价
   d. 调用 scheduled_funcs（run_daily 注册的函数）
   e. 记录净值
5. 调用 report.py 输出绩效
"""
from __future__ import annotations
import datetime
import operator
import warnings
from functools import reduce
from pathlib import Path
from typing import Dict, List, Optional

import jqdatasdk as jq
import pandas as pd

from .context import Context, GlobalVars, Log, Portfolio, Position
from . import api as _api_module
from .api import (
    _EngineState, _inject_state, OrderCost,
    set_benchmark, set_option, set_order_cost, set_universe,
    run_daily, run_weekly, run_monthly,
    get_fundamentals, get_current_data,
    attribute_history, history,
    order_value, order_target_value, order, order_target,
    cancel_order, get_open_orders, get_orders, get_trades,
    record, send_message, normalize_code,
    # jqdatasdk 透传
    query, valuation, indicator, balance, income, cash_flow,
    get_price, get_bars, get_trade_days, get_all_trade_days,
    get_all_securities, get_security_info, get_index_stocks,
    get_industry_stocks, get_industries, get_concepts,
    get_concept_stocks, get_industry, run_query,
)


class BacktestEngine:
    """
    本地 JoinQuant 兼容回测引擎。

    Parameters
    ----------
    strategy_path : str | Path
        策略文件路径（零改动，直接把平台代码保存为 .py 文件）。
    start_date : str | datetime.date
        回测开始日期，如 '2020-01-01'。
    end_date : str | datetime.date
        回测结束日期，如 '2025-01-01'。
    capital : float
        初始资金（元），默认 1_000_000。
    jq_username : str
        jqdatasdk 账号（聚宽官网注册的手机号）。
    jq_password : str
        jqdatasdk 密码。
    """

    def __init__(
        self,
        strategy_path: str | Path,
        start_date: str | datetime.date,
        end_date: str | datetime.date,
        capital: float = 1_000_000,
        jq_username: str = "",
        jq_password: str = "",
    ):
        self.strategy_path = Path(strategy_path)
        self.start_date = pd.Timestamp(start_date).date()
        self.end_date = pd.Timestamp(end_date).date()
        self.capital = capital
        self.jq_username = jq_username
        self.jq_password = jq_password

        # 运行时对象
        self._log = Log("jq_backtest")
        self._portfolio = Portfolio(starting_cash=capital)
        self._context = Context(self._portfolio)
        self._g = GlobalVars()

        # 引擎状态（注入到 api.py）
        self._state = _EngineState()
        self._state.context = self._context
        self._state.log = self._log
        self._state.order_cost = OrderCost()
        _inject_state(self._state)

        # 净值记录
        self._nav_series: Dict[datetime.date, float] = {}
        # 成交记录
        self._trade_log: List[dict] = []

    # ------------------------------------------------------------------
    # 公开入口
    # ------------------------------------------------------------------

    def run(self):
        """启动回测。"""
        self._authenticate()
        trade_days = self._get_trade_days()
        self._set_event_time(None)
        self._log.info(
            f"回测区间：{self.start_date} → {self.end_date}，"
            f"共 {len(trade_days)} 个交易日"
        )

        namespace = self._build_namespace()
        self._load_strategy(namespace)

        # 调用 initialize
        if "initialize" in namespace:
            self._set_event_time(None)
            namespace["initialize"](self._context)
        self._log.info("initialize() 完成，开始主循环")

        prev_date: Optional[datetime.date] = None

        for i, trade_date in enumerate(trade_days):
            self._context._current_date = trade_date
            self._context.previous_date = prev_date

            self._set_event_time(trade_date, datetime.time(8, 0))
            self._log.info(f"===== {trade_date} =====")
            self._process_public_events(trade_date)

            # before_trading_start（如果策略实现了）
            if "before_trading_start" in namespace:
                self._set_event_time(trade_date, datetime.time(9, 0))
                try:
                    namespace["before_trading_start"](self._context)
                except Exception as exc:
                    self._log.error(
                        f"before_trading_start 出错：{exc}")

            # 结算上一日的 pending orders（用今日 open 价成交）
            if self._state.pending_orders:
                self._set_event_time(trade_date, datetime.time(9, 30))
                self._settle_orders(trade_date)

            # 执行调度函数（run_daily 注册的 every_bar 等）
            self._run_scheduled(namespace, trade_date)

            # 结算本交易日内新下的单（当天撮合）
            if self._state.pending_orders:
                self._set_event_time(trade_date, datetime.time(9, 30))
                self._settle_orders(trade_date)

            # 更新持仓收盘价
            self._set_event_time(trade_date, datetime.time(15, 0))
            self._update_position_prices(trade_date)

            # after_trading_end（如果实现了）
            if "after_trading_end" in namespace:
                self._set_event_time(trade_date, datetime.time(15, 30))
                try:
                    namespace["after_trading_end"](self._context)
                except Exception as exc:
                    self._log.error(f"after_trading_end 出错：{exc}")

            # 记录当日净值
            nav = self._portfolio.total_value
            self._nav_series[trade_date] = nav
            self._set_event_time(trade_date, datetime.time(15, 0))
            self._log.info(
                f"  净值={nav:,.2f}  "
                f"现金={self._portfolio.cash:,.2f}  "
                f"持仓={list(self._portfolio.positions.keys())}"
            )

            prev_date = trade_date

        self._log.info("回测结束，正在生成报告…")
        return self._nav_series.copy()

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _authenticate(self):
        try:
            jq.auth(self.jq_username, self.jq_password)
            self._log.info("jqdatasdk 认证成功")
        except Exception as exc:
            raise RuntimeError(f"jqdatasdk 认证失败：{exc}") from exc

    def _get_trade_days(self) -> List[datetime.date]:
        days = jq.get_trade_days(
            start_date=self.start_date, end_date=self.end_date
        )
        return [d for d in days]

    def _set_event_time(
        self,
        trade_date: datetime.date | None,
        trade_time: datetime.time | None = None,
    ):
        """设置当前逻辑时间，并同步到 context / log。"""
        logical_dt = None
        if trade_date is not None and trade_time is not None:
            logical_dt = datetime.datetime.combine(trade_date, trade_time)
        self._context.current_dt = logical_dt
        self._log.set_datetime(logical_dt)

    def _build_namespace(self) -> dict:
        """构建策略执行的命名空间：包含所有 JQ 兼容 API + 运行时对象。"""
        ns = {
            # 运行时对象
            "context": self._context,
            "g": self._g,
            "log": self._log,

            # 策略设置函数
            "set_benchmark": set_benchmark,
            "set_option": set_option,
            "set_order_cost": set_order_cost,
            "set_universe": set_universe,
            "OrderCost": OrderCost,

            # 调度
            "run_daily": run_daily,
            "run_weekly": run_weekly,
            "run_monthly": run_monthly,

            # 数据
            "get_fundamentals": get_fundamentals,
            "get_current_data": get_current_data,
            "attribute_history": attribute_history,
            "history": history,
            "get_price": get_price,
            "get_bars": get_bars,
            "get_trade_days": get_trade_days,
            "get_all_trade_days": get_all_trade_days,
            "get_all_securities": get_all_securities,
            "get_security_info": get_security_info,
            "get_index_stocks": get_index_stocks,
            "get_industry_stocks": get_industry_stocks,
            "get_industries": get_industries,
            "get_concepts": get_concepts,
            "get_concept_stocks": get_concept_stocks,
            "get_industry": get_industry,
            "run_query": run_query,

            # 财务数据 query 对象
            "query": query,
            "valuation": valuation,
            "indicator": indicator,
            "balance": balance,
            "income": income,
            "cash_flow": cash_flow,

            # 交易
            "order": order,
            "order_target": order_target,
            "order_value": order_value,
            "order_target_value": order_target_value,
            "cancel_order": cancel_order,
            "get_open_orders": get_open_orders,
            "get_orders": get_orders,
            "get_trades": get_trades,

            # 工具
            "record": record,
            "send_message": send_message,
            "normalize_code": normalize_code,

            # 标准库
            "datetime": datetime,
        }
        return ns

    def _load_strategy(self, namespace: dict):
        code = self.strategy_path.read_text(encoding="utf-8")
        try:
            exec(compile(code, str(self.strategy_path), "exec"), namespace)
        except Exception as exc:
            raise RuntimeError(f"加载策略文件失败：{exc}") from exc
        self._log.info(f"策略文件加载成功：{self.strategy_path.name}")

        # 用批量版本替换策略内的 filter_paused_stock，避免每只股票单独发 API 请求
        state_ref = self._state

        def _batch_filter_paused_stock(stock_list):
            if not stock_list:
                return []
            date = (state_ref.context._current_date
                    if state_ref.context else datetime.date.today())
            try:
                df = jq.get_price(
                    list(stock_list),
                    start_date=date,
                    end_date=date,
                    fields=["paused"],
                    skip_paused=False,
                    panel=False,
                )
                if df is None or df.empty:
                    return list(stock_list)
                paused_codes = set(df[df["paused"] == 1]["code"].tolist())
                return [s for s in stock_list if s not in paused_codes]
            except Exception:
                return list(stock_list)

        namespace["filter_paused_stock"] = _batch_filter_paused_stock

    def _run_scheduled(self, namespace: dict, trade_date: datetime.date):
        """运行本交易日所有 run_daily 注册的函数。"""
        for entry in self._state.scheduled_funcs:
            freq = entry.get("freq", "daily")
            time_str = entry.get("time", "every_bar")
            func = entry["func"]

            # 频率过滤
            if freq == "weekly":
                weekday = entry.get("weekday", 1)
                if trade_date.isoweekday() != weekday:
                    continue
            elif freq == "monthly":
                monthday = entry.get("monthday", 1)
                # 当月第 monthday 个交易日
                if not self._is_nth_trade_day_of_month(trade_date, monthday):
                    continue
            # daily / every_bar 直接执行

            event_time = self._resolve_schedule_time(time_str)
            self._set_event_time(trade_date, event_time)

            try:
                func(self._context)
            except Exception as exc:
                self._log.error(
                    f"策略函数 {func.__name__} 在 {trade_date} 出错：{exc}")

    def _resolve_schedule_time(self, time_str: str) -> datetime.time:
        """把 run_daily 的 time 参数映射到逻辑时钟。"""
        if time_str == "every_bar":
            return datetime.time(9, 30)
        parsed = pd.Timestamp(time_str).time()
        return datetime.time(parsed.hour, parsed.minute, parsed.second)

    def _is_nth_trade_day_of_month(self, date: datetime.date, n: int) -> bool:
        """判断 date 是否是当月第 n 个交易日（n 从 1 开始）。"""
        month_start = date.replace(day=1)
        month_end = (date.replace(month=date.month % 12 + 1, day=1)
                     if date.month < 12
                     else date.replace(year=date.year + 1, month=1, day=1))
        try:
            days = jq.get_trade_days(
                start_date=month_start,
                end_date=min(month_end - datetime.timedelta(days=1), date),
            )
            return len(days) == n
        except Exception:
            return False

    # ------------------------------------------------------------------
    # 撮合引擎
    # ------------------------------------------------------------------

    def _settle_orders(self, trade_date: datetime.date):
        """
        结算 pending orders：用 trade_date 的开盘价成交，扣除手续费。
        """
        orders = self._state.pending_orders[:]
        self._state.pending_orders.clear()

        if not orders:
            return

        codes = list({o["code"] for o in orders})

        # 批量获取当日价格
        prices = self._fetch_prices(codes, trade_date)

        cost_cfg = self._state.order_cost or OrderCost()

        for o in orders:
            code = o["code"]
            price = prices.get(code)

            if price is None or price <= 0:
                self._log.warn(
                    f"  [跳过] {code} 在 {trade_date} 无有效价格")
                continue

            order_type = o["type"]

            if order_type == "order_value":
                value = o["value"]
                self._execute_value_order(
                    code, value, price, trade_date, cost_cfg)

            elif order_type == "order_target_value":
                target_value = o["value"]
                pos = self._portfolio.positions.get(code)
                current_value = pos.value if pos else 0.0
                delta_value = target_value - current_value
                if abs(delta_value) < 1:
                    continue
                self._execute_value_order(
                    code, delta_value, price, trade_date, cost_cfg)

            elif order_type == "order_amount":
                amount = o["amount"]
                value = amount * price
                self._execute_value_order(
                    code, value, price, trade_date, cost_cfg)

            elif order_type == "order_target_amount":
                target_amount = o["amount"]
                pos = self._portfolio.positions.get(code)
                current_amount = pos.amount if pos else 0
                delta_amount = target_amount - current_amount
                if delta_amount == 0:
                    continue
                value = delta_amount * price
                self._execute_value_order(
                    code, value, price, trade_date, cost_cfg)

    def _execute_value_order(
        self,
        code: str,
        value: float,
        price: float,
        trade_date: datetime.date,
        cost_cfg: OrderCost,
    ):
        """
        执行一笔按金额计算的买卖单。

        value > 0：买入
        value < 0：卖出
        """
        if value > 0:
            # 买入：取 value / price 再向下取整到 100 股
            raw_amount = int(value / price)
            amount = (raw_amount // 100) * 100
            if amount <= 0:
                return

            if amount != raw_amount:
                self._log.info(
                    f"  [手数调整] {code} 目标股数={raw_amount}，"
                    f"按 A 股规则调整为 {amount} 股"
                )

            fee = max(
                price * amount * cost_cfg.open_commission,
                cost_cfg.min_commission,
            ) + price * amount * cost_cfg.open_tax
            total_cost = price * amount + fee

            if total_cost > self._portfolio.cash:
                # 资金不足，按可用资金折算
                affordable = self._portfolio.cash / (
                    price * (1 + cost_cfg.open_commission) + cost_cfg.open_tax * price
                )
                amount = (int(affordable) // 100) * 100
                if amount <= 0:
                    self._log.warn(f"  [资金不足] 跳过买入 {code}")
                    return
                fee = max(
                    price * amount * cost_cfg.open_commission,
                    cost_cfg.min_commission,
                ) + price * amount * cost_cfg.open_tax
                total_cost = price * amount + fee

            self._portfolio.cash -= total_cost

            pos = self._portfolio.positions.get(code)
            if pos is None:
                pos = Position(code, 0, 0.0)
                self._portfolio.positions[code] = pos

            new_total = pos.amount + amount
            if new_total > 0:
                pos.avg_cost = (
                    pos.avg_cost * pos.amount + price * amount
                ) / new_total
            pos.amount = new_total
            pos.price = price

            direction = "BUY"

        else:
            # 卖出
            pos = self._portfolio.positions.get(code)
            if pos is None or pos.amount == 0:
                return

            sell_amount = min(abs(int(value / price)), pos.amount)
            sell_amount = (sell_amount // 100) * 100
            if sell_amount <= 0:
                # 若 value 极小，全卖
                sell_amount = pos.amount

            estimated_sell = min(abs(int(value / price)), pos.amount)
            if sell_amount != estimated_sell:
                self._log.info(
                    f"  [手数调整] {code} 目标卖出={estimated_sell}，"
                    f"按 A 股规则调整为 {sell_amount} 股"
                )

            fee = max(
                price * sell_amount * cost_cfg.close_commission,
                cost_cfg.min_commission,
            ) + price * sell_amount * cost_cfg.close_tax

            proceeds = price * sell_amount - fee
            self._portfolio.cash += proceeds

            pos.amount -= sell_amount
            if pos.amount <= 0:
                del self._portfolio.positions[code]

            direction = "SELL"

        self._log.info(
            f"  [{direction}] {code} 价格={price:.2f} "
            f"股数={sell_amount if direction == 'SELL' else amount} "
            f"日期={trade_date}"
        )
        self._trade_log.append({
            "date": trade_date,
            "code": code,
            "direction": direction,
            "price": price,
            "amount": sell_amount if direction == "SELL" else amount,
        })

    def _process_public_events(self, trade_date: datetime.date):
        """在 08:00 扫描持仓相关公共事件，如分红送转等。"""
        held_codes = list(self._portfolio.positions.keys())
        if not held_codes:
            return

        table = getattr(jq, "finance", None)
        if table is None:
            return

        try:
            xr_xd = getattr(table, "STK_XR_XD")
        except Exception:
            return

        candidate_date_cols = [
            "a_registration_date",
            "ex_dividend_date",
            "payable_date",
            "board_plan_pub_date",
            "implementation_pub_date",
            "report_date",
        ]
        date_columns = []
        for col_name in candidate_date_cols:
            if hasattr(xr_xd, col_name):
                date_columns.append(getattr(xr_xd, col_name))

        if not hasattr(xr_xd, "code") or not date_columns:
            return

        query_columns = [xr_xd.code] + date_columns
        detail_names = [
            "bonus_amount_rmb",
            "bonus_ratio_rmb",
            "transfer_ratio",
            "dividend_ratio",
            "progress",
        ]
        for col_name in detail_names:
            if hasattr(xr_xd, col_name):
                query_columns.append(getattr(xr_xd, col_name))

        try:
            condition = xr_xd.code.in_(held_codes)
            date_matches = [col == trade_date for col in date_columns]
            if date_matches:
                condition = condition & reduce(operator.or_, date_matches)
            df = jq.finance.run_query(jq.query(*query_columns).filter(condition))
        except Exception:
            return

        if df is None or df.empty:
            return

        for _, row in df.iterrows():
            date_parts = []
            for col_name in [col.name for col in date_columns]:
                value = row.get(col_name)
                if pd.notna(value):
                    date_parts.append(f"{col_name}={value}")

            detail_parts = []
            for col_name in detail_names:
                if col_name in df.columns and pd.notna(row.get(col_name)):
                    detail_parts.append(f"{col_name}={row[col_name]}")

            summary = "，".join(date_parts + detail_parts)
            self._log.info(f"  [公共事件] {row['code']} {summary}")

    def _fetch_prices(
        self, codes: List[str], trade_date: datetime.date
    ) -> Dict[str, float]:
        """批量获取指定日期的收盘价（用于成交撮合）。"""
        if not codes:
            return {}
        try:
            df = jq.get_price(
                codes,
                start_date=trade_date,
                end_date=trade_date,
                fields=["open", "close"],
                skip_paused=False,
                panel=False,
            )
            if df is None or df.empty:
                return {}
            # 优先使用 open 价，若为 0 则回退到 close
            result = {}
            for code in codes:
                row = df[df["code"] == code]
                if row.empty:
                    continue
                r = row.iloc[0]
                p = r.get("open", 0)
                if p == 0:
                    p = r.get("close", 0)
                result[code] = float(p)
            return result
        except Exception as exc:
            self._log.error(f"获取价格失败：{exc}")
            return {}

    def _update_position_prices(self, trade_date: datetime.date):
        """用收盘价更新所有持仓的 price 字段。"""
        codes = list(self._portfolio.positions.keys())
        if not codes:
            return
        prices = {}
        try:
            df = jq.get_price(
                codes,
                start_date=trade_date,
                end_date=trade_date,
                fields=["close"],
                skip_paused=False,
                panel=False,
            )
            if df is not None and not df.empty:
                for code in codes:
                    row = df[df["code"] == code]
                    if not row.empty:
                        prices[code] = float(row.iloc[0]["close"])
        except Exception:
            pass

        for code, pos in self._portfolio.positions.items():
            if code in prices and prices[code] > 0:
                pos.price = prices[code]
