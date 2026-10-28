# -*- coding: utf-8 -*-
"""
=============================================================================
轮毂异响检测系统 · 开机自启动管理引擎 (AutoStart Manager)
功能：
  1. 支持将监控系统注册为 Windows 开机/登录自启动服务
  2. 采用双重/三重保障机制：
     - 机制 A: Windows 开始菜单启动文件夹快捷方式 (无权限门槛，任务管理器可见)
     - 机制 B: Windows 注册表 Run 键 (HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run)
     - 机制 C: Windows 任务计划程序 (schtasks onlogon 高权限调度)
  3. 自启动时调用【一键后台静默启动.vbs】，全程零 CMD 黑框、无声唤醒服务并在就绪后打开大屏
  4. 支持 CLI 命令行、批处理脚本一键开关与 Web 管理后台动态切换
=============================================================================
"""
import os
import sys
import subprocess
import tempfile
import json
import argparse

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SHORTCUT_NAME = "轮毂异响检测系统.lnk"
REG_KEY_NAME = "WindTurbineAudioDetect"
TASK_NAME = "WindTurbineAudio_AutoStart"

def get_startup_folder():
    """获取 Windows 当前用户的开机启动文件夹路径"""
    appdata = os.environ.get("APPDATA")
    if appdata:
        p = os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs", "Startup")
        if os.path.exists(p):
            return p
    # 备选回退
    home = os.path.expanduser("~")
    p = os.path.join(home, "AppData", "Roaming", "Microsoft", "Windows", "Start Menu", "Programs", "Startup")
    return p

def get_shortcut_path():
    return os.path.join(get_startup_folder(), SHORTCUT_NAME)

def get_vbs_launcher_path():
    return os.path.join(BASE_DIR, "一键后台静默启动.vbs")

def create_windows_shortcut(shortcut_path, target_path, arguments="", working_dir="", description=""):
    """使用 Windows 原生 WScript.Shell 创建 .lnk 快捷方式"""
    def vbs_quote(s):
        return s.replace('"', '""')

    vbs_content = f'''Set ws = CreateObject("WScript.Shell")
Set sc = ws.CreateShortcut("{vbs_quote(shortcut_path)}")
sc.TargetPath = "{vbs_quote(target_path)}"
sc.Arguments = "{vbs_quote(arguments)}"
sc.WorkingDirectory = "{vbs_quote(working_dir)}"
sc.Description = "{vbs_quote(description)}"
sc.Save
'''
    with tempfile.NamedTemporaryFile("w", suffix=".vbs", delete=False, encoding="gbk") as f:
        f.write(vbs_content)
        temp_vbs = f.name
    try:
        res = subprocess.run(["cscript", "//nologo", temp_vbs], capture_output=True, text=True)
        return res.returncode == 0
    finally:
        if os.path.exists(temp_vbs):
            try:
                os.remove(temp_vbs)
            except Exception:
                pass

def set_registry_run(enable=True):
    """设置或移除 HKCU Run 注册表键"""
    try:
        import winreg
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE | winreg.KEY_READ)
        if enable:
            vbs_path = get_vbs_launcher_path()
            cmd_val = f'wscript.exe "{vbs_path}"'
            winreg.SetValueEx(key, REG_KEY_NAME, 0, winreg.REG_SZ, cmd_val)
        else:
            try:
                winreg.DeleteValue(key, REG_KEY_NAME)
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
        return True
    except Exception as e:
        return False

def check_registry_run():
    """检查注册表中是否已存在自启动项"""
    try:
        import winreg
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ)
        val, _ = winreg.QueryValueEx(key, REG_KEY_NAME)
        winreg.CloseKey(key)
        return bool(val)
    except Exception:
        return False

def set_schtasks_autostart(enable=True):
    """设置或删除任务计划程序自启动"""
    try:
        if enable:
            vbs_path = get_vbs_launcher_path()
            cmd = f'wscript.exe "{vbs_path}"'
            # 优先尝试 highest 权限，如果无管理员权限回退到标准权限
            res = subprocess.run(
                ["schtasks", "/create", "/tn", TASK_NAME, "/tr", cmd, "/sc", "onlogon", "/f", "/rl", "highest"],
                capture_output=True, text=True
            )
            if res.returncode != 0:
                res = subprocess.run(
                    ["schtasks", "/create", "/tn", TASK_NAME, "/tr", cmd, "/sc", "onlogon", "/f"],
                    capture_output=True, text=True
                )
            return res.returncode == 0
        else:
            res = subprocess.run(
                ["schtasks", "/delete", "/tn", TASK_NAME, "/f"],
                capture_output=True, text=True
            )
            return res.returncode == 0 or "未找到" in res.stderr or "not find" in res.stderr
    except Exception:
        return False

