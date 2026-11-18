#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
selftest.py — 驱动层能力自检

验证 adbkit / vision / runner 三层在**当前这台设备**上每项能力都真的可用。
和 doctor.py 的分工：
  doctor.py   → 环境体检（设备在不在、屏幕亮不亮、电量够不够），回答「能不能开跑」
  selftest.py → 能力自检（每个 API 真调一次并断言结果），回答「这层代码本身靠不靠得住」

用法：
    python scripts/selftest.py                 # 全部用例
    python scripts/selftest.py --skip-slow     # 跳过耗时的用例（模板匹配/跑测引擎）
    python scripts/selftest.py --pkg com.example.game   # 指定「已安装应用检测」用例用的包名
    python scripts/selftest.py --json

退出码 0 = 全部通过。
环境变量 GAT_PKG 也可以指定上面那个包名；不指定则该用例自动跳过。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import adbkit  # noqa: E402
import vision  # noqa: E402

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
_PKG = os.environ.get("GAT_PKG")  # 「已安装应用检测」用例的目标包名
_results: list[dict] = []


def case(name: str, slow: bool = False):
    """注册一个用例；slow=True 的用例会被 --skip-slow 跳过。"""
    def deco(fn):
        fn._case_name = name
        fn._slow = slow
        return fn
    return deco


def run_case(fn, dev, skip_slow: bool) -> None:
    name = fn._case_name
    if skip_slow and fn._slow:
        _results.append({"name": name, "status": SKIP, "detail": "跳过（--skip-slow）"})
        return
    t0 = time.time()
    try:
        detail = fn(dev) or ""
        _results.append({"name": name, "status": PASS,
                         "detail": f"{detail} ({time.time() - t0:.2f}s)"})
    except Exception as e:  # noqa: BLE001
        _results.append({"name": name, "status": FAIL,
                         "detail": f"{type(e).__name__}: {e}",
                         "trace": traceback.format_exc()})


# ---------------------------------------------------------------- 用例

@case("shell 基础命令")
def t_shell(dev):
    assert dev.shell("echo ok").strip() == "ok"
    # grep 无匹配是退出码 1，不应抛异常（这是修过的真实缺陷）
    out = dev.shell("echo abc | grep -c xyz")
    assert out.strip() in ("0", ""), f"grep 无匹配应返回 0/空，实际 {out!r}"
    return "echo ok / grep 无匹配不抛异常"


@case("屏幕尺寸与密度")
def t_size(dev):
    w, h = dev.size
    dpi = dev.density
    assert w > 0 and h > 0 and dpi > 0
    return f"{w}x{h} @ {dpi}dpi"


@case("raw 截图 + 帧解码")
def t_screencap(dev):
    raw = dev.screencap_raw()
    assert len(raw) > 16
    f = vision.Frame.from_raw(raw)
    w, h = dev.size
    assert (f.width, f.height) == (w, h), f"帧尺寸 {f.width}x{f.height} 与设备 {w}x{h} 不符"
    return f"{len(raw)}B -> {f.width}x{f.height}"


@case("PNG 截图")
def t_png(dev):
    png = dev.screencap_png()
    assert png[:8] == b"\x89PNG\r\n\x1a\n", "不是合法 PNG"
    return f"{len(png)}B"


@case("帧灰度/dhash/保存")
def t_frame_ops(dev):
    f = vision.Frame.from_raw(dev.screencap_raw())
    g = f.gray(8)
    assert g.shape[0] > 0
    sig = vision.dhash(f)
    assert isinstance(sig, str) and len(sig) == 16, f"dhash 格式异常 {sig!r}"
    tmp = Path(tempfile.gettempdir()) / "gat_selftest.png"
    f.save(tmp)
    assert tmp.exists() and tmp.stat().st_size > 1000
    return f"gray{g.shape} dhash={sig} png={tmp.stat().st_size}B"


@case("帧差（同帧应为 0）")
def t_framediff(dev):
    raw = dev.screencap_raw()
    f1 = vision.Frame.from_raw(raw)
    f2 = vision.Frame.from_raw(raw)
    diff = vision.mean_abs_diff(f1, f2)
    assert diff < 1e-6, f"同一帧差应为 0，实际 {diff}"
    # 同屏判定也必须是 True
    assert vision.same_screen(f1, f2) is True
    return f"mean_abs_diff={diff} same_screen=True"


