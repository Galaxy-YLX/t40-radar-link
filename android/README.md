# T40 雷达 · 安卓端

把 `host/t40_cloud_view.py`（PySide6 + pyqtgraph）的点云显示搬到手机上：**原生 Kotlin 重写**，
不是把 Python 打包进 Android（PySide6 没有官方 Android 轮子，Qt for Android 要自己交叉编译
Qt + PySide6 + pyqtgraph + numpy，实际走不通）。

功能对齐 PC 端的「实时 PPI 点云 + 转速控制 + 状态面板」，回放/录包没做（手机上意义不大）。

```
dist/t40radar-1.0.apk   随包附带的编译产物（release，debug 签名，709 KB），直接装
                        不想装预编译包就自己编，见下面「自己编」一节
```

## 装与用

1. 手机装 `dist/t40radar-1.0.apk`（允许「安装未知来源应用」）。
2. 手机 WiFi 连板卡的 **t40_link**（板卡 `192.168.9.1` 做 AP，手机拿 `192.168.9.x`）。
3. 打开 App → 点底部 **连接**（默认 `192.168.9.1:46000`，可在面板里改）。
4. 没有硬件时点 **演示**，会跑一条合成的走廊数据，用来确认界面正常。

顶部状态条就是 PC 端那条 banner，颜色含义一样：

| 颜色 | 含义 | 怎么办 |
|---|---|---|
| 绿 | 正常，显示 fps / 点每秒 | — |
| 黄 | 已连板卡但没点：`wifi=0`（雷达没上电/没关联）、或 >3 s 没有 0x00F0 | 按提示查雷达/信道 |
| 红 | 连不上、或连上了但收不到板卡 STATUS、或板卡侧采集卡住 | 前者先连 WiFi；后两者典型是 ath6kl 楔死 → 断电重启板卡 |

底部按钮条：**连接 / 面板 / 暂停 / 清除 / 演示**。面板里从上到下是：

- **板卡地址**（host + port，旁边「WiFi」直接跳系统 WiFi 设置）
- **显示**：量程 2–70 m、幅度阈值 c3、累积圈数、旋转角、方位逆时针、标盲区
- **雷达转速**：滑块 + 五个预设（600/900/1200/1500/1800）+ 马达开/关/停转
  （走板卡 → CAN `0x1E80` / `0x1E09` / `0x1E0A` / `0x1E82`，「停转」「马达关」会先弹确认）

界面状态（量程、阈值、累积、朝向、地址）存在 SharedPreferences 里，下次打开还在。

## 和 PC 端「一样」和「不一样」的地方

一样（都是现场锚定过的，别随手改）：

- 画面默认 **整幅逆时针 180° + 方位逆时针**，0° = 雷达正前方 = 那面 3.6 m 墙（在下方）；
- 121° 无数据扇区画灰色楔形，环/辐条/前 后 左 右标注跟着画面一起转；环间距按量程切 1/5/10 m；
- 16 级配色、`level = clamp((c3-阈值)/300, 0, 0.999) × 16`、累积点是半透明蓝；
- 转速由同一帧里 c5 对方位的斜率反推，`60e6 / (slope × 6283.19)`；
- 改朝向会清历史点（老点是按老朝向算的，转不回去）——PC 端也是这个行为。

不一样（有意为之）：

- 点以**米**存，画的时候再乘缩放：改量程不用重投影，历史点也不用清（PC 端是存像素坐标）；
- 板卡日志只进「命令」那一行，**不碰**链路状态（PC 端日志走状态栏，这边没地方放）；
- 板卡同时只接**一个**客户端：手机连着的时候电脑连不上（新连接会把旧的踢掉）。这是桥接的行为，不是 App 的问题；
- RSSI 在 ath6kl AP 模式下拿不到，一直显示 `N/A`；
- 「点/秒」按过阈值之后的点数算，和 PC 同口径；「帧率」是**界面实际处理的帧率**，UI 忙的时候会略低于链路码率。

## 自己编

工具链：JDK 17、Android SDK（compileSdk 34 / build-tools 34.0.0）。工程零第三方依赖（不引 AndroidX），
只有单元测试用 junit。

```bash
cd android
./gradlew assembleRelease      # 产物 app/build/outputs/apk/release/app-release.apk
./gradlew assembleDebug
./gradlew testDebugUnitTest    # 解码对拍（见下）
```

也可以直接用 Android Studio 打开 `android/` 目录（AGP 8.7.3 / Gradle 8.9 / Kotlin 2.0.21）。
release 用的是 debug 签名（自用工具，只为装得上），要正式签名自己加 `signingConfigs`。

## 代码分工

```
app/src/main/java/com/t40/radar/
  RadarDecoder.kt   0x00F0 解码 + 球坐标投影 + c5 反推转速（纯 Kotlin，无 Android 依赖）
  T40Client.kt      T40R 协议（24 B 头 + CRC32）/ TCP 收发自愈 / CMD 下发 / DemoSource
  PpiView.kt        自定义 View：网格、盲区楔形、历史点环形缓冲、当前帧分级配色
  MainActivity.kt   连线、状态判读（对应 PC 的 _watchdog）、面板
app/src/test/java/com/t40/radar/RadarDecoderTest.kt
tools/make_testdata.py   从 pcap 抽帧 + 用 PC 的 Python 解码器算期望值
```

## 解码对拍（改了解码一定要跑）

`RadarDecoder.kt` 是 `decode_00f0` 的手工移植，最容易错在偏移差一位、少乘/多乘 1000。
`tools/make_testdata.py` 不重新实现解码，而是**直接 import PC 那份解码器**算期望值。

测试数据（`app/src/test/resources/` 下的 `frames.bin` + `expected.txt`，60 帧真实点云）随包附带，
所以直接跑就行：

```bash
cd android && ./gradlew testDebugUnitTest
# 期望输出：对拍通过：60 帧 / 15418 点
```

要拿**你自己的**抓包重新生成期望值（比如改了阈值/换了场景）：

```bash
python3 android/tools/make_testdata.py <你的 pcap> [帧数]
```

当前状态：60 帧 / 15418 点逐点比对通过（点数、seq、两条校验和、az/el/r/c3/c5、反推转速全一致）。
另外三条断言锁住投影约定（0° 朝上 / 顺时针 90° 在右 / 逆时针 90° 在左 / 旋转 180° 倒过来）和
magic 不在 0 偏移的情况。

## 已知没做

- 回放（pcap）和录包：手机上没做，现场数据还是用 PC 端 `--src pcap:` 看；
- 直连雷达 UDP（`udp://0.0.0.0:40204`）：手机在雷达 AP 网段时理论上能用，但没做 UI；
- 平板/横屏没专门调过布局：竖屏优先，横屏能看但面板会占掉一大半。
