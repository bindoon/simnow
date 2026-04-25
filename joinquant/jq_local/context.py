"""
jq_local/context.py

提供与 JoinQuant 平台兼容的运行时对象：
  - GlobalVars  (g)
  - Log         (log)
  - Position
  - Portfolio
  - Context
"""
import logging
import datetime
from collections import defaultdict

# ---------------------------------------------------------------------------
# 日志对象 —— 替代平台内置的 log
# ---------------------------------------------------------------------------

class _LogicalTimeFormatter(logging.Formatter):
    """优先使用日志记录上的 logical_dt 字段作为时间戳。"""

    def formatTime(self, record, datefmt=None):
        logical_dt = getattr(record, "logical_dt", None)
        if logical_dt is not None:
            if datefmt:
                return logical_dt.strftime(datefmt)
            return logical_dt.isoformat(sep=" ", timespec="seconds")
        return super().formatTime(record, datefmt)

class Log:
    """模拟 JoinQuant log 对象的 4 个级别方法。"""

    def __init__(self, name: str = "jq_backtest"):
        self._logger = logging.getLogger(name)
        self._logical_dt = None
        if not self._logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(
                _LogicalTimeFormatter(
                    "%(asctime)s - %(levelname)s - %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S",
                )
            )
            self._logger.addHandler(handler)
            self._logger.setLevel(logging.DEBUG)

    def set_datetime(self, logical_dt: datetime.datetime | None):
        self._logical_dt = logical_dt

    def _extra(self):
        return {"logical_dt": self._logical_dt}

    def info(self, msg, *args):
        self._logger.info(msg, *args, extra=self._extra())

    def debug(self, msg, *args):
        self._logger.debug(msg, *args, extra=self._extra())

    def warn(self, msg, *args):
        self._logger.warning(msg, *args, extra=self._extra())

    def warning(self, msg, *args):
        self._logger.warning(msg, *args, extra=self._extra())

    def error(self, msg, *args):
        self._logger.error(msg, *args, extra=self._extra())

    def set_level(self, *args):
        """平台有此 API，本地忽略即可。"""
        pass


# ---------------------------------------------------------------------------
# g —— 策略全局变量容器
# ---------------------------------------------------------------------------

class GlobalVars:
    """
    模拟 JoinQuant `g` 对象。
    策略中可直接 `g.foo = bar` / 读取 `g.foo`。
    """

    def __repr__(self):
        return f"GlobalVars({self.__dict__})"


# ---------------------------------------------------------------------------
# Position —— 单支持仓
# ---------------------------------------------------------------------------

class Position:
    def __init__(self, code: str, amount: int = 0, avg_cost: float = 0.0):
        self.security = code          # 证券代码
        self.amount = amount          # 持仓股数（手数 * 100）
        self.avg_cost = avg_cost      # 平均持仓成本（元/股）
        self.price = avg_cost         # 最新价格（每日更新）
        self.init_time = None         # 开仓时间（datetime）

    @property
    def value(self) -> float:
        """当前市值"""
        return self.price * self.amount

    @property
    def closeable_amount(self) -> int:
        """可卖数量（简化：默认等于总持仓，日线回测不强制 T+1）"""
        return self.amount

    @property
    def total_amount(self) -> int:
        return self.amount

    def __repr__(self):
        return (f"Position({self.security}, amount={self.amount}, "
                f"avg_cost={self.avg_cost:.2f}, value={self.value:.2f})")


# ---------------------------------------------------------------------------
# Portfolio —— 账户信息
# ---------------------------------------------------------------------------

class Portfolio:
    def __init__(self, starting_cash: float):
        self.starting_cash: float = starting_cash
        self.cash: float = starting_cash
        self.positions: dict[str, Position] = {}   # {code: Position}

    @property
    def total_value(self) -> float:
        """总资产 = 现金 + 持仓市值"""
        pos_value = sum(p.value for p in self.positions.values())
        return self.cash + pos_value

    @property
    def available_cash(self) -> float:
        """可用资金（简化：等于 cash）"""
        return self.cash

    @property
    def returns(self) -> float:
        """收益率"""
        return (self.total_value - self.starting_cash) / self.starting_cash

    def __repr__(self):
        return (f"Portfolio(cash={self.cash:.2f}, "
                f"total_value={self.total_value:.2f}, "
                f"positions={list(self.positions.keys())})")


# ---------------------------------------------------------------------------
# Context —— 策略上下文
# ---------------------------------------------------------------------------

class Context:
    def __init__(self, portfolio: Portfolio):
        self.portfolio = portfolio
        self.current_dt: datetime.datetime = None   # 由引擎在每个 bar 前更新
        self.previous_date: datetime.date = None     # 上一交易日
        # 内部引用，供 API 包装层读取当前日期
        self._current_date: datetime.date = None

    def __repr__(self):
        return (f"Context(current_dt={self.current_dt}, "
                f"portfolio={self.portfolio})")