@case("区域帧差（box 裁剪）")
def t_regiondiff(dev):
    raw = dev.screencap_raw()
    f = vision.Frame.from_raw(raw)
    box = (0.0, 0.0, 0.5, 0.5)
    sub = f.crop_ratio(*box)          # (rx, ry, rw, rh) -> ndarray
    assert sub.size > 0 and sub.ndim == 3
    # 物理像素版 crop(x, y, w, h)
    px = f.crop(10, 20, 100, 80)
    assert px.shape[0] == 80 and px.shape[1] == 100, f"crop 尺寸异常 {px.shape}"
    return f"crop_ratio{box} -> {sub.shape} / crop(10,20,100,80) -> {px.shape}"


@case("模板匹配（自匹配应为 1.0）", slow=True)
def t_template(dev):
    f = vision.Frame.from_raw(dev.screencap_raw())
    # 取中间一块当模板，再整屏找它
    sub_px = f.crop(int(f.width * 0.3), int(f.height * 0.3),
                    int(f.width * 0.2), int(f.height * 0.2))
    sub = vision.Frame(sub_px.shape[1], sub_px.shape[0], sub_px)
    # 纯色画面（息屏/黑屏/白屏）方差为 0，NCC 无定义，直接跳过而不是判失败
    if float(sub_px[:, :, :3].std()) < 1.0:
        return "跳过（当前画面接近纯色，模板无纹理可比；先解锁/打开应用再测）"
    score, (x, y) = vision.match_template(f, sub)
    assert score > 0.95, f"自匹配得分应接近 1，实际 {score:.3f}"
    return f"score={score:.4f} at ({x},{y})"


@case("NCC 得分必须落在 -1~1（纯色画面不得假匹配）")
def t_ncc_bounds(dev):
    """回归：float32 积分图 + 方差钳 1e-6 曾让纯色画面算出 25.9 的 NCC，
    导致任何模板都能「匹配成功」。这里用合成帧离线验证，不依赖真机画面。"""
    import numpy as np

    flat = vision.Frame(width=320, height=320,
                        pixels=np.full((320, 320, 4), 128, np.uint8))
    rng = np.random.default_rng(233)
    noisy = vision.Frame(width=320, height=320,
                         pixels=rng.integers(0, 256, (320, 320, 4), dtype=np.uint8))
    tpl = vision.Frame(width=80, height=80,
                       pixels=rng.integers(0, 256, (80, 80, 4), dtype=np.uint8))

    cases = [
        ("纯色 vs 随机模板", flat, tpl),
        ("随机画面 vs 随机模板", noisy, tpl),
        ("随机画面 vs 自身", noisy, noisy),
    ]
    for label, frame, template in cases:
        score, _ = vision.match_template(frame, template)
        assert -1.0 - 1e-9 <= score <= 1.0 + 1e-9, \
            f"{label} 的 NCC 得分 {score:.4f} 越界（应在 -1~1）"

    score_flat, _ = vision.match_template(flat, tpl)
    assert score_flat < 0.5, f"纯色画面不应匹配到模板，实际得分 {score_flat:.4f}"
    score_self, _ = vision.match_template(noisy, noisy)
    assert score_self > 0.99, f"自匹配应接近 1，实际 {score_self:.4f}"
    return f"纯色={score_flat:.3f} 自匹配={score_self:.3f}"


@case("颜色定位能找出已知色块中心")
def t_color_blob(dev):
    """回归：Unity 面板的关闭控件位置各不相同，只能靠颜色定位。
    这里用合成帧离线验证 find_color_blob 的中心/像素数/区域限定都对。"""
    import numpy as np

    h, w = 400, 600
    px = np.zeros((h, w, 4), dtype=np.uint8)
    px[:, :, 3] = 255
    px[50:110, 100:180, 0] = 232   # 纯红块 80x60，中心 (140, 80)
    px[50:110, 100:180, 1] = 64
    px[50:110, 100:180, 2] = 42
    f = vision.Frame(width=w, height=h, pixels=px)

    blob = vision.find_color_blob(f, "red", min_pixels=100)
    assert blob is not None, "应当能找到红色块"
    cx, cy = blob["center"]
    assert abs(cx - 140) <= 3 and abs(cy - 80) <= 3, \
        f"中心应在 (140,80) 附近，实际 ({cx},{cy})"
    assert blob["pixels"] == 80 * 60, f"像素数应为 4800，实际 {blob['pixels']}"
    assert vision.find_color_blob(f, "green", min_pixels=100) is None, \
        "画面里没有绿色，应当返回 None"
    assert vision.find_color_blob(f, "red", box=(0.5, 0.0, 0.5, 0.5),
                                  min_pixels=100) is None, \
        "右上区域没有红色，限定 box 后应当返回 None"
    return f"中心=({cx},{cy}) 像素={blob['pixels']} 区域限定生效"


