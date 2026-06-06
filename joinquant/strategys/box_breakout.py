"""
箱体突破策略，适用于聚宽线上环境和本地 JoinQuant 兼容模拟器。

策略思路：
1. 先从基本面里筛出一个可交易股票池。
2. 用已经完成的日线数据判断 20 日箱体突破。
3. 对突破强度最高的股票下买入单。
4. 持仓后按固定止损、移动止损、均线破位三种条件卖出。

注意：
所有买卖信号都只使用已完成的日线数据，避免使用未来数据。
在当前本地模拟器里，attribute_history 默认取到上一交易日，
所以止损判断本质上是按昨日收盘信号，在下一次交易调度时卖出。
"""


def initialize(context):  # 聚宽策略初始化函数，回测开始时只执行一次
    set_benchmark('000300.XSHG')  # 设置策略业绩比较基准为沪深 300
    set_option('use_real_price', True)  # 使用真实价格，避免复权价格影响成交显示
    set_option('order_volume_ratio', 1)  # 设置最大成交量比例，本地模拟器里主要作为兼容参数
    set_order_cost(  # 设置股票交易费用
        OrderCost(  # 构造聚宽兼容的交易费率对象
            open_tax=0,  # 买入不收印花税
            close_tax=0.001,  # 卖出收 0.1% 印花税
            open_commission=0.0003,  # 买入佣金 0.03%
            close_commission=0.0003,  # 卖出佣金 0.03%
            close_today_commission=0,  # 股票没有平今仓额外费用
            min_commission=5,  # 单笔最低佣金 5 元
        ),
        type='stock',  # 上面的费率配置应用到股票交易
    )

    g.stocknum = 3  # 最多同时持有 3 只股票

    g.box_window = 20  # 箱体统计窗口为 20 个交易日
    g.volume_window = 5  # 放量判断使用箱体末尾 5 日平均成交量
    g.volume_ratio = 1.5  # 突破日成交量至少为 5 日均量的 1.5 倍
    g.max_box_range = 0.18  # 箱体最大振幅为 18%，超过则认为箱体太宽

    g.ma_short = 5  # 短期均线周期为 5 日
    g.ma_long = 10  # 长期均线周期为 10 日
    g.stop_loss_pct = 0.07  # 固定止损线为买入成本下跌 7%
    g.trailing_stop_pct = 0.08  # 移动止损线为最高收盘价回撤 8%

    g.min_listing_days = 120  # 只交易上市满 120 天的股票
    g.min_market_cap = 20  # 股票池最小总市值为 20 亿元
    g.max_market_cap = 500  # 股票池最大总市值为 500 亿元

    g.stock_pool = []  # 保存当前可选股票池
    g.price_peaks = {}  # 保存每只持仓股买入后的最高收盘价，用于移动止损

    run_weekly(update_stock_pool, weekday=1, time='9:05')  # 每周一 9:05 刷新股票池
    run_daily(trade, 'every_bar')  # 每个交易调度周期执行一次交易逻辑


def update_stock_pool(context):  # 刷新股票池
    q = query(  # 构造基本面查询对象
        valuation.code,  # 查询股票代码字段
        valuation.market_cap,  # 查询总市值字段
    ).filter(  # 增加筛选条件
        valuation.market_cap.between(g.min_market_cap, g.max_market_cap)  # 只保留指定市值范围的股票
    )

    df = get_fundamentals(q)  # 按当前回测日期获取基本面数据
    if df is None or df.empty:  # 如果基本面查询失败或没有结果
        g.stock_pool = []  # 清空股票池，避免继续使用过期股票池
        log.info('stock pool refresh failed: empty fundamentals result')  # 记录股票池刷新失败
        return  # 没有候选股时直接结束刷新

    stock_list = list(df['code'])  # 从基本面结果里取出股票代码列表
    stock_list = filter_board_stock(stock_list)  # 过滤科创板、北交所等不想交易的板块
    stock_list = filter_basic_stock(context, stock_list)  # 过滤新股、ST、退市风险等基础条件
    stock_list = filter_paused_stock(stock_list)  # 过滤当前停牌股票

    g.stock_pool = stock_list  # 把过滤后的结果保存为全局股票池
    log.info('stock pool refreshed, total candidates: %d', len(g.stock_pool))  # 记录股票池数量


