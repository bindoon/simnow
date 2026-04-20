# md_demo.py
from pathlib import Path

import openctp_ctp as ctp

from env_config import get_env, load_dotenv


load_dotenv()


BROKER_ID = get_env("BROKER_ID", "9999")
USER_ID = get_env("USER_ID")
PASSWORD = get_env("PASSWORD")

INSTRUMENT_IDS = ["IF2506"]
FLOW_DIR = Path("./md_log")
# 行情服务器，挨个试
SERVERS = [
    "tcp://180.168.146.187:10131",  # 电信1
    "tcp://180.168.146.187:10132",  # 电信2  
    "tcp://218.202.237.33:10112",   # 移动
]


class MyMdSpi(ctp.mdapi.CThostFtdcMdSpi):
    def __init__(self, api, instrument_ids):
        super().__init__()
        self.api = api
        self.instrument_ids = instrument_ids

    def OnFrontConnected(self):
        print("连接成功，开始登录...")
        req = ctp.mdapi.CThostFtdcReqUserLoginField()
        req.BrokerID = BROKER_ID
        req.UserID = USER_ID
        req.Password = PASSWORD
        ret = self.api.ReqUserLogin(req, 1)
        print(f"ReqUserLogin 返回: {ret}")

    def OnFrontDisconnected(self, reason):
        print(f"连接断开，原因码: {reason:#x}")

    def OnRspError(self, pRspInfo, nRequestID, bIsLast):
        if pRspInfo and pRspInfo.ErrorID != 0:
            print(f"错误应答 [{nRequestID}]: {pRspInfo.ErrorID} {pRspInfo.ErrorMsg}")

    def OnRspUserLogin(self, pRspUserLogin, pRspInfo, nRequestID, bIsLast):
        if pRspInfo and pRspInfo.ErrorID != 0:
            print(f"登录失败: {pRspInfo.ErrorID} {pRspInfo.ErrorMsg}")
            return

        trading_day = self.api.GetTradingDay()
        print(f"登录成功，交易日: {trading_day}，开始订阅行情...")
        ret = self.api.SubscribeMarketData(self.instrument_ids, len(self.instrument_ids))
        print(f"SubscribeMarketData 返回: {ret}")

    def OnRspSubMarketData(self, pSpecificInstrument, pRspInfo, nRequestID, bIsLast):
        if pRspInfo and pRspInfo.ErrorID != 0:
            print(f"订阅失败: {pRspInfo.ErrorID} {pRspInfo.ErrorMsg}")
            return
        if pSpecificInstrument:
            print(f"订阅成功: {pSpecificInstrument.InstrumentID}")

    def OnRtnDepthMarketData(self, data):
        print(
            f"[{data.InstrumentID}] "
            f"最新价: {data.LastPrice} | "
            f"买一: {data.BidPrice1} | "
            f"卖一: {data.AskPrice1}"
        )

    def OnFrontDisconnected(self, nReason):
        reasons = {
            0x1001: "网络读失败（服务器未开放或网络不通）",
            0x1002: "网络写失败",
            0x2001: "心跳超时",
            0x2002: "收到错误报文",
        }
        print(f"❌ 断开连接: {reasons.get(nReason, '未知')} (0x{nReason:04x})")

def main():
    FLOW_DIR.mkdir(exist_ok=True)

    api = ctp.mdapi.CThostFtdcMdApi.CreateFtdcMdApi(str(FLOW_DIR))
    spi = MyMdSpi(api, INSTRUMENT_IDS)

    api.RegisterSpi(spi)
    for server in SERVERS:
        api.RegisterFront(server)

    try:
        api.Init()
        api.Join()
    except KeyboardInterrupt:
        print("收到中断，准备退出...")
    finally:
        api.Release()


if __name__ == "__main__":
    main()
