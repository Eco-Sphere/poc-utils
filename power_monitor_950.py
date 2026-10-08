#!/usr/bin/env python3
"""
多节点监控脚本(ipmitool 后端，适配 Atlas 950 SuperPoD液冷)

功能：
- 对每个节点通过 SSH 采集：
    NPU (npu-smi info) 指标
    CPU (top) 指标
- 支持 Ascend950DT / A5 新版 npu-smi info 输出格式
- 同时尽量兼容旧版 Ascend910 格式
- 通过 CPU BMC 获取整机功耗
- 通过 NPU BMC 获取整机功耗
- 按固定间隔循环采集
- 每个节点的数据写入独立 CSV 日志文件
- 支持并发采集

每个节点格式：

[
    role,
    os_ip,
    os_user,
    os_pwd,
    cpu_bmc_ip,
    cpu_bmc_user,
    cpu_bmc_pwd,
    npu_bmc_ip,
    npu_bmc_user,
    npu_bmc_pwd
]

字段说明：

node[0] = role
node[1] = os_ip
node[2] = os_user
node[3] = os_pwd
node[4] = cpu_bmc_ip
node[5] = cpu_bmc_user
node[6] = cpu_bmc_pwd
node[7] = npu_bmc_ip
node[8] = npu_bmc_user
node[9] = npu_bmc_pwd

如果某节点没有 CPU BMC / NPU BMC，
可以把对应的 IP 设置为 None，此时会跳过对应 BMC 的功耗采集。
"""

import os
import time
import csv
import re
import subprocess
import random
from datetime import datetime
from concurrent.futures import (
    ThreadPoolExecutor,
    as_completed,
    TimeoutError as FuturesTimeoutError
)

import paramiko


# ==================== 配置区域 ====================

NODES = [
    ["P","141.61.54.220", "root", "os_password",
     "141.61.54.217", "root", "cpu_bmc_password",
     "141.61.54.193", "root", "npu_bmc_password"],  # TODO: 1st BMC 凭据
    ["P","141.61.54.216", "root", "os_password",
     "141.61.54.213", "root", "cpu_bmc_password",
     "141.61.54.196", "root", "npu_bmc_password"],  # TODO: 2nd BMC 凭据
    ["D","141.61.54.212", "root", "os_password",
     "141.61.54.209", "root", "cpu_bmc_password",
     "141.61.54.202", "root", "npu_bmc_password"],  # TODO: 3rd BMC 凭据
    ["D","141.61.54.208", "root", "os_password",
     "141.61.54.205", "root", "cpu_bmc_password",
     "141.61.54.199", "root", "npu_bmc_password"],  # TODO: 4th BMC 凭据
    ["D","141.61.54.180", "root", "os_password",
     "141.61.54.177", "root", "cpu_bmc_password",
     "141.61.54.190", "root", "npu_bmc_password"],  # TODO: 5th BMC 凭据
    ["D","141.61.54.176", "root", "os_password",
     "141.61.54.173", "root", "cpu_bmc_password",
     "141.61.54.187", "root", "npu_bmc_password"],  # TODO: 6th BMC 凭据
    ["D","141.61.54.172", "root", "os_password",
     "141.61.54.169", "root", "cpu_bmc_password",
     "141.61.54.184", "root", "npu_bmc_password"],  # TODO: 7th BMC 凭据
    ["D","141.61.54.168", "root", "os_password",
     "141.61.54.165", "root", "cpu_bmc_password",
     "141.61.54.181", "root", "npu_bmc_password"],  # TODO: 8th BMC 凭据 
]

MODEL = "glm51"
TESTCASE = "test"

# 建议 NPU 监控不要太频繁
INTERVAL = 1

# ================================================================
# ---------- SSH 执行 ----------
# ================================================================


def ssh_execute(
    host,
    username,
    password,
    command,
    connect_timeout=16,
    exec_timeout=20
):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(
        paramiko.AutoAddPolicy()
    )

    try:
        client.connect(
            hostname=host,
            username=username,
            password=password,
            timeout=connect_timeout
        )

        transport = client.get_transport()

        channel = transport.open_session(
            timeout=exec_timeout
        )

        channel.settimeout(exec_timeout)

        channel.exec_command(command)

        stdout_data = b""
        stderr_data = b""

        end_time = time.time() + exec_timeout

        while not channel.exit_status_ready():
            if time.time() > end_time:
                raise TimeoutError(
                    f"SSH command '{command[:30]}' "
                    f"timed out after {exec_timeout}s"
                )

            if channel.recv_ready():
                stdout_data += channel.recv(4096)

            if channel.recv_stderr_ready():
                stderr_data += channel.recv_stderr(4096)

            time.sleep(0.1)

        while channel.recv_ready():
            stdout_data += channel.recv(4096)

        while channel.recv_stderr_ready():
            stderr_data += channel.recv_stderr(4096)

        return (
            stdout_data.decode(
                "utf-8",
                errors="replace"
            ),
            stderr_data.decode(
                "utf-8",
                errors="replace"
            )
        )

    finally:
        client.close()


