# -*- coding: utf-8 -*-
"""
轮毂异响检测系统 · 后台服务停止管理器
使用 psutil 安全定位并终止监听 8088 端口及后台 app.py 进程，杜绝控制台黑框残留与批处理管道解析异常
"""
import os
import sys
import time

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

def stop_monitor_service():
    print("=" * 70)
    print("[STOP] 正在停止【轮毂异响检测系统】后台监控服务...")
    print("=" * 70)
    print()

    killed_any = False
    try:
        import psutil
        # 1. 精准定位占用 8088 端口的进程
        for conn in psutil.net_connections(kind='tcp'):
            if conn.laddr and conn.laddr.port == 8088:
                pid = conn.pid
                if pid and pid != os.getpid():
                    try:
                        proc = psutil.Process(pid)
                        name = proc.name()
                        print(f"[OK] 正在终止后台监控进程 (PID: {pid}, 名称: {name}) ...")
                        proc.terminate()
                        try:
                            proc.wait(timeout=3)
                        except psutil.TimeoutExpired:
                            proc.kill()
                        killed_any = True
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass

        # 2. 同时清理可能在加载模型中但尚未完成 8088 端口绑定的 app.py 进程
        for p in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                cmdline = p.info.get('cmdline') or []
                cmd_str = " ".join(cmdline).lower()
                if 'app.py' in cmd_str and ('collision_audio' in cmd_str or 'triple_model' in cmd_str):
                    pid = p.info['pid']
                    if pid != os.getpid():
                        print(f"[OK] 正在清理后台运行的 app.py 进程 (PID: {pid}) ...")
                        p.terminate()
                        try:
                            p.wait(timeout=3)
                        except psutil.TimeoutExpired:
                            p.kill()
                        killed_any = True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

    except Exception as e:
        # 降级备用: taskkill 命令
        import subprocess
        try:
            out = subprocess.check_output('netstat -ano | findstr :8088 | findstr LISTENING', shell=True).decode('utf-8', errors='ignore')
            for line in out.strip().splitlines():
                parts = line.split()
                if len(parts) >= 5:
                    pid = parts[-1]
                    print(f"[OK] 正在通过 taskkill 终止进程 PID: {pid} ...")
                    subprocess.run(f"taskkill /F /PID {pid}", shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    killed_any = True
        except Exception:
            pass

    if killed_any:
        print()
        print("[OK] 监控服务进程已全部安全退出，8088 端口与硬件资源已彻底释放！")
    else:
        print("[INFO] 当前未检测到正在运行的 8088 端口监控服务。")

    print()
    print("=" * 70)
    print("提示：本窗口将在 2 秒后自动关闭。")
    print("=" * 70)
    time.sleep(2)

if __name__ == "__main__":
    stop_monitor_service()
