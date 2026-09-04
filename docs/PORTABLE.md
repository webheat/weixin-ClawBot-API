# 便携版（U 盘直跑）打包

> 把 `bot.py` 及其全部依赖打成一个自包含目录，丢到 U 盘里就能跑，
> 不需要装 Python、不需要虚拟环境、不污染宿主机。

## 1. 可行性验证结论

✅ **已通过**。`weixin-clawbot.spec` + `pyinstaller 6.22.2` 一次构建成功：

| 项 | 实测值 |
|---|---|
| 构建方式 | `pyinstaller --onedir`（目录式，非单文件） |
| 输出目录 | `dist/weixin-clawbot/` |
| 总大小 | **105 MB** |
| 入口二进制 | `dist/weixin-clawbot/weixin-clawbot`（ELF 64-bit，dynamically linked） |
| Python 解释器 | 内置 3.12，无需宿主装 Python |
| 第三方依赖打包 | aiohttp / requests / qrcode / PIL / cairosvg / python-dotenv 全部 |
| CWD 行为验证 | 在 `/tmp/usb_test/` 跑，会在该目录写 `config.json`（✓ 数据跟 U 盘走） |
| 启动烟测 | banner + provider 选择器正常出现 → 所有 import 链路通畅 |
| 硬编码路径 | 无（grep `/opt\|/root\|abspath` 零命中） |

## 2. 怎么打包

```bash
# 在项目根目录
./venv/bin/pyinstaller weixin-clawbot.spec
```

产物在 `dist/weixin-clawbot/`，整个目录可以原样拷到 U 盘。

如果 `requirements.txt` 增减了依赖、或者改了 `bot.py` 的导入，记得
**重新生成 spec**：

```bash
./venv/bin/pyi-makespec \
  --onedir \
  --name weixin-clawbot \
  --collect-all qrcode \
  --collect-all PIL \
  --collect-all aiohttp \
  --collect-all cairosvg \
  bot.py
```

## 3. 怎么用（U 盘直跑）

### 3.1 准备 U 盘目录

```
/media/you/USB/
└── weixin-clawbot/           ← 整个 dist 目录拷过来
    ├── weixin-clawbot        ← 入口可执行
    └── _internal/            ← 解释器 + 依赖
```

首次运行前，把 `.env` 也拷到 U 盘根目录（和 `weixin-clawbot` 同级）：

```
/media/you/USB/
├── weixin-clawbot/
└── .env                      ← 你的 ima/ilink 配置
```

### 3.2 启动

```bash
# Linux
cd /media/you/USB
./weixin-clawbot/weixin-clawbot

# Windows (假设我们以后出 Windows 包)
cd E:\
.\weixin-clawbot\weixin-clawbot.exe
```

首次运行会：
- 读 `.env`（找不到则警告）
- 调 `load_or_create_config()` 交互式生成 `config.json`（**写在 CWD**，
  也就是 U 盘根目录）
- 弹出 iLink 二维码 → 微信扫码登录
- 登录后写 `weixin_state.json`（也是 CWD）

**所有运行时数据（config / state / 日志）都在 U 盘上**。

### 3.3 在 Linux 上跑需要的权限

U 盘挂载默认可能是 `noexec`：

```bash
# 检查
mount | grep sdb1

# 临时以 exec 重挂
sudo mount -o remount,exec /dev/sdb1
```

或在 `/etc/fstab` 里写死：

```
UUID=xxxx  /media/USB  exfat  defaults,exec,uid=1000,gid=1000  0  0
```

## 4. 文件系统要求

| FS | 支持 | 备注 |
|---|---|---|
| **exFAT** | ✅ 推荐 | 跨 Win/Mac/Linux，单文件 > 4GB，U 盘默认格式 |
| **NTFS** | ✅ | Windows + Linux 读写稳定 |
| **ext4** | ✅ | Linux 单平台，性能最好 |
| **FAT32** | ❌ | `os.replace()` 原子写在 FAT32 上不可靠，state 文件可能损坏 |