@case("缺陷日志按三类分开归类（崩溃/待修/良性）")
def t_defect_scan(dev):
    """回归：真实跑测里 [PrefabPreloadMissing] 被算成崩溃会把正常流程判红。
    它也不能进良性告警——那是必须交客户端修的真实缺陷。
    这里用假日志离线验证三类各归各位、互不串台。"""
    lines = [
        "11-18 10:34:56.123 23587 23612 E Tuanjie : [PrefabPreloadMissing] className=UI_ItemPanel，"
        "当前没有预载请求（未提前排队或已回收）；资源=main:UI_ItemPanel。",
        "11-18 10:34:56.200 23587 23612 E Tuanjie : [PrefabPreloadMissing] className=UI_ItemPanel，"
        "当前没有预载请求；资源=main:UI_ItemPanel。",
        "11-18 10:34:56.100 23587 23612 E Tuanjie : A scripted object (probably Foo?) has a "
        "different serialization layout when loading. (Read 64 bytes but expected 80 bytes)",
        "11-18 10:35:01.000 23587 23587 F libc    : Fatal signal 6 (SIGABRT), code -1 in tid 1",
        "11-18 10:35:02.000 23587 23587 E AndroidRuntime: FATAL EXCEPTION: main",
    ]
    crashes = adbkit.Device.scan_crashes(None, lines)      # type: ignore[arg-type]
    defects = adbkit.Device.scan_defects(None, lines)      # type: ignore[arg-type]
    benign = adbkit.Device.scan_crashes(None, lines, include_benign=True)  # type: ignore[arg-type]

    ck = {e["kind"] for e in crashes}
    dk = [(e["kind"], e["count"]) for e in defects]
    bk = {e["kind"] for e in benign if e.get("benign")}

    assert "团结引擎错误日志" not in ck,         f"[PrefabPreloadMissing] 不应被算成崩溃，实际崩溃类={ck}"
    assert len(dk) == 1 and "资源预载声明缺失" in dk[0][0], f"待修缺陷归类不对：{dk}"
    assert dk[0][1] == 2, f"应当统计到 2 次，实际 {dk[0][1]}"
    assert any("Fatal signal" in k or "Native" in k for k in ck), f"真崩溃没被抓到：{ck}"
    assert "Java 崩溃" in ck, f"FATAL EXCEPTION 没被抓到：{ck}"
    assert any("序列化布局" in k for k in bk), f"良性告警没被识别：{bk}"
    assert not any("资源预载" in k for k in bk), \
        f"待修缺陷不应混进良性告警：{bk}"
    return f"崩溃={len(ck)}类 待修={dk[0][1]}次 良性={len(bk)}类"


@case("托管栈续行不算崩溃")
def t_stack_frames(dev):
    """回归：GameLogger 打一条 Error 会把调用栈逐行跟出来，每行都带
    `E Tuanjie : ...` 前缀，会被通用兜底当成一条新崩溃。实测一条真实错误
    能刷出 24 条假崩溃，把正常跑测判红。这里用真实日志片段离线验证。"""
    stack = [
        "11-18 10:42:34.112 24985 25009 E Tuanjie : Game.Framework.LogPlugin:Write(EnumLogLevel, String, Exception)",
        "11-18 10:42:34.112 24985 25009 E Tuanjie : Game.Framework.Logger:Dispatch(EnumLogLevel, String, Exception)",
        "11-18 10:42:34.112 24985 25009 E Tuanjie : Game.Framework.Logger:Log(EnumLogLevel, String, Color)",
        "11-18 10:42:34.112 24985 25009 E Tuanjie : Game.Framework.PreloadManager:ReportOpenWithoutPreload(PreloadTarget)",
        # logcat 单行截断后的残帧（没有括号、方法名被砍）——同样要认出来
        "11-18 10:42:34.112 24985 25009 E Tuanjie : Game.UI.ItemBaseIconController:OnC",
    ]
    for ln in stack:
        assert adbkit.is_stack_frame(ln) or \
            any(p.search(ln) for p, _ in adbkit.DEFECT_PATTERNS), \
            f"这行应被判为栈续行/缺陷，实际两者都不是：{ln[:120]}"
    got = adbkit.Device.scan_crashes(None, stack)          # type: ignore[arg-type]
    assert not got, f"纯栈续行不应产生崩溃事件，实际 {[(e['kind'], e['count']) for e in got]}"
    # 一条真崩溃不能被误杀
    real = ["11-18 10:42:35.000 24985 24985 F libc    : Fatal signal 6 (SIGABRT), code -1"]
    got2 = adbkit.Device.scan_crashes(None, real)          # type: ignore[arg-type]
    assert got2, "真崩溃被过滤掉了"
    return f"{len(stack)} 行栈续行 → 0 崩溃；真崩溃仍能抓到"


