#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
adbkit.py — ADB 设备驱动层

为「DSH 经 adb 对游戏做整体流程实机跑测」提供最小但完整的设备控制与观测能力。
只依赖 Python 标准库 + adb 二进制，不引入 Appium / uiautomator2 / scrcpy 等外部组件。

设计要点（均基于 2026-11-17 在 某 Android 机型 / Android 12 上的实测）：
  1. 截图走 raw 模式（adb exec-out screencap），0.62s；PNG 模式 1.42s，慢 2.3 倍。
  2. uiautomator dump 走 --compressed 直写 /dev/tty，避免 /sdcard 往返。
  3. Unity 的 SurfaceView 在 uiautomator 里 0 节点 —— 游戏画面不可用对象树观测，
     必须靠坐标点击 + 截图判定；但原生弹窗（协议/权限/登录）对象树完全可见。
  4. 每次 adb 调用有 ~54ms 固定开销，流程编排要避免高频轮询。
"""

from __future__ import annotations

import os
import re
import shutil
import struct
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional


class AdbError(RuntimeError):
    """adb 调用失败。"""


class DeviceGone(AdbError):
    """设备掉线（长跑测必须能识别并重连）。"""


# ---------------------------------------------------------------- adb 定位

def _adb_candidates() -> tuple:
    """按优先级列出 adb 候选路径。

    顺序：环境变量 ADB → Android SDK 环境变量 → 各平台常见安装位置 → PATH。
    Windows 上自动补 .exe 后缀。
    """
    exe = ".exe" if os.name == "nt" else ""
    out = [os.environ.get("ADB")]

    # Android Studio / SDK Manager 的标准位置：优先读环境变量
    for env in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        sdk = os.environ.get(env)
        if sdk:
            out.append(os.path.join(sdk, "platform-tools", "adb" + exe))

    home = os.path.expanduser("~")
    localappdata = os.environ.get("LOCALAPPDATA")
    out += [
        os.path.join(home, "Library", "Android", "sdk", "platform-tools", "adb" + exe),
        os.path.join(home, "Android", "Sdk", "platform-tools", "adb" + exe),
        os.path.join(localappdata, "Android", "Sdk", "platform-tools", "adb.exe")
        if localappdata else None,
        "/opt/homebrew/bin/adb",
        "/usr/local/bin/adb",
        shutil.which("adb"),
    ]
    return tuple(out)


def find_adb() -> str:
    for c in _adb_candidates():
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    raise AdbError(
        "找不到 adb 可执行文件；请安装 Android platform-tools，"
        "或设置环境变量 ADB 指向 adb 的绝对路径"
    )


_ADB_CACHE: Optional[str] = None


def adb_path() -> str:
    """返回 adb 可执行文件路径（首次调用时解析并缓存）。"""
    global _ADB_CACHE
    if _ADB_CACHE is None:
        _ADB_CACHE = find_adb()
    return _ADB_CACHE


def __getattr__(name: str):
    """兼容 `adbkit.ADB` 旧写法：延迟到真正用到时才解析 adb。

    这样即使本机没装 adb，`runner.py --dry-run`（纯结构校验）也能跑。
    """
    if name == "ADB":
        return adb_path()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _run(args: list[str], timeout: float = 60.0, binary: bool = False,
         check: bool = True) -> bytes | str:
    try:
        p = subprocess.run(
            [adb_path(), *args],
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise AdbError(f"adb 超时({timeout}s): {' '.join(args)}") from None

    if p.returncode != 0:
        err = p.stderr.decode("utf-8", "replace").strip()
        # 传输层故障与命令退出码是两件事：设备真掉线必须抛 DeviceGone，
        # 而 `grep` 无匹配之类的非 0 退出码在 check=False 时属正常结果。
        if "device offline" in err or "no devices" in err or "device not found" in err:
            raise DeviceGone(err or f"设备不可用: {' '.join(args)}")
        if check:
            raise AdbError(f"adb 失败({p.returncode}): {' '.join(args)}\n{err}")

    return p.stdout if binary else p.stdout.decode("utf-8", "replace")


# 判定「可重试」的临时性错误：USB 传输卡住/抖动，不是设备真的没了
_TRANSIENT_MARKERS = (
    "超时",                    # 我们自己的 AdbError 文案
    "protocol fault",
    "connection reset",
    "broken pipe",
    "error: closed",
    "transport",
)


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, DeviceGone):
        return False
    return any(m in str(exc).lower() for m in _TRANSIENT_MARKERS)


def _run_retry(
    args: list[str],
    timeout: float = 60.0,
    binary: bool = False,
    retries: int = 3,
    backoff: float = 0.4,
    check: bool = True,
) -> bytes | str:
    """
    带重试的 _run。只对临时性故障重试（超时 / 协议抖动），
    设备真掉线（DeviceGone）立刻抛出，交给上层做重连。
    """
    last: Optional[BaseException] = None
    for attempt in range(retries + 1):
        try:
            return _run(args, timeout=timeout, binary=binary, check=check)
        except AdbError as e:
            last = e
            if not _is_transient(e) or attempt >= retries:
                raise
            time.sleep(backoff * (2 ** attempt))
    raise last  # pragma: no cover


def adb_reconnect(serial: Optional[str] = None) -> bool:
    """轻量自愈：`adb reconnect`（不断 server，只重连设备 transport）。"""
    try:
        args = ["-s", serial, "reconnect"] if serial else ["reconnect"]
        _run(args, timeout=15)
        time.sleep(0.8)
        return True
    except AdbError:
        return False


def adb_restart_server() -> bool:
    """重量自愈：kill-server + start-server。会打断所有会话，谨慎使用。"""
    try:
        _run(["kill-server"], timeout=15)
    except AdbError:
        pass
    time.sleep(0.5)
    try:
        _run(["start-server"], timeout=30)
        time.sleep(0.8)
        return True
    except AdbError:
        return False


# ---------------------------------------------------------------- 设备枚举


@dataclass
class DeviceInfo:
    serial: str
    state: str
    model: str = ""
    product: str = ""
    device: str = ""
    transport_id: str = ""

    @property
    def online(self) -> bool:
        return self.state == "device"


def list_devices() -> list[DeviceInfo]:
    out = _run(["devices", "-l"])
    devices: list[DeviceInfo] = []
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices") or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        info = DeviceInfo(serial=parts[0], state=parts[1])
        for kv in parts[2:]:
            if ":" not in kv:
                continue
            k, _, v = kv.partition(":")
            if k == "model":
                info.model = v
            elif k == "product":
                info.product = v
            elif k == "device":
                info.device = v
            elif k == "transport_id":
                info.transport_id = v
        devices.append(info)
    return devices


def pick_device(serial: Optional[str] = None) -> str:
    """选定设备序列号；未指定且只有一台在线设备时自动选择。"""
    devices = [d for d in list_devices() if d.online]
    if serial:
        for d in devices:
            if d.serial == serial:
                return serial
        raise DeviceGone(f"指定设备 {serial} 不在线；当前在线: {[d.serial for d in devices]}")
    if not devices:
        raise DeviceGone("没有在线设备")
    if len(devices) > 1:
        raise AdbError(f"多台设备在线，需显式指定 serial: {[d.serial for d in devices]}")
    return devices[0].serial


# ---------------------------------------------------------------- UI 节点


@dataclass
class UiNode:
    text: str
    desc: str
    cls: str
    package: str
    resource_id: str
    clickable: bool
    enabled: bool
    bounds: tuple[int, int, int, int]  # left, top, right, bottom
    raw: dict[str, str] = field(default_factory=dict)

    @property
    def center(self) -> tuple[int, int]:
        l, t, r, b = self.bounds
        return ((l + r) // 2, (t + b) // 2)

    @property
    def label(self) -> str:
        return self.text or self.desc

    def matches(self, needle: str, exact: bool = False) -> bool:
        for s in (self.text, self.desc):
            if not s:
                continue
            if (s == needle) if exact else (needle in s):
                return True
        return False


_BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")


def parse_ui_xml(xml_text: str) -> list[UiNode]:
    nodes: list[UiNode] = []
    # 用正则而非 ElementTree：uiautomator 的 xml 里 text 含 &#10; 等实体，
    # 且节点量大，正则足够且更宽容。
    for m in re.finditer(r"<node\b([^>]*?)/?>", xml_text):
        attrs_s = m.group(1)
        attrs: dict[str, str] = {}
        for am in re.finditer(r'([\w:-]+)="([^"]*)"', attrs_s):
            attrs[am.group(1)] = am.group(2)
        bm = _BOUNDS_RE.search(attrs.get("bounds", ""))
        if not bm:
            continue
        bounds = (int(bm.group(1)), int(bm.group(2)), int(bm.group(3)), int(bm.group(4)))
        nodes.append(
            UiNode(
                text=_unescape(attrs.get("text", "")),
                desc=_unescape(attrs.get("content-desc", "")),
                cls=attrs.get("class", ""),
                package=attrs.get("package", ""),
                resource_id=attrs.get("resource-id", ""),
                clickable=attrs.get("clickable") == "true",
                enabled=attrs.get("enabled") == "true",
                bounds=bounds,
                raw=attrs,
            )
        )
    return nodes


def _unescape(s: str) -> str:
    return (
        s.replace("&#10;", "\n")
        .replace("&#13;", "\r")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&apos;", "'")
        .replace("&amp;", "&")
    )


# ---------------------------------------------------------------- 设备句柄

# 崩溃/异常信号（logcat 侧）
CRASH_PATTERNS = (
    # —— 具体模式优先，命中后直接给可读结论 ——
    (re.compile(r"NotSupportNative2Managed"), "IL2CPP 不支持 native→managed 转换（AOT/热更不匹配）"),
    (re.compile(r"NotSupportAdjustorThunk"), "IL2CPP 不支持 adjustor thunk（AOT/热更不匹配）"),
    (re.compile(r"NotSupportManaged2Native"), "IL2CPP 不支持 managed→native 转换"),
    (re.compile(r"FATAL EXCEPTION"), "Java 崩溃"),
    (re.compile(r"ANR in "), "ANR 无响应"),
    (re.compile(r"Force finishing activity"), "系统强制结束 Activity"),
    (re.compile(r"Fatal signal \d+"), "Native 崩溃 (SIGSEGV/SIGABRT)"),
    (re.compile(r"Process .+ has died"), "进程死亡"),
    (re.compile(r"NullReferenceException"), "空引用异常"),
    (re.compile(r"IndexOutOfRangeException"), "数组越界异常"),
    (re.compile(r"MissingReferenceException"), "对象已销毁仍被访问"),
    (re.compile(r"OutOfMemoryException"), "内存溢出"),
    (re.compile(r"InvalidOperationException"), "非法操作异常"),
    (re.compile(r"KeyNotFoundException"), "字典键不存在"),
    (re.compile(r"Unhandled Exception"), "未捕获异常"),
    (re.compile(r"热更启动失败"), "热更启动失败"),
    (re.compile(r"任务执行失败"), "启动任务执行失败"),
    # —— 通用兜底放最后 ——
    (re.compile(r"E[/ ]Unity\s*:"), "Unity 错误日志"),
    (re.compile(r"E[/ ]Tuanjie\s*:"), "团结引擎错误日志"),
)

# 会刷屏的错误：同一类别只留首条 + 次数（scan_crashes 默认已按类聚合）
_NOISY_MARKERS = (
    "NotSupportNative2Managed",
    "NotSupportAdjustorThunk",
    "NotSupportManaged2Native",
    "ExecutionEngineException",
)

# 已知无害的告警：命中这些的行归到「良性告警」，不算崩溃。
# 实测依据（团结引擎 Android 包，每次冷启动都会出现，游戏功能完全正常）：
#   A scripted object (probably X?) has a different serialization layout when loading.
#   (Read 64 bytes but expected 80 bytes)
#   Did you #ifdef UNITY_EDITOR a section of your serialized properties in any of your scripts?
# 原因是打包时 IL2CPP 侧的字段布局与编辑器序列化布局存在差异，Unity 会打这条告警
# 但仍然能正确加载。把它算成崩溃会让「登录成功进主城」的正常跑测被判成有问题。
BENIGN_PATTERNS = (
    (re.compile(r"has a different serialization layout when loading"),
     "序列化布局差异（打包常见告警，不影响运行）"),
    (re.compile(r"Did you #ifdef UNITY_EDITOR a section of your serialized properties"),
     "序列化布局提示（同上，Unity 附带说明）"),
)


# 待修缺陷：确实有问题的错误日志，但不会让进程崩溃。
# 单独归一类、不塞进 BENIGN_PATTERNS，是因为它们必须进交付给客户端的问题清单：
#   - 判成崩溃 → 正常跑测变红（假红），后续步骤还会被打断
#   - 判成良性告警 → 被当成噪音忽略（漏报）
# 所以给第三类，跑测照常判绿，但报告里单独列「待修缺陷」。
DEFECT_PATTERNS = (
    # 典型出处：UI 预载管理器在打开未预载界面时的 Error 日志
    #   GameLogger.Error(LogTips.PrefabPreloadMissing, target.ClassName, state, ...)
    # 语义：UI 预制体要用的时候发现「当前没有预载请求」——上游 Controller/玩法
    # 没提前排队，或资源已被回收。表现是首次打开该界面卡顿/资源重复加载，不闪退。
    (re.compile(r"\[PrefabPreloadMissing\]"),
     "资源预载声明缺失（需客户端修；不崩溃）"),
)


# 托管栈续行：GameLogger 打一条 Error 后，会把整个调用栈逐行跟出来，
# 每行都带同样的 `E Tuanjie : Game.Framework.Logger:Write(...)` 前缀。
# 这些行会被 `E[/ ]Tuanjie\s*:` 这种通用兜底当成「一条新的崩溃」，
# 一条真实错误能刷出 20+ 条假崩溃。特征：正文形如 `Namespace.Type:Method(args)`，
# 没有空格分隔的方法说明，就是纯栈帧。
# logcat 行首格式有两种，实测都出现过，必须都认：
#   threadtime: `11-18 10:34:56.123 23587 23612 E Tuanjie : msg`
#   epoch     : `1794969754.039 23565 23565 E appproc : msg`
# 只写 epoch 那一种，threadtime 的栈续行就漏过去了（曾把一条错误算成 24 条崩溃）。
_LOGCAT_PREFIX_RE = re.compile(
    r"^\s*(?:[0-9]{2}-[0-9]{2}\s+[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}|[0-9]+\.[0-9]+)"
    r"\s+[0-9]+\s+[0-9]+\s+[VDIWEF]\s+\S+\s*:\s*"
)

# 正文形如 `Namespace.Type:Method` / `Namespace.Type:Method(args)`，且中间没有空格。
# 括号可选：logcat 单行有长度上限，长栈帧会被截断成 `Game.Framework.UI_XController:OnC`
# 这种没有括号的残行，也得认出来，否则截断的那一条照样算崩溃。
_STACK_FRAME_RE = re.compile(
    r"^[\w<>`+]+(?:\.[\w<>`+]+)*:[\w<>`+]+(?:\s*\([^)]*\))?\s*$"
)


def strip_logcat_prefix(line: str) -> str:
    """去掉 logcat 行首的时间/pid/tid/级别/tag，只留正文。"""
    return _LOGCAT_PREFIX_RE.sub("", line.rstrip("\n"), count=1)


def is_stack_frame(line: str) -> bool:
    """判断是否托管栈续行（不是独立事件，不能当崩溃计数）。"""
    return bool(_STACK_FRAME_RE.match(strip_logcat_prefix(line)))


class Device:
    """一台 adb 设备的控制句柄。"""

    def __init__(self, serial: Optional[str] = None, timeout: float = 60.0):
        self.serial = pick_device(serial)
        self.timeout = timeout
        self._last_logcat_ts: float = 0.0
        self._size: Optional[tuple[int, int]] = None
        self.retries: int = 3            # 临时性故障重试次数
        self.heal: bool = True           # 连续失败时是否自动 reconnect
        self._fail_streak: int = 0       # 连续失败计数（触发自愈）

    # -- 基础 ---------------------------------------------------------

    def __repr__(self) -> str:
        return f"Device({self.serial})"

    def _call(self, args: list[str], timeout: Optional[float] = None,
              binary: bool = False, check: bool = True):
        """
        统一出口：带重试 + 失败自愈。
        自愈策略（保守，不主动 kill-server）：
          - 临时性失败（超时/协议抖动）→ _run_retry 内部退避重试
          - 连续 3 次失败 → adb reconnect 一次，重置计数
          - 仍失败 → 抛 DeviceGone，让上层决定是否重启 server / 等待插拔
        """
        try:
            out = _run_retry(args, timeout=timeout or self.timeout,
                             binary=binary, retries=self.retries, check=check)
            self._fail_streak = 0
            return out
        except DeviceGone:
            raise
        except AdbError:
            self._fail_streak += 1
            if self.heal and self._fail_streak >= 3:
                self._fail_streak = 0
                if adb_reconnect(self.serial):
                    try:
                        return _run_retry(args, timeout=timeout or self.timeout,
                                          binary=binary, retries=1, check=check)
                    except AdbError:
                        pass
                raise DeviceGone(f"设备 {self.serial} 连续失败，reconnect 后仍不可用")
            raise

    def shell(self, cmd: str, timeout: Optional[float] = None, check: bool = False) -> str:
        """
        执行 shell 命令，返回 stdout 文本。

        check=False（默认）：**非 0 退出码不算失败**。这是刻意设计——
        `grep` 无匹配、`dumpsys` 某些子命令、`settings get` 缺键都会返回 1，
        若当成 adb 故障抛异常，上层探测逻辑会被误判成「设备掉线」。
        真正的传输层故障仍由 _call 的重试/自愈链路识别。

        check=True：退出码非 0 时抛 AdbError（需要严格校验时用）。
        """
        return self._call(["-s", self.serial, "shell", cmd], timeout=timeout,  # type: ignore[return-value]
                          check=check)

    def exec_out(self, cmd: str, timeout: Optional[float] = None) -> bytes:
        return self._call(["-s", self.serial, "exec-out", cmd],
                          timeout=timeout, binary=True)  # type: ignore[return-value]

    def alive(self) -> bool:
        try:
            self.shell("echo ok", timeout=5)
            return True
        except AdbError:
            return False

    # -- 屏幕 ---------------------------------------------------------

    @property
    def size(self) -> tuple[int, int]:
        if self._size is None:
            out = self.shell("wm size")
            m = re.search(r"Override size:\s*(\d+)x(\d+)", out) or re.search(
                r"Physical size:\s*(\d+)x(\d+)", out
            )
            if not m:
                raise AdbError(f"无法解析屏幕尺寸: {out!r}")
            self._size = (int(m.group(1)), int(m.group(2)))
        return self._size

    @property
    def density(self) -> int:
        out = self.shell("wm density")
        m = re.search(r"(?:Override|Physical) density:\s*(\d+)", out)
        return int(m.group(1)) if m else 0

    # 一帧 raw 至少要有 16 字节头，否则说明传输被截断
    _RAW_HEADER = 16

    def screencap_raw(self, timeout: float = 15.0, retries: int = 4) -> bytes:
        """
        原始帧（16 字节头 + RGBA8888），比 -p 快 2.3 倍。

        两种已知的 USB 抖动都要在这里吃掉，不能让上层看到残缺数据：
        1) screencap 偶发卡住（实测 60s 超时一次，之后 20 次均正常）→ 用 15s 短超时。
        2) **USB babble error 时 adb 返回 0 字节**（实测内核日志
           `AppleUSBXHCIRequest::complete: ... status 0xe00002e8 (babble error): 0 bytes transferred`）。
           原来直接把 0 字节往 Frame.from_raw 传，会抛「raw 帧太短」把整条流程打死，
           而实际上重试一次就好了。这里校验长度并重试。
        """
        last_len = 0
        for i in range(max(1, retries)):
            try:
                blob = self.exec_out("screencap", timeout=timeout)
            except AdbError:
                blob = b""
            # 不仅要够 16 字节头，还要够 w*h*4 —— 实测 USB 抖动时会返回
            # 7,520,912 字节这种「截断帧」（应为 10,402,576），
            # 只校验长度>16 会漏过去，上层解码出一张撕裂的图。
            if len(blob) >= self._RAW_HEADER:
                w, h = struct.unpack_from("<II", blob, 0)
                need = self._RAW_HEADER + w * h * 4
                if len(blob) >= need:
                    return blob
            last_len = len(blob)
            # 空帧/截断帧：等一小会儿让 USB 链路喘口气再试
            time.sleep(0.35 * (i + 1))
        raise AdbError(
            f"连续 {retries} 次拿到残缺帧（最后一次 {last_len} 字节）。"
            f"USB 链路在抖（内核日志常见 babble error），"
            f"检查数据线/换直连 USB 口，或执行 adb reconnect 后重试")

    def screencap_png(self, timeout: float = 20.0) -> bytes:
        return self.exec_out("screencap -p", timeout=timeout)

    # -- 电源 / 锁屏 --------------------------------------------------

    def wake(self) -> None:
        out = self.shell("dumpsys power | grep -m1 'mWakefulness='")
        if "Awake" not in out:
            self.shell("input keyevent KEYCODE_WAKEUP")
            time.sleep(0.4)

    def is_locked(self) -> bool:
        out = self.shell("dumpsys window 2>/dev/null | grep -m1 'mDreamingLockscreen'")
        return "mDreamingLockscreen=true" in out

    def unlock(self, pin: Optional[str] = None) -> None:
        """唤醒并解锁（无密码/滑动锁屏）。"""
        self.wake()
        if not self.is_locked():
            return
        w, h = self.size
        self.shell(f"input swipe {w // 2} {int(h * 0.82)} {w // 2} {int(h * 0.25)} 220")
        time.sleep(0.8)
        if self.is_locked() and pin:
            self.shell(f"input text {pin}")
            self.shell("input keyevent KEYCODE_ENTER")
            time.sleep(0.8)

    # -- 输入 ---------------------------------------------------------

    def tap(self, x: int, y: int) -> None:
        self.shell(f"input tap {int(x)} {int(y)}")

    def tap_ratio(self, rx: float, ry: float) -> None:
        w, h = self.size
        self.tap(int(w * rx), int(h * ry))

    def swipe(self, x1: int, y1: int, x2: int, y2: int, ms: int = 300) -> None:
        self.shell(f"input swipe {int(x1)} {int(y1)} {int(x2)} {int(y2)} {int(ms)}")

    def long_press(self, x: int, y: int, ms: int = 800) -> None:
        self.shell(f"input swipe {int(x)} {int(y)} {int(x)} {int(y)} {int(ms)}")

    def key(self, *codes: str) -> None:
        self.shell("input keyevent " + " ".join(codes))

    def text(self, s: str) -> None:
        """仅 ASCII；中文需 ADBKeyBoard 或剪贴板方案（本机未装，见 README 限制）。"""
        escaped = s.replace(" ", "%s")
        self.shell(f'input text "{escaped}"')

    # -- 对象树 -------------------------------------------------------

    def ui_dump(self, retries: int = 2) -> str:
        """uiautomator dump 直写 /dev/tty，省一次 /sdcard 往返。"""
        last_err = ""
        for _ in range(retries + 1):
            try:
                out = self.exec_out("uiautomator dump --compressed /dev/tty", timeout=30)
                text = out.decode("utf-8", "replace")
                idx = text.find("<?xml")
                if idx >= 0:
                    return text[idx:]
                last_err = f"无 XML 输出: {text[:200]!r}"
            except AdbError as e:
                last_err = str(e)
            time.sleep(0.3)
        raise AdbError(f"uiautomator dump 失败: {last_err}")

    def ui_nodes(self) -> list[UiNode]:
        return parse_ui_xml(self.ui_dump())

    def find(
        self,
        text: Optional[str] = None,
        desc: Optional[str] = None,
        cls: Optional[str] = None,
        package: Optional[str] = None,
        clickable: Optional[bool] = None,
        exact: bool = False,
    ) -> list[UiNode]:
        nodes = self.ui_nodes()
        out = []
        for n in nodes:
            if text is not None and not _match_field(n.text, text, exact):
                continue
            if desc is not None and not _match_field(n.desc, desc, exact):
                continue
            if cls is not None and cls not in n.cls:
                continue
            if package is not None and package != n.package:
                continue
            if clickable is not None and n.clickable != clickable:
                continue
            out.append(n)
        return out

    def tap_text(self, needle: str, exact: bool = False, timeout: float = 10.0) -> bool:
        """按文字/描述点按；轮询直到出现。返回是否点到。"""
        deadline = time.time() + timeout
        while True:
            for n in self.find(text=needle, exact=exact):
                x, y = n.center
                self.tap(x, y)
                return True
            for n in self.find(desc=needle, exact=exact):
                x, y = n.center
                self.tap(x, y)
                return True
            if time.time() >= deadline:
                return False
            time.sleep(0.4)

    # -- 应用 / Activity ----------------------------------------------

    def current_activity(self) -> str:
        """返回 'package/activity'；取不到返回空串。"""
        out = self.shell("dumpsys activity activities 2>/dev/null | grep -m1 mResumedActivity")
        m = re.search(r"\s([\w.]+)/([\w.$]+)\s", out)
        if m:
            return f"{m.group(1)}/{m.group(2)}"
        out = self.shell("dumpsys window 2>/dev/null | grep -m1 mCurrentFocus")
        m = re.search(r"\s([\w.]+)/([\w.$]+)\}", out)
        return f"{m.group(1)}/{m.group(2)}" if m else ""

    def current_package(self) -> str:
        act = self.current_activity()
        return act.split("/")[0] if "/" in act else ""

    def is_installed(self, package: str) -> bool:
        out = self.shell(f"pm list packages {package}")
        return f"package:{package}" in out

    def is_running(self, package: str) -> bool:
        """目标包进程是否还活着。

        用 `pidof` 而不是 `ps | grep`：前者直接返回 pid，无输出即未运行，
        不会像 grep 那样匹配到自身命令行造成假阳性。
        实测 Android 12 支持 pidof（toybox）。
        """
        out = self.shell(f"pidof {package}").strip()
        return bool(out)

    def launch(self, package: str, activity: Optional[str] = None, wait: float = 0.0) -> None:
        if activity:
            comp = f"{package}/{activity}"
            self.shell(f"am start -n {comp}")
        else:
            # monkey 是绕过 splash/launcher 差异最稳的方式
            self.shell(
                f"monkey -p {package} -c android.intent.category.LAUNCHER 1", timeout=30
            )
        if wait:
            time.sleep(wait)

    def force_stop(self, package: str) -> None:
        self.shell(f"am force-stop {package}")

    def clear_data(self, package: str) -> None:
        self.shell(f"pm clear {package}")

    # -- 日志 ---------------------------------------------------------

    def logcat_dump(self, lines: int = 500) -> list[tuple[float, str]]:
        """
        返回 [(epoch_ts, 原始行)]，用 -v epoch 便于和本地时间对齐。

        注意：**不要用 `logcat -t N`**。实测在某 vivo 机型 / Android 12 上，
        `-t N` 被解析成「N 毫秒之前」而不是「最近 N 行」：
        `-t 4000` → 0 行，`-t 5000` → 1691 行，`-t 50000` → 全部 76494 行。
        用 `-t 500` 就变成「最近 500ms」，游戏安静时返回 0 行 —— 崩溃检测会静默漏报。
        所以这里整份 dump 后本地取尾部：7.7 MB / 约 480ms，代价可接受且语义确定。
        """
        out = self.shell("logcat -d -v epoch", timeout=60)
        rows: list[tuple[float, str]] = []
        for line in out.splitlines():
            m = re.match(r"\s*(\d+\.\d+)\s", line)
            if m:
                rows.append((float(m.group(1)), line))
        if lines and lines > 0 and len(rows) > lines:
            rows = rows[-lines:]
        return rows

    def logcat_since(self, ts: Optional[float] = None,
                     max_lines: int = 0) -> list[str]:
        """
        取自 ts 之后的日志行（默认接上次调用位置）。

        max_lines=0 表示不截断：判定崩溃时必须看全窗口内的日志，
        否则「标记后跑了很久、日志超过截断长度」会把最早发生的崩溃切掉，
        造成假阴性。整份 dump 约 480ms，这个代价换来的是判定正确性。
        """
        since = ts if ts is not None else self._last_logcat_ts
        rows = self.logcat_dump(max_lines)
        if rows:
            self._last_logcat_ts = rows[-1][0]
        return [line for t, line in rows if t >= since]

    def logcat_clear(self) -> None:
        self.shell("logcat -c")

    def scan_crashes(self, lines: Iterable[str], dedupe: bool = True,
                     include_benign: bool = False) -> list[dict[str, Any]]:
        """
        扫描日志行，返回命中的崩溃/异常。默认按类别聚合：
        每类只保留首条样本，count 记录该类总出现次数，避免刷屏日志把报告淹没。
        空消息行（如 `E Tuanjie : ` 后无内容）不计入，防止把栈续行算成新异常。

        已知无害的告警（BENIGN_PATTERNS，如 Unity 序列化布局差异）默认被剔除，
        不计入崩溃；需要看全量时传 include_benign=True。
        """
        agg: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for line in lines:
            s = line.rstrip("\n")
            # 良性告警先判：命中的行直接归到「良性告警」，不再往下走崩溃匹配
            benign_kind = None
            for pat, kind in BENIGN_PATTERNS:
                if pat.search(s):
                    benign_kind = kind
                    break
            if benign_kind is not None:
                if include_benign:
                    if benign_kind in agg:
                        agg[benign_kind]["count"] += 1
                    else:
                        agg[benign_kind] = {"kind": benign_kind,
                                            "line": s[:400], "count": 1,
                                            "benign": True}
                        order.append(benign_kind)
                continue

            # 待修缺陷（如 [PrefabPreloadMissing]）也不该被算成崩溃：它们由
            # 「E Tuanjie :」这类通用兜底规则会命中，但进程根本没崩。scan_defects
            # 单独收它们，这里跳过，避免同一行被两个桶各算一次。
            if any(pat.search(s) for pat, _ in DEFECT_PATTERNS):
                continue

            # 托管栈续行（GameLogger 把调用栈逐行打出来）：不是独立事件。
            # 不跳过的话，一条错误能刷出 20+ 条假崩溃。
            if is_stack_frame(s):
                continue

            for pat, kind in CRASH_PATTERNS:
                if not pat.search(s):
                    continue
                # 去掉 logcat 前缀后的正文；为空说明是续行/空行，跳过
                body = strip_logcat_prefix(s).strip()
                if not body:
                    break
                if kind in agg:
                    agg[kind]["count"] += 1        # int，调用方不必再转换
                else:
                    agg[kind] = {"kind": kind, "line": body[:400], "count": 1}
                    order.append(kind)
                break
        return [agg[k] for k in order]

    def scan_defects(self, lines: Iterable[str]) -> list[dict[str, Any]]:
        """扫描「确实有问题的错误日志，但不会崩溃」的行（DEFECT_PATTERNS）。

        与 scan_crashes 的区别：崩溃会让跑测判红、并中断后续步骤；这些缺陷不会，
        但必须进交付给客户端的问题清单。单独一类，既不假红也不漏报。
        """
        agg: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for line in lines:
            s = line.rstrip("\n")
            for pat, kind in DEFECT_PATTERNS:
                if not pat.search(s):
                    continue
                body = strip_logcat_prefix(s).strip()
                if kind in agg:
                    agg[kind]["count"] += 1
                else:
                    agg[kind] = {"kind": kind, "line": (body or s.strip())[:400],
                                 "count": 1, "defect": True}
                    order.append(kind)
                break
        return [agg[k] for k in order]

    def wait_activity(self, needle: str, timeout: float = 30.0, poll: float = 0.5) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if needle in self.current_activity():
                return True
            time.sleep(poll)
        return False

    def wait_activity_change(self, from_activity: str, timeout: float = 30.0) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            cur = self.current_activity()
            if cur and cur != from_activity:
                return cur
            time.sleep(0.5)
        return from_activity


def _match_field(value: str, needle: str, exact: bool) -> bool:
    if not value:
        return False
    return value == needle if exact else needle in value


# ---------------------------------------------------------------- 自检

if __name__ == "__main__":
    print(f"adb = {adb_path()}")
    devs = list_devices()
    print(f"devices = {devs}")
    if not devs:
        raise SystemExit("无设备，自检结束")
    d = Device()
    print(f"size = {d.size}  density = {d.density}")
    print(f"activity = {d.current_activity()}")
    t0 = time.time()
    raw = d.screencap_raw()
    print(f"screencap_raw = {len(raw)} bytes in {time.time() - t0:.2f}s")
    t0 = time.time()
    nodes = d.ui_nodes()
    print(f"ui_nodes = {len(nodes)} in {time.time() - t0:.2f}s")
    for n in nodes[:5]:
        print(f"  {n.cls} text={n.text[:30]!r} bounds={n.bounds}")