# ================================================================
# ---------- IPMI 功耗 ----------
# ================================================================


def _ipmitool_power(
    bmc_ip,
    username,
    password,
    cipher
):
    cmd = [
        "ipmitool",
        "-H", bmc_ip,
        "-U", username,
        "-P", password,
        "-I", "lanplus",
        "-C", str(cipher),
        "dcmi",
        "power",
        "reading"
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10
        )

        if result.returncode != 0:
            return None

        match = re.search(
            r"Instantaneous\s+power\s+reading"
            r"\s*:\s*([\d.]+)\s*Watts?",
            result.stdout,
            re.IGNORECASE
        )

        if match:
            return float(match.group(1))

    except Exception:
        return None

    return None


def get_ipmi_power(
    bmc_ip,
    username,
    password
):
    """
    获取 BMC 整机功耗。

    自动尝试多个 cipher。
    """

    time.sleep(
        random.uniform(0, 0.5)
    )

    ciphers = [
        17,
        3,
        16,
        15,
        8,
        1
    ]

    for cipher in ciphers:
        power = _ipmitool_power(
            bmc_ip,
            username,
            password,
            cipher
        )

        if power is not None:
            return power

    return None


# ================================================================
# NPU-SMI 解析
# ================================================================


def parse_npu_smi_output(output):
    """
    解析 npu-smi info 输出。

    主要适配 Ascend950DT / A5：

    第一行：
    | 0 | Ascend950DT | OK | 391.6  52 ... |

    第二行：
    |   | NA | | 0  0 / 0  96180 / 98304 |

    返回：

    [
        {
            "npu": 0,
            "chip": 0,
            "power": 391.6,
            "aicore": 0.0,
            "hbm_percent": 97.83
        },
        ...
    ]
    """

    lines = output.splitlines()

    dies = []

    # 当前正在解析的 NPU
    current_npu = None

    for line in lines:

        # 跳过表格边框
        if (
            line.startswith("+")
            or line.startswith("|==")
            or not line.strip()
        ):
            continue

        # ========================================================
        # 匹配 NPU 第一行
        #
        # | 0 | Ascend950DT | OK | 391.6 52 ...
        #
        # 也支持：
        #
        # | 0 | Ascend910 | OK | ...
        # ========================================================

        first_line_match = re.match(
            r"^\|\s*"
            r"(\d+)"
            r"\s*\|\s*"
            r"([^|]+)"
            r"\s*\|\s*"
            r"(OK|Warning|Error)"
            r"\s*\|\s*"
            r"([\d.]+|-)",
            line
        )

        if first_line_match:

            npu_id = int(
                first_line_match.group(1)
            )

            npu_name = (
                first_line_match.group(2)
                .strip()
            )

            power_str = (
                first_line_match.group(4)
                .strip()
            )

            power = None

            if power_str != "-":
                try:
                    power = float(power_str)
                except ValueError:
                    power = None

            current_npu = {
                "npu": npu_id,

                # A5 新版没有单独 chip id
                # 暂时使用 NPU ID
                "chip": npu_id,

                "name": npu_name,

                "power": power,

                # 保持旧字段名兼容
                # 实际对应 NPU Util(%)
                "aicore": None,

                "hbm_percent": None
            }

            continue

        # ========================================================
        # 匹配 A5 第二行
        #
        # | | NA | | 0 0 / 0 96180 / 98304 |
        #
        # 数据一般位于最后一个非空列
        # ========================================================

        if current_npu is not None:

            # 找所有：
            #
            # 数字 / 数字
            #
            # 例如：
            #
            # 0 / 0
            # 96180 / 98304

            hbm_matches = re.findall(
                r"(\d+)\s*/\s*(\d+)",
                line
            )

            # 如果存在 HBM 信息
            if hbm_matches:

                # 最后一组通常就是 HBM
                hbm_used, hbm_total = map(
                    int,
                    hbm_matches[-1]
                )

                if hbm_total > 0:
                    current_npu[
                        "hbm_percent"
                    ] = (
                        hbm_used
                        / hbm_total
                        * 100.0
                    )

                # =================================================
                # 提取 NPU Util(%)
                #
                # 根据 A5 当前格式：
                #
                # |        | NA |    | 0  0 / 0 96180 / 98304 |
                #
                # 最后一个数据区域第一个数字
                # 就是 NPU Util
                # =================================================

                parts = [
                    p.strip()
                    for p in line.strip()
                    .strip("|")
                    .split("|")
                ]

                # 找最后一个非空字段
                data_field = None

                for p in reversed(parts):
                    if p:
                        data_field = p
                        break

                if data_field:

                    # 例如：
                    #
                    # 0 0 / 0 96180 / 98304

                    util_match = re.match(
                        r"^\s*(\d+(?:\.\d+)?)",
                        data_field
                    )

                    if util_match:
                        try:
                            current_npu[
                                "aicore"
                            ] = float(
                                util_match.group(1)
                            )

                        except ValueError:
                            current_npu[
                                "aicore"
                            ] = None

                dies.append(
                    current_npu
                )

                current_npu = None

    return dies