@case("OCR 模板存取与识别", slow=True)
def t_ocr(dev):
    tpl = Path(__file__).resolve().parent.parent / "assets" / "ocr" / "pct.npz"
    if not tpl.exists():
        return "跳过（无 assets/ocr/pct.npz）"
    ocr = vision.DigitOCR.load(tpl)
    assert ocr.templates, "模板为空"
    return f"字形 {list(ocr.templates)}"


@case("uiautomator dump 解析")
def t_uidump(dev):
    xml = dev.ui_dump()
    assert "<?xml" in xml or "<hierarchy" in xml, "dump 内容不像 XML"
    nodes = adbkit.parse_ui_xml(xml)
    return f"{len(nodes)} 节点"


@case("Activity / 包名检测")
def t_activity(dev):
    act = dev.current_activity()
    if not act:
        return "跳过（当前无前台 Activity；设备可能仍在锁屏或切换中）"
    assert "/" in act, f"Activity 格式异常 {act!r}"
    pkg = dev.current_package()
    assert pkg, "包名为空"
    return f"{pkg} :: {act}"


@case("应用安装检测")
def t_installed(dev):
    pkg = _PKG
    if not pkg:
        return "跳过（用 --pkg 或 GAT_PKG 指定一个已安装的包名）"
    assert dev.is_installed(pkg) is True, f"{pkg} 应已安装"
    assert dev.is_installed("com.definitely.not.installed.xyz") is False
    return f"{pkg} 已装 / 假包名未装 判定正确"


