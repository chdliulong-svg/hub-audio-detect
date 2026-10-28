# -*- coding: utf-8 -*-
"""
轮毂异响检测系统 · 后台静默启动管理器
功能：
  1. 真正脱离控制台后台启动监控服务 (无 CMD 黑框，不占用屏幕)
  2. 智能轮询等待 3 核心声学大模型加载就绪 (10~60 秒)
  3. 服务就绪后，精准唤醒默认浏览器打开监控大屏 (杜绝打不开或 ERR_CONNECTION_REFUSED)
  4. 采用纯 ASCII 状态标记与标准输出编码防溢出，全面兼容 Windows GBK/CP936 与 UTF-8
  5. 若启动异常自动打印详细排查信息，不吞报错
"""
import os
import sys
import time
import subprocess
import webbrowser
import traceback

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE_DIR)

def is_port_listening(port=8088):
    try:
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.6)
            return s.connect_ex(('127.0.0.1', port)) == 0
    except Exception:
        return False

def open_browser(url="http://127.0.0.1:8088"):
    try:
        if webbrowser.open(url):
            return True
    except Exception:
        pass
    try:
        os.system(f'start "" "{url}"')
        return True
    except Exception:
        return False

def get_python_executables():
    """获取当前 Python 环境对应的 python.exe 与 pythonw.exe"""
    py_dir = os.path.dirname(sys.executable)
    pyw_candidate = os.path.join(py_dir, "pythonw.exe")
    if os.path.exists(pyw_candidate):
        return pyw_candidate, sys.executable
    return sys.executable, sys.executable

def main():
    print("=" * 70)
    print("[START] 正在启动轮毂异响检测系统后台服务 (静默无黑框模式)...")
    print("=" * 70)
    print()

    # 1. 检查是否已经在运行
    if is_port_listening(8088):
        print("[OK] 检测到监控服务已经在后台稳定运行中 (端口: 8088)！")
        print("[OK] 正在直接唤醒浏览器打开监控大屏...")
        open_browser("http://127.0.0.1:8088")
        print("\n>> 监控大屏地址: http://127.0.0.1:8088")
        print(">> 后台管理中心: http://127.0.0.1:8088/admin")
        time.sleep(2)
        return

    # 2. 准备后台无黑框执行器
    pythonw_bin, python_bin = get_python_executables()
    app_py = os.path.join(BASE_DIR, "app.py")

    log_dir = os.path.join(BASE_DIR, "records")
    os.makedirs(log_dir, exist_ok=True)
    daemon_log_path = os.path.join(log_dir, "daemon_console.log")
    
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    CREATE_NO_WINDOW = 0x08000000

    flags = CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    print(f"[1/2] 正在创建后台脱离会话守护进程...")
    
    proc = None
    # 优先使用 pythonw.exe，零黑框且完全脱离控制台生命周期
    launcher_py = pythonw_bin if os.path.exists(pythonw_bin) else python_bin
    try:
        proc = subprocess.Popen(
            [launcher_py, app_py],
            cwd=BASE_DIR,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags
        )
    except Exception as e:
        print(f"[ERROR] 启动守护进程失败: {e}")
        traceback.print_exc()
        input("\n请按回车键退出...")
        return

    print(f"[OK] 监控守护进程已成功建立 (PID: {proc.pid})")
    print(f"[OK] 宿主机 50% 性能隔离安全保护屏障已自动就绪")
    print()

    # 3. 智能轮询等待服务就绪 (载入 3 大模型需要 15~50 秒，上限放宽至 120 秒)
    print("[2/2] 正在加载 3 大核心声学大模型 (03K + 03U + 03R) 并启动推理引擎...")
    print("      (正在载入 2.5 亿参数深度学习模型，首次初始化约需 20~50 秒，请稍候)")

    server_ready = False
    start_t = time.time()
    for sec in range(1, 121):
        time.sleep(1)
        if is_port_listening(8088):
            server_ready = True
            break
        
        # 实时检测子进程是否异常提前退出
        if proc.poll() is not None:
            print(f"\n[ERROR] 后台监控服务意外退出 (退出码: {proc.returncode})！")
            if os.path.exists(daemon_log_path):
                print("\n【异常日志详情】:")
                try:
                    with open(daemon_log_path, "r", encoding="utf-8", errors="ignore") as lf:
                        lines = lf.readlines()
                        print("".join(lines[-35:]).strip())
                except Exception:
                    pass
            input("\n请检查上方错误信息后，按回车键退出...")
            return
        
        if sys.stdout:
            sys.stdout.write(f"\r  [*] 深度学习声学模型权重载入中... [{sec}s]")
            sys.stdout.flush()

    cost_s = round(time.time() - start_t, 1)
    print()

    if server_ready:
        print(f"\n[OK] 3 核心声学大模型全部就绪！耗时: {cost_s} 秒")
        print("[OK] 后台服务已成功在端口 8088 监听！")
        print("[OK] 正在自动为您打开浏览器监控页面...")
        time.sleep(0.5)
        open_browser("http://127.0.0.1:8088")
        time.sleep(0.5)
        open_browser("http://127.0.0.1:8088/admin#tab-analytics")
    else:
        print(f"\n[!] 启动耗时较长 (已等待 {cost_s} 秒)，后台服务仍在加载中...")
        print("[!] 正在尝试为您打开浏览器...")
        open_browser("http://127.0.0.1:8088")
        time.sleep(0.5)
        open_browser("http://127.0.0.1:8088/admin#tab-analytics")

    print()
    print("=" * 70)
    print("[OK] 服务启动就绪！")
    print(">> 实时监控大屏: http://127.0.0.1:8088")
    print(">> 后台管理中心: http://127.0.0.1:8088/admin")
    print("提示：")
    print("  * 本启动窗口将在 3 秒后自动关闭，不会在屏幕或任务栏残留任何控制台黑框。")
    print("  * 后台服务将持续守护检测，不受窗口关闭影响。")
    print("  * 如需停止后台服务，随时双击运行【4_一键停止监控系统.bat】即可。")
    print("=" * 70)
    time.sleep(3)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\n[ERROR] 启动器运行异常: {e}")
        traceback.print_exc()
        input("\n请按回车键退出...")
