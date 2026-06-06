"""
箱体突破 + 均线金叉多头排列策略。

买入条件：
1. 昨日收盘价突破前 20 日箱体上沿。
2. 昨日成交量至少达到箱体末尾 5 日均量的 1.5 倍。
3. 两天前 MA5 上穿 MA10。
4. 当前均线多头排列：MA5 > MA10 > MA20 > MA60。
5. 记录 90 日成交量分析结果，用于日志和排序。

卖出条件：
1. 固定止损：收盘价较持仓成本下跌 7%。
2. 移动止损：收盘价较持仓后最高收盘价回撤 8%。
3. 均线止损：MA5 下穿 MA10。

本策略只使用已完成日线数据，适合聚宽线上回测/模拟和本地兼容环境。
"""


def initialize(context):
    set_benchmark('000300.XSHG')
    set_option('use_real_price', True)
    set_option('order_volume_ratio', 1)
    set_order_cost(
        OrderCost(
            open_tax=0,
            close_tax=0.001,
            open_commission=0.0003,
            close_commission=0.0003,
            close_today_commission=0,
            min_commission=5,
        ),
        type='stock',
    )

    g.stocknum = 3

    g.box_window = 20
    g.volume_window = 5
    g.volume_ratio = 1.5
    g.max_box_range = 0.18

    g.ma_short = 5
    g.ma_mid = 10
    g.ma_20 = 20
    g.ma_long = 60
    g.cross_days_ago = 2
    g.volume_analysis_period = 90

    g.stop_loss_pct = 0.07
    g.trailing_stop_pct = 0.08

    g.min_listing_days = 120
    g.min_market_cap = 20
    g.max_market_cap = 500

    g.stock_pool = []
    g.price_peaks = {}

    run_weekly(update_stock_pool, weekday=1, time='9:05')
    run_daily(trade, 'every_bar')


def update_stock_pool(context):
    q = query(
        valuation.code,
        valuation.market_cap,
    ).filter(
        valuation.market_cap.between(g.min_market_cap, g.max_market_cap)
    )

    df = get_fundamentals(q)
    if df is None or df.empty:
        g.stock_pool = []
        log.info('stock pool refresh failed: empty fundamentals result')
        return

    stock_list = list(df['code'])
    stock_list = filter_board_stock(stock_list)
    stock_list = filter_basic_stock(context, stock_list)
    stock_list = filter_paused_stock(stock_list)

    g.stock_pool = stock_list
    log.info('stock pool refreshed, total candidates: %d', len(g.stock_pool))


def trade(context):
    if not g.stock_pool:
        update_stock_pool(context)

    sell_list = []
    for stock in list(context.portfolio.positions.keys()):
        reason = should_sell(context, stock)
        if reason:
            sell_list.append(stock)
            order_target_value(stock, 0)
            log.info('sell signal: %s, reason=%s', stock, reason)

    current_count = len(context.portfolio.positions) - len(sell_list)
    available_slots = g.stocknum - current_count
    if available_slots <= 0:
        return

    buy_candidates = check_stocks(context)
    if not buy_candidates:
        return

    held_stocks = set(context.portfolio.positions.keys())
    blocked_stocks = held_stocks.union(set(sell_list))
    target_value = context.portfolio.total_value / float(g.stocknum)

    for item in buy_candidates:
        stock = item['code']
        if stock in blocked_stocks:
            continue
        if available_slots <= 0:
            break

        order_value(stock, target_value)
        log.info(
            'buy signal: %s, score=%.4f, box_high=%.2f, close=%.2f, '
            'vol_ratio=%.2f, vol_trend=%.2f',
            stock,
            item['score'],
            item['box_high'],
            item['close'],
            item['volume_ratio_90'],
            item['volume_trend_90'],
        )
        available_slots -= 1


def check_stocks(context):
    candidates = []
    for stock in g.stock_pool:
        signal = get_combined_signal(stock)
        if signal is not None:
            candidates.append(signal)

    candidates.sort(key=lambda item: item['score'], reverse=True)
    log.info('box + ma golden cross count: %d', len(candidates))
    return candidates


def get_combined_signal(stock):
    bar_count = max(
        g.volume_analysis_period,
        g.ma_long + g.cross_days_ago + 2,
        g.box_window + 1,
    )
    history_data = attribute_history(
        stock,
        bar_count,
        '1d',
        ['close', 'high', 'low', 'volume'],
        skip_paused=True,
    )

    if history_data is None or len(history_data) < g.ma_long + g.cross_days_ago + 2:
        return None

    ma_data = add_moving_averages(history_data)

    if not is_box_breakout(ma_data):
        return None
    if not detect_ma5_cross_ma10(ma_data, g.cross_days_ago):
        return None
    if not check_bullish_alignment(ma_data):
        return None

    volume_analysis = analyze_volume(ma_data, g.volume_analysis_period)
    latest = ma_data.iloc[-1]
    box_high = get_box_high(ma_data)
    breakout_pct = (float(latest['close']) - box_high) / box_high
    score = breakout_pct * 100 + volume_analysis['volume_ratio'] + volume_analysis['volume_trend']

    return {
        'code': stock,
        'close': float(latest['close']),
        'box_high': float(box_high),
        'score': float(score),
        'volume_ratio_90': float(volume_analysis['volume_ratio']),
        'volume_trend_90': float(volume_analysis['volume_trend']),
    }


def add_moving_averages(df):
    df = df.copy()
    close = df['close']
    df['ma5'] = close.rolling(g.ma_short).mean()
    df['ma10'] = close.rolling(g.ma_mid).mean()
    df['ma20'] = close.rolling(g.ma_20).mean()
    df['ma60'] = close.rolling(g.ma_long).mean()
    return df


