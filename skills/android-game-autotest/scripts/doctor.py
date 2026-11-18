#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
doctor.py — 跑测前置体检

一条命令回答：「这台机器 + 这台手机，现在能不能开跑？」
每个检查项给出 OK / WARN / FAIL 与可执行建议，FAIL 时退出码非 0。

用法：
    python3 scripts/doctor.py --pkg com.example.game   # 检查目标包是否已安装
    python3 scripts/doctor.py                          # 不查包，只体检链路
    python3 scripts/doctor.py --json                   # 输出 JSON（给 agent 解析）
    python3 scripts/doctor.py --serial <序列号>

设计原则（每条都来自实测踩坑）：
  - 截图耗时是链路健康度最敏感的指标，偶发 60s 卡死是 USB 传输层故障，
    所以这里做 N 次采样看 max，而不是只测一次。
  - shell 非 0 退出码不代表设备故障（grep 无匹配就是 1），体检里显式区分。
  - 游戏画面在 uiautomator 里 0 节点是**正常现象**（Unity SurfaceView），
    不能当失败；这里只验证 uiautomator 本身可用。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from adbkit import AdbError, Device, DeviceGone, find_adb, list_devices  # noqa: E402

OK, WARN, FAIL = "OK", "WARN", "FAIL"
_ICON = {OK: "✅", WARN: "⚠️ ", FAIL: "❌"}


class Report:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def add(self, name: str, status: str, detail: str, hint: str = "") -> None:
        self.items.append({"name": name, "status": status, "detail": detail, "hint": hint})

    def has_fail(self) -> bool:
        return any(i["status"] == FAIL for i in self.items)

    def render(self) -> str:
        lines = []
        width = max(len(i["name"]) for i in self.items) if self.items else 10
        for i in self.items:
            lines.append(f"{_ICON[i['status']]} {i['name']:<{width}}  {i['detail']}")
            if i["hint"] and i["status"] != OK:
                lines.append(f"   {'':<{width}}  ↳ {i['hint']}")
        n_fail = sum(1 for i in self.items if i["status"] == FAIL)
        n_warn = sum(1 for i in self.items if i["status"] == WARN)
        lines.append("")
        if n_fail:
            lines.append(f"结论：{n_fail} 项失败、{n_warn} 项警告 —— 先按提示修，别急着跑。")
        elif n_warn:
            lines.append(f"结论：可以跑，但有 {n_warn} 项警告。")
        else:
            lines.append("结论：全部通过，可以开跑。")
        return "\n".join(lines)


def check_adb(rep: Report) -> str | None:
    try:
        p = find_adb()
        rep.add("adb 二进制", OK, p)
        return p
    except AdbError as e:
        rep.add("adb 二进制", FAIL, str(e),
                "装 Android platform-tools，或设环境变量 ADB=/path/to/adb")
        return None


def check_devices(rep: Report, want_serial: str | None) -> str | None:
    try:
        devs = list_devices()
    except AdbError as e:
        rep.add("设备列表", FAIL, str(e), "adb start-server 后重试")
        return None
    if not devs:
        rep.add("设备列表", FAIL, "没有设备",
                "插 USB 并确认手机已开「USB 调试」；若已插好仍为空，"
                "看 `log show --predicate 'process == \"kernel\"'` 是否有 USB 枚举记录")
        return None
    online = [d for d in devs if d.online]
    if not online:
        rep.add("设备列表", FAIL, f"{len(devs)} 个设备但都不在线",
                "手机上点「允许 USB 调试」授权弹窗")
        return None
    detail = ", ".join(f"{d.serial}({d.model})" for d in online)
    rep.add("设备列表", OK, detail)
    if want_serial:
        if any(d.serial == want_serial for d in online):
            return want_serial
        rep.add("指定序列号", FAIL, f"{want_serial} 不在线", "换 --serial 或插回那台机器")
        return None
    if len(online) > 1:
        rep.add("设备选择", WARN, f"{len(online)} 台在线，默认取第一台 {online[0].serial}",
                "多设备时用 --serial 明确指定，避免跑错机器")
    return online[0].serial


