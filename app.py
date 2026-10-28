# -*- coding: utf-8 -*-
"""
=============================================================================
03 系列 3 核心声学大模型 (03K + 03U + 03R) 黄金铁三角协同投票实时监控系统
服务端架构: Flask + 多线程文件监听 + 零误报协同仲裁门禁 + 结果分类归档 + 人工复核 + Web 大屏 & 管理后台
=============================================================================
"""

import os
import sys
import time
import json
import shutil
import threading
import glob
import psutil
import gc
import re
import io
import csv
from datetime import datetime, timedelta
import importlib
import traceback
import torch
from flask import Flask, request, jsonify, render_template, send_from_directory, send_file, Response

# 当在后台静默运行或控制台脱离时，提供防崩溃安全日志流
APP_DIR = os.path.dirname(os.path.abspath(__file__))
log_dir = os.path.join(APP_DIR, "records")
os.makedirs(log_dir, exist_ok=True)
daemon_log_path = os.path.join(log_dir, "daemon_console.log")

class SafeStreamWrapper:
    """包装标准输出流，保证双写文件并防止父进程退出时管道破裂崩溃"""
    def __init__(self, target_stream, log_file_path):
        self.target = target_stream
        self.log_file_path = log_file_path
        self._fp = None

    def _get_fp(self):
        if self._fp is None and self.log_file_path:
            try:
                self._fp = open(self.log_file_path, "a", encoding="utf-8", buffering=1)
            except Exception:
                pass
        return self._fp

    def write(self, s):
        if self.target and hasattr(self.target, "write"):
            try:
                self.target.write(s)
            except Exception:
                self.target = None
        fp = self._get_fp()
        if fp:
            try:
                fp.write(s)
            except Exception:
                pass

    def flush(self):
        if self.target and hasattr(self.target, "flush"):
            try:
                self.target.flush()
            except Exception:
                self.target = None
        fp = self._get_fp()
        if fp:
            try:
                fp.flush()
            except Exception:
                pass

sys.stdout = SafeStreamWrapper(sys.stdout, daemon_log_path)
sys.stderr = SafeStreamWrapper(sys.stderr, daemon_log_path)
CONFIG_PATH = os.path.join(APP_DIR, "config.json")

# 读取配置
with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    CONFIG = json.load(f)

SERVER_CFG = CONFIG.get("server", {"host": "127.0.0.1", "port": 8088})
MONITOR_CFG = CONFIG.get("monitor", {})

def resolve_dir_path(path_str, default_rel):
    if not path_str:
        return os.path.normpath(os.path.join(APP_DIR, default_rel))
    p = str(path_str).strip()
    if os.path.isabs(p):
        return os.path.normpath(p)
    return os.path.normpath(os.path.join(APP_DIR, p))

WATCH_DIR = resolve_dir_path(MONITOR_CFG.get("watch_dir", "incoming_audio"), "incoming_audio")
raw_proc = MONITOR_CFG.get("processed_dir")
if raw_proc:
    PROCESSED_DIR = resolve_dir_path(raw_proc, os.path.join(WATCH_DIR, "processed"))
else:
    PROCESSED_DIR = os.path.join(WATCH_DIR, "processed")
RECORDS_DIR = os.path.join(APP_DIR, "records")
HISTORY_JSON = os.path.join(RECORDS_DIR, "history.json")
SYSTEM_LOG_PATH = os.path.join(RECORDS_DIR, "system.log")

FIELD_RECORD_DIR = resolve_dir_path(CONFIG.get("field_record_dir", "incoming_audio"), "incoming_audio")
raw_code_base = CONFIG.get("code_base_dir", "models_repo")
CODE_BASE_DIR = resolve_dir_path(raw_code_base, "models_repo")
if not os.path.exists(CODE_BASE_DIR):
    local_repo = os.path.join(APP_DIR, "models_repo")
    if os.path.exists(local_repo):
        CODE_BASE_DIR = local_repo
    else:
        parent_repo = os.path.join(os.path.dirname(APP_DIR), "models_repo")
        if os.path.exists(parent_repo):
            CODE_BASE_DIR = parent_repo
        elif os.path.exists(r"P:\01-work\022-audio detect"):
            CODE_BASE_DIR = r"P:\01-work\022-audio detect"
        else:
            CODE_BASE_DIR = APP_DIR

ALARM_THRESH_PCT = float(MONITOR_CFG.get("alarm_threshold_pct", 90.0))

