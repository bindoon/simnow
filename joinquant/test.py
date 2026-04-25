import jqdatasdk as jq
import datetime

QUERY_DATE = datetime.date(2025, 12, 29)

# 1. 认证登录（需替换为你聚宽官网绑定的手机号和登录密码）
# jq.auth('手机号', '登录密码')
jq.auth('15658059081', 'Qianchen_0') 

def get_my_stock_pool():
    print("正在从聚宽服务器获取数据...")
    
    # 查询全市场市值在 20亿 到 30亿 之间的股票（与原始策略一致）
    q = jq.query(
        jq.valuation.code,
        jq.valuation.market_cap
    ).filter(
        jq.valuation.market_cap.between(20, 30)
    ).order_by(
        jq.valuation.market_cap.asc()
    ).limit(5)
    
    df = jq.get_fundamentals(q, date=QUERY_DATE)
    
    return df

if __name__ == "__main__":
    # 执行选股逻辑
    df_result = get_my_stock_pool()
    
    print("\n--- 今日备选小市值股票 ---")
    print(f"数据日期: {QUERY_DATE}")
    print(df_result)
    
    # 4. 获取某只股票的最新日K线（比如第一只选出的股票）
    if not df_result.empty:
        target_code = df_result.iloc[0]['code']
        # 获取最近5天的日线数据
        hist = jq.get_bars(
            target_code,
            count=5,
            unit='1d',
            fields=['date', 'open', 'close', 'high', 'low', 'volume'],
            end_dt=QUERY_DATE,
        )
        print(f"\n--- {target_code} 最近5日行情 ---")
        print(hist)

    # 5. 查询剩余流量（聚宽免费版每天有额度限制）
    count = jq.get_query_count()
    print(f"\n今日剩余流量: {count['spare']} 条")