# ================================================================
# CPU 解析
# ================================================================


def parse_top_output(output):
    """
    解析 top -b -n 1 输出。

    返回：

    vllm_total_cpu:
        所有 vLLM 进程 CPU 使用率之和

    system_cpu:
        系统整体 CPU 使用率
    """

    lines = output.splitlines()

    # ------------------------------------------------------------
    # 系统整体 CPU 利用率
    #
    # %Cpu(s): ...
    # ------------------------------------------------------------

    cpu_line = next(
        (
            l
            for l in lines
            if l.startswith("%Cpu(s)")
        ),
        None
    )

    system_cpu = None

    if cpu_line:

        idle_match = re.search(
            r"(\d+\.?\d*)\s*id",
            cpu_line
        )

        if idle_match:

            system_cpu = (
                100.0
                - float(
                    idle_match.group(1)
                )
            )

    # ------------------------------------------------------------
    # 查找 top 进程表头
    # ------------------------------------------------------------

    header_line = next(
        (
            l
            for l in lines
            if "PID" in l
            and "%CPU" in l
        ),
        None
    )

    if not header_line:
        return None, system_cpu

    headers = header_line.split()

    try:

        cpu_col = headers.index(
            "%CPU"
        )

        command_col = len(
            headers
        ) - 1

    except ValueError:
        return None, system_cpu

    data_start = (
        lines.index(header_line)
        + 1
    )

    vllm_total = 0.0

    # ------------------------------------------------------------
    # 累加 vLLM 进程 CPU
    # ------------------------------------------------------------

    for line in lines[data_start:]:

        parts = line.split()

        if len(parts) <= command_col:
            continue

        command = (
            parts[command_col]
            .lower()
        )

        if "vllm" in command:

            try:
                vllm_total += float(
                    parts[cpu_col]
                )

            except (
                ValueError,
                IndexError
            ):
                continue

    return (
        vllm_total,
        system_cpu
    )


# ================================================================
# 单节点采集
# ================================================================


