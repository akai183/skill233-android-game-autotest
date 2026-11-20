#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
runner.py — JSON 步骤流跑测引擎

定位：给「桌面端 agent（DSH）经 adb 跑游戏流程」提供一个可声明、可复用、可留证的
执行层。agent 只负责「想清楚步骤」，引擎负责「稳定执行 + 判定 + 留证 + 出报告」。

设计原则：
  1. 步骤用 JSON 描述，不写代码 → agent 容易生成、人容易审、可版本化
  2. 每步默认截图留证 + 计时 + 异常捕获 → 失败时现场不丢
  3. 断言失败不中断（除非 fatal），收集全部问题后一次性出报告 → 一次跑测发现多个 bug
  4. 报告格式对齐项目自带 GameAutoTest233（report.json / index.html / junit.xml）

用法：
    python scripts/runner.py flows/smoke.json
    python scripts/runner.py flows/xxx.json --serial <序列号> --out runs
    python scripts/runner.py flows/xxx.json --dry-run     # 只校验流程文件，不连设备

流程文件格式见 references/flow-schema.md
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from adbkit import (  # noqa: E402
    AdbError,
    Device,
    DeviceGone,
    adb_restart_server,
    find_adb,
    list_devices,
    pick_device,
)
def _need_vision():
    """延迟导入图像层：没有 numpy/Pillow 时 --dry-run 仍能校验流程结构。"""
    try:
        import vision as mod
        return mod
    except ModuleNotFoundError as e:
        raise RuntimeError(
            "图像判定需要 numpy + Pillow：在技能根目录执行 "
            "python -m pip install -r requirements.txt（Python 3.13+）；"
            "只想临时装也可以 python -m pip install \"numpy>=2.3,<3\" \"pillow>=12,<14\""
        ) from e


class _VisionProxy:
    """把 vision 的导入推迟到真正调用时。"""

    def __getattr__(self, item):
        return getattr(_need_vision(), item)


vision = _VisionProxy()

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_FLOWS = os.path.join(ROOT, "flows")
DEFAULT_RUNS = os.path.join(ROOT, "runs")
ASSETS = os.path.join(ROOT, "assets")


# ---------------------------------------------------------------- 结果模型

STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_ERROR = "error"
STATUS_SKIP = "skip"


@dataclass
class StepResult:
    index: int
    step_id: str
    action: str
    status: str = STATUS_PASS
    started_at: float = 0.0
    duration: float = 0.0
    message: str = ""
    detail: dict = field(default_factory=dict)
    screenshot: str = ""
    screenshot_before: str = ""

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "id": self.step_id,
            "action": self.action,
            "status": self.status,
            "duration": round(self.duration, 3),
            "message": self.message,
            "detail": self.detail,
            "screenshot": self.screenshot,
            "screenshot_before": self.screenshot_before,
        }


@dataclass
class RunResult:
    flow_name: str
    flow_path: str
    run_id: str
    started_at: str
    device: str
    steps: list[StepResult] = field(default_factory=list)
    finished_at: str = ""
    duration: float = 0.0
    crashed: bool = False
    crash_events: list[dict] = field(default_factory=list)
    benign_events: list[dict] = field(default_factory=list)
    # 有错误日志但不崩溃的缺陷（如 [PrefabPreloadMissing]）：跑测照常判绿，
    # 但要单独列出交客户端修。既不算崩溃也不算良性，所以是第三个桶。
    defect_events: list[dict] = field(default_factory=list)
    fatal: str = ""
    artifacts_dir: str = ""

    @property
    def failed(self) -> list[StepResult]:
        return [s for s in self.steps if s.status in (STATUS_FAIL, STATUS_ERROR)]

    @property
    def passed(self) -> bool:
        return not self.failed and not self.crashed and not self.fatal

    def summary(self) -> dict:
        return {
            "flow": self.flow_name,
            "run_id": self.run_id,
            "device": self.device,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration": round(self.duration, 2),
            "total": len(self.steps),
            "pass": sum(1 for s in self.steps if s.status == STATUS_PASS),
            "fail": sum(1 for s in self.steps if s.status == STATUS_FAIL),
            "error": sum(1 for s in self.steps if s.status == STATUS_ERROR),
            "skip": sum(1 for s in self.steps if s.status == STATUS_SKIP),
            "crashed": self.crashed,
            "crash_events": self.crash_events,
            "benign_events": self.benign_events,
            "defect_events": self.defect_events,
            "fatal": self.fatal,
            "passed": self.passed,
            "artifacts": self.artifacts_dir,
        }


# ---------------------------------------------------------------- 断言表达式

class SafeExpr:
    """
    受限表达式求值：只允许 变量名 / 数字 / 字符串 / 比较 / 布尔 / 算术 / in。
    不用 eval 的裸 namespace，改为 AST 白名单校验后再 eval。
    """

    _ALLOWED_NODES = {
        "Expression", "BoolOp", "BinOp", "UnaryOp", "Compare", "Name", "Load",
        "Constant", "And", "Or", "Not", "Eq", "NotEq", "Lt", "LtE", "Gt", "GtE",
        "In", "NotIn", "Add", "Sub", "Mult", "Div", "Mod", "USub", "Is", "IsNot",
        "List", "Tuple", "Attribute", "Call", "IfExp",
    }
    _ALLOWED_CALLS = {"len", "str", "int", "float", "abs", "round", "min", "max",
                      "all", "any", "bool", "lower", "upper", "startswith", "endswith"}

    @classmethod
    def check(cls, expr: str) -> None:
        import ast

        tree = ast.parse(expr, mode="eval")
        for node in ast.walk(tree):
            name = type(node).__name__
            if name not in cls._ALLOWED_NODES:
                raise ValueError(f"表达式含不允许的语法：{name}")
            if name == "Call":
                fn = node.func
                ok = (isinstance(fn, ast.Name) and fn.id in cls._ALLOWED_CALLS) or \
                     (isinstance(fn, ast.Attribute) and fn.attr in cls._ALLOWED_CALLS)
                if not ok:
                    raise ValueError(f"表达式含不允许的函数调用：{ast.dump(fn)[:60]}")

    @classmethod
    def eval(cls, expr: str, variables: dict) -> Any:
        cls.check(expr)
        return eval(compile(expr, "<assert>", "eval"), {"__builtins__": {}}, dict(variables))


# ---------------------------------------------------------------- 引擎

