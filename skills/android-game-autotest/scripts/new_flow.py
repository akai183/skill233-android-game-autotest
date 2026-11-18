#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
new_flow.py — 从模板生成一份可跑的流程 JSON

模板里的 __PKG__ / __ACTIVITY__ 占位符会被替换成你给的包名与 Activity。
只依赖标准库，不需要 numpy / Pillow，也不需要连设备。

用法：
    python scripts/new_flow.py --pkg com.example.game
    python scripts/new_flow.py --pkg com.example.game --activity com.unity3d.player.UnityPlayerActivity
    python scripts/new_flow.py --pkg com.example.game --name 登录冒烟 --out flows/login_smoke.json

不传 --activity 时按 Unity 标准入口填 com.unity3d.player.UnityPlayerActivity；
如果那个类不存在，launch 步骤会走 monkey 兜底，不会失败。

生成物默认写到 local/flows/ 而不是 flows/：flows/ 只放随技能分发的模板，
生成出来的流程属于本地产物，不该进版本库，也不该让技能副本与仓库产生差异。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FLOWS = os.path.join(ROOT, "flows")
LOCAL_FLOWS = os.path.join(ROOT, "local", "flows")

DEFAULT_ACTIVITY = "com.unity3d.player.UnityPlayerActivity"


def build(pkg: str, activity: str, name: str | None) -> dict:
    with open(os.path.join(FLOWS, "smoke.json"), encoding="utf-8") as fp:
        text = fp.read()
    text = text.replace("__PKG__", pkg).replace("__ACTIVITY__", activity)
    flow = json.loads(text)
    flow["name"] = name or f"启动冒烟 - {pkg}"
    flow["description"] = (
        f"拉起 {pkg}，等画面稳定，留证并扫崩溃。"
        f"由 new_flow.py 从 flows/smoke.json 生成。"
    )
    return flow


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="从模板生成流程 JSON")
    ap.add_argument("--pkg", required=True, help="应用包名，如 com.example.game")
    ap.add_argument("--activity", default=DEFAULT_ACTIVITY,
                    help=f"启动后应出现的 Activity（默认 {DEFAULT_ACTIVITY}）")
    ap.add_argument("--name", default=None, help="流程名（默认按包名生成）")
    ap.add_argument("--out", default=None,
                    help="输出路径（默认 local/flows/<pkg>.smoke.json）")
    args = ap.parse_args(argv)

    flow = build(args.pkg, args.activity, args.name)
    out = args.out or os.path.join(LOCAL_FLOWS, f"{args.pkg}.smoke.json")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fp:
        json.dump(flow, fp, ensure_ascii=False, indent=2)
        fp.write("\n")
    shown = os.path.relpath(out)
    if shown.startswith(".." + os.sep):
        shown = os.path.abspath(out)
    print(f"已生成 {out}（{len(flow['steps'])} 步）")
    print(f"下一步：python scripts/runner.py {shown} --dry-run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