def check_basic(rep: Report, d: Device) -> None:
    try:
        t0 = time.time()
        ok = d.shell("echo ok").strip()
        dt = time.time() - t0
        rep.add("shell 往返", OK if ok == "ok" else FAIL, f"{dt * 1000:.0f}ms")
    except AdbError as e:
        rep.add("shell 往返", FAIL, str(e), "adb reconnect 后重试")
        return

    try:
        w, h = d.size
        rep.add("屏幕尺寸", OK, f"{w}x{h} @ {d.density}dpi")
    except AdbError as e:
        rep.add("屏幕尺寸", WARN, f"读不到：{e}")


def check_screen(rep: Report, d: Device) -> None:
    """屏幕必须是亮的且已解锁——否则点击全部无效。"""
    try:
        wake = d.shell("dumpsys power | grep -m1 'mWakefulness='").strip()
        awake = "Awake" in wake
        rep.add("屏幕唤醒", OK if awake else WARN, wake or "(读不到)",
                "input keyevent KEYCODE_WAKEUP 唤醒")
    except AdbError as e:
        rep.add("屏幕唤醒", WARN, str(e))

    try:
        locked = d.is_locked()
        rep.add("锁屏状态", WARN if locked else OK,
                "已锁屏" if locked else "未锁屏",
                "d.unlock() 上滑解锁；有 PIN 时传 pin")
    except AdbError as e:
        rep.add("锁屏状态", WARN, str(e))

    try:
        stay = d.shell("settings get global stay_on_while_plugged_in").strip()
        timeout = d.shell("settings get system screen_off_timeout").strip()
        good = stay not in ("", "0", "null")
        rep.add("常亮设置", OK if good else WARN,
                f"stay_on_while_plugged_in={stay}, screen_off_timeout={timeout}",
                "长跑测必须常亮：adb shell settings put global stay_on_while_plugged_in 2")
    except AdbError as e:
        rep.add("常亮设置", WARN, str(e))


def check_battery(rep: Report, d: Device) -> None:
    try:
        out = d.shell("dumpsys battery")
        level = temp = None
        powered = False
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("level:"):
                level = int(line.split(":")[1])
            elif line.startswith("temperature:"):
                temp = int(line.split(":")[1]) / 10.0
            elif line.startswith("AC powered:") or line.startswith("USB powered:"):
                powered = powered or "true" in line
        detail = f"电量 {level}%"
        if temp is not None:
            detail += f"，温度 {temp:.1f}°C"
        detail += "，已供电" if powered else "，未供电"
        if level is not None and level < 20:
            rep.add("电量", WARN, detail, "插上电源再跑，长跑测会耗电")
        elif temp is not None and temp > 42:
            rep.add("电量", WARN, detail, "机身偏热，可能降频影响时序判定")
        else:
            rep.add("电量", OK, detail)
    except (AdbError, ValueError) as e:
        rep.add("电量", WARN, f"读不到：{e}")


def check_screencap(rep: Report, d: Device, samples: int = 5) -> None:
    """
    截图是整条链路最脆弱的环节（实测偶发 60s 卡死 = USB 传输层故障）。
    采 N 次看 max，不只看单次。
    """
    times: list[float] = []
    sizes: set[int] = set()
    errs = 0
    for _ in range(samples):
        try:
            t0 = time.time()
            raw = d.screencap_raw()
            times.append(time.time() - t0)
            sizes.add(len(raw))
        except AdbError:
            errs += 1
    if not times:
        rep.add("截图链路", FAIL, f"{samples} 次全部失败",
                "USB 传输层异常；adb reconnect 或换直连 USB 口")
        return
    med = statistics.median(times)
    mx = max(times)
    detail = f"{len(times)}/{samples} 成功，中位 {med * 1000:.0f}ms，最慢 {mx * 1000:.0f}ms"
    if len(sizes) > 1:
        rep.add("截图链路", WARN, detail + f"，尺寸不一致 {sorted(sizes)}",
                "画面尺寸变化通常是旋转/分屏，确认方向锁定")
    elif mx > 3.0:
        rep.add("截图链路", WARN, detail,
                "最慢一次超过 3s，USB 链路不稳（走 Hub 时常见）；建议直连")
    else:
        rep.add("截图链路", OK, detail)


