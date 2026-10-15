#!/usr/bin/env python3
"""离线自测：不接任何 iOS 设备，验证 Mock/Real 两个 provider 的契约与中文降级逻辑。

用法：

    ./.venv/bin/python tools/selftest_location.py

覆盖：
1. `core.location_mock` / `core.location_provider` 可 import（即使未安装 pymobiledevice3）。
2. MockLocationProvider 五个接口 + 5~10ms 延时 + 事件循环内运行。
3. RealLocationProvider 的降级：未连接下发 / 未配对 / 未开开发者模式 / iOS17+ 无隧道 /
   probe() 永不抛异常 / close() 幂等 —— 全部应为**中文可读信息**，不得是 AttributeError。
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.location_mock import MockLocationProvider  # noqa: E402
from core.location_provider import (  # noqa: E402
    LocationProviderError,
    RealLocationProvider,
    pymobiledevice3_available,
    pymobiledevice3_version,
)

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  [通过] {name}" + (f" —— {detail}" if detail else ""))
    else:
        FAILED.append(f"{name}: {detail}")
        print(f"  [失败] {name} —— {detail}")


def has_chinese(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


async def test_mock() -> None:
    print("\n== 1. MockLocationProvider ==")
    provider = MockLocationProvider()
    check("初始 connected=False", provider.connected is False)

    info = await provider.connect({"udid": "MOCK-0001", "ios_version": "26.0", "ios_major": 26})
    check("connect 返回 dict 且 ok=True", isinstance(info, dict) and info.get("ok") is True, str(info.get("detail")))
    check("connect 后 connected=True", provider.connected is True)
    check("connect 返回中文说明", has_chinese(str(info.get("detail"))))

    started = time.perf_counter()
    await provider.set_location(39.908, 116.397)
    elapsed_ms = (time.perf_counter() - started) * 1000
    check("set_location 延时 5~10ms", 4.0 <= elapsed_ms <= 60.0, f"实测 {elapsed_ms:.2f}ms")
    check("send_count 递增", provider.send_count == 1)
    check("last_location 记录正确", provider.last_location == (39.908, 116.397))

    await provider.clear_location()
    check("clear_location 后 last_location=None", provider.last_location is None)
    await provider.close()
    check("close 后 connected=False", provider.connected is False)
    await provider.close()  # 幂等
    check("close 幂等", provider.connected is False)

    probe = await provider.probe()
    check("mock probe ok=True", probe.get("ok") is True and probe.get("ios_major") == 26, str(probe))


async def test_real_degradation() -> None:
    print("\n== 2. RealLocationProvider 降级逻辑 ==")
    installed = pymobiledevice3_available()
    print(f"  pymobiledevice3 已安装：{installed}（版本 {pymobiledevice3_version()}）")

    provider = RealLocationProvider()
    check("初始 connected=False", provider.connected is False)

    # 2.1 未连接就下发
    try:
        await provider.set_location(39.9, 116.4)
        check("未连接下发应抛异常", False, "没有抛异常")
    except LocationProviderError as exc:
        check("未连接下发抛 LocationProviderError", True, str(exc))
        check("未连接下发错误为中文", has_chinese(str(exc)), str(exc))
    except Exception as exc:  # noqa: BLE001
        check("未连接下发抛 LocationProviderError", False, f"{type(exc).__name__}: {exc}")

    # 2.2 未连接 clear 应为 no-op（不抛）
    try:
        await provider.clear_location()
        check("未连接 clear_location 为 no-op", True)
    except Exception as exc:  # noqa: BLE001
        check("未连接 clear_location 为 no-op", False, f"{type(exc).__name__}: {exc}")

    # 2.3 未配对
    try:
        await RealLocationProvider().connect({"udid": "X", "state": "unpaired", "ios_major": 16})
        check("未配对设备应拒绝连接", False, "没有抛异常")
    except LocationProviderError as exc:
        check("未配对设备报中文错误", has_chinese(str(exc)) and "信任" in str(exc), str(exc))
    except Exception as exc:  # noqa: BLE001
        check("未配对设备报中文错误", False, f"{type(exc).__name__}: {exc}")

    # 2.4 未开开发者模式
    try:
        await RealLocationProvider().connect({"udid": "X", "developer_mode": False, "ios_major": 16})
        check("未开开发者模式应拒绝连接", False, "没有抛异常")
    except LocationProviderError as exc:
        check("未开开发者模式报中文错误", has_chinese(str(exc)) and "开发者模式" in str(exc), str(exc))
    except Exception as exc:  # noqa: BLE001
        check("未开开发者模式报中文错误", False, f"{type(exc).__name__}: {exc}")

    # 2.5 iOS 17+ 没有隧道
    try:
        await RealLocationProvider().connect(
            {"udid": "X", "ios_version": "26.0", "ios_major": 26, "developer_mode": True, "state": "connected"}
        )
        check("iOS17+ 无隧道应拒绝连接", False, "没有抛异常")
    except LocationProviderError as exc:
        text = str(exc)
        if installed:
            check("iOS17+ 无隧道提示建隧道", has_chinese(text) and ("隧道" in text or "run.sh" in text), text)
        else:
            check("未装 pymobiledevice3 时提示安装", has_chinese(text) and "pymobiledevice3" in text, text)
    except Exception as exc:  # noqa: BLE001
        check("iOS17+ 无隧道提示建隧道", False, f"{type(exc).__name__}: {exc}")

    # 2.6 经纬度越界
    provider_range = RealLocationProvider()
    provider_range._connected = True  # 仅用于测试参数校验分支
    try:
        await provider_range.set_location(123.0, 116.0)
        check("越界纬度应拒绝", False, "没有抛异常")
    except LocationProviderError as exc:
        check("越界纬度报中文错误", has_chinese(str(exc)), str(exc))
    except Exception as exc:  # noqa: BLE001
        check("越界纬度报中文错误", False, f"{type(exc).__name__}: {exc}")

    # 2.7 probe 永不抛异常
    probe = await RealLocationProvider(rsd_host=None, rsd_port=None).probe()
    check(
        "probe 返回 ok/detail/ios_major",
        isinstance(probe, dict) and set(probe) >= {"ok", "detail", "ios_major"},
        str(probe),
    )
    check("probe detail 为中文", has_chinese(str(probe.get("detail"))), str(probe.get("detail")))

    probe_tunnel = await RealLocationProvider(rsd_host="127.0.0.1", rsd_port=1).probe()
    check("probe(隧道不可达) 不抛异常且 ok=False", probe_tunnel.get("ok") is False, str(probe_tunnel.get("detail")))
    check("probe(隧道不可达) 文案为中文", has_chinese(str(probe_tunnel.get("detail"))))

    # 2.7b probe(device)：带设备快照时做静态判断并回填 ios_major
    probe_dev = await RealLocationProvider().probe({"ios_major": 26, "developer_mode": False, "udid": "X"})
    check(
        "probe(device) 未开开发者模式 → ok=False 且中文",
        probe_dev.get("ok") is False and "开发者模式" in str(probe_dev.get("detail")),
        str(probe_dev.get("detail")),
    )
    check("probe(device) 回填 ios_major=26", probe_dev.get("ios_major") == 26, str(probe_dev.get("ios_major")))
    probe_dev17 = await RealLocationProvider().probe({"ios_major": 26, "developer_mode": True, "udid": "X"})
    check(
        "probe(device) iOS17+ 无隧道 → 提示建隧道",
        probe_dev17.get("ok") is False and has_chinese(str(probe_dev17.get("detail"))),
        str(probe_dev17.get("detail"))[:80],
    )

    # 2.8 close 幂等
    fresh = RealLocationProvider()
    await fresh.close()
    await fresh.close()
    check("close 幂等且 connected=False", fresh.connected is False)

    # 2.9 真实 RSD 地址不可达时的中文错误
    unreachable = RealLocationProvider(rsd_host="127.0.0.1", rsd_port=1, connect_timeout=3.0)
    if installed:
        try:
            await unreachable.connect({"udid": "X", "ios_major": 26, "developer_mode": True})
            check("不可达隧道应报错", False, "没有抛异常")
        except LocationProviderError as exc:
            check("不可达隧道报中文错误", has_chinese(str(exc)), str(exc)[:120])
        except Exception as exc:  # noqa: BLE001
            check("不可达隧道报中文错误", False, f"{type(exc).__name__}: {exc}")
        finally:
            await unreachable.close()


class _MiniRunner:
    """``core/server.py`` 的 ``AsyncRunner`` 最小替身（后台线程 + 独立事件循环）。"""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="selftest-runner", daemon=True)
        self._thread.start()
        self._ready.wait(5.0)

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()

    def run(self, coro, timeout: float = 30.0, wait: bool = True):
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout) if wait else future

    def shutdown(self) -> None:
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=3.0)
        except Exception:
            pass


def test_lead_integration() -> None:
    """照抄 core/server.py 的真实用法：关键字 log=、connect_sync(runner)、runner.run(coro)。"""
    print("\n== 3. 与 lead 的 core/server.py 集成 ==")
    runner = _MiniRunner()
    logs: list[tuple[str, str]] = []
    try:
        mock = MockLocationProvider(log=lambda level, msg: logs.append((level, msg)))
        info = mock.connect_sync(runner)
        check("MockLocationProvider(log=...) 构造成功", isinstance(info, dict) and info.get("ok") is True)
        check("connect_sync(runner) 建立会话", mock.connected is True)
        check("log= 回调收到日志", len(logs) > 0, f"{len(logs)} 条")
        runner.run(mock.set_location(39.908, 116.397))
        check("set_location(lat, lon) 经 runner 调用成功", mock.send_count == 1)
        runner.run(mock.clear_location())
        runner.run(mock.close())
        check("close 后 connected=False", mock.connected is False)

        real = RealLocationProvider(rsd_host=None, rsd_port=None, udid="X", log=lambda level, msg: None)
        check("RealLocationProvider(log=...) 构造成功", real.connected is False)
        detail = runner.run(real.probe({"ios_major": 26, "developer_mode": False}))
        check("probe(device) 经 runner 调用不抛异常", isinstance(detail, dict) and detail.get("ok") is False)
        try:
            real.connect_sync(runner, {"udid": "X", "ios_major": 26, "developer_mode": True})
            check("connect_sync 无隧道时抛中文异常", False, "没有抛异常")
        except LocationProviderError as exc:
            check("connect_sync 无隧道时抛中文异常", has_chinese(str(exc)), str(exc)[:60])
        runner.run(real.close())
    finally:
        runner.shutdown()


async def main_async() -> int:
    print("=" * 68)
    print("虚拟定位后端离线自测（无需 iOS 设备）")
    print(f"Python {sys.version.split()[0]} | cwd={os.getcwd()}")
    print("=" * 68)
    await test_mock()
    await test_real_degradation()
    test_lead_integration()

    print("\n" + "=" * 68)
    print(f"通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    if FAILED:
        for item in FAILED:
            print(f"  - {item}")
        return 1
    print("全部通过 ✅")
    return 0


def main() -> int:
    try:
        return asyncio.run(main_async())
    except KeyboardInterrupt:
        return 130
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    sys.exit(main())