def is_box_breakout(df):
    if len(df) < g.box_window + 1:
        return False

    signal_bar = df.iloc[-1]
    box_data = df.iloc[-g.box_window - 1:-1]
    box_high = box_data['high'].max()
    box_low = box_data['low'].min()
    if box_low <= 0 or box_high <= 0:
        return False

    box_range = (box_high - box_low) / box_low
    if box_range > g.max_box_range:
        return False

    volume_ma = box_data['volume'].tail(g.volume_window).mean()
    if volume_ma <= 0:
        return False

    close_price = float(signal_bar['close'])
    volume = float(signal_bar['volume'])

    if close_price <= box_high:
        return False
    if volume < volume_ma * g.volume_ratio:
        return False

    return True


def get_box_high(df):
    box_data = df.iloc[-g.box_window - 1:-1]
    return float(box_data['high'].max())


def detect_ma5_cross_ma10(df, days_ago=2):
    if len(df) < days_ago + 2:
        return False

    cross_day_idx = -days_ago - 1
    prev_day_idx = cross_day_idx - 1

    ma5_prev = df['ma5'].iloc[prev_day_idx]
    ma10_prev = df['ma10'].iloc[prev_day_idx]
    ma5_cross = df['ma5'].iloc[cross_day_idx]
    ma10_cross = df['ma10'].iloc[cross_day_idx]

    if has_nan(ma5_prev, ma10_prev, ma5_cross, ma10_cross):
        return False

    return ma5_prev <= ma10_prev and ma5_cross > ma10_cross


def check_bullish_alignment(df):
    if len(df) < 1:
        return False

    latest = df.iloc[-1]
    ma5 = latest['ma5']
    ma10 = latest['ma10']
    ma20 = latest['ma20']
    ma60 = latest['ma60']

    if has_nan(ma5, ma10, ma20, ma60):
        return False

    return ma5 > ma10 > ma20 > ma60


def analyze_volume(df, period=90):
    if len(df) < period:
        recent_df = df
    else:
        recent_df = df.tail(period)

    avg_volume = recent_df['volume'].mean()
    latest_volume = df['volume'].iloc[-1]
    max_volume = recent_df['volume'].max()
    min_volume = recent_df['volume'].min()
    volume_ratio = latest_volume / avg_volume if avg_volume > 0 else 0
    recent_5d_avg = df['volume'].tail(5).mean()
    volume_trend = recent_5d_avg / avg_volume if avg_volume > 0 else 0

    return {
        'avg_volume': avg_volume,
        'latest_volume': latest_volume,
        'volume_ratio': volume_ratio,
        'volume_trend': volume_trend,
        'max_volume': max_volume,
        'min_volume': min_volume,
    }


def should_sell(context, stock):
    position = context.portfolio.positions.get(stock)
    if position is None:
        return None

    history_data = attribute_history(
        stock,
        g.ma_long + 2,
        '1d',
        ['close'],
        skip_paused=False,
    )
    if history_data is None or len(history_data) < g.ma_long + 2:
        return None

    ma_data = add_moving_averages(history_data)
    close_series = ma_data['close']
    last_close = float(close_series.iloc[-1])

    peak_price = g.price_peaks.get(stock, position.avg_cost)
    peak_price = max(peak_price, last_close)
    g.price_peaks[stock] = peak_price

    if last_close <= position.avg_cost * (1 - g.stop_loss_pct):
        g.price_peaks.pop(stock, None)
        return 'stop_loss'

    if peak_price > 0 and last_close <= peak_price * (1 - g.trailing_stop_pct):
        g.price_peaks.pop(stock, None)
        return 'trailing_stop'

    ma5_prev = ma_data['ma5'].iloc[-2]
    ma10_prev = ma_data['ma10'].iloc[-2]
    ma5_now = ma_data['ma5'].iloc[-1]
    ma10_now = ma_data['ma10'].iloc[-1]
    if not has_nan(ma5_prev, ma10_prev, ma5_now, ma10_now):
        if ma5_prev >= ma10_prev and ma5_now < ma10_now:
            g.price_peaks.pop(stock, None)
            return 'ma5_death_cross_ma10'

    return None


def has_nan(*values):
    for value in values:
        if value != value:
            return True
    return False


def filter_basic_stock(context, stock_list):
    current_date = None
    if context.current_dt is not None:
        current_date = context.current_dt.date()
    elif context.previous_date is not None:
        current_date = context.previous_date

    if current_date is None:
        return stock_list

    all_stocks = get_all_securities(types=['stock'], date=current_date)
    if all_stocks is None or all_stocks.empty:
        return stock_list

    filtered = []
    for stock in stock_list:
        if stock not in all_stocks.index:
            continue

        row = all_stocks.loc[stock]
        start_date = row['start_date']
        if (current_date - start_date).days < g.min_listing_days:
            continue

        stock_name = ''
        if 'display_name' in all_stocks.columns:
            stock_name = str(row['display_name'])
        elif 'name' in all_stocks.columns:
            stock_name = str(row['name'])

        upper_name = stock_name.upper()
        if 'ST' in upper_name or '*' in stock_name:
            continue

        filtered.append(stock)

    return filtered


def filter_board_stock(stock_list):
    filtered = []
    for stock in stock_list:
        if stock.startswith('688'):
            continue
        if stock.startswith('8') or stock.startswith('4'):
            continue
        filtered.append(stock)
    return filtered


def filter_paused_stock(stock_list):
    if not stock_list:
        return []

    try:
        current_data = get_current_data()
        return [stock for stock in stock_list if not current_data[stock].paused]
    except Exception as exc:
        log.warn('filter_paused_stock failed, keep original list: %s', exc)
        return stock_list