class FlowRunner:
    def __init__(self, flow: dict, flow_path: str, serial: Optional[str] = None,
                 out_dir: str = DEFAULT_RUNS, keep_all_shots: bool = True,
                 verbose: bool = True):
        self.flow = flow
        self.flow_path = flow_path
        self.serial = serial
        self.out_dir = out_dir
        self.keep_all_shots = keep_all_shots
        self.verbose = verbose

        self.dev: Optional[Device] = None
        self.vars: dict[str, Any] = dict(flow.get("vars") or {})
        self.run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.art_dir = os.path.join(out_dir, self.run_id)
        self._log_lines: list[str] = []
        self._events_path = ""
        self._last_logcat_ts: float = 0.0
        self._step_index = 0
        # 进程存活看护：launch 之后记下包名，等待类步骤每轮都顺手查一次。
        # 不加这个，游戏崩溃后 wait_template 会傻等到 timeout 才报「没匹配到」，
        # 既浪费几分钟又给出误导性的失败原因（实际是崩溃，不是画面不对）。
        self._guard_pkg: str = ""

        self.result = RunResult(
            flow_name=flow.get("name", os.path.basename(flow_path)),
            flow_path=flow_path,
            run_id=self.run_id,
            started_at=datetime.now().isoformat(timespec="seconds"),
            device="",
            artifacts_dir=self.art_dir,
        )

    # -- 日志 ---------------------------------------------------------

    def log(self, msg: str) -> None:
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
        self._log_lines.append(line)
        if self.verbose:
            print(line, flush=True)

    def _event(self, obj: dict) -> None:
        if not self._events_path:
            return
        with open(self._events_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")

    # -- 初始化 -------------------------------------------------------

    def setup(self) -> None:
        os.makedirs(self.art_dir, exist_ok=True)
        self._events_path = os.path.join(self.art_dir, "events.jsonl")

        adb = find_adb()
        self.log(f"adb = {adb}")
        devices = list_devices()
        if not devices:
            raise RuntimeError("没有检测到任何 adb 设备；请检查 USB 连接与调试授权")
        self.dev = Device(self.serial)
        self.result.device = self.dev.serial
        self.log(f"device = {self.dev.serial}  size={self.dev.size} density={self.dev.density}")

        self.vars["DEVICE_SERIAL"] = self.dev.serial
        self.vars["SCREEN_W"], self.vars["SCREEN_H"] = self.dev.size

        dev_cfg = self.flow.get("device") or {}
        if dev_cfg.get("stay_awake", True):
            self._stay_awake()
        if dev_cfg.get("unlock", True):
            self._ensure_unlocked()

        # 记录本次跑测的基线 logcat 时间戳
        try:
            self.dev.logcat_clear()
        except AdbError:
            pass
        self._last_logcat_ts = time.time()

        # 保存流程文件副本，保证「报告能自解释这次跑的是什么」
        shutil.copyfile(self.flow_path, os.path.join(self.art_dir, "flow.json"))

    def _stay_awake(self) -> None:
        """防止测试过程中息屏：USB 供电保持常亮 + 关闭超时息屏。"""
        try:
            self.dev.shell("svc power stayon usb")
            self.log("已设置 svc power stayon usb（USB 连接时保持常亮）")
        except AdbError as e:
            self.log(f"设置 stayon 失败（不致命）：{e}")
        try:
            self.dev.shell("settings put system screen_off_timeout 2147483647")
            self.log("已关闭系统息屏超时")
        except AdbError as e:
            self.log(f"关闭息屏超时失败（不致命）：{e}")

    def _ensure_unlocked(self) -> None:
        try:
            self.dev.unlock()
            if self.dev.is_locked():
                self.log("⚠ 设备仍处于锁屏状态，点击类步骤可能失败")
            else:
                self.log("设备已唤醒并解锁")
        except AdbError as e:
            self.log(f"解锁失败（不致命）：{e}")

    # -- 主循环 -------------------------------------------------------

    def run(self) -> RunResult:
        t0 = time.time()
        try:
            self.setup()
        except Exception as e:
            self.result.fatal = f"初始化失败：{type(e).__name__}: {e}"
            self.result.finished_at = datetime.now().isoformat(timespec="seconds")
            self.result.duration = time.time() - t0
            self.log(f"❌ {self.result.fatal}")
            self._write_reports()
            return self.result

        steps = self.flow.get("steps") or []
        self.log(f"开始执行流程「{self.result.flow_name}」，共 {len(steps)} 步")

        abort = False
        for i, step in enumerate(steps):
            self._step_index = i
            if abort:
                r = StepResult(i, step.get("id", f"step{i}"), step.get("action", "?"),
                               status=STATUS_SKIP, message="前序步骤要求中断")
                self.result.steps.append(r)
                continue
            r = self._run_step(i, step)
            self.result.steps.append(r)
            icon = {STATUS_PASS: "✅", STATUS_FAIL: "❌",
                    STATUS_ERROR: "💥", STATUS_SKIP: "⏭"}.get(r.status, "?")
            self.log(f"{icon} [{i + 1}/{len(steps)}] {r.step_id} ({r.action}) "
                     f"{r.duration:.2f}s {r.message}")
            if r.status in (STATUS_FAIL, STATUS_ERROR) and step.get("fatal"):
                abort = True
                self.log("该步骤标记为 fatal，中断后续步骤")

        # 收尾：扫一遍 logcat 抓崩溃
        self._collect_crashes()

        self.result.finished_at = datetime.now().isoformat(timespec="seconds")
        self.result.duration = time.time() - t0
        self._write_reports()
        return self.result

    # -- 单步执行 -----------------------------------------------------

    def _expand(self, value):
        """把 ${VAR} 替换成 vars 里的值；支持嵌套在 dict/list 里递归展开。

        只认 ${NAME} 形式（NAME 允许字母数字下划线），不做任意表达式求值 ——
        表达式请用 set/assert 的 expr 字段，避免流程文件里藏代码。
        """
        if isinstance(value, str):
            def sub(m):
                name = m.group(1)
                if name not in self.vars:
                    raise AssertionError(
                        f"流程里引用了未定义变量 ${{{name}}}；"
                        f"已定义：{sorted(k for k in self.vars if not k.startswith('_'))}")
                return str(self.vars[name])

            return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", sub, value)
        if isinstance(value, list):
            return [self._expand(v) for v in value]
        if isinstance(value, dict):
            return {k: self._expand(v) for k, v in value.items()}
        return value

    def _run_step(self, index: int, step: dict) -> StepResult:
        sid = step.get("id") or f"step{index}"
        action = step.get("action", "")
        r = StepResult(index=index, step_id=sid, action=action, started_at=time.time())

        # 展开 ${VAR}（when 也要展开，因为里面可能引用变量）
        try:
            step = self._expand(step)
        except AssertionError as e:
            r.status = STATUS_ERROR
            r.message = str(e)
            r.duration = time.time() - r.started_at
            return r

        # 前置条件：when 表达式为假则跳过
        when = step.get("when")
        if when:
            try:
                if not SafeExpr.eval(when, self.vars):
                    r.status = STATUS_SKIP
                    r.message = f"when 条件不成立：{when}"
                    return r
            except Exception as e:
                r.status = STATUS_ERROR
                r.message = f"when 表达式错误：{e}"
                return r

        shot = step.get("screenshot", True)
        if shot and step.get("before_shot", False):
            r.screenshot_before = self._capture(f"{index:02d}-{sid}-before")

        try:
            handler = getattr(self, f"_act_{action}", None)
            if handler is None:
                raise ValueError(f"未知 action：{action!r}")
            out = handler(step, r) or {}
            r.detail.update(out)
            if r.status == STATUS_PASS:
                msg = step.get("message") or out.get("_message") or ""
                r.message = msg
        except AssertionError as e:
            r.status = STATUS_FAIL
            r.message = str(e)
        except DeviceGone as e:
            r.status = STATUS_ERROR
            r.message = f"设备断开：{e}"
            self.log("设备断开，尝试重启 adb server 自愈…")
            if adb_restart_server():
                try:
                    self.dev = Device(self.dev.serial)
                    self.log("adb server 已重启，设备重新可用")
                except AdbError as e2:
                    r.message += f"；重启后仍不可用：{e2}"
            else:
                r.message += "；adb server 重启失败"
        except AdbError as e:
            r.status = STATUS_ERROR
            r.message = f"adb 错误：{e}"
        except Exception as e:
            r.status = STATUS_ERROR
            r.message = f"{type(e).__name__}: {e}"
            r.detail["traceback"] = traceback.format_exc(limit=6)
        finally:
            r.duration = time.time() - r.started_at
            if shot and r.status != STATUS_SKIP:
                r.screenshot = self._capture(f"{index:02d}-{sid}-after")
            self._event({
                "ts": datetime.now().isoformat(timespec="seconds"),
                "step": r.to_dict(),
                "vars": {k: v for k, v in self.vars.items()
                         if isinstance(v, (str, int, float, bool))},
            })
        return r

    def _capture(self, name: str) -> str:
        """截一帧存 PNG，返回相对路径；失败返回空串（不打断流程）。"""
        try:
            assert self.dev is not None
            f = vision.Frame.from_raw(self.dev.screencap_raw())
            path = os.path.join(self.art_dir, f"{name}.png")
            f.save(path)
            return os.path.relpath(path, self.art_dir)
        except Exception as e:
            self.log(f"截图失败 {name}: {e}")
            return ""

    def _frame(self):
        assert self.dev is not None
        return vision.Frame.from_raw(self.dev.screencap_raw())

    def _box(self, step: dict) -> Optional[tuple]:
        b = step.get("box")
        return tuple(b) if b else None

    # ---------------------------------------------------------------- actions
    # 命名规则：_act_<action>

    def _act_wake(self, step, r):
        assert self.dev is not None
        self.dev.wake()
        return {"_message": "已唤醒"}

    def _act_unlock(self, step, r):
        assert self.dev is not None
        self.dev.unlock(step.get("pin"))
        locked = self.dev.is_locked()
        if locked and step.get("must_unlock", True):
            raise AssertionError("设备仍处于锁屏状态")
        return {"locked": locked}

    def _act_sleep(self, step, r):
        sec = float(step.get("seconds", 1.0))
        time.sleep(sec)
        return {"_message": f"等待 {sec}s"}

    def _act_launch(self, step, r):
        assert self.dev is not None
        pkg = step["package"]
        act = step.get("activity")
        if not self.dev.is_installed(pkg):
            raise AssertionError(f"应用未安装：{pkg}")
        self.dev.launch(pkg, act)
        # 记下包名，后续等待类步骤会用它做进程存活看护
        self._guard_pkg = pkg
        wait = float(step.get("wait", 0))
        if wait:
            time.sleep(wait)
        if step.get("expect_activity"):
            ok = self.dev.wait_activity(step["expect_activity"],
                                        timeout=float(step.get("timeout", 30)))
            if not ok:
                raise AssertionError(
                    f"启动后未进入预期 Activity（期望含 {step['expect_activity']}，"
                    f"实际 {self.dev.current_activity()}）")
        return {"activity": self.dev.current_activity()}

    def _act_force_stop(self, step, r):
        assert self.dev is not None
        self.dev.force_stop(step["package"])
        return {"_message": f"已强停 {step['package']}"}

    def _act_clear_data(self, step, r):
        assert self.dev is not None
        self.dev.clear_data(step["package"])
        return {"_message": f"已清数据 {step['package']}"}

    def _act_tap(self, step, r):
        assert self.dev is not None
        if "ratio" in step:
            rx, ry = step["ratio"]
            self.dev.tap_ratio(float(rx), float(ry))
            return {"_message": f"点击比例 ({rx}, {ry})"}
        self.dev.tap(int(step["x"]), int(step["y"]))
        return {"_message": f"点击 ({step['x']}, {step['y']})"}

    def _act_tap_color(self, step, r):
        """按颜色找按钮再点（适合位置随面板变化、颜色固定的控件）。"""
        assert self.dev is not None
        color = step["color"]
        box = self._box(step)
        f = self._frame()
        blob = vision.find_color_blob(f, color, box=box,
                                      tolerance=step.get("tolerance"),
                                      min_pixels=int(step.get("min_pixels", 60)))
        if blob is None:
            if step.get("must_find", True):
                raise AssertionError(f"画面里没找到颜色 {color!r}（区域 {box}）")
            return {"found": False}
        cx, cy = blob["center"]
        if step.get("tap", True):
            self.dev.tap(cx, cy)
        return {"found": True, "at": (cx, cy), "ratio": blob["ratio"],
                "pixels": blob["pixels"],
                "_message": f"颜色 {color} 定位到 ({cx},{cy}) 比例 "
                            f"({blob['ratio'][0]:.4f},{blob['ratio'][1]:.4f})"}

    def _act_close_panel(self, step, r):
        """关掉当前面板，直到回到期望画面。

        Unity 面板的关闭控件位置各不相同（实测 7 个主城入口里，
        右上 X 只对 3 个有效、左上箭头只对 2 个有效、BACK 键一个都关不掉），
        所以这里按候选策略轮着试，每轮都用 expect 模板验证是否真的回到目标画面。
        顺序：红 X 色块 → 右上 X 坐标 → 左上箭头 → 左上白色返回文字。
        """
        assert self.dev is not None
        expect = self._resolve_asset(step["expect"])
        tpl = vision.Frame.load(expect)
        threshold = float(step.get("threshold", 0.90))
        scale = int(step.get("scale", 2))
        timeout = float(step.get("timeout", 20))
        poll = float(step.get("interval", 0.8))
        x_box = step.get("x_box", [0.75, 0.08, 0.25, 0.18])

        # 候选关闭策略：(名字, 动作函数)
        strategies = []

        def by_color(f):
            blob = vision.find_color_blob(f, step.get("color", "red"), box=x_box,
                                          tolerance=step.get("tolerance"),
                                          min_pixels=int(step.get("min_pixels", 60)))
            if blob is None:
                return False
            self.dev.tap(*blob["center"])
            return True

        def by_x(f):
            self.dev.tap_ratio(*step.get("x_ratio", [0.905, 0.205]))
            return True

        def by_arrow(f):
            # 实测精确值 (0.065, 0.125)：原来的 (0.055, 0.128) 偏 10px，
            # 对某些面板，左上角 < 返回箭头点不中。
            self.dev.tap_ratio(*step.get("arrow_ratio", [0.065, 0.125]))
            return True

        def by_back_text(f):
            self.dev.tap_ratio(*step.get("back_text_ratio", [0.18, 0.071]))
            return True

        def by_outside(f):
            # 模态浮层（如点货币计数弹出的「获取途径」）只能点浮层外的空白关掉，
            # 实测 BACK / ESC / 点浮层左右两侧都无效，只有点上方空白有效。
            self.dev.tap_ratio(*step.get("outside_ratio", [0.5, 0.20]))
            return True

        def by_outside_bottom(f):
            # 另一类浮层（详情卡，如系统升级点的材料卡）相反：
            # 点上方空白无效，要点面板下方的空白 (0.5, 0.92) 才关得掉。
            self.dev.tap_ratio(*step.get("outside_bottom_ratio", [0.5, 0.92]))
            return True

        strategies = [("红X色块", by_color), ("右上X坐标", by_x),
                      ("左上箭头", by_arrow), ("左上返回文字", by_back_text),
                      ("浮层上空白", by_outside), ("浮层下空白", by_outside_bottom)]

        t0 = time.time()
        tries = 0
        used: list[str] = []
        samples = int(step.get("samples", 3))
        while time.time() - t0 < timeout:
            # 多帧取最大值：主城带云层动画，单帧得分 0.858~0.963 抖动，
            # 只看一帧会把动画低谷当成「已回到主城」而假通过。
            frame = None
            scores = []
            for _ in range(max(1, samples)):
                frame = self._frame()
                scores.append(vision.match_template(frame, tpl, scale=scale)[0])
            score = max(scores)
            if score >= threshold:
                return {"closed": True, "score": round(score, 4), "tries": tries,
                        "strategies": used,
                        "_message": f"已回到目标画面（{os.path.basename(expect)} "
                                    f"得分 {score:.3f}，尝试 {tries} 次，"
                                    f"策略 {used or ['无']}）"}
            tries += 1
            name, fn = strategies[(tries - 1) % len(strategies)]
            used.append(name)
            # 注意：这里必须把刚截的 frame 传进去（按颜色找控件要用它），
            # 早期版本写成 fn(f) 引用了一个不存在的变量 f → NameError，
            # 又被 except Exception: pass 吞掉，导致所有关闭策略其实一次都没执行，
            # 表现为「关面板必然超时」。
            try:
                ok = fn(frame)
                if ok is False:
                    used[-1] = f"{name}(未找到控件)"
            except Exception as e:
                used[-1] = f"{name}(失败:{type(e).__name__})"
            time.sleep(poll)

        raise AssertionError(
            f"关面板失败：{timeout}s 内没回到 {os.path.basename(expect)}"
            f"（尝试 {tries} 次，策略 {used}）。"
            f"可能是模态浮层盖住了关闭控件——实测「金币获取途径」这类浮层"
            f"点遮罩外部/BACK/ESC 都关不掉，只能重启游戏。")

    def _act_swipe(self, step, r):
        assert self.dev is not None
        if "from_ratio" in step:
            w, h = self.dev.size
            x1, y1 = step["from_ratio"]
            x2, y2 = step["to_ratio"]
            self.dev.swipe(int(w * x1), int(h * y1), int(w * x2), int(h * y2),
                           int(step.get("ms", 300)))
        else:
            self.dev.swipe(int(step["x1"]), int(step["y1"]),
                           int(step["x2"]), int(step["y2"]), int(step.get("ms", 300)))
        return {"_message": "滑动完成"}

    def _act_key(self, step, r):
        assert self.dev is not None
        codes = step["codes"]
        if isinstance(codes, str):
            codes = [codes]
        self.dev.key(*codes)
        return {"_message": f"按键 {codes}"}

    def _act_text(self, step, r):
        """输入文本。注意：设备若启用了中文输入法，ASCII 会被拼音组词污染
        （输入 autotest233 变成「啊去to'te'st」），先跑 ime disable 或 ime_ascii。

        clear: true 先清空输入框（光标移到末尾再按 backspace，数量可调）
        """
        assert self.dev is not None
        if step.get("clear", False):
            self.dev.key("KEYCODE_MOVE_END")
            self.dev.key(*(["KEYCODE_DEL"] * int(step.get("clear_times", 40))))
        self.dev.text(str(step["text"]))
        return {"_message": f"输入 {step['text']!r}"}

    def _act_ime(self, step, r):
        """输入法控制。Unity 输入框吃 adb input text 时，中文输入法会把 ASCII
        变成拼音组词（实测：autotest233 → 「啊去to'te'st」），必须切到 ASCII。

        mode:
          "off"   禁用当前输入法（ime disable），input text 直落 Unity 输入框
          "on"    重新启用（需要 ime_id 或先跑过 off）
          "list"  只列出输入法，不改状态
          "reset" 恢复系统默认输入法（ime reset）
        ime_id: 指定输入法（默认自动探测）
        """
        assert self.dev is not None
        mode = str(step.get("mode", "off")).lower()

        if mode == "list":
            out = self.dev.shell("ime list -s")
            return {"imes": out.strip().splitlines(),
                    "_message": f"输入法：{out.strip() or '(无)'}"}

        ime_id = step.get("ime_id") or self.vars.get("IME_ID") or ""
        if not ime_id:
            ime_id = self._detect_ime_id()

        if mode == "reset":
            self.dev.shell("ime reset")
            return {"_message": "已恢复系统默认输入法"}

        if mode == "off":
            if not ime_id:
                return {"_message": "未探测到任何输入法，跳过（若 input text 被拼音污染请手工指定 ime_id）"}
            out = self.dev.shell(f"ime disable {ime_id}")
            self.vars["IME_ID"] = ime_id
            self.vars["IME_DISABLED"] = ime_id
            return {"ime": ime_id, "_message": f"已禁用输入法 {ime_id}：{out.strip()}"}

        if mode == "on":
            target = ime_id or self.vars.get("IME_DISABLED") or ""
            if not target:
                raise AssertionError("ime on 需要 ime_id，或先执行 ime off")
            out = self.dev.shell(f"ime enable {target}")
            self.dev.shell(f"ime set {target}")
            self.vars.pop("IME_DISABLED", None)
            return {"ime": target, "_message": f"已恢复输入法 {target}：{out.strip()}"}

        raise AssertionError(f"未知 ime mode：{mode!r}（可用 off/on/list/reset）")

    def _detect_ime_id(self) -> str:
        """探测输入法 ID。default_input_method 可能为空（曾被 ime disable 过），
        所以依次尝试：default_input_method → enabled_input_methods →
        ime list -s → ime list -a 的第一行。"""
        assert self.dev is not None

        cur = self.dev.shell("settings get secure default_input_method").strip()
        if cur and cur != "null" and "/" in cur:
            return cur

        enabled = self.dev.shell("settings get secure enabled_input_methods").strip()
        if enabled and enabled != "null":
            first = enabled.split(":")[0].split(";")[0].strip()
            if "/" in first:
                return first

        listed = self.dev.shell("ime list -s").strip()
        for line in listed.splitlines():
            line = line.strip()
            if "/" in line:
                return line

        # ime list -a 输出形如 "com.pkg/.Service:" 的多段详情
        full = self.dev.shell("ime list -a")
        for line in full.splitlines():
            line = line.strip().rstrip(":")
            if "/" in line and not line.startswith("m"):
                return line
        return ""

    def _act_wait_activity(self, step, r):
        assert self.dev is not None
        timeout = float(step.get("timeout", 30))
        t0 = time.time()
        while time.time() - t0 < timeout:
            self._check_alive(f"等 Activity 含 {step['contains']!r}")
            if step["contains"] in self.dev.current_activity():
                return {"activity": self.dev.current_activity(),
                        "elapsed": round(time.time() - t0, 2)}
            time.sleep(0.5)
        raise AssertionError(
            f"等待 Activity 含 {step['contains']!r} 超时，实际 {self.dev.current_activity()}")

    def _act_wait_stable(self, step, r):
        """等画面稳定（不再变化）—— 用于「等加载/等动画结束」。"""
        assert self.dev is not None
        ok, elapsed, frame = vision.wait_until_stable(
            self.dev,
            timeout=float(step.get("timeout", 60)),
            settle=float(step.get("settle", 2.0)),
            interval=float(step.get("interval", 0.8)),
            threshold=float(step.get("threshold", 0.01)),
        )
        if not ok and step.get("must_stable", False):
            raise AssertionError(f"画面 {elapsed:.1f}s 内未稳定")
        return {"stable": ok, "elapsed": round(elapsed, 2)}

    def _act_wait_change(self, step, r):
        """等画面发生变化 —— 用于「点击后确认有反应」。"""
        assert self.dev is not None
        box = self._box(step)
        timeout = float(step.get("timeout", 30))
        threshold = float(step.get("threshold", 0.01))
        interval = float(step.get("interval", 0.6))
        base = self._frame()
        t0 = time.time()
        while time.time() - t0 < timeout:
            self._check_alive("等画面变化")
            time.sleep(interval)
            cur = self._frame()
            d = vision.mean_abs_diff(base, cur, 8, box)
            if d >= threshold:
                return {"changed": True, "diff": round(d, 4),
                        "elapsed": round(time.time() - t0, 2)}
        if step.get("must_change", True):
            raise AssertionError(f"{timeout}s 内画面无变化（判定无响应/卡死）")
        return {"changed": False, "diff": 0.0}

    def _act_wait_stuck(self, step, r):
        """
        等画面「卡住」—— 用于验证某状态确实停滞。
        samples 个采样点里一次都没变 → 判定卡死。
        """
        assert self.dev is not None
        w = vision.ScreenWatcher(self.dev,
                          interval=float(step.get("interval", 1.0)),
                          samples=int(step.get("samples", 6)),
                          box=self._box(step))
        v = w.observe()
        if step.get("must_stuck") and not v.stuck:
            raise AssertionError(
                f"画面仍在变化（changed={v.changed_samples}/{v.samples}），"
                f"未达到预期卡死状态")
        if step.get("must_move") and v.stuck:
            raise AssertionError(f"画面完全静止 {v.samples} 个采样点，疑似卡死")
        return v.summary()

    def _check_alive(self, where: str) -> None:
        """等待类步骤的进程存活看护。

        游戏崩掉时进程会直接消失，但画面停在最后一帧，模板匹配会一直失败，
        最后报「timeout 内未匹配到模板」——把崩溃误报成画面不符。
        每轮顺手查一次 pidof，发现进程没了立刻抛错，省下几分钟空等。
        """
        if not self._guard_pkg or self.dev is None:
            return
        try:
            running = self.dev.is_running(self._guard_pkg)
        except Exception:
            return  # 查询本身失败（如设备抖动）不该改变流程结论
        if not running:
            raise AssertionError(
                f"{where} 期间进程 {self._guard_pkg} 已消失（崩溃或被系统杀掉）；"
                f"当前前台 {self.dev.current_activity() or '(无)'}。"
                f"看 logcat 里的 Fatal signal / FATAL EXCEPTION 定位")

    def _act_wait_template(self, step, r):
        """等模板出现在画面上（模板图存 assets/ 或绝对路径）。

        save：把是否找到写进变量（True/False），配合 must_find=false 可做分支。
        """
        assert self.dev is not None
        tpl_path = self._resolve_asset(step["template"])
        tpl = vision.Frame.load(tpl_path)
        timeout = float(step.get("timeout", 30))
        threshold = float(step.get("threshold", 0.85))
        box = self._box(step)
        scale = int(step.get("scale", 2))
        save_as = step.get("save")
        t0 = time.time()
        best = -1.0
        while time.time() - t0 < timeout:
            self._check_alive(f"等 {os.path.basename(tpl_path)}")
            f = self._frame()
            score, (x, y) = vision.match_template(f, tpl, box=box, scale=scale)
            best = max(best, score)
            if score >= threshold:
                if step.get("tap", False):
                    # match_template 返回的 (x, y) 已经是物理像素的模板左上角，
                    # 中心直接加模板原始宽高的一半。注意不要再除以 scale ——
                    # scale 只影响搜索时的降采样，坐标已换算回物理像素。
                    self.dev.tap(x + tpl.width // 2, y + tpl.height // 2)
                if save_as:
                    self.vars[save_as] = True
                return {"found": True, "score": round(score, 4),
                        "at": [x, y], "elapsed": round(time.time() - t0, 2)}
            time.sleep(float(step.get("interval", 0.8)))
        if save_as:
            self.vars[save_as] = False
        if step.get("must_find", True):
            raise AssertionError(
                f"{timeout}s 内未匹配到模板 {os.path.basename(tpl_path)}"
                f"（最高得分 {best:.3f} < {threshold}）")
        return {"found": False, "score": round(best, 4)}

    def _act_dismiss_tutorial(self, step, r):
        """轮询关掉新手引导浮层（它会在主城出现后**延迟**弹出，不是一进主城就有）。

        踩过的坑：主城加载完立刻截图，红 X 还没出来，`tap_color must_find=false`
        直接跳过；1~2 秒后引导浮层才弹出并拦截所有点击，后续所有入口全部点空，
        但 close_panel 又会因为「一开始就匹配主城」假通过——整条巡检静默假绿。
        所以这里必须轮询等待浮层出现（或超时确认真的没有）。
        """
        assert self.dev is not None
        color = step.get("color", "red")
        box = self._box(step)
        min_pixels = int(step.get("min_pixels", 300))
        tolerance = step.get("tolerance")
        timeout = float(step.get("timeout", 12))
        poll = float(step.get("interval", 0.8))
        max_taps = int(step.get("max_taps", 3))

        t0 = time.time()
        taps = 0
        while time.time() - t0 < timeout:
            f = self._frame()
            blob = vision.find_color_blob(f, color, box=box, tolerance=tolerance,
                                          min_pixels=min_pixels)
            if blob is None:
                if taps > 0:
                    # 已经点掉了，再等一轮确认没重新弹出
                    time.sleep(poll)
                    f2 = self._frame()
                    if vision.find_color_blob(f2, color, box=box, tolerance=tolerance,
                                              min_pixels=min_pixels) is None:
                        return {"dismissed": True, "taps": taps,
                                "_message": f"已点掉引导浮层（{taps} 次）"}
                # 还没出现：继续轮询等它弹出来（它是延迟弹的，早查一定查不到）
                time.sleep(poll)
                continue
            if taps >= max_taps:
                break
            self.dev.tap(*blob["center"])
            taps += 1
            time.sleep(1.0)
        if taps == 0:
            return {"dismissed": False, "taps": 0,
                    "_message": f"{timeout}s 内未出现引导浮层，跳过"}
        raise AssertionError(
            f"引导浮层点了 {taps} 次仍然存在（{color} 色块 box={box}）——"
            f"可能点到了别处的同色元素，或浮层有别的关闭方式")

    def _act_probe_entry(self, step, r):
        """点一个入口，区分三种结果：面板打开 / 没反应 / 进程崩溃。

        Unity 游戏里「点完画面变了」不等于「面板打开了」——也可能是崩溃退回桌面。
        主城模板得分掉到 0.85 以下时，普通 tap 无法区分这两种情况，
        会把崩溃误报成「面板打开」。这个 action 先查进程存活再判画面。
        """
        assert self.dev is not None
        if "ratio" in step:
            rx, ry = step["ratio"]
            self.dev.tap_ratio(float(rx), float(ry))
            at = f"比例 ({rx}, {ry})"
        else:
            self.dev.tap(int(step["x"]), int(step["y"]))
            at = f"({step['x']}, {step['y']})"
        time.sleep(float(step.get("wait", 1.8)))
        pkg = step.get("package") or self._guard_pkg
        if pkg and not self.dev.is_running(pkg):
            raise AssertionError(
                f"点 {at} 后进程 {pkg} 已消失（崩溃）；当前前台 {self.dev.current_activity()}。"
                f"这不是「面板打开了」，是闪退——看 logcat 里的 Fatal signal / stack corruption")
        expect = step.get("expect")
        if expect:
            tpl = vision.Frame.load(self._resolve_asset(expect))
            score = max(vision.match_template(self._frame(), tpl,
                                              box=self._box(step),
                                              scale=int(step.get("scale", 2)))[0]
                        for _ in range(int(step.get("samples", 2))))
            opened = score < float(step.get("threshold", 0.85))
            if step.get("must_open", True) and not opened:
                raise AssertionError(
                    f"点 {at} 后画面没变化（{os.path.basename(expect)} 仍得分 {score:.3f} ≥ "
                    f"{step.get('threshold', 0.85)}）——坐标偏了、被浮层挡住，或这个入口还没解锁")
            return {"opened": opened, "score": round(score, 4),
                    "_message": f"点 {at} → {'面板打开' if opened else '没反应'}（得分 {score:.3f}）"}
        return {"_message": f"点 {at}（未指定 expect，只做了存活检查）"}

    def _act_assert_gone(self, step, r):
        """断言某个模板**不在**画面上（用于「面板确实打开了」这种负向断言）。

        为什么需要它：点入口后画面如果没变化（点偏了、被浮层挡住），
        后续 close_panel 会因为「一开始就匹配主城」直接判成功，
        整条流程假通过。用 assert_gone 断言主城模板消失，才能真正证明面板打开了。
        """
        assert self.dev is not None
        tpl_path = self._resolve_asset(step["template"])
        tpl = vision.Frame.load(tpl_path)
        threshold = float(step.get("threshold", 0.90))
        box = self._box(step)
        scale = int(step.get("scale", 2))
        timeout = float(step.get("timeout", 8))
        poll = float(step.get("interval", 0.6))

        t0 = time.time()
        best = 1.0
        samples = int(step.get("samples", 3))
        while time.time() - t0 < timeout:
            # 连拍 samples 帧取**最大值**：主城这类画面带云层动画，
            # 单帧得分会在 0.86~0.96 之间抖，只看一帧会等到动画低谷就误判「面板已打开」。
            scores = []
            for _ in range(max(1, samples)):
                f = self._frame()
                scores.append(vision.match_template(f, tpl, box=box, scale=scale)[0])
            score = max(scores)
            best = min(best, score)
            if score < threshold:
                return {"gone": True, "score": round(score, 4),
                        "elapsed": round(time.time() - t0, 2),
                        "_message": f"{os.path.basename(tpl_path)} 已消失"
                                    f"（{len(scores)} 帧最高得分 {score:.3f} < {threshold}），画面确实变了"}
            time.sleep(poll)
        raise AssertionError(
            f"{os.path.basename(tpl_path)} 仍然在画面上（最低得分 {best:.3f} ≥ "
            f"{threshold}）——点击可能没生效：坐标偏了、被浮层挡住，或按钮还没加载完。"
            f"这会让后续 close_panel 假通过，所以这里直接判失败")

    def _act_probe_template(self, step, r):
        """只看一帧判断模板在不在，不等。用于「这条路径该不该走」的分支判断。

        结果写进 vars[step['save']]（默认 PRESENT）。比 wait_template 快，
        但只适合「画面已经稳定」的时机；画面还在动就用 wait_template。
        """
        assert self.dev is not None
        tpl_path = self._resolve_asset(step["template"])
        tpl = vision.Frame.load(tpl_path)
        threshold = float(step.get("threshold", 0.85))
        box = self._box(step)
        scale = int(step.get("scale", 2))
        f = self._frame()
        score, (x, y) = vision.match_template(f, tpl, box=box, scale=scale)
        present = score >= threshold
        save_as = step.get("save", "PRESENT")
        self.vars[save_as] = present
        self.vars[save_as + "_score"] = round(score, 4)
        return {"found": present, "score": round(score, 4), "at": [x, y],
                "_message": f"{'找到' if present else '未找到'} "
                            f"{os.path.basename(tpl_path)}（得分 {score:.3f}）"}

    def _act_wait_text(self, step, r):
        """等原生控件文字出现（只对原生 UI 有效，Unity 画面无效）。"""
        assert self.dev is not None
        ok = self.dev.tap_text(step["text"], exact=step.get("exact", False),
                               timeout=float(step.get("timeout", 15))) \
            if step.get("tap", False) else False
        if not step.get("tap", False):
            timeout = float(step.get("timeout", 15))
            t0 = time.time()
            ok = False
            while time.time() - t0 < timeout:
                self._check_alive(f"等文字 {step['text']!r}")
                nodes = self.dev.find(text=step["text"], exact=step.get("exact", False))
                if nodes:
                    ok = True
                    break
                time.sleep(0.5)
        if not ok and step.get("must_find", True):
            raise AssertionError(f"未找到文字 {step['text']!r}")
        return {"found": ok}

    def _act_screenshot(self, step, r):
        name = step.get("name") or f"shot-{self._step_index:02d}"
        path = self._capture(name)
        if not path:
            raise AssertionError("截图失败")
        return {"file": path, "_message": f"截图 {path}"}

    def _act_ocr(self, step, r):
        """
        识别数字区域。模板需先用 DigitOCR.harvest 自举并保存为 npz。
        结果写入 vars[step['save']]。
        """
        assert self.dev is not None
        tpl_path = self._resolve_asset(step["template"])
        ocr = vision.DigitOCR.load(tpl_path)
        f = self._frame()
        box = self._box(step)
        text = ocr.read(f, box, threshold=float(step.get("threshold", 0.72)))
        save_as = step.get("save")
        if save_as:
            self.vars[save_as] = text
            # 额外抽出数字，方便断言比较
            nums = re.findall(r"\d+", text)
            if nums:
                self.vars[save_as + "_num"] = int(nums[0])
        if step.get("must_match") and not re.search(step["must_match"], text):
            raise AssertionError(f"OCR 结果 {text!r} 不匹配 /{step['must_match']}/")
        return {"text": text, "_message": f"OCR={text!r}"}

    def _act_assert(self, step, r):
        """断言表达式为真；失败记为 fail 但默认不中断。"""
        expr = step["expr"]
        ok = bool(SafeExpr.eval(expr, self.vars))
        detail = {"expr": expr, "result": ok}
        if not ok:
            raise AssertionError(step.get("message") or f"断言失败：{expr}")
        return detail

    def _act_set(self, step, r):
        """设置变量：{"action":"set","name":"x","expr":"..."}"""
        val = SafeExpr.eval(step["expr"], self.vars) if "expr" in step else step.get("value")
        self.vars[step["name"]] = val
        return {"_message": f"{step['name']} = {val!r}"}

    def _act_logcat_mark(self, step, r):
        self._last_logcat_ts = time.time()
        return {"_message": "已标记 logcat 时间点"}

    def _act_check_crash(self, step, r):
        """扫描自上次标记以来的 logcat，找崩溃/异常。

        良性告警（BENIGN_PATTERNS，如 Unity 序列化布局差异）单独记录到
        result.benign_events，不计入崩溃、不影响 passed。
        """
        assert self.dev is not None
        lines = self.dev.logcat_since(self._last_logcat_ts)
        found = self.dev.scan_crashes(lines)
        benign = self.dev.scan_crashes(lines, include_benign=True)
        benign = [e for e in benign if e.get("benign")]
        if benign:
            self._merge_benign(benign)
        if found:
            self._merge_crashes(found)
            if step.get("must_clean", True):
                kinds = sorted({e["kind"] for e in found})
                raise AssertionError(f"发现 {len(found)} 条崩溃/异常日志：{kinds}")
        return {"crashes": len(found),
                "kinds": sorted({e["kind"] for e in found}),
                "benign": len(benign)}

    def _act_dump_ui(self, step, r):
        assert self.dev is not None
        nodes = self.dev.ui_nodes()
        name = step.get("name") or f"ui-{self._step_index:02d}"
        path = os.path.join(self.art_dir, f"{name}.xml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.dev.ui_dump())
        return {"nodes": len(nodes), "file": os.path.relpath(path, self.art_dir)}

    def _act_dump_logcat(self, step, r):
        assert self.dev is not None
        lines = self.dev.logcat_since(self._last_logcat_ts)
        name = step.get("name") or f"logcat-{self._step_index:02d}"
        path = os.path.join(self.art_dir, f"{name}.log")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        return {"lines": len(lines), "file": os.path.relpath(path, self.art_dir)}

    def _act_ocr_harvest(self, step, r):
        """
        从当前帧自举数字模板（把已知文本切成字形存起来）。
        用于在新分辨率/新皮肤下快速建立 OCR 模板。
        """
        f = self._frame()
        ocr = vision.DigitOCR()
        n = ocr.harvest(f, self._box(step), step["text"])
        if n == 0:
            raise AssertionError(
                f"未能切出字符（期望 {len(step['text'])} 个，区域可能不对）")
        out = self._resolve_asset(step["save"], must_exist=False)
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        ocr.save(out)
        return {"chars": list(ocr.templates), "file": out,
                "_message": f"自举 {n} 个字形 -> {out}"}

    # -- 辅助 ---------------------------------------------------------

    def _resolve_asset(self, name: str, must_exist: bool = True) -> str:
        if os.path.isabs(name):
            path = name
        elif os.path.exists(name):
            path = os.path.abspath(name)
        else:
            path = os.path.join(ASSETS, name)
        if must_exist and not os.path.exists(path):
            raise AssertionError(f"资源文件不存在：{path}")
        return path

    def _merge_crashes(self, found: list[dict]) -> None:
        """按 kind 合并崩溃事件：同类累加 count（统一用 int），保留首条样本。"""
        index = {e["kind"]: e for e in self.result.crash_events}
        for ev in found:
            kind = ev.get("kind", "?")
            try:
                n = int(ev.get("count") or 1)
            except (TypeError, ValueError):
                n = 1
            if kind in index:
                cur = index[kind]
                try:
                    cur_n = int(cur.get("count") or 1)
                except (TypeError, ValueError):
                    cur_n = 1
                cur["count"] = cur_n + n
            else:
                ev = dict(ev)
                ev["count"] = n
                self.result.crash_events.append(ev)
                index[kind] = ev

    def _merge_benign(self, found: list[dict]) -> None:
        """按 kind 合并良性告警：只统计次数，不影响 passed。"""
        index = {e["kind"]: e for e in self.result.benign_events}
        for ev in found:
            kind = ev.get("kind", "?")
            try:
                n = int(ev.get("count") or 1)
            except (TypeError, ValueError):
                n = 1
            if kind in index:
                cur = index[kind]
                try:
                    cur_n = int(cur.get("count") or 1)
                except (TypeError, ValueError):
                    cur_n = 1
                cur["count"] = cur_n + n
            else:
                ev = dict(ev)
                ev["count"] = n
                self.result.benign_events.append(ev)
                index[kind] = ev

    def _merge_defects(self, found: list[dict]) -> None:
        """按 kind 合并待修缺陷：只统计次数，不影响 passed（跑测照常判绿）。"""
        index = {e["kind"]: e for e in self.result.defect_events}
        for ev in found:
            kind = ev.get("kind", "?")
            try:
                n = int(ev.get("count") or 1)
            except (TypeError, ValueError):
                n = 1
            if kind in index:
                cur = index[kind]
                try:
                    cur_n = int(cur.get("count") or 1)
                except (TypeError, ValueError):
                    cur_n = 1
                cur["count"] = cur_n + n
            else:
                ev = dict(ev)
                ev["count"] = n
                self.result.defect_events.append(ev)
                index[kind] = ev

    def _collect_crashes(self) -> None:
        if not self.dev:
            return
        try:
            lines = self.dev.logcat_since(self._last_logcat_ts)
            found = self.dev.scan_crashes(lines)
            benign = [e for e in self.dev.scan_crashes(lines, include_benign=True)
                      if e.get("benign")]
            if benign:
                self._merge_benign(benign)
            defects = self.dev.scan_defects(lines)
            if defects:
                self._merge_defects(defects)
                self.log(f"⚠ 扫到 {len(defects)} 类需修复的缺陷日志（不崩溃，不影响判定）")
            self._merge_crashes(found)
            if found:
                self.result.crashed = True
                path = os.path.join(self.art_dir, "crash-logcat.log")
                with open(path, "w", encoding="utf-8") as f:
                    f.write("\n".join(lines))
                self.log(f"⚠ 收尾扫到 {len(found)} 条崩溃日志 -> crash-logcat.log")
        except Exception as e:
            self.log(f"崩溃扫描失败（不致命）：{e}")

    # -- 报告 ---------------------------------------------------------

    def _write_reports(self) -> None:
        os.makedirs(self.art_dir, exist_ok=True)
        summary = self.result.summary()

        with open(os.path.join(self.art_dir, "report.json"), "w", encoding="utf-8") as f:
            json.dump({"summary": summary,
                       "steps": [s.to_dict() for s in self.result.steps]},
                      f, ensure_ascii=False, indent=2)

        with open(os.path.join(self.art_dir, "run.log"), "w", encoding="utf-8") as f:
            f.write("\n".join(self._log_lines))

        self._write_junit(summary)
        self._write_html(summary)

        # 维护 runs/latest 软链（方便 agent 直接读最新一次结果）
        latest = os.path.join(self.out_dir, "latest")
        try:
            if os.path.islink(latest) or os.path.exists(latest):
                if os.path.islink(latest):
                    os.unlink(latest)
                else:
                    shutil.rmtree(latest)
            os.symlink(self.run_id, latest)
        except OSError:
            pass

        self.log(f"报告已写入 {self.art_dir}")
        self.log(f"结论：{'✅ 全部通过' if self.result.passed else '❌ 存在问题'}")

    def _write_junit(self, summary: dict) -> None:
        from xml.sax.saxutils import escape, quoteattr

        fails = len(self.result.failed) + (1 if self.result.crashed else 0)
        cases = []
        for s in self.result.steps:
            cls = quoteattr(f"{self.result.flow_name}.{s.action}")
            nm = quoteattr(s.step_id)
            t = f"{s.duration:.3f}"
            if s.status in (STATUS_PASS, STATUS_SKIP):
                body = f'    <testcase classname={cls} name={nm} time="{t}"/>\n'
                if s.status == STATUS_SKIP:
                    body = (f'    <testcase classname={cls} name={nm} time="{t}">'
                            f'<skipped message={quoteattr(s.message)}/></testcase>\n')
            else:
                body = (f'    <testcase classname={cls} name={nm} time="{t}">'
                        f'<failure message={quoteattr(s.message)}>'
                        f'{escape(json.dumps(s.detail, ensure_ascii=False))}'
                        f'</failure></testcase>\n')
            cases.append(body)
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<testsuite name={quoteattr(self.result.flow_name)} '
            f'tests="{summary["total"]}" failures="{fails}" '
            f'time="{summary["duration"]}" '
            f'timestamp="{self.result.started_at}">\n'
            + "".join(cases)
            + "</testsuite>\n"
        )
        with open(os.path.join(self.art_dir, "junit.xml"), "w", encoding="utf-8") as f:
            f.write(xml)

    def _write_html(self, summary: dict) -> None:
        rows = []
        for s in self.result.steps:
            color = {"pass": "#1a7f37", "fail": "#cf222e",
                     "error": "#9a6700", "skip": "#6e7781"}.get(s.status, "#333")
            shot = (f'<a href="{s.screenshot}">图</a>' if s.screenshot else "")
            rows.append(
                f'<tr><td>{s.index + 1}</td><td><code>{_h(s.step_id)}</code></td>'
                f'<td><code>{_h(s.action)}</code></td>'
                f'<td style="color:{color};font-weight:600">{s.status}</td>'
                f'<td>{s.duration:.2f}s</td>'
                f'<td>{_h(s.message)}</td><td>{shot}</td></tr>'
            )
        crashes = ""
        if self.result.crash_events:
            items = "".join(
                f'<li><code>{_h(e.get("kind", "?"))}</code>'
                + (f' <b>×{_h(e.get("count"))}</b>' if e.get("count") else "")
                + f' <span style="color:#6e7781">{_h(e.get("line", "")[:300])}</span></li>'
                for e in self.result.crash_events[:50]
            )
            crashes = (f"<h2>崩溃/异常日志（{len(self.result.crash_events)} 类）</h2>"
                       f"<ul>{items}</ul>")

        defects = ""
        if self.result.defect_events:
            ditems = "".join(
                f'<li><code>{_h(e.get("kind", "?"))}</code>'
                + (f' <b>×{_h(e.get("count"))}</b>' if e.get("count") else "")
                + f' <span style="color:#6e7781">{_h(e.get("line", "")[:300])}</span></li>'
                for e in self.result.defect_events[:20]
            )
            defects = (f"<h2>待修缺陷（{len(self.result.defect_events)} 类，"
                       f"有错误日志但不崩溃，不影响判定，需客户端修）</h2>"
                       f"<ul>{ditems}</ul>")

        benign = ""
        if self.result.benign_events:
            bitems = "".join(
                f'<li><code>{_h(e.get("kind", "?"))}</code>'
                + (f' <b>×{_h(e.get("count"))}</b>' if e.get("count") else "")
                + f' <span style="color:#6e7781">{_h(e.get("line", "")[:300])}</span></li>'
                for e in self.result.benign_events[:20]
            )
            benign = (f"<h2>良性告警（{len(self.result.benign_events)} 类，不计入失败）</h2>"
                      f"<ul>{bitems}</ul>")

        verdict = "✅ 全部通过" if self.result.passed else "❌ 存在问题"
        vcolor = "#1a7f37" if self.result.passed else "#cf222e"
        html = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>跑测报告 · {_h(self.result.flow_name)} · {self.run_id}</title>
<style>
 body{{font:14px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;
      margin:32px;color:#1f2328;background:#fff}}
 h1{{font-size:20px;margin:0 0 4px}} h2{{font-size:16px;margin:24px 0 8px}}
 .meta{{color:#6e7781;font-size:13px;margin-bottom:16px}}
 .verdict{{font-size:16px;font-weight:600;color:{vcolor};margin:12px 0}}
 table{{border-collapse:collapse;width:100%;font-size:13px}}
 th,td{{border:1px solid #d0d7de;padding:6px 8px;text-align:left;vertical-align:top}}
 th{{background:#f6f8fa}} code{{font-family:ui-monospace,SFMono-Regular,monospace;
      font-size:12px;background:#f6f8fa;padding:1px 4px;border-radius:3px}}
 tr:nth-child(even) td{{background:#fcfcfd}}
</style></head><body>
<h1>跑测报告 · {_h(self.result.flow_name)}</h1>
<div class="meta">run_id {self.run_id} · 设备 {_h(self.result.device)} ·
 开始 {self.result.started_at} · 耗时 {summary["duration"]}s · 产物 <code>{_h(self.art_dir)}</code></div>
<div class="verdict">{verdict}</div>
<table>
<tr><th>#</th><th>步骤</th><th>动作</th><th>结果</th><th>耗时</th><th>说明</th><th>截图</th></tr>
{"".join(rows)}
</table>
{crashes}
{defects}
{benign}
</body></html>
"""
        with open(os.path.join(self.art_dir, "index.html"), "w", encoding="utf-8") as f:
            f.write(html)


def _h(s: Any) -> str:
    from html import escape

    return escape(str(s))


# ---------------------------------------------------------------- CLI

def load_flow(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        flow = json.load(f)
    if not isinstance(flow, dict):
        raise ValueError("流程文件根节点必须是对象")
    if "steps" not in flow or not isinstance(flow["steps"], list):
        raise ValueError("流程文件缺少 steps 数组")
    for i, s in enumerate(flow["steps"]):
        if not isinstance(s, dict):
            raise ValueError(f"第 {i} 个步骤不是对象")
        if "action" not in s:
            raise ValueError(f"第 {i} 个步骤缺少 action")
        if "expr" in s:
            SafeExpr.check(s["expr"])
    return flow


def validate_only(flow: dict) -> int:
    """dry-run：校验流程结构与表达式，不碰设备。"""
    known = {n[5:] for n in dir(FlowRunner) if n.startswith("_act_")}
    problems = 0
    print(f"流程：{flow.get('name')}  步骤数={len(flow['steps'])}")
    for i, s in enumerate(flow["steps"]):
        act = s["action"]
        if act not in known:
            print(f"  ❌ [{i}] 未知 action {act!r}；可用：{sorted(known)}")
            problems += 1
        else:
            print(f"  ✅ [{i}] {s.get('id', f'step{i}')} -> {act}")
    print(f"校验结果：{'通过' if problems == 0 else f'{problems} 个问题'}")
    return 0 if problems == 0 else 1


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="JSON 步骤流跑测引擎")
    ap.add_argument("flow", help="流程 JSON 文件路径")
    ap.add_argument("--serial", help="设备序列号（多设备时必填）")
    ap.add_argument("--out", default=DEFAULT_RUNS, help="产物根目录")
    ap.add_argument("--dry-run", action="store_true", help="只校验流程，不执行")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--json", action="store_true", help="末尾输出一行 JSON 摘要")
    ap.add_argument("--var", action="append", default=[], metavar="NAME=VALUE",
                    help="覆盖流程里的 vars，可重复：--var ACCOUNT=demo03")
    args = ap.parse_args(argv)

    try:
        flow = load_flow(args.flow)
    except Exception as e:
        print(f"❌ 流程文件无效：{e}", file=sys.stderr)
        return 2

    if args.dry_run:
        return validate_only(flow)

    overrides: dict[str, str] = {}
    for item in args.var:
        if "=" not in item:
            print(f"❌ --var 需要 NAME=VALUE 形式，收到 {item!r}", file=sys.stderr)
            return 2
        k, v = item.split("=", 1)
        overrides[k.strip()] = v.strip()
    if overrides:
        flow.setdefault("vars", {}).update(overrides)

    runner = FlowRunner(flow, args.flow, serial=args.serial, out_dir=args.out,
                        verbose=not args.quiet)
    res = runner.run()
    if args.json:
        print(json.dumps(res.summary(), ensure_ascii=False))
    return 0 if res.passed else 1


if __name__ == "__main__":
    sys.exit(main())