@case("点击 / 按键 / 滑动")
def t_input(dev):
    w, h = dev.size
    dev.tap(w // 2, h // 2)                 # 屏幕中央，通常无副作用
    dev.key("KEYCODE_WAKEUP")
    dev.swipe(w // 2, int(h * 0.75), w // 2, int(h * 0.8), ms=120)
    return "tap / key / swipe 均可执行"


@case("logcat 全量 dump")
def t_logcat(dev):
    rows = dev.logcat_dump(0)
    assert isinstance(rows, list)
    if rows:
        ts, line = rows[-1]
        assert ts > 1_600_000_000, f"时间戳异常 {ts}"
        return f"{len(rows)} 行，末行 ts={ts:.3f}"
    return "0 行（日志刚被清空，属正常）"


@case("logcat 时间窗口（since）")
def t_logcat_since(dev):
    """
    关键回归：曾因 `logcat -t N` 被这台设备当成「N 毫秒之前」而静默返回 0 行，
    导致崩溃检测假阴性。这里用**已有日志行的时间戳**做标记来验证窗口语义，
    不依赖「能否现场制造新日志」（部分机型 toybox log 写不进 logcat）。
    """
    rows = dev.logcat_dump(0)
    if len(rows) < 5:
        return f"仅 {len(rows)} 行，样本不足，跳过窗口语义校验"
    mark = rows[-3][0]                      # 倒数第 3 行的时间戳
    lines = dev.logcat_since(mark)
    assert lines, (
        "since 返回 0 行 —— 崩溃检测会静默漏报。"
        "若设备日志刚被清空可重试；持续为 0 说明窗口逻辑坏了（-t 语义误用）")
    ts = []
    for l in lines:
        head = l.strip().split(" ")[0] if l.strip() else ""
        try:
            ts.append(float(head))
        except ValueError:
            pass
    assert ts, f"since 返回的行解析不出时间戳：{lines[:2]}"
    assert all(t >= mark - 1.0 for t in ts), (
        f"返回了 mark({mark:.3f}) 之前的日志，最早 {min(ts):.3f}")
    assert len(lines) <= len(rows), "since 返回行数不应超过全量"
    return f"mark={mark:.3f} -> {len(lines)} 行（全量 {len(rows)}），时间戳均不早于标记"


@case("崩溃扫描（按类聚合）")
def t_crashscan(dev):
    sample = [
        "  1794899748.280  5985  6058 E Tuanjie : ExecutionEngineException: NotSupportNative2Managed",
        "  1794899748.281  5985  6058 E Tuanjie : ExecutionEngineException: NotSupportNative2Managed",
        "  1794899748.282  5985  6058 E Tuanjie : ",
        "  1794899748.283  5985  6058 I ActivityManager: clean",
    ]
    found = dev.scan_crashes(sample)
    kinds = {f["kind"]: f["count"] for f in found}
    assert any("native→managed" in k for k in kinds), f"未识别 IL2CPP 错误：{kinds}"
    assert all(isinstance(v, int) for v in kinds.values()), f"count 应为 int：{kinds}"
    assert sum(kinds.values()) >= 2, f"聚合计数不对：{kinds}"
    # 纯空白续行不应被算成独立异常
    assert len(found) <= 2, f"聚合失效，返回 {len(found)} 类：{kinds}"
    return f"识别 {len(found)} 类：{kinds}"


@case("设备存活 / 自愈参数")
def t_alive(dev):
    assert dev.alive() is True
    assert dev.retries >= 1 and dev.heal is True
    return f"retries={dev.retries} heal={dev.heal}"


@case("流程引擎 dry-run 校验", slow=True)
def t_runner_dryrun(dev):
    import runner
    root = Path(__file__).resolve().parent.parent
    flow_path = root / "flows" / "smoke.json"
    if not flow_path.exists():
        return "跳过（无 flows/smoke.json）"
    flow = runner.load_flow(str(flow_path))
    rc = runner.validate_only(flow)
    assert rc == 0, f"dry-run 校验返回 {rc}"
    return f"{len(flow.get('steps', []))} 步校验通过"


# ---------------------------------------------------------------- 入口

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="驱动层能力自检")
    ap.add_argument("--serial", default=None)
    ap.add_argument("--skip-slow", action="store_true")
    ap.add_argument("--pkg", default=None, help="已安装应用检测用例用的包名（也可用环境变量 GAT_PKG）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    global _PKG
    if args.pkg:
        _PKG = args.pkg

    cases = [v for k, v in sorted(globals().items())
             if k.startswith("t_") and callable(v) and hasattr(v, "_case_name")]

    try:
        dev = adbkit.Device(args.serial)
    except Exception as e:  # noqa: BLE001
        print(f"❌ 无法连接设备：{e}", file=sys.stderr)
        return 2

    # 息屏时截图全黑、Activity 为空、模板无纹理，会让截图/判定类用例假失败。
    # 先唤醒（不解锁，避免动到别人的锁屏）；仍然锁屏的用例会自己报跳过。
    try:
        dev.wake()
    except Exception as e:  # noqa: BLE001
        print(f"⚠ 唤醒失败（不影响自检）：{e}", file=sys.stderr)

    for fn in cases:
        run_case(fn, dev, args.skip_slow)

    if args.json:
        print(json.dumps({"results": _results}, ensure_ascii=False, indent=2))
    else:
        width = max(len(r["name"]) for r in _results)
        icon = {PASS: "✅", FAIL: "❌", SKIP: "⏭️ "}
        for r in _results:
            print(f"{icon[r['status']]} {r['name']:<{width}}  {r['detail']}")
        n_pass = sum(1 for r in _results if r["status"] == PASS)
        n_fail = sum(1 for r in _results if r["status"] == FAIL)
        n_skip = sum(1 for r in _results if r["status"] == SKIP)
        print()
        print(f"结果：{n_pass} 通过 / {n_fail} 失败 / {n_skip} 跳过")
        if n_fail:
            print("\n失败详情：")
            for r in _results:
                if r["status"] == FAIL:
                    print(f"\n--- {r['name']} ---\n{r['detail']}")
    return 0 if not any(r["status"] == FAIL for r in _results) else 1


if __name__ == "__main__":
    sys.exit(main())
