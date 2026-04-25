"""
jq_local/api.py

提供与 JoinQuant 平台兼容的 API 函数，策略代码可直接调用。

架构说明
--------
所有 API 函数都通过访问模块级的 _engine_state 读写引擎状态（当前日期、
pending orders、费率设置等），由 engine.py 在初始化时注入。

策略文件通过 exec(strategy_code, namespace) 加载，namespace 中包含本文件
export 的所有符号，因此策略不需要任何 import。
"""
from __future__ import annotations
import datetime
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import jqdatasdk as jq

# ---------------------------------------------------------------------------
# 引擎状态（由 engine.py 注入）
# ---------------------------------------------------------------------------

class _EngineState:
    """模块级单例，在引擎初始化时被填充。"""

    def __init__(self):
        self.context = None          # jq_local.context.Context 实例
        self.benchmark: str = "000300.XSHG"
        self.use_real_price: bool = True
        self.order_volume_ratio: float = 1.0
        self.order_cost: "OrderCost" = None  # 手续费设置
        self.scheduled_funcs: List[dict] = []   # [{func, time_str}]
        self.pending_orders: List[dict] = []    # [{code, value, type}]
        self.nav_series: Dict[datetime.date, float] = {}  # 净值序列
        self.log = None              # Log 实例


_engine_state = _EngineState()


def _inject_state(state: _EngineState):
    """由 engine.py 在启动前调用，注入运行时状态。"""
    global _engine_state
    _engine_state = state


# ---------------------------------------------------------------------------
# OrderCost —— 手续费结构
# ---------------------------------------------------------------------------

@dataclass
class OrderCost:
    open_tax: float = 0.0
    close_tax: float = 0.001        # 印花税（卖出）
    open_commission: float = 0.0003
    close_commission: float = 0.0003
    close_today_commission: float = 0.0
    min_commission: float = 5.0     # 最低手续费（元）


# ---------------------------------------------------------------------------
# 策略设置函数
# ---------------------------------------------------------------------------

def set_benchmark(code: str):
    _engine_state.benchmark = code


def set_option(key: str, value: Any):
    if key == "use_real_price":
        _engine_state.use_real_price = value
    elif key == "order_volume_ratio":
        _engine_state.order_volume_ratio = value
    # 其余 option 本地忽略


def set_order_cost(cost: OrderCost, type: str = "stock"):
    _engine_state.order_cost = cost


def set_universe(stocks):
    """已废弃 API，本地忽略。"""
    pass


# ---------------------------------------------------------------------------
# 定时调度注册
# ---------------------------------------------------------------------------

def run_daily(func: Callable, time: str, reference_security: str = None):
    """注册一个每日运行的函数（time='every_bar' 或具体时间如 '9:30'）。"""
    _engine_state.scheduled_funcs.append({
        "func": func,
        "time": time,
    })


def run_weekly(func: Callable, weekday: int = 1, time: str = "every_bar",
               reference_security: str = None):
    _engine_state.scheduled_funcs.append({
        "func": func,
        "time": time,
        "weekday": weekday,
        "freq": "weekly",
    })


def run_monthly(func: Callable, monthday: int = 1, time: str = "every_bar",
                reference_security: str = None):
    _engine_state.scheduled_funcs.append({
        "func": func,
        "time": time,
        "monthday": monthday,
        "freq": "monthly",
    })


# ---------------------------------------------------------------------------
# 数据获取 API
# ---------------------------------------------------------------------------

def get_fundamentals(query_object, date=None):
    """
    包装 jqdatasdk.get_fundamentals。

    JoinQuant 平台上此函数在回测时会自动传入当前回测日期；jqdatasdk 本地
    调用时 date 必须显式指定。此包装层自动从引擎状态读取当前日期。
    """
    if date is None:
        if _engine_state.context and _engine_state.context._current_date:
            date = _engine_state.context._current_date
        else:
            date = datetime.date.today()
    return jq.get_fundamentals(query_object, date=date)


class _CurrentDataItem:
    """单支股票的当前数据，模拟 JoinQuant get_current_data()[code] 接口。"""

    def __init__(self, paused: bool = False, high_limit: float = None,
                 low_limit: float = None, last_price: float = None,
                 name: str = ""):
        self.paused = paused
        self.high_limit = high_limit if high_limit is not None else float("inf")
        self.low_limit = low_limit if low_limit is not None else 0.0
        self.last_price = last_price
        self.name = name