def query_node(node):

    (
        role,
        os_ip,
        os_user,
        os_pwd,
        cpu_bmc_ip,
        cpu_bmc_user,
        cpu_bmc_pwd,
        npu_bmc_ip,
        npu_bmc_user,
        npu_bmc_pwd
    ) = node

    node_label = (
        os_ip
        if os_ip
        else cpu_bmc_ip
    )

    result = {
        "timestamp":
            datetime.now().strftime(
                "%Y-%m-%d %H:%M:%S"
            ),

        "node_ip":
            node_label,

        "role":
            role,

        "avg_aicore":
            None,

        "avg_npu_power":
            None,

        "avg_hbm_percent":
            None,

        "vllm_cpu":
            None,

        "total_cpu":
            None,

        "cpu_bmc_power":
            None,

        "npu_bmc_power":
            None,

        "error":
            ""
    }

    errors = []

    # ------------------------------------------------------------
    # OS 节点信息
    # ------------------------------------------------------------

    if os_ip:

        # ========================
        # NPU-SMI
        # ========================

        try:

            out, err = ssh_execute(
                os_ip,
                os_user,
                os_pwd,
                "npu-smi info"
            )

            if err:
                errors.append(
                    f"npu-smi stderr: "
                    f"{err[:100]}"
                )

            dies = parse_npu_smi_output(
                out
            )

            aicores = [
                d["aicore"]
                for d in dies
                if d["aicore"] is not None
            ]

            powers = [
                d["power"]
                for d in dies
                if d["power"] is not None
            ]

            hbms = [
                d["hbm_percent"]
                for d in dies
                if d["hbm_percent"] is not None
            ]

            if aicores:
                result[
                    "avg_aicore"
                ] = round(
                    sum(aicores)
                    / len(aicores),
                    2
                )

            if powers:
                result[
                    "avg_npu_power"
                ] = round(
                    sum(powers)
                    / len(powers),
                    2
                )

            if hbms:
                result[
                    "avg_hbm_percent"
                ] = round(
                    sum(hbms)
                    / len(hbms),
                    2
                )

        except Exception as e:

            errors.append(
                f"npu-smi failed: {e}"
            )

        # ========================
        # TOP / CPU
        # ========================

        try:

            top_out, _ = ssh_execute(
                os_ip,
                os_user,
                os_pwd,
                "top -b -n 1"
            )

            vllm, sys_cpu = (
                parse_top_output(
                    top_out
                )
            )

            result[
                "vllm_cpu"
            ] = (
                round(vllm, 1)
                if vllm is not None
                else None
            )

            result[
                "total_cpu"
            ] = (
                round(sys_cpu, 2)
                if sys_cpu is not None
                else None
            )

        except Exception as e:

            errors.append(
                f"top failed: {e}"
            )

    # ------------------------------------------------------------
    # CPU BMC 功耗
    # ------------------------------------------------------------

    if cpu_bmc_ip:

        try:

            power = get_ipmi_power(
                cpu_bmc_ip,
                cpu_bmc_user,
                cpu_bmc_pwd
            )

            if power is not None:

                result[
                    "cpu_bmc_power"
                ] = int(
                    round(power)
                )

            else:

                errors.append(
                    "CPU BMC power read returned None"
                )

        except Exception as e:

            errors.append(
                f"CPU BMC query failed: {e}"
            )

    # ------------------------------------------------------------
    # NPU BMC 功耗
    # ------------------------------------------------------------

    if npu_bmc_ip:

        try:

            power = get_ipmi_power(
                npu_bmc_ip,
                npu_bmc_user,
                npu_bmc_pwd
            )

            if power is not None:

                result[
                    "npu_bmc_power"
                ] = int(
                    round(power)
                )

            else:

                errors.append(
                    "NPU BMC power read returned None"
                )

        except Exception as e:

            errors.append(
                f"NPU BMC query failed: {e}"
            )

    # ------------------------------------------------------------
    # 错误信息
    # ------------------------------------------------------------

    if errors:

        result[
            "error"
        ] = "; ".join(
            errors
        )

    return result


# ================================================================
# 监控主类
# ================================================================


