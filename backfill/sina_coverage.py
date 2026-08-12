"""量一件事：新浪对我们这三个大商所品种的**在市合约**覆盖到什么程度。

之前记下的「新浪只覆盖 44%」是全大商所 186 个合约的口径，包含大量我们不看的品种。
两张历史表现在已经收敛到八个品种，所以真正要回答的是这个窄得多的问题。

只读，逐个合约一次请求，1 秒一发。
"""

import time

import akshare

CONTRACTS = (
    "JD2608,JD2609,JD2610,JD2611,JD2612,JD2701,JD2702,JD2703,JD2704,JD2705,"
    "JD2706,JD2707,JM2608,JM2609,JM2610,JM2612,JM2701,JM2702,JM2703,JM2704,"
    "JM2705,JM2706,JM2707,LH2609,LH2611,LH2701,LH2703,LH2705,LH2707"
).split(",")


def main() -> int:
    covered, missing, newest = [], [], {}
    for index, contract in enumerate(CONTRACTS):
        if index:
            time.sleep(1.0)
        try:
            frame = akshare.futures_zh_daily_sina(symbol=contract)
            if frame is None or frame.empty:
                missing.append(contract)
                continue
            last = frame.iloc[-1]
            covered.append(contract)
            newest[contract] = str(last["date"])
        except Exception as error:  # noqa: BLE001 - 诊断，如实报出来
            missing.append(f"{contract}({type(error).__name__})")

    print(f"在市合约 {len(CONTRACTS)} 个：覆盖 {len(covered)}，缺 {len(missing)}")
    if missing:
        print("缺：", ", ".join(missing))
    stamps = sorted(set(newest.values()))
    print("各合约最新交易日：", ", ".join(stamps))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