os.makedirs(WATCH_DIR, exist_ok=True)
os.makedirs(PROCESSED_DIR, exist_ok=True)
os.makedirs(RECORDS_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# 检测结果分类归档目录映射
# ---------------------------------------------------------------------------
RESULT_SUBDIRS = {
    "CRITICAL_COLLISION_UNANIMOUS": "01_全票确诊碰撞_3票",
    "MAJORITY_COLLISION_ALARM": "02_多数表决碰撞_2票",
    "ISOLATED_DISTURBANCE_SAFE": "03_孤立扰动拦截_1票",
    "NORMAL_CLEAN_NOISE": "04_正常平稳底噪_0票",
    "HARDWARE_CLIPPING_DISCARD": "05_硬件削顶死锁",
    "MACHINE_STARTUP_SAFE": "06_现场机组启动工况",
    "MACHINE_SHUTDOWN_SAFE": "07_现场机组停机工况"
}

def archive_processed_file(src_path, consensus_level):
    """根据诊断结果将检测过的音频文件移动存入对应分类子目录 (带 Windows 句柄锁重试容错)"""
    if not os.path.exists(src_path):
        return ""
    fn = os.path.basename(src_path)
    sub = RESULT_SUBDIRS.get(consensus_level, "04_正常平稳底噪_0票")
    target_dir = os.path.join(PROCESSED_DIR, sub)
    os.makedirs(target_dir, exist_ok=True)
    dest_path = os.path.join(target_dir, fn)
    
    # Windows 平台文件移动重试机制 (应对 GC 释放延迟或系统杀毒/监听扫描临时占用)
    for attempt in range(6):
        try:
            if os.path.exists(dest_path):
                os.remove(dest_path)
            shutil.move(src_path, dest_path)
            add_log("INFO", f"音频文件按结果归档至: incoming_audio/processed/{sub}/{fn}")
            return dest_path
        except PermissionError:
            time.sleep(0.2)
        except Exception as e:
            time.sleep(0.2)
            
    # 若重试后仍被占用，退化为 copy + 尝试删除
    try:
        shutil.copy2(src_path, dest_path)
        try:
            os.remove(src_path)
        except Exception:
            pass
        add_log("INFO", f"音频文件按结果归档至: incoming_audio/processed/{sub}/{fn}")
        return dest_path
    except Exception as e:
        add_log("ERROR", f"归档文件最终移动失败: {e}")
        return src_path

# ---------------------------------------------------------------------------
# 人工复核状态定义
# ---------------------------------------------------------------------------
REVIEW_STATUS_MAP = {
    "CONFIRMED_COLLISION": {"text": "确认为真碰撞", "badge": "badge-rev-collision"},
    "CONFIRMED_FALSE_ALARM": {"text": "确认为误报/扰动", "badge": "badge-rev-false-alarm"},
    "CONFIRMED_NORMAL": {"text": "确认正常运转", "badge": "badge-rev-normal"},
    "CONFIRMED_STARTUP": {"text": "确认现场启动", "badge": "badge-rev-startup"},
    "CONFIRMED_SHUTDOWN": {"text": "确认现场停机", "badge": "badge-rev-shutdown"},
    "CONFIRMED_HARDWARE_FAULT": {"text": "传感器/硬件故障", "badge": "badge-rev-hardware"},
    "UNREVIEWED": {"text": "待复核", "badge": "badge-unreviewed"}
}

def format_review_info(record):
    """标准化记录中的人工复核字段"""
    rev = record.get("review")
    if not isinstance(rev, dict):
        record["review"] = {
            "status": "UNREVIEWED",
            "status_text": "待复核",
            "badge_class": "badge-unreviewed",
            "reviewer": "",
            "reviewed_at": "",
            "notes": ""
        }
    else:
        status = rev.get("status", "UNREVIEWED")
        info = REVIEW_STATUS_MAP.get(status, REVIEW_STATUS_MAP["UNREVIEWED"])
        rev["status_text"] = info["text"]
        rev["badge_class"] = info["badge"]
    return record

# ---------------------------------------------------------------------------
# 系统日志管理器
# ---------------------------------------------------------------------------
system_logs = []
log_lock = threading.Lock()

def add_log(level, msg):
    """记录一条格式化系统日志"""
    now_str = time.strftime("%Y-%m-%d %H:%M:%S")
    entry = {"time": now_str, "level": level.upper(), "msg": msg}
    with log_lock:
        system_logs.append(entry)
        if len(system_logs) > 3000:
            system_logs.pop(0)
        try:
            with open(SYSTEM_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(f"[{now_str}] [{level.upper()}] {msg}\n")
        except Exception:
            pass
    print(f"[{now_str}] [{level.upper()}] {msg}")

# 加载历史日志
if os.path.exists(SYSTEM_LOG_PATH):
    try:
        with open(SYSTEM_LOG_PATH, "r", encoding="utf-8") as f:
            for line in f.readlines()[-500:]:
                line = line.strip()
                if line.startswith("[") and "] [" in line:
                    parts = line.split("] [", 2)
                    if len(parts) >= 3:
                        t_str = parts[0].lstrip("[")
                        lvl = parts[1]
                        m_str = parts[2].rstrip("]")
                        system_logs.append({"time": t_str, "level": lvl, "msg": m_str})
    except Exception as e:
        print(f"[!] 读取历史日志提示: {e}")

# ---------------------------------------------------------------------------
# 宿主机性能保护：CPU 与内存上限限制 (最大 50% 保护隔离屏障)
# ---------------------------------------------------------------------------
RESOURCE_LIMIT_INFO = {
    "enabled": False,
    "max_cpu_percent": 50,
    "max_memory_percent": 50,
    "total_cores": os.cpu_count() or 4,
    "allowed_cores_count": os.cpu_count() or 4,
    "allowed_cores": list(range(os.cpu_count() or 4)),
    "total_ram_gb": round(psutil.virtual_memory().total / (1024**3), 2),
    "max_allowed_ram_gb": round(psutil.virtual_memory().total * 0.5 / (1024**3), 2),
    "pytorch_threads": torch.get_num_threads(),
    "process_priority": "NORMAL",
    "status_msg": "未启用资源限制"
}

def apply_system_resource_limits():
    """根据配置文件动态应用 CPU 与内存资源硬性隔离限制 (最大 50%)"""
    global RESOURCE_LIMIT_INFO
    res_cfg = CONFIG.get("resource_limit", {})
    if not res_cfg.get("enable", True):
        RESOURCE_LIMIT_INFO["enabled"] = False
        RESOURCE_LIMIT_INFO["status_msg"] = "资源限制已关闭 (全量性能模式)"
        return

    try:
        proc = psutil.Process()
        total_cores = os.cpu_count() or 4
        max_cpu_pct = float(res_cfg.get("max_cpu_percent", 50))
        max_mem_pct = float(res_cfg.get("max_memory_percent", 50))
        prio_cfg = res_cfg.get("process_priority", "BELOW_NORMAL")

        # 1. CPU 核心亲和性锁定 (CPU Affinity - 内核级硬件隔离)
        allowed_count = max(1, int(total_cores * (max_cpu_pct / 100.0)))
        allowed_cores = list(range(allowed_count))
        try:
            proc.cpu_affinity(allowed_cores)
        except Exception:
            pass

        # 2. PyTorch 计算并发线程限制 (GEMM 算力压制)
        torch.set_num_threads(allowed_count)
        try:
            torch.set_num_interop_threads(max(1, allowed_count // 2))
        except Exception:
            pass

        # 3. 降低进程调度优先级 (让路风机 SCADA 与主控)
        prio_applied = "NORMAL"
        try:
            if hasattr(psutil, "BELOW_NORMAL_PRIORITY_CLASS") and prio_cfg == "BELOW_NORMAL":
                proc.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
                prio_applied = "BELOW_NORMAL"
            elif prio_cfg == "IDLE":
                if hasattr(psutil, "IDLE_PRIORITY_CLASS"):
                    proc.nice(psutil.IDLE_PRIORITY_CLASS)
                else:
                    proc.nice(19)
                prio_applied = "IDLE"
            else:
                if os.name != "nt":
                    proc.nice(10)
                    prio_applied = "NICE_10"
        except Exception:
            pass

        # 4. 内存限额计算
        total_ram = psutil.virtual_memory().total
        max_ram_bytes = int(total_ram * (max_mem_pct / 100.0))
        max_ram_gb = round(max_ram_bytes / (1024**3), 2)

        RESOURCE_LIMIT_INFO = {
            "enabled": True,
            "max_cpu_percent": max_cpu_pct,
            "max_memory_percent": max_mem_pct,
            "total_cores": total_cores,
            "allowed_cores_count": allowed_count,
            "allowed_cores": allowed_cores,
            "total_ram_gb": round(total_ram / (1024**3), 2),
            "max_allowed_ram_gb": max_ram_gb,
            "pytorch_threads": allowed_count,
            "process_priority": prio_applied,
            "status_msg": f"已锁定使用最大 50% CPU ({allowed_count}/{total_cores} 核) 与最大 50% 内存 ({max_ram_gb} GB 上限)"
        }

        add_log("INFO", f"[🛡️ 宿主机性能保护生效] CPU锁定前 {allowed_count}/{total_cores} 核 ({max_cpu_pct}%)，PyTorch并发限制为 {allowed_count} 线程，进程优先级降为 {prio_applied}，内存上限 {max_ram_gb} GB ({max_mem_pct}%)")
    except Exception as e:
        add_log("WARN", f"[!] 宿主机性能限制初始化异常: {e}")

# 启动初期即刻生效资源限制
apply_system_resource_limits()

def memory_guard_loop():
    """轻量级内存巡检守护线程 (每 10 秒检测一次，防止边缘端内存超限)"""
    while True:
        try:
            time.sleep(10)
            if not RESOURCE_LIMIT_INFO.get("enabled", False):
                continue
            proc = psutil.Process()
            rss_bytes = proc.memory_info().rss
            total_ram = psutil.virtual_memory().total
            max_pct = float(RESOURCE_LIMIT_INFO.get("max_memory_percent", 50))
            cur_pct = (rss_bytes / total_ram) * 100.0

            # 若接近限额 (达到 45%)，主动执行垃圾回收与缓存清理
            if cur_pct >= (max_pct * 0.90):
                gc.collect()
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                add_log("WARN", f"[🛡️ 内存水位预警] 进程内存占用达到 {cur_pct:.1f}% (上限 {max_pct}%)，已自动触发主动垃圾回收与切片缓存释放")
        except Exception:
            pass

memory_guard_thread = threading.Thread(target=memory_guard_loop, daemon=True)
memory_guard_thread.start()

add_log("INFO", "🚀 [3-Model Monitor Server] 初始化 3 核心模型协同投票系统...")
add_log("INFO", f"系统配置载入: 监听目录={WATCH_DIR}, 单模型阈值={ALARM_THRESH_PCT}%")

# 导入仲裁引擎
from models.voter import TripleModelVoterEngine

# 初始化模型单例
MIN_RMS_THRESH = float(MONITOR_CFG.get("min_rms_threshold", 0.02))
engine = TripleModelVoterEngine(
    code_base_dir=CODE_BASE_DIR,
    alarm_thresh_pct=ALARM_THRESH_PCT,
    min_rms_threshold=MIN_RMS_THRESH
)
add_log("INFO", "3 核心黄金铁三角模型 (03K Dasheng + 03U ECHOv2 + 03R GRAM-Mono) 载入完成，门禁仲裁就绪")

# 全局状态与线程安全锁
inference_lock = threading.Lock()
processing_lock = threading.Lock()
processing_filenames = set()
history_records = []
latest_result = None
is_algorithm_running = True

def save_history():
    """保存历史记录至本地磁盘 (原子写入，杜绝外部并发读取冲突)"""
    try:
        os.makedirs(os.path.dirname(HISTORY_JSON), exist_ok=True)
        tmp_path = HISTORY_JSON + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(history_records[:50000], f, ensure_ascii=False, indent=2)
        if os.path.exists(tmp_path):
            os.replace(tmp_path, HISTORY_JSON)
    except Exception as e:
        add_log("ERROR", f"保存历史记录失败: {e}")

def sync_processed_files_to_history():
    """
    扫描 incoming_audio/processed 各分类子目录，补充磁盘存在但历史台账尚未收录的文件记录。
    确保启动时全量对齐所有已处理的历史音频。
    """
    global history_records
    DIR_TO_CONSENSUS = {
        "01_全票确诊碰撞_3票": {"consensus_level": "UNANIMOUS_COLLISION", "consensus_title": "全票碰撞", "consensus_badge": "badge-danger", "alarm_votes": 3, "is_alarm": True, "mean_confidence_pct": 95.0},
        "02_多数表决碰撞_2票": {"consensus_level": "MAJORITY_COLLISION", "consensus_title": "多数碰撞", "consensus_badge": "badge-danger", "alarm_votes": 2, "is_alarm": True, "mean_confidence_pct": 88.0},
        "03_孤立扰动拦截_1票": {"consensus_level": "SHIELDED_ISOLATED_DISTURBANCE", "consensus_title": "扰动拦截", "consensus_badge": "badge-warning", "alarm_votes": 1, "is_alarm": False, "mean_confidence_pct": 33.0},
        "04_正常平稳底噪_0票": {"consensus_level": "NORMAL_CLEAN_NOISE", "consensus_title": "正常平稳", "consensus_badge": "badge-success", "alarm_votes": 0, "is_alarm": False, "mean_confidence_pct": 0.0},
        "05_硬件削顶死锁": {"consensus_level": "HARDWARE_CLIPPING_DISCARD", "consensus_title": "幅值饱和", "consensus_badge": "badge-secondary", "alarm_votes": 0, "is_alarm": False, "mean_confidence_pct": 0.0},
        "06_现场机组启动工况": {"consensus_level": "MACHINE_STARTUP_SAFE", "consensus_title": "机组启动", "consensus_badge": "badge-secondary", "alarm_votes": 0, "is_alarm": False, "mean_confidence_pct": 0.0},
        "07_现场机组停机工况": {"consensus_level": "MACHINE_SHUTDOWN_SAFE", "consensus_title": "机组停机", "consensus_badge": "badge-secondary", "alarm_votes": 0, "is_alarm": False, "mean_confidence_pct": 0.0},
    }
    
    if not os.path.exists(PROCESSED_DIR):
        return

    known_filenames = {r.get("filename") for r in history_records if "filename" in r}
    added_count = 0
    
    for sub, meta in DIR_TO_CONSENSUS.items():
        sub_dir = os.path.join(PROCESSED_DIR, sub)
        if not os.path.isdir(sub_dir):
            continue
        try:
            for item in os.listdir(sub_dir):
                if not item.lower().endswith((".bin", ".wav")):
                    continue
                if item in known_filenames:
                    continue
                
                full_p = os.path.join(sub_dir, item)
                m = re.search(r"(?:SY)?(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})", item)
                if m:
                    ts = f"{m.group(1)}-{m.group(2)}-{m.group(3)} {m.group(4)}:{m.group(5)}:{m.group(6)}"
                else:
                    try:
                        mtime = os.path.getmtime(full_p)
                        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime))
                    except Exception:
                        ts = time.strftime("%Y-%m-%d %H:%M:%S")

                syn_record = {
                    "success": True,
                    "filename": item,
                    "timestamp": ts,
                    "duration_sec": 20.0,
                    "peak_amplitude": 0.0,
                    "rms_amplitude": 0.0,
                    "crest_factor": 0.0,
                    "is_clipped": (meta["consensus_level"] == "HARDWARE_CLIPPING_DISCARD"),
                    "rail_voltage": 0.0,
                    "clip_msg": "",
                    "scores": {},
                    "alarms": {},
                    "model_details": {},
                    "alarm_votes": meta["alarm_votes"],
                    "mean_confidence_pct": meta["mean_confidence_pct"],
                    "consensus_level": meta["consensus_level"],
                    "consensus_title": meta["consensus_title"],
                    "consensus_badge": meta["consensus_badge"],
                    "is_alarm": meta["is_alarm"],
                    "infer_ms": 0,
                    "wav_file": "",
                    "plot_file": "",
                }
                format_review_info(syn_record)
                history_records.append(syn_record)
                known_filenames.add(item)
                added_count += 1
        except Exception as e:
            add_log("WARN", f"扫描归档目录补充历史记录异常 ({sub}): {e}")

    if added_count > 0:
        history_records.sort(key=lambda x: str(x.get("timestamp", "")), reverse=True)
        save_history()
        add_log("INFO", f"已从本地 processed 归档目录自动补充已处理音频记录 {added_count} 条")

def load_initial_history_records():
    """
    启动时自动加载历史处理记录：
    1. 优先读取当前部署包 records/history.json；
    2. 若本地无记录或为空，自动从母本历史记录目录回退并同步拷贝；
    3. 补充已处理归档子目录中可能未入库的历史音频；
    4. 格式化复核字段与状态，全局生效。
    """
    global history_records, latest_result
    loaded = []

    # 1. 尝试从本地 records/history.json 读取
    if os.path.exists(HISTORY_JSON):
        try:
            with open(HISTORY_JSON, "r", encoding="utf-8") as f:
                content = json.load(f)
                if isinstance(content, list) and len(content) > 0:
                    loaded = content
        except Exception as e:
            add_log("ERROR", f"读取本地历史记录失败: {e}")
            loaded = []

    # 2. 本地为空则自动尝试从候选备份源恢复
    if not loaded:
        fallback_candidates = [
            r"P:\BaiduSyncdisk\022-collision_audio_system\03_triple_model_realtime_monitor\records\history.json",
            os.path.join(CODE_BASE_DIR, "03_triple_model_realtime_monitor", "records", "history.json"),
            os.path.join(os.path.dirname(BASE_DIR), "03_triple_model_realtime_monitor", "records", "history.json")
        ]
        for fb_path in fallback_candidates:
            if os.path.exists(fb_path):
                try:
                    with open(fb_path, "r", encoding="utf-8") as f:
                        fb_records = json.load(f)
                        if isinstance(fb_records, list) and len(fb_records) > 0:
                            loaded = fb_records
                            add_log("INFO", f"已从历史母本归档成功恢复 {len(loaded)} 条诊断记录: {fb_path}")
                            try:
                                os.makedirs(os.path.dirname(HISTORY_JSON), exist_ok=True)
                                shutil.copy2(fb_path, HISTORY_JSON)
                            except Exception as copy_e:
                                add_log("WARN", f"同步母本历史记录至本地失败: {copy_e}")
                            break
                except Exception as e:
                    add_log("WARN", f"读取历史候选源失败 {fb_path}: {e}")

    # 3. 标准化 review 字段与标签
    for r in loaded:
        format_review_info(r)
        if r.get("consensus_level") == "HARDWARE_CLIPPING_DISCARD":
            r["consensus_title"] = "幅值饱和"

    history_records = loaded

    # 4. 同步 processed 归档目录中遗留的有效已处理音频
    sync_processed_files_to_history()

    if history_records:
        latest_result = history_records[0]
    add_log("INFO", f"历史诊断记录加载完成，系统就绪总数: {len(history_records)} 条")

# 启动时将 processed 根目录现存散落的文件按结果自动整理到分类子目录
def organize_existing_processed_files():
    fn_to_level = {r["filename"]: r.get("consensus_level", "NORMAL_CLEAN_NOISE") for r in history_records if "filename" in r}
    try:
        if not os.path.exists(PROCESSED_DIR):
            return
        for item in os.listdir(PROCESSED_DIR):
            p = os.path.join(PROCESSED_DIR, item)
            if os.path.isfile(p) and item.lower().endswith((".bin", ".wav")):
                lvl = fn_to_level.get(item, "NORMAL_CLEAN_NOISE")
                archive_processed_file(p, lvl)
    except Exception as e:
        add_log("WARN", f"整理历史归档散落文件异常: {e}")

# 启动执行自动加载历史记录与散落归档文件整理
load_initial_history_records()
organize_existing_processed_files()

def process_file_and_record(file_path):
    """单例互斥执行 3 模型诊断并入库，在历史记录基础上递增"""
    global latest_result
    with inference_lock:
        res = engine.analyze_audio_file(file_path, output_dir=RECORDS_DIR, generate_plot=True)
        if res.get("success", False):
            # 初始化复核字段
            format_review_info(res)
            
            latest_result = res
            
            # 去重：若历史中已存在同名记录，先移除旧记录，再将最新诊断结果插在首位递增
            fn = res.get("filename")
            for i, old_r in enumerate(history_records):
                if old_r.get("filename") == fn:
                    history_records.pop(i)
                    break

            history_records.insert(0, res)
            if len(history_records) > 50000:
                history_records.pop()
            save_history()
            
            is_alarm = res.get("is_alarm", False)
            lvl_name = "WARN" if is_alarm else "INFO"
            add_log(lvl_name, f"诊断完成 [{res.get('consensus_level')}] 文件: {res.get('filename')} | 票数: {res.get('alarm_votes')}/3 | 均值: {res.get('mean_confidence_pct')}% | 耗时: {res.get('infer_ms')}ms")
        else:
            add_log("ERROR", f"诊断失败: {res.get('error', '未知异常')}")
        return res

# ---------------------------------------------------------------------------
# 后台文件夹监听守护线程
# ---------------------------------------------------------------------------
def directory_watcher_loop():
    add_log("INFO", f"启动后台音频目录监听守护线程 (每 {MONITOR_CFG.get('scan_interval_sec', 1.0)} 秒扫描一次)...")
    while True:
        try:
            if not is_algorithm_running:
                time.sleep(1.0)
                continue
            
            cur_watch_dir = WATCH_DIR
            if not os.path.exists(cur_watch_dir):
                try:
                    os.makedirs(cur_watch_dir, exist_ok=True)
                except Exception:
                    time.sleep(1.0)
                    continue

            valid_exts = (".bin", ".wav")
            items = os.listdir(cur_watch_dir)
            for item in items:
                p = os.path.join(cur_watch_dir, item)
                if os.path.isfile(p) and item.lower().endswith(valid_exts):
                    # 校验文件大小稳定性
                    size1 = os.path.getsize(p)
                    time.sleep(0.3)
                    size2 = os.path.getsize(p)
                    if size1 != size2 or size1 == 0:
                        continue
                    
                    with processing_lock:
                        if item in processing_filenames:
                            continue
                        processing_filenames.add(item)
                    
                    try:
                        add_log("INFO", f"[📡 实时推流捕获] 发现待测音频: {item} ({size2/1024:.1f} KB)")
                        res = process_file_and_record(p)
                        archive_processed_file(p, res.get("consensus_level", "NORMAL_CLEAN_NOISE"))
                    finally:
                        with processing_lock:
                            processing_filenames.discard(item)
                    
        except Exception as e:
            add_log("ERROR", f"监听循环异常: {e}")
            
        time.sleep(float(MONITOR_CFG.get("scan_interval_sec", 1.0)))

watcher_thread = threading.Thread(target=directory_watcher_loop, daemon=True)
watcher_thread.start()

# ---------------------------------------------------------------------------
# Flask Web 服务
# ---------------------------------------------------------------------------
app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["TEMPLATES_AUTO_RELOAD"] = True

# 1. 实时大屏页面
@app.route("/")
def index():
    return render_template("index.html", config=CONFIG)

# 2. 后台管理页面与分析统计页面
@app.route("/admin")
def admin_page():
    active_tab = request.args.get("tab", "records")
    return render_template("admin.html", config=CONFIG, active_tab=active_tab)

@app.route("/analytics")
def analytics_page():
    return render_template("admin.html", config=CONFIG, active_tab="analytics")

# 3. 实时遥测数据接口
@app.route("/api/latest", methods=["GET"])
def api_latest():
    if latest_result:
        res = dict(latest_result)
        res["is_algorithm_running"] = is_algorithm_running
        res["watch_dir"] = MONITOR_CFG.get("watch_dir", "incoming_audio")
        return jsonify(res)
    return jsonify({
        "success": False,
        "is_algorithm_running": is_algorithm_running,
        "watch_dir": MONITOR_CFG.get("watch_dir", "incoming_audio"),
        "filename": "等待音频流输入...",
        "consensus_level": "NORMAL_CLEAN_NOISE",
        "consensus_title": "系统待机中，等待 incoming_audio 音频注入或手动投递..." if is_algorithm_running else "【算法已暂停】检测系统已停止，等待管理员启动...",
        "alarm_votes": 0,
        "mean_confidence_pct": 0.0,
        "scores": {"03K": 0.0, "03U": 0.0, "03R": 0.0},
        "alarms": {"03K": 0, "03U": 0, "03R": 0},
        "peak_amplitude": 0.0,
        "rms_amplitude": 0.0,
        "crest_factor": 0.0,
        "is_clipped": False,
        "infer_ms": 0.0,
        "review": {
            "status": "UNREVIEWED",
            "status_text": "待复核",
            "badge_class": "badge-unreviewed"
        }
    })

@app.route("/api/history", methods=["GET"])
def api_history():
    limit = request.args.get("limit", default=10000, type=int)
    
    total = len(history_records)
    unanimous = sum(1 for r in history_records if r.get("alarm_votes") == 3)
    majority = sum(1 for r in history_records if r.get("alarm_votes") == 2 and r.get("is_alarm", False))
    shielded = sum(1 for r in history_records if r.get("consensus_level") == "ISOLATED_DISTURBANCE_SAFE")
    normal = sum(1 for r in history_records if r.get("consensus_level") == "NORMAL_CLEAN_NOISE")
    clipped = sum(1 for r in history_records if r.get("is_clipped", False))
    startup = sum(1 for r in history_records if r.get("consensus_level") == "MACHINE_STARTUP_SAFE")
    shutdown = sum(1 for r in history_records if r.get("consensus_level") == "MACHINE_SHUTDOWN_SAFE")

    recs = history_records[:limit] if limit > 0 else history_records
    return jsonify({
        "success": True,
        "total": total,
        "is_algorithm_running": is_algorithm_running,
        "kpi": {
            "total": total,
            "unanimous": unanimous,
            "majority": majority,
            "shielded": shielded,
            "normal": normal,
            "clipped": clipped,
            "startup": startup,
            "shutdown": shutdown
        },
        "records": recs
    })

# 4. 手动音频投递上传分析 (按结果分类归档)
@app.route("/api/upload", methods=["POST"])
def api_upload():
    if not is_algorithm_running:
        return jsonify({"success": False, "error": "算法检测系统当前已停止运行，请先在后台设置界面点击【启动算法检测】后再投递分析！"}), 400
    if "file" not in request.files:
        return jsonify({"success": False, "error": "未提供上传文件"}), 400
    file = request.files["file"]
    if file.filename == "":
        return jsonify({"success": False, "error": "文件名为空"}), 400

    fn = file.filename
    ext = os.path.splitext(fn)[1].lower()
    if ext not in [".wav", ".bin"]:
        return jsonify({"success": False, "error": f"不支持的文件格式: {ext}，系统全面支持 .wav 与 .bin 音频格式"}), 400

    with processing_lock:
        processing_filenames.add(fn)

    try:
        save_path = os.path.join(WATCH_DIR, fn)
        file.save(save_path)
        add_log("INFO", f"[📁 工作台手动投递] 上传音频文件: {fn}")
        res = process_file_and_record(save_path)
        
        # 按检测结果存放归档
        archive_processed_file(save_path, res.get("consensus_level", "NORMAL_CLEAN_NOISE"))
        return jsonify({"success": True, "result": res})
    finally:
        with processing_lock:
            processing_filenames.discard(fn)

# 5. 静态资源免缓存路由
@app.route("/records/<path:filename>")
def serve_records(filename):
    local_path = os.path.join(RECORDS_DIR, filename)
    target_dir = RECORDS_DIR
    if not os.path.exists(local_path):
        fallback_dirs = [
            os.path.join(CODE_BASE_DIR, "03_triple_model_realtime_monitor", "records"),
            r"P:\BaiduSyncdisk\022-collision_audio_system\03_triple_model_realtime_monitor\records",
            os.path.join(os.path.dirname(BASE_DIR), "03_triple_model_realtime_monitor", "records")
        ]
        for fb in fallback_dirs:
            if os.path.exists(os.path.join(fb, filename)):
                target_dir = fb
                break
    resp = send_from_directory(target_dir, filename)
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp

# ---------------------------------------------------------------------------
# 人工复核接口 (Manual Review API)
# ---------------------------------------------------------------------------
@app.route("/api/records/review", methods=["POST"])
def api_records_review():
    """保存或更新人工复核结论与审核备注"""
    global latest_result
    data = request.json or {}
    fn = data.get("filename")
    ts = data.get("timestamp")
    status = data.get("status", "CONFIRMED_COLLISION")
    reviewer = data.get("reviewer", "现场审核员").strip() or "现场审核员"
    notes = data.get("notes", "").strip()

    if not fn:
        return jsonify({"success": False, "error": "未提供文件名"}), 400

    if status not in REVIEW_STATUS_MAP:
        return jsonify({"success": False, "error": f"无效的复核状态: {status}"}), 400

    status_info = REVIEW_STATUS_MAP[status]
    now_str = time.strftime("%Y-%m-%d %H:%M:%S")

    matched = None
    with inference_lock:
        for r in history_records:
            if r.get("filename") == fn and (not ts or r.get("timestamp") == ts):
                r["review"] = {
                    "status": status,
                    "status_text": status_info["text"],
                    "badge_class": status_info["badge"],
                    "reviewer": reviewer,
                    "reviewed_at": now_str,
                    "notes": notes
                }
                matched = r
                break
        
        if matched:
            save_history()
            if latest_result and latest_result.get("filename") == fn:
                latest_result["review"] = matched["review"]

    if matched:
        add_log("INFO", f"[✍️ 人工复核] 文件 {fn} 审核结论: 【{status_info['text']}】(审核员: {reviewer}) 备注: {notes or '无'}")
        return jsonify({
            "success": True,
            "record": matched,
            "review": matched["review"]
        })
    return jsonify({"success": False, "error": "未找到匹配记录"}), 404

# ---------------------------------------------------------------------------
# 管理后台专用 REST API
# ---------------------------------------------------------------------------
@app.route("/api/admin/logs", methods=["GET"])
def api_admin_logs():
    """获取系统运行日志"""
    level_filter = request.args.get("level", "ALL").upper()
    keyword = request.args.get("search", "").strip().lower()
    limit = int(request.args.get("limit", 500))

    with log_lock:
        filtered = []
        for l in reversed(system_logs):
            if level_filter != "ALL" and l["level"] != level_filter:
                continue
            if keyword and keyword not in l["msg"].lower() and keyword not in l["time"]:
                continue
            filtered.append(l)
            if len(filtered) >= limit:
                break

    return jsonify({
        "success": True,
        "total": len(system_logs),
        "count": len(filtered),
        "logs": filtered
    })

@app.route("/api/admin/logs/clear", methods=["POST"])
def api_admin_logs_clear():
    """清空运行日志"""
    global system_logs
    with log_lock:
        system_logs = []
        try:
            with open(SYSTEM_LOG_PATH, "w", encoding="utf-8") as f:
                f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [INFO] 管理员清空了运行日志\n")
        except Exception:
            pass
    add_log("INFO", "管理员执行了运行日志清空操作")
    return jsonify({"success": True, "msg": "系统日志已清空"})

@app.route("/api/admin/logs/download", methods=["GET"])
def api_admin_logs_download():
    """下载完整日志文件"""
    if os.path.exists(SYSTEM_LOG_PATH):
        return send_file(SYSTEM_LOG_PATH, as_attachment=True, download_name=f"system_log_{time.strftime('%Y%m%d')}.log")
    return jsonify({"success": False, "error": "日志文件不存在"}), 404

@app.route("/api/admin/records", methods=["GET"])
def api_admin_records():
    """获取管理端诊断记录列表 (支持分页与多维筛选)"""
    search = request.args.get("search", "").strip().lower()
    consensus = request.args.get("consensus", "ALL").upper()
    rev_filter = request.args.get("review_status", "ALL").upper()
    date_filter = request.args.get("date", "").strip()
    page = int(request.args.get("page", 1))
    page_size = int(request.args.get("page_size", 50))

    filtered = []
    for r in history_records:
        format_review_info(r)
        
        if search and search not in r.get("filename", "").lower() and search not in r.get("timestamp", ""):
            continue

        if date_filter:
            d_no_h = date_filter.replace("-", "")
            ts_hit = r.get("timestamp", "").startswith(date_filter)
            fn_hit = d_no_h in r.get("filename", "")
            if not ts_hit and not fn_hit:
                continue
        
        if consensus != "ALL":
            if consensus == "ALARM" and not r.get("is_alarm", False):
                continue
            elif consensus == "VOTE3" and r.get("alarm_votes") != 3 and r.get("consensus_level") != "CRITICAL_COLLISION_UNANIMOUS":
                continue
            elif consensus == "VOTE2" and r.get("alarm_votes") != 2 and r.get("consensus_level") != "MAJORITY_COLLISION_ALARM":
                continue
            elif consensus == "SHIELDED" and r.get("consensus_level") != "ISOLATED_DISTURBANCE_SAFE" and r.get("alarm_votes") != 1:
                continue
            elif consensus == "NORMAL" and r.get("consensus_level") != "NORMAL_CLEAN_NOISE" and r.get("alarm_votes") != 0:
                continue
            elif consensus == "STARTUP" and r.get("consensus_level") != "MACHINE_STARTUP_SAFE" and r.get("review", {}).get("status") != "CONFIRMED_STARTUP":
                continue
            elif consensus == "SHUTDOWN" and r.get("consensus_level") != "MACHINE_SHUTDOWN_SAFE" and r.get("review", {}).get("status") != "CONFIRMED_SHUTDOWN":
                continue
            elif consensus == "CLIPPED" and not r.get("is_clipped", False):
                continue

        if rev_filter != "ALL":
            cur_rev = r.get("review", {}).get("status", "UNREVIEWED")
            if rev_filter == "UNREVIEWED" and cur_rev != "UNREVIEWED":
                continue
            elif rev_filter == "REVIEWED" and cur_rev == "UNREVIEWED":
                continue
            elif rev_filter in REVIEW_STATUS_MAP and cur_rev != rev_filter:
                continue

        filtered.append(r)

    total = len(filtered)
    start_idx = (page - 1) * page_size
    end_idx = start_idx + page_size
    paginated = filtered[start_idx:end_idx]

    return jsonify({
        "success": True,
        "total": total,
        "page": page,
        "page_size": page_size,
        "records": paginated
    })

@app.route("/api/admin/records/delete", methods=["POST"])
def api_admin_records_delete():
    """删除指定的单条诊断记录"""
    global latest_result
    data = request.json or {}
    fn = data.get("filename")
    ts = data.get("timestamp")

    if not fn:
        return jsonify({"success": False, "error": "未提供文件名"}), 400

    deleted = False
    with inference_lock:
        for i, r in enumerate(history_records):
            if r.get("filename") == fn and (not ts or r.get("timestamp") == ts):
                history_records.pop(i)
                deleted = True
                break
        if deleted:
            save_history()
            if latest_result and latest_result.get("filename") == fn:
                latest_result = history_records[0] if history_records else None
            add_log("WARN", f"管理员删除了历史记录: {fn} ({ts or ''})")
            return jsonify({"success": True, "msg": f"记录 {fn} 已删除"})
        return jsonify({"success": False, "error": "未匹配到指定记录"}), 404

@app.route("/api/admin/records/clear_all", methods=["POST"])
def api_admin_records_clear_all():
    """清空所有历史记录"""
    global history_records, latest_result
    with inference_lock:
        count = len(history_records)
        history_records = []
        latest_result = None
        save_history()
    add_log("WARN", f"管理员执行了全量历史记录清空操作 (共清除 {count} 条记录)")
    return jsonify({"success": True, "msg": f"已成功清除全部 {count} 条历史诊断记录"})

# ---------------------------------------------------------------------------
# 多维数据分析与统计可视化 API
# ---------------------------------------------------------------------------
ANALYTICS_CATEGORIES = [
    {
        "key": "CRITICAL_COLLISION_UNANIMOUS",
        "name": "全票确诊碰撞 (3票)",
        "short_name": "全票碰撞",
        "badge": "badge-crit",
        "color": "#ef4444",
        "is_alarm": True,
        "is_safe": False
    },
    {
        "key": "MAJORITY_COLLISION_ALARM",
        "name": "多数表决碰撞 (2票)",
        "short_name": "多数碰撞",
        "badge": "badge-maj",
        "color": "#f97316",
        "is_alarm": True,
        "is_safe": False
    },
    {
        "key": "ISOLATED_DISTURBANCE_SAFE",
        "name": "孤立扰动拦截 (1票)",
        "short_name": "扰动拦截",
        "badge": "badge-shield",
        "color": "#eab308",
        "is_alarm": False,
        "is_safe": True
    },
    {
        "key": "NORMAL_CLEAN_NOISE",
        "name": "正常平稳底噪 (0票)",
        "short_name": "平稳底噪",
        "badge": "badge-normal",
        "color": "#10b981",
        "is_alarm": False,
        "is_safe": True
    },
    {
        "key": "HARDWARE_CLIPPING_DISCARD",
        "name": "幅值饱和",
        "short_name": "幅值饱和",
        "badge": "badge-clip",
        "color": "#8b5cf6",
        "is_alarm": False,
        "is_safe": True
    },
    {
        "key": "MACHINE_STARTUP_SAFE",
        "name": "现场机组启动工况",
        "short_name": "机组启动",
        "badge": "badge-startup",
        "color": "#06b6d4",
        "is_alarm": False,
        "is_safe": True
    },
    {
        "key": "MACHINE_SHUTDOWN_SAFE",
        "name": "现场机组停机工况",
        "short_name": "机组停机",
        "badge": "badge-shutdown",
        "color": "#64748b",
        "is_alarm": False,
        "is_safe": True
    }
]

def classify_record_category(r):
    level = r.get("consensus_level")
    if level == "HARDWARE_CLIPPING_DISCARD" or r.get("is_clipped", False):
        return "HARDWARE_CLIPPING_DISCARD"
    if level == "MACHINE_STARTUP_SAFE":
        return "MACHINE_STARTUP_SAFE"
    if level == "MACHINE_SHUTDOWN_SAFE":
        return "MACHINE_SHUTDOWN_SAFE"
    if level == "CRITICAL_COLLISION_UNANIMOUS" or r.get("alarm_votes") == 3:
        return "CRITICAL_COLLISION_UNANIMOUS"
    if level == "MAJORITY_COLLISION_ALARM" or (r.get("alarm_votes") == 2 and r.get("is_alarm")):
        return "MAJORITY_COLLISION_ALARM"
    if level == "ISOLATED_DISTURBANCE_SAFE" or (r.get("alarm_votes") == 1 and not r.get("is_alarm")):
        return "ISOLATED_DISTURBANCE_SAFE"
    return "NORMAL_CLEAN_NOISE"

def extract_record_time_info(r, time_dim="audio"):
    fn = r.get("filename", "")
    ts = r.get("timestamp", "")
    d_str = None
    h_str = "00:00"

    if time_dim == "audio":
        m = re.search(r'SY(\d{4})(\d{2})(\d{2})_?(\d{2})?', fn)
        if m:
            d_str = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
            if m.group(4):
                try:
                    h_str = f"{int(m.group(4)):02d}:00"
                except:
                    pass
            elif ts and len(ts) >= 13:
                try:
                    h_str = f"{int(ts[11:13]):02d}:00"
                except:
                    pass

    if not d_str:
        if ts and len(ts) >= 10:
            d_str = ts[:10]
            if len(ts) >= 13:
                try:
                    h_str = f"{int(ts[11:13]):02d}:00"
                except:
                    pass
        else:
            d_str = "2026-08-01"

    return d_str, h_str

@app.route("/api/admin/analytics", methods=["GET"])
def api_admin_analytics():
    """多维数据分析与统计聚合接口"""
    time_dim = request.args.get("time_dim", "audio").lower()
    time_range = request.args.get("range", "all").lower()
    start_date = request.args.get("start_date", "").strip()
    end_date = request.args.get("end_date", "").strip()
    consensus_filter = request.args.get("consensus", "ALL").upper()

    with inference_lock:
        records_snapshot = list(history_records)

    parsed = []
    all_dates_set = set()
    for r in records_snapshot:
        format_review_info(r)
        d_str, h_str = extract_record_time_info(r, time_dim)
        cat = classify_record_category(r)
        parsed.append({
            "record": r,
            "date": d_str,
            "hour": h_str,
            "cat": cat
        })
        all_dates_set.add(d_str)

    sorted_all_dates = sorted(list(all_dates_set))
    max_date_str = sorted_all_dates[-1] if sorted_all_dates else "2026-10-26"
    min_date_str = sorted_all_dates[0] if sorted_all_dates else "2026-08-01"

    # 时间过滤
    filtered = []
    if time_range == "all":
        filtered = parsed
    elif time_range == "custom":
        for p in parsed:
            if start_date and p["date"] < start_date:
                continue
            if end_date and p["date"] > end_date:
                continue
            filtered.append(p)
    elif time_range in ["30d", "7d", "24h"]:
        try:
            max_dt = datetime.strptime(max_date_str, "%Y-%m-%d")
            if time_range == "30d":
                cutoff = (max_dt - timedelta(days=30)).strftime("%Y-%m-%d")
            elif time_range == "7d":
                cutoff = (max_dt - timedelta(days=7)).strftime("%Y-%m-%d")
            elif time_range == "24h":
                cutoff = (max_dt - timedelta(days=1)).strftime("%Y-%m-%d")
            filtered = [p for p in parsed if p["date"] >= cutoff]
        except Exception:
            filtered = parsed
    else:
        filtered = parsed

    if consensus_filter != "ALL":
        filtered = [p for p in filtered if p["cat"] == consensus_filter]

    total_matched = len(filtered)
    cat_keys = [c["key"] for c in ANALYTICS_CATEGORIES]

    # 1. KPI 计算
    unanimous_cnt = sum(1 for p in filtered if p["cat"] == "CRITICAL_COLLISION_UNANIMOUS")
    majority_cnt = sum(1 for p in filtered if p["cat"] == "MAJORITY_COLLISION_ALARM")
    alarm_cnt = unanimous_cnt + majority_cnt
    alarm_rate = round(alarm_cnt / total_matched * 100, 2) if total_matched else 0.0

    shielded_cnt = sum(1 for p in filtered if p["cat"] == "ISOLATED_DISTURBANCE_SAFE")
    shielded_rate = round(shielded_cnt / total_matched * 100, 2) if total_matched else 0.0

    normal_cnt = sum(1 for p in filtered if p["cat"] == "NORMAL_CLEAN_NOISE")
    clipped_cnt = sum(1 for p in filtered if p["cat"] == "HARDWARE_CLIPPING_DISCARD")
    startup_cnt = sum(1 for p in filtered if p["cat"] == "MACHINE_STARTUP_SAFE")
    shutdown_cnt = sum(1 for p in filtered if p["cat"] == "MACHINE_SHUTDOWN_SAFE")
    safe_cnt = normal_cnt + startup_cnt + shutdown_cnt

    reviewed_cnt = sum(1 for p in filtered if p["record"].get("review", {}).get("status", "UNREVIEWED") != "UNREVIEWED")
    review_rate = round(reviewed_cnt / total_matched * 100, 2) if total_matched else 0.0
    confirmed_collision_cnt = sum(1 for p in filtered if p["record"].get("review", {}).get("status") == "CONFIRMED_COLLISION")
    confirmed_false_alarm_cnt = sum(1 for p in filtered if p["record"].get("review", {}).get("status") == "CONFIRMED_FALSE_ALARM")

    dates_in_filtered = sorted(list(set(p["date"] for p in filtered)))

    kpis = {
        "total_samples": total_matched,
        "distinct_dates_count": len(dates_in_filtered),
        "min_date": dates_in_filtered[0] if dates_in_filtered else min_date_str,
        "max_date": dates_in_filtered[-1] if dates_in_filtered else max_date_str,
        "alarm_count": alarm_cnt,
        "alarm_rate_pct": alarm_rate,
        "unanimous_count": unanimous_cnt,
        "majority_count": majority_cnt,
        "shielded_count": shielded_cnt,
        "shielded_rate_pct": shielded_rate,
        "normal_count": normal_cnt,
        "clipped_count": clipped_cnt,
        "startup_count": startup_cnt,
        "shutdown_count": shutdown_cnt,
        "safe_count": safe_cnt,
        "reviewed_count": reviewed_cnt,
        "review_rate_pct": review_rate,
        "confirmed_collision_count": confirmed_collision_cnt,
        "confirmed_false_alarm_count": confirmed_false_alarm_cnt
    }

    # 2. Chart 1: 趋势图 (Trend)
    trend_labels = dates_in_filtered
    trend_matrix = {c: [0] * len(trend_labels) for c in cat_keys}
    date_to_idx = {d: i for i, d in enumerate(trend_labels)}
    for p in filtered:
        d = p["date"]
        if d in date_to_idx:
            idx = date_to_idx[d]
            trend_matrix[p["cat"]][idx] += 1

    trend_datasets = []
    for c_cfg in ANALYTICS_CATEGORIES:
        k = c_cfg["key"]
        trend_datasets.append({
            "label": c_cfg["name"],
            "data": trend_matrix[k],
            "backgroundColor": c_cfg["color"],
            "borderColor": c_cfg["color"],
            "borderWidth": 1
        })

    # 3. Chart 2: 7 大工况分类占比 (Types Doughnut)
    type_counts = [sum(1 for p in filtered if p["cat"] == c_cfg["key"]) for c_cfg in ANALYTICS_CATEGORIES]
    type_pcts = [round(c / total_matched * 100, 2) if total_matched else 0.0 for c in type_counts]
    types_chart = {
        "labels": [c["name"] for c in ANALYTICS_CATEGORIES],
        "short_labels": [c["short_name"] for c in ANALYTICS_CATEGORIES],
        "counts": type_counts,
        "percentages": type_pcts,
        "colors": [c["color"] for c in ANALYTICS_CATEGORIES]
    }

    # 4. Chart 3: 24 小时时段分布 (Hourly)
    hourly_hours = [f"{h:02d}:00" for h in range(24)]
    hour_to_idx = {h: i for i, h in enumerate(hourly_hours)}
    hourly_alarms = [0] * 24
    hourly_disturbances = [0] * 24
    hourly_normals = [0] * 24

    for p in filtered:
        h = p["hour"]
        idx = hour_to_idx.get(h, 0)
        cat = p["cat"]
        if cat in ["CRITICAL_COLLISION_UNANIMOUS", "MAJORITY_COLLISION_ALARM"]:
            hourly_alarms[idx] += 1
        elif cat in ["ISOLATED_DISTURBANCE_SAFE", "HARDWARE_CLIPPING_DISCARD", "MACHINE_STARTUP_SAFE", "MACHINE_SHUTDOWN_SAFE"]:
            hourly_disturbances[idx] += 1
        else:
            hourly_normals[idx] += 1

    hourly_chart = {
        "labels": hourly_hours,
        "alarms": hourly_alarms,
        "disturbances": hourly_disturbances,
        "normals": hourly_normals
    }

    # 5. Chart 4: 3 大模型 (03K / 03U / 03R) 性能对比 (Models Grouped Bar)
    models_meta = [
        {"id": "03K", "label": "03K (Dasheng-Base)"},
        {"id": "03U", "label": "03U (ECHOv2)"},
        {"id": "03R", "label": "03R (GRAM-Mono)"}
    ]
    models_chart = {
        "labels": [m["label"] for m in models_meta],
        "collision_avg_conf": [],
        "normal_avg_conf": [],
        "alarm_votes": [],
        "avg_infer_ms": []
    }
    for m in models_meta:
        mid = m["id"]
        c_confs = []
        n_confs = []
        votes = 0
        inf_times = []
        for p in filtered:
            r = p["record"]
            scores = r.get("scores", {})
            alarms = r.get("alarms", {})
            m_details = r.get("model_details", {}).get(mid, {})
            
            if mid in scores:
                sc = scores[mid]
                if p["cat"] in ["CRITICAL_COLLISION_UNANIMOUS", "MAJORITY_COLLISION_ALARM"]:
                    c_confs.append(sc)
                else:
                    n_confs.append(sc)
            
            if mid in alarms:
                votes += int(alarms.get(mid, 0))
            
            if "infer_time_ms" in m_details:
                inf_times.append(float(m_details["infer_time_ms"]))

        models_chart["collision_avg_conf"].append(round(sum(c_confs) / len(c_confs), 1) if c_confs else 0.0)
        models_chart["normal_avg_conf"].append(round(sum(n_confs) / len(n_confs), 1) if n_confs else 0.0)
        models_chart["alarm_votes"].append(votes)
        models_chart["avg_infer_ms"].append(round(sum(inf_times) / len(inf_times), 1) if inf_times else 0.0)

    # 6. Chart 5: 人工专家质检复核 (Reviews Horizontal Bar)
    review_status_defs = [
        {"key": "UNREVIEWED", "name": "待复核", "color": "#94a3b8"},
        {"key": "CONFIRMED_COLLISION", "name": "确认为真碰撞", "color": "#ef4444"},
        {"key": "CONFIRMED_FALSE_ALARM", "name": "确认为误报/扰动", "color": "#f59e0b"},
        {"key": "CONFIRMED_NORMAL", "name": "确认正常底噪", "color": "#10b981"},
        {"key": "CONFIRMED_HARDWARE_FAULT", "name": "幅值饱和/硬件故障", "color": "#8b5cf6"},
        {"key": "OTHER", "name": "启停机及其他", "color": "#06b6d4"}
    ]
    review_counts = []
    for rdef in review_status_defs:
        k = rdef["key"]
        if k == "OTHER":
            c = sum(1 for p in filtered if p["record"].get("review", {}).get("status") in ["CONFIRMED_STARTUP", "CONFIRMED_SHUTDOWN", "OTHER"])
        else:
            c = sum(1 for p in filtered if p["record"].get("review", {}).get("status", "UNREVIEWED") == k)
        review_counts.append(c)

    reviews_chart = {
        "labels": [rdef["name"] for rdef in review_status_defs],
        "counts": review_counts,
        "colors": [rdef["color"] for rdef in review_status_defs]
    }

    # 7. Chart 6: 声学物理特征散点图 (Physics Scatter)
    step = max(1, total_matched // 350) if total_matched > 350 else 1
    sampled_points = []
    for idx, p in enumerate(filtered):
        if idx % step == 0:
            r = p["record"]
            sampled_points.append({
                "x": round(float(r.get("rms_amplitude", 0.0)), 5),
                "y": round(float(r.get("peak_amplitude", 0.0)), 4),
                "cat": p["cat"],
                "fn": r.get("filename", ""),
                "conf": round(float(r.get("mean_confidence_pct", 0.0)), 1),
                "ts": r.get("timestamp", "")
            })

    # 7.5 声学有效值 (RMS) 能级分布与各工况统计
    all_rmss = [float(p["record"].get("rms_amplitude", 0.0)) for p in filtered if "rms_amplitude" in p["record"]]
    all_rmss_sorted = sorted(all_rmss)
    total_rms_count = len(all_rmss_sorted)

    def calc_percentile(sorted_list, pct):
        if not sorted_list:
            return 0.0
        k = (len(sorted_list) - 1) * (pct / 100.0)
        f = int(k)
        c = f + 1
        if c < len(sorted_list):
            return sorted_list[f] + (k - f) * (sorted_list[c] - sorted_list[f])
        return sorted_list[f]

    min_rms = all_rmss_sorted[0] if all_rmss_sorted else 0.0
    max_rms = all_rmss_sorted[-1] if all_rmss_sorted else 0.0
    avg_rms = sum(all_rmss_sorted) / total_rms_count if total_rms_count else 0.0
    median_rms = calc_percentile(all_rmss_sorted, 50)
    p25_rms = calc_percentile(all_rmss_sorted, 25)
    p75_rms = calc_percentile(all_rmss_sorted, 75)
    p95_rms = calc_percentile(all_rmss_sorted, 95)
    variance = sum((x - avg_rms) ** 2 for x in all_rmss_sorted) / total_rms_count if total_rms_count else 0.0
    std_rms = variance ** 0.5

    rms_kpis = {
        "min": round(min_rms, 5),
        "max": round(max_rms, 4),
        "avg": round(avg_rms, 5),
        "median": round(median_rms, 5),
        "p25": round(p25_rms, 5),
        "p75": round(p75_rms, 5),
        "p95": round(p95_rms, 5),
        "std": round(std_rms, 5)
    }

    rms_bins = [
        {"min": 0.0,   "max": 0.02, "label": "< 0.02",    "name": "极微弱静默底噪", "color": "#0ea5e9"},
        {"min": 0.02,  "max": 0.05, "label": "0.02-0.05", "name": "轻载平稳底噪", "color": "#10b981"},
        {"min": 0.05,  "max": 0.10, "label": "0.05-0.10", "name": "稳态中载风噪", "color": "#84cc16"},
        {"min": 0.10,  "max": 0.15, "label": "0.10-0.15", "name": "强气流与风剪", "color": "#eab308"},
        {"min": 0.15,  "max": 0.20, "label": "0.15-0.20", "name": "高能态工况变点", "color": "#f97316"},
        {"min": 0.20,  "max": 0.30, "label": "0.20-0.30", "name": "异常振动/冲击", "color": "#ef4444"},
        {"min": 0.30,  "max": 0.45, "label": "0.30-0.45", "name": "剧烈碰擦/高能量", "color": "#dc2626"},
        {"min": 0.45,  "max": 99.0, "label": "≥ 0.45",    "name": "供电轨饱和截幅", "color": "#8b5cf6"}
    ]

    rms_histogram = []
    for b in rms_bins:
        sub = [p for p in filtered if b["min"] <= float(p["record"].get("rms_amplitude", 0.0)) < b["max"]]
        cnt = len(sub)
        pct = round(cnt / total_matched * 100, 2) if total_matched else 0.0
        rms_histogram.append({
            "label": b["label"],
            "tier_name": b["name"],
            "count": cnt,
            "pct": pct,
            "color": b["color"]
        })

    rms_category_stats = []
    for c_cfg in ANALYTICS_CATEGORIES:
        k = c_cfg["key"]
        sub_rmss = sorted([float(p["record"].get("rms_amplitude", 0.0)) for p in filtered if p["cat"] == k])
        sub_cnt = len(sub_rmss)
        c_avg = round(sum(sub_rmss) / sub_cnt, 5) if sub_cnt else 0.0
        c_p95 = round(calc_percentile(sub_rmss, 95), 5) if sub_cnt else 0.0
        c_median = round(calc_percentile(sub_rmss, 50), 5) if sub_cnt else 0.0
        c_max = round(sub_rmss[-1], 4) if sub_rmss else 0.0
        rms_category_stats.append({
            "key": k,
            "name": c_cfg["short_name"],
            "full_name": c_cfg["name"],
            "color": c_cfg["color"],
            "count": sub_cnt,
            "avg_rms": c_avg,
            "median_rms": c_median,
            "p95_rms": c_p95,
            "max_rms": c_max
        })

    # 8. 汇总矩阵报表 (Table Summary)
    table_summary = []
    for c_cfg in ANALYTICS_CATEGORIES:
        k = c_cfg["key"]
        sub = [p for p in filtered if p["cat"] == k]
        c_count = len(sub)
        c_pct = round(c_count / total_matched * 100, 2) if total_matched else 0.0
        confs = [p["record"].get("mean_confidence_pct", 0.0) for p in sub]
        peaks = [p["record"].get("peak_amplitude", 0.0) for p in sub]
        rmss = [p["record"].get("rms_amplitude", 0.0) for p in sub]
        crests = [p["record"].get("crest_factor", 0.0) for p in sub]
        rev_count = sum(1 for p in sub if p["record"].get("review", {}).get("status", "UNREVIEWED") != "UNREVIEWED")
        conf_coll = sum(1 for p in sub if p["record"].get("review", {}).get("status") == "CONFIRMED_COLLISION")

        if k in ["CRITICAL_COLLISION_UNANIMOUS", "MAJORITY_COLLISION_ALARM"]:
            takeaway = "需重点排查，结合变桨工况确认结构异响"
        elif k == "ISOLATED_DISTURBANCE_SAFE":
            takeaway = "门禁成功拦截，单模型偶发虚警无需停机"
        elif k == "HARDWARE_CLIPPING_DISCARD":
            takeaway = "传感器供电轨饱和截断，建议检修传感器线路"
        elif k in ["MACHINE_STARTUP_SAFE", "MACHINE_SHUTDOWN_SAFE"]:
            takeaway = "CPD 变点自适应匹配启停机过程，安全放行"
        else:
            takeaway = "基座平稳运行无冲击，背景底噪正常"

        table_summary.append({
            "key": k,
            "name": c_cfg["name"],
            "badge": c_cfg["badge"],
            "color": c_cfg["color"],
            "count": c_count,
            "pct": c_pct,
            "avg_conf": round(sum(confs) / len(confs), 2) if confs else 0.0,
            "avg_peak": round(sum(peaks) / len(peaks), 4) if peaks else 0.0,
            "avg_rms": round(sum(rmss) / len(rmss), 5) if rmss else 0.0,
            "avg_crest": round(sum(crests) / len(crests), 2) if crests else 0.0,
            "reviewed_count": rev_count,
            "confirmed_collision_count": conf_coll,
            "takeaway": takeaway
        })

    return jsonify({
        "success": True,
        "filters": {
            "time_dim": time_dim,
            "range": time_range,
            "start_date": start_date or (dates_in_filtered[0] if dates_in_filtered else min_date_str),
            "end_date": end_date or (dates_in_filtered[-1] if dates_in_filtered else max_date_str),
            "total_matched": total_matched,
            "all_dates": sorted_all_dates
        },
        "kpis": kpis,
        "trend": {
            "labels": trend_labels,
            "datasets": trend_datasets
        },
        "types": types_chart,
        "hourly": hourly_chart,
        "models": models_chart,
        "reviews": reviews_chart,
        "physics": sampled_points,
        "rms_analytics": {
            "kpis": rms_kpis,
            "histogram": rms_histogram,
            "by_category": rms_category_stats
        },
        "table_summary": table_summary
    })

@app.route("/api/admin/analytics/export_csv", methods=["GET"])
def api_admin_analytics_export_csv():
    """导出分析汇总报表为 CSV 格式"""
    res = api_admin_analytics()
    data = res.get_json() if hasattr(res, "get_json") else json.loads(res.data)
    if not data or not data.get("success"):
        return jsonify({"success": False, "error": "获取分析数据失败"}), 500

    filters = data.get("filters", {})
    kpis = data.get("kpis", {})
    summary_list = data.get("table_summary", [])
    trend = data.get("trend", {})

    output = io.StringIO()
    output.write("\ufeff") # UTF-8 BOM
    writer = csv.writer(output)

    # 头部概览
    writer.writerow(["=== 风电机组碰撞声学智能诊断系统 · 多维统计分析报表 ==="])
    writer.writerow(["导出时间", time.strftime("%Y-%m-%d %H:%M:%S")])
    writer.writerow(["时间聚合维度", "音频采样时间" if filters.get("time_dim") == "audio" else "系统诊断时间"])
    writer.writerow(["分析时间跨度", f"{filters.get('start_date')} 至 {filters.get('end_date')}"])
    writer.writerow(["纳管样本总量", kpis.get("total_samples", 0)])
    writer.writerow(["结构碰撞告警数", f"{kpis.get('alarm_count', 0)} ({kpis.get('alarm_rate_pct', 0)}%)"])
    writer.writerow(["孤立扰动拦截数", f"{kpis.get('shielded_count', 0)} ({kpis.get('shielded_rate_pct', 0)}%)"])
    writer.writerow(["人工质检复核率", f"{kpis.get('reviewed_count', 0)} ({kpis.get('review_rate_pct', 0)}%)"])
    writer.writerow([])

    # 1. 工况汇总矩阵
    writer.writerow(["--- 1. 七大工况分类统计矩阵 ---"])
    writer.writerow(["工况类别", "样本数量", "占比(%)", "平均置信度(%)", "平均峰值幅值", "平均有效值RMS", "波峰因数", "已质检复核数", "确诊真碰撞数", "诊断处置建议"])
    for row in summary_list:
        writer.writerow([
            row["name"],
            row["count"],
            f"{row['pct']}%",
            f"{row['avg_conf']}%",
            f"{row['avg_peak']:.4f}",
            f"{row['avg_rms']:.5f}",
            f"{row['avg_crest']:.2f}",
            row["reviewed_count"],
            row["confirmed_collision_count"],
            row["takeaway"]
        ])
    writer.writerow([])

    # 2. 每日趋势明细
    writer.writerow(["--- 2. 时间趋势分布明细 (每日样本数) ---"])
    labels = trend.get("labels", [])
    ds_list = trend.get("datasets", [])
    header = ["日期"] + [ds["label"] for ds in ds_list]
    writer.writerow(header)
    for i, d in enumerate(labels):
        line = [d]
        for ds in ds_list:
            line.append(ds["data"][i] if i < len(ds["data"]) else 0)
        writer.writerow(line)

    # 3. RMS 能级梯度直方图分布
    rms_analytics = data.get("rms_analytics", {})
    rms_hist = rms_analytics.get("histogram", [])
    if rms_hist:
        writer.writerow([])
        writer.writerow(["--- 3. 声学有效值 (RMS) 能级梯度分布统计 ---"])
        writer.writerow(["有效值能级区间", "工况特征代表", "样本数量", "占比(%)"])
        for h in rms_hist:
            writer.writerow([h.get("label"), h.get("tier_name"), h.get("count"), f"{h.get('pct')}%"])

    # 4. 各工况 RMS 能量统计对比
    rms_cat = rms_analytics.get("by_category", [])
    if rms_cat:
        writer.writerow([])
        writer.writerow(["--- 4. 七大工况有效值 (RMS) 振动能级对比 ---"])
        writer.writerow(["工况类别", "样本数量", "平均RMS", "中位数RMS", "P95高能位RMS", "最大RMS"])
        for c in rms_cat:
            writer.writerow([c.get("full_name"), c.get("count"), f"{c.get('avg_rms'):.5f}", f"{c.get('median_rms'):.5f}", f"{c.get('p95_rms'):.5f}", f"{c.get('max_rms'):.4f}"])

    csv_data = output.getvalue()
    filename = f"analytics_report_{filters.get('start_date')}_{filters.get('end_date')}.csv"
    return Response(
        csv_data,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )

_records_disk_cache = {"time": 0, "size_mb": 0.0, "file_count": 0}

@app.route("/api/admin/system_status", methods=["GET"])
def api_admin_system_status():
    """获取当前系统主机资源与分类目录统计"""
    try:
        cpu_pct = psutil.cpu_percent(interval=None)
        cpu_count = psutil.cpu_count(logical=True)
        mem = psutil.virtual_memory()
        process = psutil.Process(os.getpid())
        proc_mem_mb = round(process.memory_info().rss / (1024 * 1024), 1)

        # 统计 records 目录大小 (scandir 单次系统调用 + 60秒缓存加速，杜绝万级文件IO阻塞)
        now_ts = time.time()
        if now_ts - _records_disk_cache["time"] > 60:
            records_size_bytes = 0
            records_file_count = 0
            try:
                for entry in os.scandir(RECORDS_DIR):
                    if entry.is_file(follow_symlinks=False):
                        records_size_bytes += entry.stat().st_size
                        records_file_count += 1
                _records_disk_cache["size_mb"] = round(records_size_bytes / (1024 * 1024), 2)
                _records_disk_cache["file_count"] = records_file_count
                _records_disk_cache["time"] = now_ts
            except Exception:
                pass
        records_size_mb = _records_disk_cache["size_mb"]
        records_file_count = _records_disk_cache["file_count"]

        # 统计结果分类子目录分布
        subdir_stats = {}
        for k, sub in RESULT_SUBDIRS.items():
            p_sub = os.path.join(PROCESSED_DIR, sub)
            count = len(os.listdir(p_sub)) if os.path.exists(p_sub) else 0
            subdir_stats[sub] = count

        return jsonify({
            "success": True,
            "status": {
                "is_algorithm_running": is_algorithm_running,
                "cpu_percent": cpu_pct,
                "cpu_cores": cpu_count,
                "ram_total_gb": round(mem.total / (1024**3), 2),
                "ram_percent": mem.percent,
                "process_memory_mb": proc_mem_mb,
                "records_file_count": records_file_count,
                "records_size_mb": records_size_mb,
                "total_history_count": len(history_records),
                "total_logs_count": len(system_logs),
                "subdir_stats": subdir_stats,
                "models": {
                    "03K": {"name": "Dasheng-Base", "params": "89M", "status": "READY"},
                    "03U": {"name": "ECHOv2", "params": "82M", "status": "READY"},
                    "03R": {"name": "GRAM-Mono", "params": "86M", "status": "READY"}
                },
                "watch_dir": MONITOR_CFG.get("watch_dir", "incoming_audio"),
                "watch_dir_abs": WATCH_DIR,
                "processed_dir": MONITOR_CFG.get("processed_dir", "incoming_audio/processed"),
                "processed_dir_abs": PROCESSED_DIR,
                "alarm_threshold_pct": engine.alarm_thresh_pct,
                "min_rms_threshold": getattr(engine, "min_rms_thresh", 0.02),
                "scan_interval_sec": MONITOR_CFG.get("scan_interval_sec", 1.0),
                "resource_limit": RESOURCE_LIMIT_INFO
            }
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/admin/update_config", methods=["POST"])
def api_admin_update_config():
    """动态更新系统运行配置并热生效"""
    global WATCH_DIR, PROCESSED_DIR
    data = request.json or {}
    try:
        new_thresh = float(data.get("alarm_threshold_pct", engine.alarm_thresh_pct))
        new_interval = float(data.get("scan_interval_sec", MONITOR_CFG.get("scan_interval_sec", 1.0)))
        new_min_rms = float(data.get("min_rms_threshold", getattr(engine, "min_rms_thresh", 0.02)))
        new_min_rms = max(0.0, min(0.5, new_min_rms))

        engine.alarm_thresh_pct = new_thresh
        engine.min_rms_thresh = new_min_rms
        CONFIG["monitor"]["alarm_threshold_pct"] = new_thresh
        CONFIG["monitor"]["scan_interval_sec"] = new_interval
        CONFIG["monitor"]["min_rms_threshold"] = new_min_rms
        MONITOR_CFG["alarm_threshold_pct"] = new_thresh
        MONITOR_CFG["scan_interval_sec"] = new_interval
        MONITOR_CFG["min_rms_threshold"] = new_min_rms

        # 1. 待测音频监听检测路径热更新
        if "watch_dir" in data and str(data["watch_dir"]).strip():
            raw_watch = str(data["watch_dir"]).strip()
            new_watch_dir = resolve_dir_path(raw_watch, "incoming_audio")
            try:
                os.makedirs(new_watch_dir, exist_ok=True)
            except Exception as e:
                return jsonify({"success": False, "error": f"无法创建或访问待测监听目录 [{new_watch_dir}]: {e}"}), 400
            
            WATCH_DIR = new_watch_dir
            CONFIG["monitor"]["watch_dir"] = raw_watch
            MONITOR_CFG["watch_dir"] = raw_watch

        # 2. 归档处理目录热更新
        if "processed_dir" in data:
            raw_processed = str(data["processed_dir"]).strip()
            if raw_processed:
                new_processed_dir = resolve_dir_path(raw_processed, os.path.join(WATCH_DIR, "processed"))
            else:
                new_processed_dir = os.path.join(WATCH_DIR, "processed")
                raw_processed = os.path.relpath(new_processed_dir, APP_DIR) if not os.path.isabs(new_processed_dir) else new_processed_dir
            
            try:
                os.makedirs(new_processed_dir, exist_ok=True)
                for sub in RESULT_SUBDIRS.values():
                    os.makedirs(os.path.join(new_processed_dir, sub), exist_ok=True)
            except Exception as e:
                return jsonify({"success": False, "error": f"无法创建或访问归档目录 [{new_processed_dir}]: {e}"}), 400
            
            PROCESSED_DIR = new_processed_dir
            CONFIG["monitor"]["processed_dir"] = raw_processed
            MONITOR_CFG["processed_dir"] = raw_processed

        # 3. 宿主机资源限制配置热更新
        res_cfg = CONFIG.setdefault("resource_limit", {})
        if "resource_limit_enable" in data:
            res_cfg["enable"] = bool(data["resource_limit_enable"])
        if "max_cpu_percent" in data:
            res_cfg["max_cpu_percent"] = float(data["max_cpu_percent"])
        if "max_memory_percent" in data:
            res_cfg["max_memory_percent"] = float(data["max_memory_percent"])
        if "process_priority" in data:
            res_cfg["process_priority"] = str(data["process_priority"])
        apply_system_resource_limits()

        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(CONFIG, f, ensure_ascii=False, indent=2)

        add_log("INFO", f"管理员更新了系统配置: 待测监听路径={WATCH_DIR}, 归档路径={PROCESSED_DIR}, 单模型阈值={new_thresh}%, 静音门限={new_min_rms}, 扫描周期={new_interval}s")
        return jsonify({
            "success": True,
            "msg": "系统配置已成功保存！待测检测路径与系统参数已即时热生效。",
            "config": CONFIG,
            "watch_dir": MONITOR_CFG.get("watch_dir"),
            "watch_dir_abs": WATCH_DIR,
            "processed_dir": MONITOR_CFG.get("processed_dir"),
            "processed_dir_abs": PROCESSED_DIR,
            "resource_limit": RESOURCE_LIMIT_INFO
        })
    except Exception as e:
        return jsonify({"success": False, "error": f"更新配置失败: {e}"}), 400

@app.route("/api/admin/restart_engine", methods=["POST"])
def api_admin_restart_engine():
    """重启算法引擎与检测系统：热重载 Python 算法模块、CPD 变点门禁与 3 大核心基座模型"""
    global engine, history_records, is_algorithm_running
    add_log("WARN", "【系统控制】管理员触发算法引擎重启，准备重载检测系统...")
    
    with inference_lock:
        t0 = time.time()
        try:
            # 1. 释放显存与旧模型引用
            if hasattr(engine, "models"):
                engine.models.clear()
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
            
            # 2. 强制热重载 preprocessor 与 voter 模块
            import common.audio_preprocessor as cap
            import models.voter as mv
            importlib.reload(cap)
            importlib.reload(mv)
            
            # 3. 重新实例化 3 核心黄金铁三角仲裁引擎 (03K + 03U + 03R + CPD)
            engine = mv.TripleModelVoterEngine(
                code_base_dir=CODE_BASE_DIR,
                alarm_thresh_pct=float(CONFIG.get("monitor", {}).get("alarm_threshold_pct", 90.0)),
                min_rms_threshold=float(CONFIG.get("monitor", {}).get("min_rms_threshold", 0.02))
            )
            is_algorithm_running = True
            
            # 4. 重新同步本地历史台账与增量补充
            load_initial_history_records()
            
            cost_s = round(time.time() - t0, 2)
            add_log("INFO", f"【系统控制】算法检测系统全量重启完成！耗时: {cost_s}s，CPD 启停机门禁与 3 核心大模型已全部刷新就绪")
            return jsonify({
                "success": True,
                "msg": f"算法检测系统已成功重启就绪！耗时 {cost_s} 秒，最新 CPD 算法与 3 核心模型已全量加载生效。",
                "cost_seconds": cost_s,
                "is_algorithm_running": True
            })
        except Exception as e:
            tb = traceback.format_exc()
            add_log("ERROR", f"【系统控制】算法引擎重启失败: {e}\n{tb}")
            return jsonify({"success": False, "error": f"算法引擎重启异常: {e}"}), 500

@app.route("/api/admin/algorithm/stop", methods=["POST"])
def api_admin_algorithm_stop():
    """停止算法检测引擎与后台推流分析"""
    global is_algorithm_running
    is_algorithm_running = False
    add_log("WARN", "【系统控制】管理员执行了【停止算法检测】，后台实时推流与检测分析已暂停")
    return jsonify({
        "success": True,
        "is_algorithm_running": False,
        "msg": "算法检测系统已成功停止！实时音频监听与推流分析已暂停。",
        "status_text": "STOPPED"
    })

@app.route("/api/admin/algorithm/start", methods=["POST"])
def api_admin_algorithm_start():
    """启动算法检测引擎与后台推流分析"""
    global is_algorithm_running
    is_algorithm_running = True
    add_log("INFO", "【系统控制】管理员执行了【启动算法检测】，后台实时推流监听与检测分析已恢复运行")
    return jsonify({
        "success": True,
        "is_algorithm_running": True,
        "msg": "算法检测系统已恢复启动！正在实时监听音频推流并执行诊断。",
        "status_text": "RUNNING"
    })

@app.route("/api/admin/update_resource_limits", methods=["POST"])
def api_admin_update_resource_limits():
    """实时动态调整 CPU 与内存上限限制并即时热生效 (免重启)"""
    data = request.json or {}
    try:
        res_cfg = CONFIG.setdefault("resource_limit", {})
        if "enable" in data:
            res_cfg["enable"] = bool(data["enable"])
        if "max_cpu_percent" in data:
            res_cfg["max_cpu_percent"] = max(10.0, min(100.0, float(data["max_cpu_percent"])))
        if "max_memory_percent" in data:
            res_cfg["max_memory_percent"] = max(10.0, min(95.0, float(data["max_memory_percent"])))
        if "process_priority" in data:
            res_cfg["process_priority"] = str(data["process_priority"])

        apply_system_resource_limits()

        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(CONFIG, f, ensure_ascii=False, indent=2)

        cores_txt = f"{RESOURCE_LIMIT_INFO['allowed_cores_count']}/{RESOURCE_LIMIT_INFO['total_cores']}核"
        mem_txt = f"{RESOURCE_LIMIT_INFO['max_allowed_ram_gb']}GB"
        msg = f"宿主机保护限制已实时生效！CPU上限已调为 {res_cfg.get('max_cpu_percent')}% (锁定{cores_txt})，内存上限已调为 {res_cfg.get('max_memory_percent')}% (限额{mem_txt})。"
        add_log("INFO", f"【资源保护】管理员实时调整了系统资源上限: CPU={res_cfg.get('max_cpu_percent')}% ({cores_txt}), 内存={res_cfg.get('max_memory_percent')}% ({mem_txt})")
        return jsonify({
            "success": True,
            "msg": msg,
            "resource_limit": RESOURCE_LIMIT_INFO
        })
    except Exception as e:
        return jsonify({"success": False, "error": f"调整资源限制失败: {e}"}), 400

@app.route("/api/admin/restart_service", methods=["POST"])
def api_admin_restart_service():
    """完全重启底层 Python 监控服务进程 (冷重启)"""
    add_log("WARN", "【系统控制】管理员触发服务底层进程冷重启，正在准备重启 Python 服务...")
    def do_cold_restart():
        time.sleep(1.0)
        python = sys.executable
        app_file = os.path.abspath(__file__)
        import subprocess
        subprocess.Popen([python, app_file])
        os._exit(0)
    threading.Thread(target=do_cold_restart, daemon=True).start()
    return jsonify({"success": True, "msg": "服务进程正在执行完整冷重启，页面将在 15 秒后自动刷新..."})

@app.route("/api/admin/check_update", methods=["GET"])
def api_admin_check_update():
    """检测 GitHub 远程仓库是否有新版本"""
    try:
        import auto_update
        res = auto_update.check_for_updates()
        return jsonify({"success": True, "data": res})
    except Exception as e:
        return jsonify({"success": False, "error": f"检查更新失败: {e}"}), 500

@app.route("/api/admin/apply_update", methods=["POST"])
def api_admin_apply_update():
    """触发系统在线热更新 (拉取最新代码并重启)"""
    try:
        add_log("WARN", "【系统更新】管理员在 Web 后台触发在线自动升级流程...")
        def do_update():
            time.sleep(1.0)
            python = sys.executable
            updater_py = os.path.join(APP_DIR, "auto_update.py")
            import subprocess
            # 使用 CREATE_NEW_CONSOLE 独立脱离进程执行更新
            CREATE_NEW_CONSOLE = 0x00000010
            subprocess.Popen([python, updater_py, "--apply"], creationflags=CREATE_NEW_CONSOLE)
            os._exit(0)
        threading.Thread(target=do_update, daemon=True).start()
        return jsonify({"success": True, "msg": "系统已启动自动升级子进程，正在拉取最新代码并验证依赖，完成后将自动重启服务！"})
    except Exception as e:
        return jsonify({"success": False, "error": f"触发更新失败: {e}"}), 500

@app.route("/api/admin/autostart_status", methods=["GET"])
def api_admin_autostart_status():
    """获取当前开机自启动配置状态"""
    try:
        import autostart
        st = autostart.get_status()
        return jsonify({"success": True, "status": st})
    except Exception as e:
        return jsonify({"success": False, "error": f"获取自启动状态失败: {e}"}), 500

@app.route("/api/admin/toggle_autostart", methods=["POST"])
def api_admin_toggle_autostart():
    """Web 端一键切换开机自启动"""
    try:
        data = request.get_json(silent=True) or {}
        enable = data.get("enable", True)
        import autostart
        if enable:
            res = autostart.enable_autostart()
            add_log("INFO", "【系统设置】管理员通过 Web 后台开启了开机自启动功能")
        else:
            res = autostart.disable_autostart()
            add_log("INFO", "【系统设置】管理员通过 Web 后台取消了开机自启动功能")
        return jsonify(res)
    except Exception as e:
        return jsonify({"success": False, "error": f"切换自启动状态失败: {e}"}), 500

# ---------------------------------------------------------------------------
# 主程序入口
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    host = SERVER_CFG.get("host", "127.0.0.1")
    port = int(SERVER_CFG.get("port", 8088))
    
    print("=" * 70)
    print(f">> [3-Model Realtime Monitor] 服务已启动！")
    print(f">> 监控网页端控制台: http://{host}:{port}")
    print(f">> 后台管理中心页面: http://{host}:{port}/admin")
    print("=" * 70 + "\n")
    
    app.run(host=host, port=port, debug=False, threaded=True)