def trade(context):  # 每日核心交易函数，先卖出再买入
    if not g.stock_pool:  # 如果股票池为空
        update_stock_pool(context)  # 立即刷新一次股票池，避免没有候选股

    sell_list = []  # 记录本轮已经发出卖出信号的股票
    for stock in list(context.portfolio.positions.keys()):  # 遍历当前所有持仓股票
        reason = should_sell(context, stock)  # 检查该股票是否触发卖出条件
        if reason:  # 如果返回了卖出原因
            sell_list.append(stock)  # 记录该股票本轮准备卖出
            order_target_value(stock, 0)  # 下单把该股票目标市值调到 0，即清仓
            log.info('sell signal: %s, reason=%s', stock, reason)  # 记录卖出信号和原因

    current_count = len(context.portfolio.positions) - len(sell_list)  # 估算卖出后还会剩下的持仓数量
    available_slots = g.stocknum - current_count  # 计算最多还能买入几只股票
    if available_slots <= 0:  # 如果持仓数量已经达到上限
        return  # 不再寻找新的买入机会

    breakout_list = check_stocks(context)  # 扫描股票池，找出所有满足箱体突破的股票
    if not breakout_list:  # 如果没有任何突破候选
        return  # 本轮不买入

    held_stocks = set(context.portfolio.positions.keys())  # 当前已经持仓的股票集合
    blocked_stocks = held_stocks.union(set(sell_list))  # 已持仓或刚卖出的股票本轮不再买入
    target_value = context.portfolio.total_value / float(g.stocknum)  # 单只股票目标仓位为总资产的三分之一

    for item in breakout_list:  # 按突破强度从高到低遍历候选股
        stock = item['code']  # 取出候选股代码
        if stock in blocked_stocks:  # 如果该股票已持仓或本轮刚触发卖出
            continue  # 跳过，避免重复买入或当天卖了又买
        if available_slots <= 0:  # 如果可用持仓名额已经用完
            break  # 停止买入循环
        order_value(stock, target_value)  # 按目标金额买入该股票
        log.info(  # 记录买入信号详情
            'buy signal: %s, score=%.4f, box_high=%.2f, close=%.2f',  # 日志模板
            stock,  # 买入股票代码
            item['score'],  # 突破评分
            item['box_high'],  # 箱体上沿价格
            item['close'],  # 突破日收盘价
        )
        available_slots -= 1  # 买入一个候选后，占用一个持仓名额


def check_stocks(context):  # 扫描股票池并返回突破候选列表
    breakout_list = []  # 保存所有触发突破信号的股票
    for stock in g.stock_pool:  # 遍历股票池中的每只股票
        signal = get_breakout_signal(stock)  # 计算该股票是否满足箱体突破
        if signal is not None:  # 如果返回了有效突破信号
            breakout_list.append(signal)  # 加入候选列表

    breakout_list.sort(key=lambda item: item['score'], reverse=True)  # 按评分从高到低排序
    log.info('box breakout count: %d', len(breakout_list))  # 记录本轮突破股票数量
    return breakout_list  # 返回排序后的突破候选


def get_breakout_signal(stock):  # 判断单只股票是否触发箱体突破买入信号
    bar_count = g.box_window + 1  # 需要 20 天箱体数据加 1 天信号数据
    history_data = attribute_history(  # 获取历史日线数据
        stock,  # 当前股票代码
        bar_count,  # 获取的日线数量
        '1d',  # 日线周期
        ['close', 'high', 'low', 'volume'],  # 获取收盘价、最高价、最低价、成交量
        skip_paused=True,  # 跳过停牌日，保证取到足够的有效交易日
    )

    if history_data is None or len(history_data) < bar_count:  # 如果历史数据不足
        return None  # 不产生买入信号

    signal_bar = history_data.iloc[-1]  # 最后一根已完成日线作为突破信号日
    box_data = history_data.iloc[:-1]  # 信号日前面的 20 根日线作为箱体区间

    box_high = box_data['high'].max()  # 箱体上沿为 20 日内最高价
    box_low = box_data['low'].min()  # 箱体下沿为 20 日内最低价
    if box_low <= 0:  # 如果最低价异常，避免除以 0 或负数
        return None  # 放弃该股票

    box_range = (box_high - box_low) / box_low  # 计算箱体振幅
    if box_range > g.max_box_range:  # 如果箱体振幅超过设定上限
        return None  # 认为箱体不够紧，不买入

    volume_ma = box_data['volume'].tail(g.volume_window).mean()  # 计算箱体末尾 5 日平均成交量
    if volume_ma is None or volume_ma <= 0:  # 如果平均成交量异常
        return None  # 放弃该股票

    close_price = float(signal_bar['close'])  # 取突破信号日收盘价
    volume = float(signal_bar['volume'])  # 取突破信号日成交量

    if close_price <= box_high:  # 如果收盘价没有站上箱体上沿
        return None  # 不算有效突破
    if volume < volume_ma * g.volume_ratio:  # 如果突破日没有明显放量
        return None  # 不买入

    breakout_pct = (close_price - box_high) / box_high  # 计算突破幅度
    volume_ratio = volume / volume_ma  # 计算成交量放大倍数
    score = breakout_pct * 100 + volume_ratio  # 综合突破幅度和放量倍数作为排序分数

    return {  # 返回突破信号详情
        'code': stock,  # 股票代码
        'close': close_price,  # 突破日收盘价
        'box_high': float(box_high),  # 箱体上沿价格
        'score': float(score),  # 突破评分
    }


