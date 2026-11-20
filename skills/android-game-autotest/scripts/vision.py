#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
vision.py — 屏幕图像判定层

设计目标：在「Unity 画面在 uiautomator 里 0 节点」的前提下，仅靠截图就能判断
  - 现在停在哪一屏（感知哈希 / 模板匹配）
  - 画面有没有变化、是不是卡死了（帧差 + 稳定窗口）
  - 进度百分比是多少（数字模板 OCR，模板可从真实截图自举）

依赖：numpy + Pillow（无 cv2 / 无 scipy，纯 Python 实现，跨平台）
      装依赖：在技能根目录执行 python -m pip install -r requirements.txt（Python 3.13+）

坐标约定：全部使用设备物理像素，左上原点（与 adb screencap 一致）。
"""

from __future__ import annotations

import hashlib
import os
import struct
import tempfile
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

import numpy as np
from PIL import Image

# ---------------------------------------------------------------- 原始帧解析

# adb exec-out screencap（无 -p）输出：16 字节头 + RGBA8888 像素
#   头结构：<III = width, height, format(1=RGBA_8888) 然后 <I = colorspace
_HEADER_SIZE = 16
_FORMAT_RGBA_8888 = 1


class FrameError(RuntimeError):
    pass


@dataclass
class Frame:
    """一帧屏幕画面。pixels 为 (H, W, 4) uint8 的 RGBA 数组。"""

    width: int
    height: int
    pixels: np.ndarray
    meta: dict = field(default_factory=dict)

    # ---- 构造 / 转换

    @classmethod
    def from_raw(cls, blob: bytes) -> "Frame":
        """解析 adb screencap raw 输出。"""
        if len(blob) < _HEADER_SIZE:
            raise FrameError(f"raw 帧太短：{len(blob)} 字节")
        w, h, fmt = struct.unpack_from("<III", blob, 0)
        colorspace = struct.unpack_from("<I", blob, 12)[0]
        if fmt != _FORMAT_RGBA_8888:
            raise FrameError(f"不支持的像素格式 {fmt}（只处理 RGBA_8888）")
        need = _HEADER_SIZE + w * h * 4
        if len(blob) < need:
            raise FrameError(f"raw 帧不完整：需要 {need}，实际 {len(blob)}")
        px = np.frombuffer(blob, dtype=np.uint8, count=w * h * 4, offset=_HEADER_SIZE)
        px = px.reshape(h, w, 4)
        return cls(w, h, px, {"colorspace": colorspace, "fmt": fmt})

    @classmethod
    def from_png_bytes(cls, blob: bytes) -> "Frame":
        import io

        img = Image.open(io.BytesIO(blob)).convert("RGBA")
        arr = np.asarray(img, dtype=np.uint8)
        return cls(arr.shape[1], arr.shape[0], arr)

    @classmethod
    def load(cls, path: str) -> "Frame":
        with open(path, "rb") as f:
            return cls.from_png_bytes(f.read())

    def to_png_bytes(self) -> bytes:
        import io

        img = Image.fromarray(self.pixels, mode="RGBA")
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=False, compress_level=6)
        return buf.getvalue()

    def save(self, path: str) -> str:
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        with open(path, "wb") as f:
            f.write(self.to_png_bytes())
        return path

    # ---- 派生视图

    def gray(self, scale: int = 1) -> np.ndarray:
        """转灰度 (H, W) uint8；scale>1 时按整数倍降采样（块均值）。"""
        # 用整数权重的定点亮度，避免浮点开销：0.299R+0.587G+0.114B
        p = self.pixels
        g = (p[:, :, 0].astype(np.uint16) * 77
             + p[:, :, 1].astype(np.uint16) * 150
             + p[:, :, 2].astype(np.uint16) * 29) >> 8
        g = g.astype(np.uint8)
        if scale > 1:
            g = downsample(g, scale)
        return g

    def crop(self, x: int, y: int, w: int, h: int) -> np.ndarray:
        """按物理像素裁剪，返回 (h, w, 4) 视图（越界自动夹紧）。"""
        x0 = max(0, min(int(x), self.width))
        y0 = max(0, min(int(y), self.height))
        x1 = max(x0, min(int(x + w), self.width))
        y1 = max(y0, min(int(y + h), self.height))
        return self.pixels[y0:y1, x0:x1]

    def crop_ratio(self, rx: float, ry: float, rw: float, rh: float) -> np.ndarray:
        """按相对比例裁剪（0~1），便于跨分辨率复用。"""
        return self.crop(int(rx * self.width), int(ry * self.height),
                         int(rw * self.width), int(rh * self.height))

    def sha256(self) -> str:
        return hashlib.sha256(self.pixels.tobytes()).hexdigest()

    def __repr__(self) -> str:
        return f"Frame({self.width}x{self.height})"


# ---------------------------------------------------------------- 基础图像运算

def downsample(gray: np.ndarray, scale: int) -> np.ndarray:
    """整数倍块均值降采样，裁掉不能整除的边。"""
    if scale <= 1:
        return gray
    h, w = gray.shape
    h2, w2 = h // scale, w // scale
    if h2 == 0 or w2 == 0:
        return gray
    g = gray[: h2 * scale, : w2 * scale].astype(np.float32)
    g = g.reshape(h2, scale, w2, scale).mean(axis=(1, 3))
    return np.clip(g, 0, 255).astype(np.uint8)


def to_gray_any(img: np.ndarray) -> np.ndarray:
    """把 (H,W,3|4) 或 (H,W) 统一成 (H,W) uint8 灰度。"""
    if img.ndim == 2:
        return img.astype(np.uint8)
    p = img
    g = (p[:, :, 0].astype(np.uint16) * 77
         + p[:, :, 1].astype(np.uint16) * 150
         + p[:, :, 2].astype(np.uint16) * 29) >> 8
    return g.astype(np.uint8)


def mean_abs_diff(a: Frame, b: Frame, scale: int = 8,
                  box: Optional[Sequence[float]] = None) -> float:
    """
    两帧的平均绝对差（0~255），按 scale 降采样后比较。
    box = (rx, ry, rw, rh) 只比较该区域；默认全屏。
    """
    ga = a.gray(scale)
    gb = b.gray(scale)
    if box is not None:
        h, w = ga.shape
        rx, ry, rw, rh = box
        x0, y0 = int(rx * w), int(ry * h)
        x1, y1 = int((rx + rw) * w), int((ry + rh) * h)
        ga = ga[y0:y1, x0:x1]
        gb = gb[y0:y1, x0:x1]
    if ga.shape != gb.shape or ga.size == 0:
        return 255.0
    return float(np.abs(ga.astype(np.int16) - gb.astype(np.int16)).mean())


def block_diff_map(a: Frame, b: Frame, blocks: int = 16) -> np.ndarray:
    """
    分块差异图，返回 (blocks, blocks) 的 float 数组（0~255）。
    用于定位「画面哪一块在变」，例如进度条区域。
    """
    ga = to_gray_any(a.pixels)
    gb = to_gray_any(b.pixels)
    h, w = ga.shape
    bh, bw = max(1, h // blocks), max(1, w // blocks)
    out = np.zeros((blocks, blocks), dtype=np.float32)
    for i in range(blocks):
        for j in range(blocks):
            y0, y1 = i * bh, min(h, (i + 1) * bh)
            x0, x1 = j * bw, min(w, (j + 1) * bw)
            if y1 <= y0 or x1 <= x0:
                continue
            d = np.abs(ga[y0:y1, x0:x1].astype(np.int16)
                       - gb[y0:y1, x0:x1].astype(np.int16))
            out[i, j] = float(d.mean())
    return out


# ---------------------------------------------------------------- 感知哈希

def dhash(frame: Frame, size: int = 8, box=None) -> str:
    """差值哈希：比较相邻像素亮度，抗亮度/压缩变化。"""
    g = to_gray_any(frame.pixels if box is None else frame.crop_ratio(*box))
    img = Image.fromarray(g).resize((size + 1, size), Image.LANCZOS)
    arr = np.asarray(img, dtype=np.int16)
    diff = arr[:, 1:] > arr[:, :-1]
    bits = 0
    for v in diff.flatten():
        bits = (bits << 1) | int(v)
    return f"{bits:0{size * size // 4}x}"


def ahash(frame: Frame, size: int = 8, box=None) -> str:
    """均值哈希：比全图均值大的位为 1。"""
    g = to_gray_any(frame.pixels if box is None else frame.crop_ratio(*box))
    img = Image.fromarray(g).resize((size, size), Image.LANCZOS)
    arr = np.asarray(img, dtype=np.float32)
    bits = 0
    for v in (arr > arr.mean()).flatten():
        bits = (bits << 1) | int(v)
    return f"{bits:0{size * size // 4}x}"


def hamming(hex_a: str, hex_b: str) -> int:
    """两个十六进制哈希串的汉明距离。"""
    if len(hex_a) != len(hex_b):
        raise ValueError("哈希长度不一致")
    return bin(int(hex_a, 16) ^ int(hex_b, 16)).count("1")


def same_screen(a: Frame, b: Frame, max_hamming: int = 6) -> bool:
    """判断两帧是否为同一屏（dhash 汉明距离阈值）。"""
    return hamming(dhash(a), dhash(b)) <= max_hamming


# ---------------------------------------------------------------- 模板匹配

def _integral(a: np.ndarray) -> np.ndarray:
    """积分图，首行首列补 0。"""
    return np.pad(a.cumsum(0).cumsum(1), ((1, 0), (1, 0)), mode="constant")


def _window_sums(integral: np.ndarray, h: int, w: int) -> np.ndarray:
    """由积分图算每个 h×w 窗口的和，返回 (H-h+1, W-w+1)。"""
    return (integral[h:, w:] - integral[:-h, w:]
            - integral[h:, :-w] + integral[:-h, :-w])


def match_template(frame: Frame, template: Frame | np.ndarray,
                   box: Optional[Sequence[float]] = None,
                   scale: int = 1) -> tuple[float, tuple[int, int]]:
    """
    归一化互相关（NCC）模板匹配，返回 (最佳得分 -1~1, 左上角坐标(物理像素))。

    用 FFT 做互相关 + 积分图算窗口均值/方差，避免 O(W*H*tpl) 的纯 Python 循环。
    box 限定搜索区域（相对比例），可大幅提速。
    """
    src = frame.pixels if box is None else frame.crop_ratio(*box)
    g = to_gray_any(src)
    t = to_gray_any(template.pixels if isinstance(template, Frame) else template)

    if scale > 1:
        g = downsample(g, scale)
        t = downsample(t, scale)
    if t.shape[0] > g.shape[0] or t.shape[1] > g.shape[1]:
        return -1.0, (0, 0)

    gi = g.astype(np.float64)
    ti = t.astype(np.float64)
    th, tw = ti.shape
    n = th * tw
    t_mean = float(ti.mean())
    t_zm = ti - t_mean
    t_var = float((t_zm ** 2).sum())
    if t_var <= 1e-6:
        return -1.0, (0, 0)

    # 互相关分子：sum(I * T) - n * mean_I * mean_T
    # 用 FFT 计算 sum(I*T)（相关，不是卷积 → 模板翻转两次等价于不翻转）
    fh, fw = gi.shape[0] + th - 1, gi.shape[1] + tw - 1
    fh = 1 << (fh - 1).bit_length()
    fw = 1 << (fw - 1).bit_length()
    F = np.fft.rfft2(gi, s=(fh, fw))
    T = np.fft.rfft2(ti[::-1, ::-1], s=(fh, fw))
    corr_full = np.fft.irfft2(F * T, s=(fh, fw))
    # 有效相关区域：corr_full[th-1 : gi.h, tw-1 : gi.w]
    corr = corr_full[th - 1: gi.shape[0], tw - 1: gi.shape[1]]

    # 窗口均值/平方和 → 方差
    #
    # 注意：这里必须用 float64，且方差要夹在 0 而不是某个极小的正数。
    # 原实现用 float32 累加、再 np.maximum(var, 1e-6)，平坦窗口（纯色画面）
    # 的方差本应为 0，被夹到 1e-6 后分母 sqrt(1e-6*n) 趋近于零，
    # 而分子 num 残留着 FFT 的浮点误差 → NCC 爆到 25.9 这种量级，
    # 于是「任何模板都能在纯色画面上匹配成功」。这是假匹配的根源。
    Ii = _integral(gi)
    S1 = _window_sums(Ii, th, tw)
    S2 = _window_sums(_integral(gi * gi), th, tw)
    mean_I = S1 / n
    var_I = np.maximum(S2 / n - mean_I * mean_I, 0.0)

    num = corr - n * mean_I * t_mean
    den = np.sqrt(var_I * n) * np.sqrt(t_var)

    # 方差低于「半个灰阶」的窗口视为无纹理：NCC 在该窗口没有定义，
    # 记 0 分（既不算匹配也不算反向匹配），避免除零放大噪声。
    flat = var_I < 0.25
    ncc = np.zeros_like(num, dtype=np.float64)
    ok = (~flat) & (den > 1e-9)
    ncc[ok] = num[ok] / den[ok]
    ncc = np.clip(ncc, -1.0, 1.0)

    idx = int(np.argmax(ncc))
    ry, rx = divmod(idx, ncc.shape[1])
    score = float(ncc[ry, rx])

    # 换算回原始物理像素
    ox, oy = (0, 0)
    if box is not None:
        ox = int(box[0] * frame.width)
        oy = int(box[1] * frame.height)
    return score, (ox + rx * scale, oy + ry * scale)


# ---------------------------------------------------------------- 颜色定位

# 常用按钮色预设。范围都留了余量：UI 有描边、渐变、抗锯齿，
# 太窄会漏检，太宽会把背景吞进来。
COLOR_PRESETS: dict[str, tuple[tuple[int, int, int], int]] = {
    # 名称: ((R, G, B) 基准色, 每通道容差)
    "red": ((232, 64, 42), 55),        # 关闭 X / 危险按钮
    "orange": ((240, 140, 40), 55),    # 主行动按钮（开始游戏、确定、主操作）
    "white": ((245, 245, 245), 25),    # 白字标题
    "gold": ((240, 200, 90), 50),      # 金色描边
    "green": ((90, 190, 90), 55),
    "blue": ((70, 150, 230), 55),
}


def color_mask(frame: Frame, color: str | Sequence[int],
               box: Optional[Sequence[float]] = None,
               tolerance: Optional[int] = None) -> np.ndarray:
    """返回布尔掩码 (H, W)，标出与目标色接近的像素。

    color 可以是 COLOR_PRESETS 里的名字，也可以是 (R, G, B) 三元组。
    box 限定搜索区域（相对比例，语义同 match_template）。
    """
    if isinstance(color, str):
        if color not in COLOR_PRESETS:
            raise ValueError(f"未知颜色预设 {color!r}；可用：{sorted(COLOR_PRESETS)}")
        base, tol = COLOR_PRESETS[color]
        if tolerance is None:
            tolerance = tol
    else:
        base = tuple(int(c) for c in color)
        if len(base) != 3:
            raise ValueError(f"颜色三元组需要 3 个分量，收到 {color!r}")
        if tolerance is None:
            tolerance = 50

    px = frame.pixels if box is None else frame.crop_ratio(*box)
    p = px[:, :, :3].astype(np.int16)
    return (np.abs(p[:, :, 0] - base[0]) <= tolerance) & \
           (np.abs(p[:, :, 1] - base[1]) <= tolerance) & \
           (np.abs(p[:, :, 2] - base[2]) <= tolerance)


def find_color_blob(frame: Frame, color: str | Sequence[int],
                    box: Optional[Sequence[float]] = None,
                    tolerance: Optional[int] = None,
                    min_pixels: int = 60) -> Optional[dict]:
    """找出目标色最大的一团，返回其中心与包围盒（物理像素）。

    比模板匹配更适合「按钮位置随面板变化、但颜色固定」的场景：
    每个面板的关闭 X 位置都不一样，用颜色定位就不必逐个量坐标。
    """
    mask = color_mask(frame, color, box=box, tolerance=tolerance)
    n = int(mask.sum())
    if n < min_pixels:
        return None
    ys, xs = np.nonzero(mask)
    ox = int(box[0] * frame.width) if box is not None else 0
    oy = int(box[1] * frame.height) if box is not None else 0
    x0, x1 = int(xs.min()) + ox, int(xs.max()) + ox
    y0, y1 = int(ys.min()) + oy, int(ys.max()) + oy
    cx = int(round(xs.mean())) + ox
    cy = int(round(ys.mean())) + oy
    return {"center": (cx, cy), "bbox": (x0, y0, x1 - x0, y1 - y0),
            "pixels": n, "ratio": (cx / frame.width, cy / frame.height)}


def find_template(frame: Frame, template: Frame | np.ndarray,
                  threshold: float = 0.85,
                  box: Optional[Sequence[float]] = None,
                  scale: int = 2) -> Optional[tuple[int, int]]:
    """匹配成功返回模板中心物理坐标，否则 None。"""
    score, (x, y) = match_template(frame, template, box=box, scale=scale)
    if score < threshold:
        return None
    t = template.pixels if isinstance(template, Frame) else template
    th = t.shape[0] // scale
    tw = t.shape[1] // scale
    return (x + tw // 2, y + th // 2)


# ---------------------------------------------------------------- 数字 OCR（自举模板）

# 7 段风格的手写模板不可靠；这里改为「从真实截图里自举」：
#   给定一张截图 + 一个数字区域 + 该区域真实文本（如 "95%"），
#   自动切出每个字符字形存成模板，之后用 NCC 匹配识别。
@dataclass
class DigitOCR:
    templates: dict[str, np.ndarray] = field(default_factory=dict)

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        np.savez_compressed(path, **{k: v for k, v in self.templates.items()})

    @classmethod
    def load(cls, path: str) -> "DigitOCR":
        d = np.load(path)
        return cls({k: d[k] for k in d.files})

    def harvest(self, frame: Frame, box: Sequence[float], text: str,
                min_gap: int = 2) -> int:
        """
        从已知区域自举模板。box=(rx,ry,rw,rh)，text 为该区域真实字符（如 "95%"）。
        返回成功入库的字符数。
        """
        crop = to_gray_any(frame.crop_ratio(*box))
        # 二值化：取亮的像素为字（进度文字通常浅色）
        thr = max(60.0, float(crop.mean()) + 1.2 * float(crop.std()))
        mask = crop >= thr
        cols = mask.any(axis=0)
        # 按列投影切字符
        spans, start = [], None
        for i, v in enumerate(cols):
            if v and start is None:
                start = i
            elif not v and start is not None:
                if i - start >= min_gap:
                    spans.append((start, i))
                start = None
        if start is not None and len(cols) - start >= min_gap:
            spans.append((start, len(cols)))
        if len(spans) != len(text):
            return 0
        n = 0
        for (x0, x1), ch in zip(spans, text):
            sub = mask[:, x0:x1]
            rows = sub.any(axis=1)
            ys = np.where(rows)[0]
            if ys.size == 0:
                continue
            sub = sub[ys[0]: ys[-1] + 1, :]
            img = Image.fromarray((sub * 255).astype(np.uint8)).resize((16, 24), Image.LANCZOS)
            self.templates[ch] = np.asarray(img, dtype=np.uint8)
            n += 1
        return n

    def read(self, frame: Frame, box: Sequence[float], min_gap: int = 2,
             threshold: float = 0.72) -> str:
        """识别区域内的字符序列；无法识别的字符跳过。"""
        if not self.templates:
            return ""
        crop = to_gray_any(frame.crop_ratio(*box))
        thr = max(60.0, float(crop.mean()) + 1.2 * float(crop.std()))
        mask = crop >= thr
        cols = mask.any(axis=0)
        spans, start = [], None
        for i, v in enumerate(cols):
            if v and start is None:
                start = i
            elif not v and start is not None:
                if i - start >= min_gap:
                    spans.append((start, i))
                start = None
        if start is not None and len(cols) - start >= min_gap:
            spans.append((start, len(cols)))
        out = []
        for x0, x1 in spans:
            sub = mask[:, x0:x1]
            ys = np.where(sub.any(axis=1))[0]
            if ys.size == 0:
                continue
            sub = sub[ys[0]: ys[-1] + 1, :]
            img = np.asarray(
                Image.fromarray((sub * 255).astype(np.uint8)).resize((16, 24), Image.LANCZOS),
                dtype=np.float32,
            )
            img = (img - img.mean()) / (img.std() + 1e-6)
            best, best_ch = -2.0, ""
            for ch, tpl in self.templates.items():
                t = tpl.astype(np.float32)
                t = (t - t.mean()) / (t.std() + 1e-6)
                score = float((img * t).mean())
                if score > best:
                    best, best_ch = score, ch
            if best >= threshold:
                out.append(best_ch)
            else:
                out.append("?")
        return "".join(out)


# ---------------------------------------------------------------- 卡死/稳定判定

@dataclass
class Sample:
    ts: float
    frame: Frame
    dhash: str
    diff_from_prev: float


class ScreenWatcher:
    """
    连续采样若干帧，判断画面是否「静止」。

    用法：
        w = ScreenWatcher(device, interval=1.0, samples=6, scale=8)
        verdict = w.observe()      # 阻塞约 samples*interval 秒
        verdict.stuck               # True = 画面完全没动
        verdict.changed_ratio       # 有变化的采样占比
        verdict.max_diff            # 最大帧差
    """

    def __init__(self, device, interval: float = 1.0, samples: int = 6,
                 scale: int = 8, box: Optional[Sequence[float]] = None):
        self.device = device
        self.interval = interval
        self.samples = max(2, samples)
        self.scale = scale
        self.box = box
        self.samples_log: list[Sample] = []

    def observe(self) -> "StabilityVerdict":
        import time

        self.samples_log = []
        prev: Optional[Frame] = None
        for i in range(self.samples):
            f = Frame.from_raw(self.device.screencap_raw())
            d = 0.0 if prev is None else mean_abs_diff(prev, f, self.scale, self.box)
            self.samples_log.append(Sample(time.time(), f, dhash(f), d))
            prev = f
            if i < self.samples - 1:
                time.sleep(self.interval)
        diffs = [s.diff_from_prev for s in self.samples_log[1:]]
        hashes = [s.dhash for s in self.samples_log]
        moved = sum(1 for d in diffs if d >= 0.01)
        return StabilityVerdict(
            samples=len(self.samples_log),
            max_diff=max(diffs) if diffs else 0.0,
            mean_diff=float(np.mean(diffs)) if diffs else 0.0,
            changed_samples=moved,
            distinct_hashes=len(set(hashes)),
            frames=[s.frame for s in self.samples_log],
            diffs=diffs,
        )


@dataclass
class StabilityVerdict:
    samples: int
    max_diff: float
    mean_diff: float
    changed_samples: int
    distinct_hashes: int
    frames: list[Frame]
    diffs: list[float]

    @property
    def stuck(self) -> bool:
        """画面在采样窗口内一次都没变 → 判定卡死。"""
        return self.changed_samples == 0

    @property
    def static(self) -> bool:
        """画面基本不变（允许极小抖动），适合「等界面稳定」用。"""
        return self.max_diff < 0.01

    def summary(self) -> dict:
        return {
            "samples": self.samples,
            "max_diff": round(self.max_diff, 4),
            "mean_diff": round(self.mean_diff, 4),
            "changed_samples": self.changed_samples,
            "distinct_hashes": self.distinct_hashes,
            "stuck": self.stuck,
            "static": self.static,
        }


def wait_until_stable(device, timeout: float = 60.0, settle: float = 2.0,
                      interval: float = 0.8, threshold: float = 0.01,
                      scale: int = 8) -> tuple[bool, float, Frame]:
    """
    等画面稳定：连续两次采样帧差 < threshold 且持续 settle 秒即返回。
    返回 (是否稳定, 实际耗时, 最后一帧)。
    """
    import time

    t0 = time.time()
    prev = Frame.from_raw(device.screencap_raw())
    stable_since = None
    while time.time() - t0 < timeout:
        time.sleep(interval)
        cur = Frame.from_raw(device.screencap_raw())
        d = mean_abs_diff(prev, cur, scale)
        prev = cur
        if d < threshold:
            if stable_since is None:
                stable_since = time.time()
            elif time.time() - stable_since >= settle:
                return True, time.time() - t0, cur
        else:
            stable_since = None
    return False, time.time() - t0, prev


# ---------------------------------------------------------------- 自检

if __name__ == "__main__":
    import sys
    import time

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from adbkit import Device, pick_device

    dev = Device(pick_device())
    print(f"device = {dev}")

    t0 = time.time()
    blob = dev.screencap_raw()
    f = Frame.from_raw(blob)
    print(f"raw {len(blob)} B -> {f} in {time.time() - t0:.2f}s")

    t0 = time.time()
    g = f.gray(8)
    print(f"gray(8) -> {g.shape} in {time.time() - t0:.3f}s")

    t0 = time.time()
    print(f"dhash = {dhash(f)}  ahash = {ahash(f)}  in {time.time() - t0:.3f}s")

    t0 = time.time()
    p = f.save(os.path.join(tempfile.gettempdir(), "vision_selfcheck.png"))
    print(f"save png -> {p} in {time.time() - t0:.3f}s")

    # 模板匹配自检：用当前帧的一块区域当模板，应在同一帧上匹配到自身
    tpl = Frame(f.width // 4, f.height // 4,
                f.crop(f.width // 3, f.height // 3, f.width // 4, f.height // 4).copy())
    t0 = time.time()
    score, (x, y) = match_template(f, tpl, scale=2)
    print(f"match_template self -> score={score:.4f} at ({x},{y}) in {time.time() - t0:.2f}s")

    # 稳定性采样
    t0 = time.time()
    w = ScreenWatcher(dev, interval=1.0, samples=4)
    v = w.observe()
    print(f"watcher -> {v.summary()} in {time.time() - t0:.2f}s")