`config.json` / `weixin_state.json` 用 `tmp + os.replace` 原子写，**不要用 FAT32**。

## 5. 不会被打包进 U 盘目录的东西

`bot.py` 启动时**按 CWD 相对路径**读这些，所以**它们要单独放**：

- `.env` —— 凭据，**已经在 `.gitignore`**，不会自动进 U 盘
- `config.json` —— 首次运行交互式生成
- `weixin_state.json` —— 首次登录后生成

如果想一次性带配置：把 `.env` / `config.json` 跟 `weixin-clawbot/`
**同级**放在 U 盘根目录。

## 6. 跨平台

每个 OS 都要单独打包：

```bash
# Linux x86_64（在 Linux 上跑）
pyinstaller weixin-clawbot.spec

# Windows 10/11 x64（在 Windows 上跑，或者 Wine 交叉编译）
pyinstaller weixin-clawbot.spec

# macOS arm64 / x64（在对应 Mac 上跑）
pyinstaller weixin-clawbot.spec
```

`spec` 文件**跨平台通用**，不需要改。

## 7. 构建前置依赖（cairosvg）

`cairosvg` 在 Linux 上需要系统级 Cairo 库（不是 pip 装得了的）：

```bash
# Debian / Ubuntu
sudo apt install libcairo2

# RHEL / CentOS
sudo dnf install cairo

# Alpine
apk add cairo
```

Windows / macOS 上 Cairo 通常随 PyInstaller 自动打包，无需手动装。
**如果打包后 `weixin-clawbot` 启动报"cannot find libcairo"**，说明宿主
缺这个系统库。

## 8. 与开发模式共存

打包过程**不会污染**开发环境：

- `dist/` 在 `.gitignore` 里（如果没加，建议加）
- `build/` 在 `.gitignore` 里
- `weixin-clawbot.spec` 在仓库根目录，**应该入库**

```bash
echo -e "dist/\nbuild/\n*.spec.bak" >> .gitignore
git add .gitignore weixin-clawbot.spec
git commit -m "build: 登记 PyInstaller spec + 忽略构建产物"
```

## 9. 已知限制

1. **大小 105 MB** —— Python 3.12 解释器 + aiohttp + PIL 已经占大头，
   不太可能压到 50MB 以下。要更小可以走 **Nuitka**（编译到 C）但配置
   复杂得多。
2. **单平台打包** —— U 盘在另一台架构不同的机器上跑不起来（ARM Mac
   打出的包不能在 x86 Linux 上跑，反之亦然）。
3. **没有自动更新** —— 代码改了要重新打包，U 盘上的副本不会自动升级。
   可以写一个 `update.sh` 拉新 `weixin-clawbot/` 覆盖。
4. **首次启动 ~2-3 秒** —— `_internal/` 解压+加载 Python 解释器，
   比 `python bot.py` 慢一点。

## 10. 验证清单（每次打包后跑一遍）

```bash
# 1. 入口可执行
file dist/weixin-clawbot/weixin-clawbot
# 期望: ELF 64-bit LSB executable, x86-64 ...

# 2. 启动到 banner
timeout 5 ./dist/weixin-clawbot/weixin-clawbot 2>&1 | head -10
# 期望: 看到微信 ClawBot ASCII banner

# 3. 跨目录运行（模拟 U 盘）
mkdir -p /tmp/usb_test && cp .env /tmp/usb_test/
cd /tmp/usb_test && timeout 5 /opt/weixin-ClawBot-API/dist/weixin-clawbot/weixin-clawbot
# 期望: 在 /tmp/usb_test 写出 config.json

# 4. 依赖完整性（应该不报 ImportError / libcairo 缺失）
ls dist/weixin-clawbot/_internal/ | head -20
# 期望: 看到 aiohttp/ requests/ PIL/ dotenv/ 等子目录
```
