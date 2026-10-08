# power_monitor_950.py 使用说明

> 
> 文件地址：[https://github.com/Eco](https://github.com/Eco)‑Sphere/poc‑utils/blob/main/power_monitor_950.py
> 适配：Ascend950DT / A5 / Ascend910，多节点集群监控脚本
> 功能：通过 SSH 采集 NPU 指标 (npu‑smi)、CPU 指标 (top)；通过 IPMI (ipmitool) 读取 BMC 整机功耗；输出独立 CSV 时序日志，用于压测性能分析。

## 📋 功能简介

1. 并发多台服务器采集指标
2. 获取指标：
   - NPU：AICore 利用率、单卡功耗、HBM 内存占用百分比
   - CPU：整机 CPU 使用率、vLLM 进程 CPU 总占用
   - BMC：CPU‑BMC 整机功耗、NPU‑BMC 整机功耗
3. 每台节点输出独立 CSV 日志文件
4. 同一轮采集使用统一时间戳，方便多节点时序对齐绘图分析
5. Ctrl‑C 可以优雅退出监控程序

## ⚠️前置依赖（必须先装好）

### 系统依赖（运行脚本的机器）

```
# ipmitool 用于读取BMC功耗
yum install ipmitool -y
# 或者ubuntu
apt install ipmitool -y
```

### Python 依赖

```
pip3 install paramiko
```

## 📥 获取代码

```
git clone https://github.com/Eco-Sphere/poc-utils.git
cd poc-utils
ls
# 可以看到 power_monitor_950.py
```

> ⚠️**禁止直接把密码硬编码写在脚本内部！脚本采用环境变量传入密码。**

字段说明

表格

| 字段 | 说明 |
| --- | --- |
| role | P 代表主节点，D 代表数据节点，自己随便标记 |
| os_ip | 服务器操作系统 ssh IP，如果没有填 None |
| os_user | ssh 账号，集群一般是 root |
| os_pwd | ssh 密码 |
| cpu_bmc_ip | CPU 侧 BMC 管理 IP；没有填`None`自动跳过 BMC 功耗采集 |
| cpu_bmc_user | BMC 登录账号 |
| cpu_bmc_pwd | BMC 密码 |
| npu_bmc_ip | NPU 侧 BMC 管理 IP；没有填`None`跳过采集 |
| npu_bmc_user | NPU BMC 账号 |
| npu_bmc_pwd | NPU BMC 密码 |

> 
> 如果某台机器没有 NPU‑BMC，直接写`None`，脚本会自动跳过该 BMC 功耗采集。

## 🚀 启动运行脚本
### 1. 运行脚本参数说明

```
# 示例：间隔5秒，模型glm51，测试用例perf_run01
python3 power_monitor_950.py
```

### 2. 停止脚本

控制台按 `Ctrl + C`，脚本会完成本轮写入，优雅退出。

## 📂 日志输出

脚本运行成功后，当前目录会自动生成日志文件夹：

```
monitor_glm51_perf_run01_20261008_162010/
├── node_141.61.54.220_P.csv
├── node_141.61.54.216_P.csv
├── node_141.61.54.212_D.csv
……
```

- 每一台节点一个独立 csv 文件
- csv 列：`timestamp,node_ip,role,avg_aicore,avg_npu_power,avg_hbm_percent,vllm_cpu,total_cpu,cpu_bmc_power,npu_bmc_power,error`
- `error`字段记录采集失败的报错信息，方便定位网络 / 密码 / BMC 问题

## 📊 简单查看日志示例

```
import csv

with open("monitor_xxx/node_141.61.54.220_P.csv","r",encoding="utf-8") as f:
    reader = csv.DictReader(f)
    for row in reader:
        print(row["timestamp"], row["avg_aicore"], row["avg_npu_power"])
```

> 
> 可以使用 Excel 直接打开 csv 做查看绘图；也可以用 pandas 读取做压测数据分析。

## ❗常见问题排查

1. **BMC 读取返回 None**
   - 检查 BMC IP、账号密码是否正确；
   - 网络是否通，能否`ping BMC_IP`；
   - ipmitool cipher 加密套件版本兼容问题，脚本内部已经自动尝试多套 cipher。
2. **SSH 采集报错、npu‑smi failed**
   - 确认目标机器 ssh 网络通，账号密码正确；
   - 确认远端机器已经安装 CANN，`npu‑smi info`命令可以单独在远端执行成功。
3. **采集超时 timeout**
   - 调大`--interval`，不要用 1 秒高频采集；BMC 设备处理能力有限，过高频率会限流超时。
4. **paramiko 相关报错**
   - 确认已经执行`pip3 install paramiko`安装依赖。
5. csv 日志中文乱码
   - 脚本内部已经指定`encoding="utf‑8"`；Windows Excel 打开乱码，可以使用记事本打开，另选编码格式。

## 💡注意事项

1. 不建议设置采集间隔小于 5 秒，高频轮询会打满 BMC 和 SSH 连接。
2. 密码全部使用环境变量传入，**严禁提交带明文密码的脚本到 git 仓库**。
3. 仅适用于内网可信集群环境；脚本使用密码 SSH 登录，公网环境不建议使用。
4. 如果节点没有 BMC，对应 IP 字段填写`None`即可，脚本自动跳过功耗读取。

## 📌 后续拓展方向

1. 对接 Prometheus+Grafana 做实时可视化
2. 增加阈值告警，AICore / 功耗异常控制台打印告警
3. 增加 csv 自动切割，防止长时间压测单个 csv 文件过大

## 📄 输出样例控制台打印

```
[INFO] 日志目录: monitor_glm51_perf_run01_20261008_162010
[INFO] 监控节点数量: 8
[INFO] 采集间隔: 5s

[INFO] 监控启动，Ctrl+C 停止
[2026‑10‑08 16:20:10] node=141.61.54.220 role=P AICore=85.20% NPU_Power=380.50W HBM=72.30% vLLM_CPU=120.50% Sys_CPU=75.20% CPU_BMC=1850W NPU_BMC=2800W
[2026‑10‑08 16:20:15] node=141.61.54.216 role=P AICore=88.10% NPU_Power=385.20W HBM=75.12% vLLM_CPU=130.20% Sys_CPU=78.30% CPU_BMC=1890W NPU_BMC=2830W
```

> 
> `N/A`代表该次采集没有拿到有效数据，查看 csv 里面 error 字段看具体失败原因。