def get_current_data():
    """
    模拟 JoinQuant get_current_data()。

    返回一个 dict-like 对象，键为股票代码，Value 为 _CurrentDataItem
    （包含 .paused 等字段）。

    因为此函数通常用于过滤停牌股，只拉取当日 paused 字段，避免大量流量消耗。
    策略代码：
        current_data = get_current_data()
        [stock for stock in list if not current_data[stock].paused]
    """
    date = None
    if _engine_state.context and _engine_state.context._current_date:
        date = _engine_state.context._current_date

    class CurrentDataDict:
        """懒加载：第一次访问时批量拉取所有查询过的 code 数据。"""

        def __init__(self, query_date):
            self._date = query_date
            self._cache: Dict[str, _CurrentDataItem] = {}

        def _fetch(self, codes):
            missing = [c for c in codes if c not in self._cache]
            if not missing:
                return
            try:
                df = jq.get_price(
                    missing,
                    start_date=self._date,
                    end_date=self._date,
                    fields=["open", "close", "high_limit", "low_limit", "paused"],
                    skip_paused=False,
                    panel=False,
                )
                if df is not None and not df.empty:
                    for code in missing:
                        row = df[df["code"] == code]
                        if row.empty:
                            self._cache[code] = _CurrentDataItem(paused=True)
                        else:
                            r = row.iloc[0]
                            self._cache[code] = _CurrentDataItem(
                                paused=bool(r.get("paused", False)),
                                high_limit=r.get("high_limit"),
                                low_limit=r.get("low_limit"),
                                last_price=r.get("close"),
                            )
            except Exception:
                for code in missing:
                    if code not in self._cache:
                        self._cache[code] = _CurrentDataItem(paused=False)

        def __getitem__(self, code: str) -> _CurrentDataItem:
            if code not in self._cache:
                self._fetch([code])
            return self._cache.get(code, _CurrentDataItem(paused=False))

        def __contains__(self, code):
            return True  # 始终返回 True，让策略可以直接用 in

    return CurrentDataDict(date)


def attribute_history(security: str, count: int, unit: str,
                      fields, skip_paused: bool = True,
                      df: bool = True, fq: str = "pre"):
    """模拟 JoinQuant attribute_history（回测环境专用）。"""
    end_date = None
    if _engine_state.context and _engine_state.context._current_date:
        # 取上一个交易日
        end_date = _engine_state.context.previous_date or _engine_state.context._current_date

    result = jq.get_price(
        security,
        count=count,
        end_date=end_date,
        frequency=unit,
        fields=fields if isinstance(fields, list) else [fields],
        skip_paused=skip_paused,
        fq=fq,
    )
    return result


def history(count: int, unit: str, field: str, security_list,
            skip_paused: bool = False, df: bool = True, fq: str = "pre"):
    """模拟 JoinQuant history 函数。"""
    end_date = None
    if _engine_state.context and _engine_state.context._current_date:
        end_date = _engine_state.context.previous_date or _engine_state.context._current_date

    return jq.get_price(
        list(security_list) if not isinstance(security_list, list) else security_list,
        count=count,
        end_date=end_date,
        frequency=unit,
        fields=[field],
        skip_paused=skip_paused,
        fq=fq,
        panel=False,
    )


# ---------------------------------------------------------------------------
# 交易函数
# ---------------------------------------------------------------------------

def order_value(security: str, value: float, pindex: int = 0):
    """按金额下单买入（若 value 为负则卖出）。"""
    _engine_state.pending_orders.append({
        "code": security,
        "value": value,
        "type": "order_value",
    })
    if _engine_state.log:
        direction = "买入" if value >= 0 else "卖出"
        _engine_state.log.info(f"  [{direction}] {security} 金额={value:.2f}")


def order_target_value(security: str, target_value: float, pindex: int = 0):
    """调整持仓至目标金额。"""
    _engine_state.pending_orders.append({
        "code": security,
        "value": target_value,
        "type": "order_target_value",
    })
    if _engine_state.log:
        _engine_state.log.info(
            f"  [调仓] {security} 目标金额={target_value:.2f}")


def order(security: str, amount: int, pindex: int = 0):
    """按股数下单（amount 为负则卖出）。"""
    _engine_state.pending_orders.append({
        "code": security,
        "amount": amount,
        "type": "order_amount",
    })


def order_target(security: str, amount: int, pindex: int = 0):
    """调整持仓至目标股数。"""
    _engine_state.pending_orders.append({
        "code": security,
        "amount": amount,
        "type": "order_target_amount",
    })


def cancel_order(order_param):
    """简化：本地日线回测不支持撤单（所有订单当日撮合）。"""
    pass


def get_open_orders():
    """返回空字典（日线回测当日所有订单即时撮合）。"""
    return {}


def get_orders(order_id=None):
    return {}


def get_trades():
    return {}


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def record(**kwargs):
    """平台画图函数，本地忽略（引擎可扩展）。"""
    pass


def send_message(msg: str):
    """平台消息推送，本地忽略。"""
    pass


def normalize_code(code: str) -> str:
    return jq.normalize_code(code)


# ---------------------------------------------------------------------------
# 透传 jqdatasdk 原生 API（策略可能直接使用）
# ---------------------------------------------------------------------------

from jqdatasdk import (
    query,
    valuation,
    indicator,
    balance,
    income,
    cash_flow,
    get_price,
    get_bars,
    get_trade_days,
    get_all_trade_days,
    get_all_securities,
    get_security_info,
    get_index_stocks,
    get_industry_stocks,
    get_industries,
    get_concepts,
    get_concept_stocks,
    get_industry,
)

run_query = jq.finance.run_query

# get_fundamentals 已由本文件包装，不再透传