def check_uiautomator(rep: Report, d: Device) -> None:
    try:
        t0 = time.time()
        nodes = d.ui_nodes()
        dt = time.time() - t0
        detail = f"{len(nodes)} 个节点，{dt:.1f}s"
        if len(nodes) <= 2:
            detail += "（Unity 画面不可观测，属正常；原生弹窗仍可见）"
        rep.add("uiautomator", OK, detail)
    except AdbError as e:
        rep.add("uiautomator", WARN, f"不可用：{e}",
                "部分机型/权限下 uiautomator 会失败；游戏跑测不依赖它也能跑")


def check_activity(rep: Report, d: Device) -> str | None:
    try:
        act = d.current_activity()
        if act:
            rep.add("前台 Activity", OK, act)
        else:
            rep.add("前台 Activity", WARN, "(空)",
                    "息屏或锁屏中；流程开头的 wake + unlock 步骤会处理，"
                    "有 PIN 需在流程里传 pin")
        return act
    except AdbError as e:
        rep.add("前台 Activity", WARN, str(e))
        return None


def check_pkg(rep: Report, d: Device, pkg: str | None) -> None:
    if not pkg:
        return
    try:
        installed = d.is_installed(pkg)
        rep.add("目标应用", OK if installed else FAIL,
                f"{pkg} {'已安装' if installed else '未安装'}",
                "adb install -r -t <apk>；45MB 包经 USB 2.0 Hub 约 1.5 分钟")
    except AdbError as e:
        rep.add("目标应用", WARN, str(e))


def check_logcat(rep: Report, d: Device) -> None:
    try:
        t0 = time.time()
        lines = d.logcat_dump(50)
        dt = time.time() - t0
        rep.add("logcat", OK, f"{len(lines)} 行，{dt:.2f}s")
    except AdbError as e:
        rep.add("logcat", FAIL, str(e), "日志是判定崩溃的唯一可靠来源，必须可用")


def check_clock(rep: Report, d: Device) -> None:
    """设备与主机时钟偏差过大会让 logcat 时间窗口判定错位。"""
    try:
        dev = int(d.shell("date +%s").strip())
        drift = abs(dev - int(time.time()))
        if drift > 120:
            rep.add("时钟偏差", WARN, f"{drift}s",
                    "设备时间与主机差太多；logcat_since 会取不到正确窗口")
        else:
            rep.add("时钟偏差", OK, f"{drift}s")
    except (AdbError, ValueError) as e:
        rep.add("时钟偏差", WARN, f"读不到：{e}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="跑测前置体检")
    ap.add_argument("--pkg", default=None, help="要检查是否安装的包名（不传则跳过这项）")
    ap.add_argument("--serial", default=None)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--samples", type=int, default=5, help="截图采样次数")
    args = ap.parse_args(argv)

    rep = Report()
    check_adb(rep)
    serial = check_devices(rep, args.serial)
    if serial:
        try:
            d = Device(serial)
            check_basic(rep, d)
            check_screen(rep, d)
            check_battery(rep, d)
            check_screencap(rep, d, samples=max(1, args.samples))
            check_uiautomator(rep, d)
            check_activity(rep, d)
            check_pkg(rep, d, args.pkg or None)
            check_logcat(rep, d)
            check_clock(rep, d)
        except DeviceGone as e:
            rep.add("设备连接", FAIL, f"运行中掉线：{e}", "检查 USB 线/口，重新插拔")
        except AdbError as e:
            rep.add("设备连接", FAIL, str(e))

    if args.json:
        print(json.dumps({"ok": not rep.has_fail(), "items": rep.items},
                         ensure_ascii=False, indent=2))
    else:
        print(rep.render())
    return 1 if rep.has_fail() else 0


if __name__ == "__main__":
    sys.exit(main())
