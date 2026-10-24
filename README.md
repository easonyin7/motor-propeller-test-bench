# 电机桨叶自动测试台

面向当前 PX4 Motor 1 测试台的中文 Windows 桌面上位机。通过 MAVLink 控制已配置的飞控输出，使用 USB 摄像头读取拉力和电参数仪表，自动逐档采集、统计并绘制性能曲线。

## 功能

- 不转电机的设备预检、现场确认及多档自动测试。
- 自定义输出指令、缓升、目标保持与档间停机。
- LCD / LED 数码管识别、短暂漏读处理及数据时效检查。
- 实时监控、拉力–功率及输出指令相关曲线、CSV和报告导出。
- 统计不足时单档重测一次、中止后续测、300～500g区间补点方案。
- 内置28条历史汇总对应的回放数据，可不接硬件体验低输出方案。

**当前不设置固定的电流、功率或拉力过载中止门槛。** 软件保留通信、数据时效、界面心跳、有限输出租约、停止回执及输出检查；现场仍须可靠固定台架、隔离旋转区域，并能独立切断动力电源。历史测试范围不代表其他电机、桨叶或供电组合的允许范围。

## 从源码启动

当前代码使用 Windows 和 Python 3.12，建议使用64位Python并安装其Tcl/Tk组件。在项目根目录执行：

```powershell
py -3.12 -m venv cv_mvp/.venv
.\cv_mvp\.venv\Scripts\python.exe -m pip install -r cv_mvp/requirements.txt -r px4_mvp/requirements.txt
```

随后双击 **启动上位机.cmd**。默认进入“历史回放”，不会自动连接设备。先用“低输出验证”回放检查界面；正式运行前按操作指南核对串口、当前硬件及标定。

依赖版本记录来自现有运行环境；本仓库没有随Git分发Python及第三方二进制环境。已有的离线文件夹发布包可直接使用其内置环境，但大型ZIP未放入Git源码历史。

## 文档

- [操作指南](bench_app/上位机操作指南.md)：现场操作、测试、补点、续测和故障处理。
- [上位机说明](bench_app/README.md)：实现规则、记录和统计范围。
- [交付说明](distribution/DELIVERY_README.md)：离线发布包内容及首次使用步骤。
- [部署与验收清单](distribution/部署与验收清单.md)：接收方逐项验收。
- [开发交接说明](distribution/开发交接说明.md)：模块、迁移及构建入口。
- [交付验证记录](distribution/交付验证记录.md)：2026-10-20离线交付检查及未验证事项。
- [近期实测汇总](examples/results/README.md)：10月19日主测与补点的结果表。

## 目录

| 目录 | 内容 |
|---|---|
| `bench_app/` | 桌面界面、任务编排、统计及导出。 |
| `px4_mvp/` | MAVLink控制、有限输出流程和离线故障测试。 |
| `cv_mvp/` | 摄像头采集、仪表识别与当前参考照片。 |
| `distribution/` | 文件夹发布包构建、迁移、自检及交付文档。 |
| `integration_runs/` | 默认历史回放所需的28条原始运行数据和汇总。 |
| `examples/results/` | 近期上位机实测的汇总示例。 |

默认标定参考照片使用项目内相对路径；它只对应原台架布局。更换相机、位置、镜头、仪表或页面后需重新验证 / 标定。仓库内部分早期MVP分析和演示脚本还依赖本地实验材料，不属于当前主界面的独立功能入口。

## 离线测试

```powershell
.\cv_mvp\.venv\Scripts\python.exe -m unittest bench_app.test_app distribution.test_distribution
.\cv_mvp\.venv\Scripts\python.exe -m unittest discover -s px4_mvp -p test_*.py
.\cv_mvp\.venv\Scripts\python.exe -m unittest discover -s cv_mvp -p test_instrument_reader.py
.\cv_mvp\.venv\Scripts\python.exe -m bench_app --smoke-test
```

这些测试不启动电机；隐藏界面测试使用历史回放并禁止硬件控制子进程。离线通过不等于新电脑或新台架已经完成实机验收。

## 数据与发布范围

新测试数据写入 `bench_app/runs/`，本机环境、缓存、完整相机实验、开发备份与大型交付ZIP默认不提交。保留这些本地目录及完整工程压缩包作为原始资料备份，勿用清理Git目录的方式删除它们。

本项目当前没有转速 / 温度测量、拉力闭环或自动扣零。代码未附开源授权许可证；仓库公开本身不授予额外使用许可，第三方依赖遵循其各自许可证。
