# PX4 USB 控制 MVP

本方案根据项目对话《自动化测试评估》收敛：第一阶段分别验证 PX4 控制和摄像头读表。本目录只实现前者，沿用现有飞控与电调，拆桨验证一个电机。所有输出均在本目录；不修改 `sources/` 同步资料。

## 本机实际检查结果

2026-10-12，只读检查 COM42 已成功，发送地面站心跳及固件查询后收到响应：

| 项目 | 实测结果 |
|---|---|
| 端口 | COM42 |
| 固件 | PX4 1.14.3 |
| MAVLink system / component | 1 / 1 |
| 解锁状态 | 未解锁（读取时） |
| COM_MOT_TEST_EN | 1：允许执行器测试 |
| PWM_MAIN_FUNC1～4 | 101、102、103、104：Motor 1～4 |
| PWM_MAIN_FUNC5～8 | 0：未分配 |
| PWM_AUX_FUNC1～8 | 未收到这些参数；不据此认定不存在相关物理接口 |
| MAIN 1～4 DIS / MIN / MAX | 1000 / 1000 / 2000 |
| PWM_MAIN_TIM0 | -4：DShot300（实际定时器分组依赖板型） |
| DSHOT_MIN | 约 0.055 |

诊断 JSON 和 CSV 保存在 `logs/`。用户确认当前电机未接入，信号线接 MOTOR1；因此随后进行了无电机的 1 秒短时输出检查。

实测发送 `MAV_CMD_ACTUATOR_TEST`，Motor 1、value=0.02、timeout=1；输出、NaN 停止与释放测试控制三个命令均收到 ACCEPTED。`SERVO_OUTPUT_RAW` port=0 第一路回报从 **0 → 146 → 0**，第二路保持 0。证据见 `logs/20261012_152555_775000.csv`。

这证明飞控控制接口和软件输出回报有效；不证明物理引脚波形、电调供电或电机响应，也未验证拔 USB 后的实际停机。DShot 使用自身的输出范围，146 不能解释为 146 微秒，读取到的 PWM DIS/MIN/MAX 参数也不能直接用于解释该 DShot 路的实时输出。

## 实现方式

PC Python → USB MAVLink → PX4 执行器测试 → 现有输出 → ESC → 无桨电机。

使用 `pymavlink`，保留飞控现有参数，不刷固件、不自动解锁。串口只由一个程序占用；用 QGroundControl 检查接线/配置后关闭其串口连接，再运行本程序。

`probe` 每秒发送地面站心跳，识别 PX4 心跳和源地址，查询 `AUTOPILOT_VERSION`。`-Parameters` 只读查询输出映射及上下限；按能力标志解码 PX4 参数的字节编码。

`pulse` 使用 `MAV_CMD_ACTUATOR_TEST`（310），param1 为归一化输出，param2 为有效期，param5 为 MAVLink 逻辑电机号。例如 Motor 1 填 **1**，而 PX4 输出函数参数中的 **101** 是另一套编号。

当前程序将输出限制在 0～0.10、单次时长限制在 0.2～3 秒。这是软件限幅，不代表某台电机的安全转速或额定功率比例。没有转速传感器时，只能验证不同输出下的转动响应，不能宣称实现了 RPM 闭环。

输出值 0 表示最小电机输出，不能通用地视为停转。`stop` 先发送 NaN 请求停机输出，再用 timeout=0 释放测试控制。程序等待 ACK，异常退出也尝试停止；ACK 表示命令接收结果，实际停转仍需现场观察或测量。

测试超时会释放控制、恢复常规输出。在未解锁、无其他控制来源的条件下，预期恢复停机输出；当前已观察到正常结束后软件回报恢复为 0。仍需单独验证测试命令超时、USB 断开和程序强制终止的行为，不能把它们等同于独立切断电调电源。测试后重新运行命令才会再次输出，不自动恢复。

## 本机运行

打开本项目目录的 PowerShell：

```powershell
.\px4_mvp\run.ps1 -Action ports
.\px4_mvp\run.ps1 -Action probe -Parameters
```

以上两个命令不会发送电机控制指令。`run.ps1` 优先使用本机 Codex 自带 Python；依赖已安装到本目录 `.deps`，不修改系统 Python。

在电机确实未接入时，可复现已经通过的输出检查：

```powershell
.\px4_mvp\run.ps1 -Action pulse -Motor 1 -Value 0.02 -Seconds 1 -NoMotorConnected -ObserveOutput
```

`-ObserveOutput` 请求 10 Hz 的原始输出回报，记录测试前、测试中和停止后的数据到 CSV。原始值的单位由输出协议决定。`-NoMotorConnected` 是操作者对当前物理状态的声明，接入电机后不能再使用这个参数。

确认电调信号对应的 MAIN 路数，移除所有桨叶、可靠固定电机并准备独立电调断电手段后，才执行短时输出。下例 **Motor 1 和 0.02 是演示参数，应与实际接线及电机允许的测试条件核对**：

```powershell
.\px4_mvp\run.ps1 -Action pulse -Motor 1 -Value 0.02 -Seconds 1 -BenchReady
.\px4_mvp\run.ps1 -Action stop -Motor 1
```

`-BenchReady` 表示操作者已经完成现场条件检查，不是程序自动检测结果。若报 DENIED，检查解锁状态、硬件安全按钮和 COM_MOT_TEST_EN；保留拒绝原因，不通过自动解锁或关闭保护来绕过。

如果只连接 USB，飞控能响应不代表电调已供电。电机不转时先核对电调供电、信号/地线和输出协议，不连续增大数值试探。

## MVP 验收顺序

1. 已完成：识别 COM42、PX4 心跳、固件和输出映射。
2. 已完成：无电机时，验证 Motor 1 短时指令、停止/释放指令 ACK，以及第 1 路软件输出回报变化和恢复。
3. 待现场确认：实际电调协议/接线与飞控配置一致，接入电机后拆桨、固定及独立断电。
4. 待实机：一个允许的短时输出；确认只驱动目标电机，随后停止。再验证少量允许的输出档位，不把输出比例当转速。
5. 待实机：正常结束、Ctrl+C、程序被强制终止、USB 中断；测量输出撤销和实际停转时间。断线测试需保持电调供电，才能检验超时行为。

完成以上检查后，再扩展为定时续发、分档运行和 CV 采集集成。持续输出接口应设置总运行期限和数据过期中止条件，不能由独立后台线程永久续发。

## 官方依据

- [PX4 v1.14 执行器配置与测试](https://docs.px4.io/v1.14/en/config/actuators)
- [PX4 v1.14.3 Commander 源码](https://github.com/PX4/PX4-Autopilot/blob/v1.14.3/src/modules/commander/Commander.cpp)：未解锁、安全按钮、COM_MOT_TEST_EN、功能编号与最长 3 秒有效期。
- [PX4 v1.14.3 DShot 源码](https://github.com/PX4/PX4-Autopilot/blob/v1.14.3/src/drivers/dshot/DShot.cpp)：定时器 -4 对应 DShot300，DShot 使用自身输出范围。
- [MAVLink 执行器测试定义](https://mavlink.io/en/messages/common.html#MAV_CMD_ACTUATOR_TEST)：输出值、NaN、超时及逻辑电机编号。
- [pymavlink 官方用法](https://mavlink.io/en/mavgen_python/)：串口、心跳、发送与接收。

离线故障检查 `test_control.py` 不打开串口，覆盖限幅、已解锁/心跳过期拒绝、ACK 丢失、USB 异常及 NaN 的 MAVLink 编码。它不替代实机停机验证。