def should_sell(context, stock):  # 判断单只持仓股票是否应该卖出
    position = context.portfolio.positions.get(stock)  # 读取该股票当前持仓对象
    if position is None:  # 如果没有持仓
        return None  # 不需要卖出

    history_data = attribute_history(  # 获取最近若干日收盘价
        stock,  # 当前持仓股票代码
        g.ma_long,  # 获取长期均线所需的 10 根日线
        '1d',  # 日线周期
        ['close'],  # 只需要收盘价
        skip_paused=False,  # 不跳过停牌日，保持最近日期序列连续
    )
    if history_data is None or len(history_data) < g.ma_long:  # 如果收盘价数据不足
        return None  # 暂时不卖出，避免误判

    close_series = history_data['close']  # 提取收盘价序列
    last_close = float(close_series.iloc[-1])  # 取最后一个已完成交易日的收盘价

    peak_price = g.price_peaks.get(stock, position.avg_cost)  # 读取该股历史最高收盘价，首次用持仓成本兜底
    peak_price = max(peak_price, last_close)  # 用最新收盘价更新最高价
    g.price_peaks[stock] = peak_price  # 保存更新后的最高收盘价

    if last_close <= position.avg_cost * (1 - g.stop_loss_pct):  # 如果最新收盘价跌破成本价 7%
        g.price_peaks.pop(stock, None)  # 清除该股票的移动止损最高价记录
        return 'stop_loss'  # 返回固定止损卖出原因

    if peak_price > 0 and last_close <= peak_price * (1 - g.trailing_stop_pct):  # 如果从最高收盘价回撤 8%
        g.price_peaks.pop(stock, None)  # 清除该股票的移动止损最高价记录
        return 'trailing_stop'  # 返回移动止损卖出原因

    ma_short = close_series.tail(g.ma_short).mean()  # 计算最近 5 日均线
    ma_long = close_series.tail(g.ma_long).mean()  # 计算最近 10 日均线
    if ma_short < ma_long:  # 如果 5 日均线跌破 10 日均线
        g.price_peaks.pop(stock, None)  # 清除该股票的移动止损最高价记录
        return 'ma5_below_ma10'  # 返回均线破位卖出原因

    return None  # 没有触发任何卖出条件


def filter_basic_stock(context, stock_list):  # 按上市时间和 ST 状态过滤股票
    current_date = None  # 初始化当前日期变量
    if context.current_dt is not None:  # 如果上下文里有当前逻辑时间
        current_date = context.current_dt.date()  # 使用当前逻辑时间对应的日期
    elif context.previous_date is not None:  # 如果当前逻辑时间不可用但有上一交易日
        current_date = context.previous_date  # 使用上一交易日作为查询日期

    if current_date is None:  # 如果无法确定查询日期
        return stock_list  # 不做基础过滤，直接返回原列表

    all_stocks = get_all_securities(types=['stock'], date=current_date)  # 获取当前日期全部股票基础信息
    if all_stocks is None or all_stocks.empty:  # 如果基础信息获取失败
        return stock_list  # 不做基础过滤，直接返回原列表

    filtered = []  # 保存通过基础过滤的股票
    for stock in stock_list:  # 遍历输入股票列表
        if stock not in all_stocks.index:  # 如果股票不在基础信息表里
            continue  # 跳过该股票

        row = all_stocks.loc[stock]  # 取出该股票的基础信息行
        start_date = row['start_date']  # 读取股票上市日期
        if (current_date - start_date).days < g.min_listing_days:  # 如果上市天数不足 120 天
            continue  # 跳过新股

        stock_name = ''  # 初始化股票名称
        if 'display_name' in all_stocks.columns:  # 如果基础信息里有展示名称字段
            stock_name = str(row['display_name'])  # 优先使用展示名称
        elif 'name' in all_stocks.columns:  # 如果只有名称字段
            stock_name = str(row['name'])  # 使用名称字段

        upper_name = stock_name.upper()  # 转成大写，方便识别 ST
        if 'ST' in upper_name or '*' in stock_name:  # 如果名称里包含 ST 或星号
            continue  # 跳过风险警示股票

        filtered.append(stock)  # 该股票通过基础过滤，加入结果列表

    return filtered  # 返回基础过滤后的股票列表


def filter_board_stock(stock_list):  # 过滤不想交易的板块
    filtered = []  # 保存通过板块过滤的股票
    for stock in stock_list:  # 遍历输入股票列表
        if stock.startswith('688'):  # 688 开头通常是科创板股票
            continue  # 跳过科创板
        if stock.startswith('8') or stock.startswith('4'):  # 8 或 4 开头通常是北交所股票
            continue  # 跳过北交所相关股票
        filtered.append(stock)  # 其他股票保留
    return filtered  # 返回板块过滤后的股票列表


def filter_paused_stock(stock_list):  # 过滤当前停牌股票
    if not stock_list:  # 如果输入列表为空
        return []  # 直接返回空列表

    try:  # 尝试读取当前行情状态
        current_data = get_current_data()  # 获取聚宽当前数据对象
        return [stock for stock in stock_list if not current_data[stock].paused]  # 只保留没有停牌的股票
    except Exception as exc:  # 如果停牌状态查询失败
        log.warn('filter_paused_stock failed, keep original list: %s', exc)  # 记录警告并说明保留原列表
        return stock_list  # 查询失败时不做停牌过滤，避免股票池被错误清空
