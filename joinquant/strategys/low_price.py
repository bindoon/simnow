'''
筛选出市值介于20-30亿的股票，选取其中市值最小的三只股票，
每天开盘买入，持有五个交易日，然后调仓。
'''

## 初始化函数，设定要操作的股票、基准等等
def initialize(context):
    # 设定沪深300作为基准
    set_benchmark('000300.XSHG')
    # True为开启动态复权模式，使用真实价格交易
    set_option('use_real_price', True) 
    # 设定成交量比例
    set_option('order_volume_ratio', 1)
    # 股票类交易手续费是：买入时佣金万分之三，卖出时佣金万分之三加千分之一印花税, 每笔交易佣金最低扣5块钱
    set_order_cost(OrderCost(open_tax=0, close_tax=0.001, \
                             open_commission=0.0003, close_commission=0.0003,\
                             close_today_commission=0, min_commission=5), type='stock')
    # 持仓数量
    g.stocknum = 3 
    # 交易日计时器
    g.days = 0 
    # 调仓频率
    g.refresh_rate = 5
    # 运行函数
    run_daily(trade, 'every_bar')

## 选出小市值股票
def check_stocks(context):
    # 设定查询条件
    q = query(
            valuation.code,
            valuation.market_cap
        ).filter(
            valuation.market_cap.between(20,30)
        ).order_by(
            valuation.market_cap.asc()
        )

    # 选出低市值的股票，构成buylist
    df = get_fundamentals(q)
    buylist = list(df['code'])

    # 过滤停牌股票
    buylist = filter_paused_stock(buylist)

    # 过滤：当日成交量相对昨日放大 1.5 倍以上
    def volume_expanded(stock):
        try:
            volumes = attribute_history(stock, 2, '1d', 'volume')['volume'].values
            if len(volumes) < 2:
                return False

            yesterday_volume = volumes[0]
            today_volume = volumes[1]
            if yesterday_volume <= 0:
                return False

            return today_volume >= yesterday_volume * 1.5
        except:
            return False

    buylist = [s for s in buylist if volume_expanded(s)]

    return buylist[:g.stocknum]
  
## 交易函数
def trade(context):
        # --- 新增：9月不参与买卖的逻辑 ---
    # 获取当前日期
    current_date = context.current_dt

    if g.days%g.refresh_rate == 0:

        ## 获取持仓列表
        sell_list = list(context.portfolio.positions.keys())
        # 如果有持仓，则卖出
        if len(sell_list) > 0 :
            for stock in sell_list:
                order_target_value(stock, 0)
    
        # 判断月份是否为 9 月
        if current_date.month == 9 or current_date.month == 1 or current_date.month == 12:
            log.info("当前是9月，进入休息期，不进行任何调仓操作")
            return # 直接跳出函数，不执行后面的逻辑
        # ---------------------------

        ## 分配资金
        if len(context.portfolio.positions) < g.stocknum :
            Num = g.stocknum - len(context.portfolio.positions)
            Cash = context.portfolio.cash/Num
        else: 
            Cash = 0

        ## 选股
        stock_list = check_stocks(context)

        ## 买入股票
        for stock in stock_list:
            if len(context.portfolio.positions.keys()) < g.stocknum:
                order_value(stock, Cash)

        # 天计数加一
        g.days = 1
    else:
        g.days += 1

# 过滤停牌股票
def filter_paused_stock(stock_list):
    current_data = get_current_data()
    return [stock for stock in stock_list if not current_data[stock].paused]