class Monitor:

    def __init__(
        self,
        nodes,
        interval,
        model,
        testcase
    ):

        self.nodes = nodes
        self.interval = interval
        self.model = model
        self.testcase = testcase

        self.timestamp = (
            datetime.now()
            .strftime(
                "%Y%m%d_%H%M%S"
            )
        )

        self.log_dir = (
            f"monitor_"
            f"{model}_"
            f"{testcase}_"
            f"{self.timestamp}"
        )

        os.makedirs(
            self.log_dir,
            exist_ok=True
        )

        # --------------------------------------------------------
        # 每个节点独立 CSV
        # --------------------------------------------------------

        self.file_paths = {}

        self.fieldnames = [
            "timestamp",
            "node_ip",
            "role",
            "avg_aicore",
            "avg_npu_power",
            "avg_hbm_percent",
            "vllm_cpu",
            "total_cpu",
            "cpu_bmc_power",
            "npu_bmc_power",
            "error"
        ]

        for node in nodes:

            if node[0] is None:
                continue

            role = node[0]
            os_ip = node[1]
            cpu_bmc_ip = node[4]

            node_key = (
                os_ip
                if os_ip
                else cpu_bmc_ip,
                role
            )

            file_name = (
                f"node_"
                f"{node_key[0]}_"
                f"{node_key[1]}"
                f".csv"
            )

            file_path = os.path.join(
                self.log_dir,
                file_name
            )

            self.file_paths[
                node_key
            ] = file_path

            with open(
                file_path,
                "w",
                newline=""
            ) as f:

                csv.DictWriter(
                    f,
                    fieldnames=self.fieldnames
                ).writeheader()

        print(
            f"日志文件夹: "
            f"{self.log_dir}"
        )

        print(
            f"监控节点数: "
            f"{len(self.file_paths)}"
        )

        print(
            f"监控间隔: "
            f"{self.interval}s\n"
        )

    def run(self):

        print(
            "监控开始，按 Ctrl+C 停止"
        )

        next_cycle = time.time()

        while True:

            cycle_start = time.time()

            task_timeout = max(
                20,
                self.interval * 0.9
            )

            with ThreadPoolExecutor(
                max_workers=len(
                    self.nodes
                )
            ) as executor:

                futures = {
                    executor.submit(
                        query_node,
                        node
                    ): node

                    for node in self.nodes

                    if node[0] is not None
                }

                try:

                    for future in as_completed(
                        futures,
                        timeout=task_timeout
                    ):

                        node = futures.pop(
                            future
                        )

                        try:

                            data = future.result()

                        except Exception as e:

                            data = {
                                "timestamp":
                                    datetime.now()
                                    .strftime(
                                        "%Y-%m-%d %H:%M:%S"
                                    ),

                                "node_ip":
                                    (
                                        node[1]
                                        if node[1]
                                        else node[4]
                                    ),

                                "role":
                                    node[0],

                                "avg_aicore":
                                    None,

                                "avg_npu_power":
                                    None,

                                "avg_hbm_percent":
                                    None,

                                "vllm_cpu":
                                    None,

                                "total_cpu":
                                    None,

                                "cpu_bmc_power":
                                    None,

                                "npu_bmc_power":
                                    None,

                                "error":
                                    f"Future exception: {e}"
                            }

                        node_key = (
                            data["node_ip"],
                            data["role"]
                        )

                        file_path = (
                            self.file_paths.get(
                                node_key
                            )
                        )

                        if file_path:

                            with open(
                                file_path,
                                "a",
                                newline=""
                            ) as f:

                                csv.DictWriter(
                                    f,
                                    fieldnames=self.fieldnames
                                ).writerow(
                                    data
                                )

                        self._print_row(
                            data
                        )

                except FuturesTimeoutError:

                    for future, node in futures.items():

                        ip = (
                            node[1]
                            if node[1]
                            else node[4]
                        )

                        data = {
                            "timestamp":
                                datetime.now()
                                .strftime(
                                    "%Y-%m-%d %H:%M:%S"
                                ),

                            "node_ip":
                                ip,

                            "role":
                                node[0],

                            "avg_aicore":
                                None,

                            "avg_npu_power":
                                None,

                            "avg_hbm_percent":
                                None,

                            "vllm_cpu":
                                None,

                            "total_cpu":
                                None,

                            "cpu_bmc_power":
                                None,

                            "npu_bmc_power":
                                None,

                            "error":
                                f"Timeout after "
                                f"{task_timeout}s"
                        }

                        node_key = (
                            ip,
                            node[0]
                        )

                        file_path = (
                            self.file_paths.get(
                                node_key
                            )
                        )

                        if file_path:

                            with open(
                                file_path,
                                "a",
                                newline=""
                            ) as f:

                                csv.DictWriter(
                                    f,
                                    fieldnames=self.fieldnames
                                ).writerow(
                                    data
                                )

                        self._print_row(
                            data
                        )

                        future.cancel()

            elapsed = (
                time.time()
                - cycle_start
            )

            sleep_time = (
                next_cycle
                + self.interval
                - time.time()
            )

            next_cycle += (
                self.interval
            )

            if sleep_time > 0:

                time.sleep(
                    sleep_time
                )

            else:

                print(
                    f"本轮耗时 "
                    f"{elapsed:.1f}s，"
                    f"超过间隔，"
                    f"立即开始下一轮"
                )

    def _print_row(
        self,
        data
    ):

        def fmt(v):

            return (
                f"{v:.2f}"
                if v is not None
                else "N/A"
            )

        print(
            f"[{data['timestamp']}] "
            f"node={data['node_ip']} "
            f"role={data['role']} "
            f"AICore="
            f"{fmt(data['avg_aicore'])}% "
            f"NPU_Power="
            f"{fmt(data['avg_npu_power'])}W "
            f"HBM="
            f"{fmt(data['avg_hbm_percent'])}% "
            f"vLLM_CPU="
            f"{fmt(data['vllm_cpu'])}% "
            f"Sys_CPU="
            f"{fmt(data['total_cpu'])}% "
            f"CPU_BMC_Power="
            f"{fmt(data['cpu_bmc_power'])}W "
            f"NPU_BMC_Power="
            f"{fmt(data['npu_bmc_power'])}W "
            f"{'ERR=' + data['error'] if data['error'] else ''}"
        )


# ================================================================
# Main
# ================================================================


if __name__ == "__main__":

    monitor = Monitor(
        NODES,
        INTERVAL,
        MODEL,
        TESTCASE
    )

    monitor.run()