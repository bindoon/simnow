"""
Box breakout strategy for JoinQuant-compatible environments.

Signal design
-------------
1. Build a broad but tradable stock pool from fundamentals.
2. Detect 20-day box breakouts using yesterday's close and volume.
3. Buy the strongest breakouts at today's open.
4. Sell on fixed stop loss, trailing stop, or MA5 below MA10.

All signals use completed daily bars only, so this strategy can run on
JoinQuant and on the local simulator without look-ahead bias.
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
    g.ma_long = 10
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

    breakout_list = check_stocks(context)
    if not breakout_list:
        return

    held_stocks = set(context.portfolio.positions.keys())
    blocked_stocks = held_stocks.union(set(sell_list))
    target_value = context.portfolio.total_value / float(g.stocknum)

    for item in breakout_list:
        stock = item['code']
        if stock in blocked_stocks:
            continue
        if available_slots <= 0:
            break
        order_value(stock, target_value)
        log.info(
            'buy signal: %s, score=%.4f, box_high=%.2f, close=%.2f',
            stock,
            item['score'],
            item['box_high'],
            item['close'],
        )
        available_slots -= 1


def check_stocks(context):
    breakout_list = []
    for stock in g.stock_pool:
        signal = get_breakout_signal(stock)
        if signal is not None:
            breakout_list.append(signal)

    breakout_list.sort(key=lambda item: item['score'], reverse=True)
    log.info('box breakout count: %d', len(breakout_list))
    return breakout_list


def get_breakout_signal(stock):
    bar_count = g.box_window + 1
    history_data = attribute_history(
        stock,
        bar_count,
        '1d',
        ['close', 'high', 'low', 'volume'],
        skip_paused=True,
    )

    if history_data is None or len(history_data) < bar_count:
        return None

    signal_bar = history_data.iloc[-1]
    box_data = history_data.iloc[:-1]

    box_high = box_data['high'].max()
    box_low = box_data['low'].min()
    if box_low <= 0:
        return None

    box_range = (box_high - box_low) / box_low
    if box_range > g.max_box_range:
        return None

    volume_ma = box_data['volume'].tail(g.volume_window).mean()
    if volume_ma is None or volume_ma <= 0:
        return None

    close_price = float(signal_bar['close'])
    volume = float(signal_bar['volume'])

    if close_price <= box_high:
        return None
    if volume < volume_ma * g.volume_ratio:
        return None

    breakout_pct = (close_price - box_high) / box_high
    volume_ratio = volume / volume_ma
    score = breakout_pct * 100 + volume_ratio

    return {
        'code': stock,
        'close': close_price,
        'box_high': float(box_high),
        'score': float(score),
    }


def should_sell(context, stock):
    position = context.portfolio.positions.get(stock)
    if position is None:
        return None

    history_data = attribute_history(
        stock,
        g.ma_long,
        '1d',
        ['close'],
        skip_paused=False,
    )
    if history_data is None or len(history_data) < g.ma_long:
        return None

    close_series = history_data['close']
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

    ma_short = close_series.tail(g.ma_short).mean()
    ma_long = close_series.tail(g.ma_long).mean()
    if ma_short < ma_long:
        g.price_peaks.pop(stock, None)
        return 'ma5_below_ma10'

    return None


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