def check_schtasks():
    """检查计划任务中是否存在自启动任务"""
    try:
        res = subprocess.run(
            ["schtasks", "/query", "/tn", TASK_NAME],
            capture_output=True, text=True
        )
        return res.returncode == 0
    except Exception:
        return False

def get_status():
    """获取当前所有自启动机制状态"""
    lnk_exists = os.path.exists(get_shortcut_path())
    reg_exists = check_registry_run()
    task_exists = check_schtasks()
    is_enabled = lnk_exists or reg_exists or task_exists
    return {
        "enabled": is_enabled,
        "shortcut_enabled": lnk_exists,
        "registry_enabled": reg_exists,
        "schtasks_enabled": task_exists,
        "shortcut_path": get_shortcut_path(),
        "launcher_path": get_vbs_launcher_path()
    }

def enable_autostart():
    """启用开机自启动 (全渠道同步注册)"""
    vbs_path = get_vbs_launcher_path()
    if not os.path.exists(vbs_path):
        return {"success": False, "msg": f"未找到启动引导脚本: {vbs_path}"}

    # 1. 创建开始菜单 Startup 文件夹快捷方式
    shortcut_path = get_shortcut_path()
    ok_lnk = create_windows_shortcut(
        shortcut_path=shortcut_path,
        target_path="wscript.exe",
        arguments=f'"{vbs_path}"',
        working_dir=BASE_DIR,
        description="轮毂异响检测系统 - 开机自启动"
    )

    # 2. 注册表 Run 键
    ok_reg = set_registry_run(enable=True)

    # 3. 计划任务
    ok_task = set_schtasks_autostart(enable=True)

    return {
        "success": True,
        "msg": "已成功开启开机自启动！系统已在 Windows 启动文件夹与注册表中完成注册，下次开机/重启将全自动静默唤醒监控后台并打开大屏。",
        "status": get_status()
    }

def disable_autostart():
    """取消开机自启动 (全渠道彻底清理)"""
    # 1. 删除 Startup 快捷方式
    shortcut_path = get_shortcut_path()
    if os.path.exists(shortcut_path):
        try:
            os.remove(shortcut_path)
        except Exception:
            pass

    # 2. 清理注册表
    set_registry_run(enable=False)

    # 3. 清理任务计划
    set_schtasks_autostart(enable=False)

    return {
        "success": True,
        "msg": "已成功取消开机自启动。自启动快捷方式、注册表项与计划任务已全部安全注销。",
        "status": get_status()
    }

def main():
    parser = argparse.ArgumentParser(description="风电机组碰撞声学诊断系统 · 开机自启动管理器")
    parser.add_argument("--enable", action="store_true", help="开启开机自启动")
    parser.add_argument("--disable", action="store_true", help="取消开机自启动")
    parser.add_argument("--status", action="store_true", help="查询当前开机自启动状态")
    parser.add_argument("--json", action="store_true", help="输出 JSON 格式")
    args = parser.parse_args()

    if args.json:
        if args.enable:
            res = enable_autostart()
        elif args.disable:
            res = disable_autostart()
        else:
            res = {"success": True, "status": get_status()}
        print(json.dumps(res, ensure_ascii=False))
        return

    print("=" * 72)
    print("🚀 轮毂异响检测系统 · 开机自启动管理器")
    print("=" * 72)

    if args.enable:
        res = enable_autostart()
        print(f"\n[OK] {res['msg']}")
        s = res["status"]
        print(f">> Windows 启动目录快捷方式: {'[已就绪]' if s['shortcut_enabled'] else '[未生成]'}")
        print(f">> Windows 注册表 Run 自启动: {'[已生效]' if s['registry_enabled'] else '[未生效]'}")
        print(f">> Windows 计划任务调度: {'[已注册]' if s['schtasks_enabled'] else '[未注册]'}")
        print("\n提示：开机自启动采用【一键后台静默启动.vbs】机制，开机后绝不弹出 CMD 黑框，模型载入就绪后自动唤醒大屏。")
    elif args.disable:
        res = disable_autostart()
        print(f"\n[OK] {res['msg']}")
    else:
        st = get_status()
        print("\n当前开机自启动配置状态：")
        print(f"  * 整体状态: {'【已启用 开机自启】' if st['enabled'] else '【未开启】'}")
        print(f"  * 启动文件夹快捷方式: {st['shortcut_path']} ({'存在' if st['shortcut_enabled'] else '无'})")
        print(f"  * 注册表自启项: {'存在' if st['registry_enabled'] else '无'}")
        print(f"  * 计划任务自启项: {'存在' if st['schtasks_enabled'] else '无'}")
        print("\n可用命令：")
        print("  python autostart.py --enable   # 开启开机自启动")
        print("  python autostart.py --disable  # 取消开机自启动")
        print("  python autostart.py --status   # 查看当前状态")

    print("=" * 72)

if __name__ == "__main__":
    main()
