# skill233-android-phone-debug

> 给 AI Agent 一双能操作 Android 真机的手。

基于 `adb` 的 Android 真机调试 Skill：连接设备、安装应用、抓取 logcat、截图录屏、解析 UI 树、模拟点击输入，全程用自然语言驱动，不必打开 Android Studio。

> **状态：🚧 初始化中** — `SKILL.md`、参考手册与安装脚本正在编写，首个版本发布前请勿用于生产流程。

---

## 解决什么问题

Android 调试的知识散落在几十条 adb 命令、uiautomator 文档和厂商差异里。本 Skill 把这些沉淀成 Agent 可以直接执行的工作流：

- **少记命令**：说「装一下这个 APK 并启动」，不用自己拼 `adb install -r` 和 `am start`
- **结构化输出**：设备、包名、Activity、崩溃堆栈、控件坐标都返回可解析的结果
- **闭环验证**：每次操作后自动截图、读 UI 树，确认界面真的变了
- **安全边界**：清数据、卸载、重启等破坏性操作先确认再执行

## 能力规划

| 领域 | 能力 |
|------|------|
| 设备 | USB / 无线调试连接，多设备枚举与切换，连接排障 |
| 应用 | 安装、卸载、启动、停止、清除数据，查看包信息与签名 |
| 日志 | logcat 实时抓取，按包名 / Tag / 级别过滤，崩溃与 ANR 定位 |
| 界面 | 截图、录屏、UI 树 dump，按文本 / Resource ID 定位控件 |
| 交互 | 点击、长按、滑动、输入文本、按键、解锁手势 |
| 性能 | CPU / 内存 / 帧率 / 电量 / 网络快照，冷启动耗时 |
| 文件 | push / pull，截图与日志导出 |

## 使用示例

```text
连上我的 Pixel 7，把 build/outputs/apk/debug/app-debug.apk 装上并启动
```

```text
复现首页闪退：抓 30 秒 logcat，找出 FATAL EXCEPTION 并解释原因
```

```text
截图当前页面，找到「登录」按钮，输入账号密码后提交
```

```text
对比三次冷启动的耗时，输出平均值和最慢的一次
```

## 环境要求

- `adb`（Android Platform Tools）已加入 `PATH`
- Android 7.0+ 真机或模拟器，已开启开发者选项与 USB 调试
- 无线调试需 Android 11+，或使用 `adb tcpip` 方式
- 可选：`scrcpy`（实时画面）、`aapt2`（解析 APK 元信息）

## 安装（规划）

Skill 发布后，把 `skills/android-phone-debug` 复制到 Agent 的 skills 目录：

```bash
git clone https://github.com/neko233-com/skill233-android-phone-debug.git

# Claude Code
cp -r skill233-android-phone-debug/skills/android-phone-debug ~/.claude/skills/

# OpenCode / MiMoCode
cp -r skill233-android-phone-debug/skills/android-phone-debug ~/.config/opencode/skills/
```

## 安全边界

- 仅操作你拥有或已获授权的设备
- `pm clear`、`uninstall`、`reboot`、恢复出厂等操作必须二次确认
- 日志与截图可能包含隐私信息，导出前先脱敏
- 不尝试绕过锁屏、支付验证、验证码或任何访问控制

## Roadmap

- [ ] `SKILL.md`：核心工作流与决策树
- [ ] `references/`：logcat、uiautomator、dumpsys 速查手册
- [ ] `scripts/`：设备连接、日志抓取、截图导出
- [ ] `install.sh` / `install.ps1`：一键安装
- [ ] 真实项目调试案例

## 贡献

欢迎 Issue 和 PR。
