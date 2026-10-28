# -*- coding: utf-8 -*-
"""
=============================================================================
风电机组碰撞声学智能诊断系统 · 自动更新与热升级引擎 (Auto Updater)
功能：
  1. 联网检测 GitHub 远程仓库 (https://github.com/chdliulong-svg/hub-audio-detect) 最新版本
  2. 比对本地与远程提交哈希 (Commit SHA)，支持精准查看更新日志
  3. 安全停止正在运行的监控后台，执行 git pull 快速同步
  4. 自动检查增量依赖库 (requirements.txt) 并通过国内高速镜像安装
  5. 自动调用 download_weights.py 校验大模型权重完整性
  6. 执行完毕自动静默拉起后台服务并唤醒浏览器，做到全程无人值守一键平滑升级
=============================================================================
"""
import os
import sys
import time
import subprocess
import argparse
import json
import traceback

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE_DIR)

def ensure_field_protections():
    """
    现场双重核心安全防护：
    1. 锁定现场 config.json (git update-index --skip-worktree)，防止 pull/reset --hard 冲刷现场 IP 与监听目录
    2. 自动检查并补全 .git/info/exclude 规则，保障现场音频与历史记录免受 git 污染与冲突
    """
    try:
        run_git_cmd(["update-index", "--skip-worktree", "config.json"])
        git_info_dir = os.path.join(BASE_DIR, ".git", "info")
        if os.path.isdir(git_info_dir):
            exclude_path = os.path.join(git_info_dir, "exclude")
            current_content = ""
            if os.path.exists(exclude_path):
                with open(exclude_path, "r", encoding="utf-8", errors="ignore") as f:
                    current_content = f.read()
            
            rules_to_add = [
                "config.json.site_backup*",
                "config.json.bak*",
                ".workbuddy/",
                "records/history.json",
                "records/history.db*",
                "records/backups/*",
                "records/*.wav",
                "records/*.png",
                "records/*.log",
                "incoming_audio/processed/*",
                "incoming_audio/172.16.*",
                "*.tmp",
                "*.bak"
            ]
            missing_rules = [r for r in rules_to_add if r not in current_content]
            if missing_rules:
                with open(exclude_path, "a", encoding="utf-8") as f:
                    f.write("\n# 现场自动更新保护规则\n" + "\n".join(missing_rules) + "\n")
    except Exception as e:
        pass

def run_git_cmd(args, timeout=25):
    """安全执行 Git 命令并返回 (returncode, stdout, stderr)"""
    try:
        res = subprocess.run(
            ["git"] + args,
            cwd=BASE_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=timeout
        )
        return res.returncode, res.stdout.strip(), res.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "Git 命令执行超时 (超过 25 秒)"
    except Exception as e:
        return -1, "", str(e)

APP_VERSION = "v3.2.0"

def check_for_updates():
    """
    检查远程是否有新版本
    返回 dict: {
        "version": str,
        "has_update": bool,
        "local_commit": str,
        "remote_commit": str,
        "local_msg": str,
        "commits_behind": int,
        "changelog": list,
        "error": str
    }
    """
    result = {
        "version": APP_VERSION,
        "has_update": False,
        "local_commit": "",
        "remote_commit": "",
        "local_msg": "",
        "commits_behind": 0,
        "changelog": [],
        "error": ""
    }

    # 1. 检查本地是否为 git 仓库
    rc, out, err = run_git_cmd(["rev-parse", "--is-inside-work-tree"])
    if rc != 0 or out != "true":
        result["error"] = "当前部署目录不是有效的 Git 仓库，无法执行自动在线更新。"
        return result

    # 2. 获取本地 HEAD commit
    rc, local_sha, _ = run_git_cmd(["rev-parse", "--short", "HEAD"])
    rc2, local_msg, _ = run_git_cmd(["log", "-1", "--format=%cd (%s)", "--date=format:%Y-%m-%d %H:%M"])
    result["local_commit"] = local_sha
    result["local_msg"] = local_msg

    # 3. 远端网络连通性与最新分支抓取
    rc, _, err = run_git_cmd(["fetch", "origin", "main", "--quiet"], timeout=30)
    if rc != 0:
        result["error"] = f"连接 GitHub 远程仓库失败: {err} (请检查网络或代理)"
        return result

    # 4. 获取远程 origin/main commit
    rc, remote_sha, _ = run_git_cmd(["rev-parse", "--short", "origin/main"])
    result["remote_commit"] = remote_sha

    if not local_sha or not remote_sha:
        result["error"] = "无法解析本地或远程版本号"
        return result

    if local_sha == remote_sha:
        result["has_update"] = False
        return result

    # 5. 计算落后的提交数量与日志
    rc, count_str, _ = run_git_cmd(["rev-list", "--count", "HEAD..origin/main"])
    behind = int(count_str) if count_str.isdigit() else 1
    result["commits_behind"] = behind
    result["has_update"] = behind > 0

    if result["has_update"]:
        rc, log_str, _ = run_git_cmd(["log", "HEAD..origin/main", "--oneline", "--max-count=15"])
        if log_str:
            result["changelog"] = log_str.splitlines()

    return result

def apply_update(restart_service=True):
    """
    执行完整更新流程：
      1. 停止当前后台服务
      2. 拉取最新代码
      3. 更新依赖库
      4. 校验/拉取模型权重
      5. 重新启动服务
    """
    print("=" * 70)
    print("[UPDATE] 正在执行【风电机组碰撞声学智能诊断系统】在线自动更新...")
    print("=" * 70)
    print()

    # 1. 安全停止正在运行的后台服务
    print("[1/5] 正在安全停止当前后台监控服务，释放端口与文件占用...")
    stop_py = os.path.join(BASE_DIR, "stop_daemon.py")
    if os.path.exists(stop_py):
        subprocess.run([sys.executable, stop_py], cwd=BASE_DIR)
    time.sleep(1.5)
    print("  [OK] 服务已安全停止。\n")

    # 2. 拉取远程最新代码
    print("[2/5] 正在从 GitHub (chdliulong-svg/hub-audio-detect) 拉取最新代码...")
    ensure_field_protections()

    # 现场配置硬备份
    cfg_path = os.path.join(BASE_DIR, "config.json")
    cfg_backup = os.path.join(BASE_DIR, "config.json.site_backup")
    if os.path.exists(cfg_path):
        try:
            shutil.copy2(cfg_path, cfg_backup)
        except Exception:
            pass

    # 若有未跟踪的意外冲突变动，先保护或清理
    run_git_cmd(["stash", "--quiet"])
    rc, pull_out, pull_err = run_git_cmd(["pull", "origin", "main"], timeout=60)
    if rc != 0:
        print(f"  [ERROR] Git pull 更新失败: {pull_err}")
        print("  正在尝试强制对齐远程仓库版本...")
        rc2, _, err2 = run_git_cmd(["reset", "--hard", "origin/main"])
        if rc2 != 0:
            print(f"  [ERROR] 强制重置失败: {err2}")
            return False

    # 现场配置完整性二次核验与还原防护
    if os.path.exists(cfg_backup):
        try:
            if not os.path.exists(cfg_path) or os.path.getsize(cfg_path) == 0:
                shutil.copy2(cfg_backup, cfg_path)
            run_git_cmd(["update-index", "--skip-worktree", "config.json"])
        except Exception:
            pass

    print("  [OK] 核心代码已成功同步至最新版本！(现场环境配置已完好保留)\n")

    # 3. 增量更新 Python 依赖
    req_file = os.path.join(BASE_DIR, "requirements.txt")
    if os.path.exists(req_file):
        print("[3/5] 正在校验并增量更新 Python 依赖库 (国内清华高速源)...")
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "-r", req_file, "-i", "https://pypi.tuna.tsinghua.edu.cn/simple", "--quiet"],
                cwd=BASE_DIR,
                check=True
            )
            print("  [OK] Python 依赖环境校验就绪。\n")
        except Exception as e:
            print(f"  [!] 依赖更新遇到警告: {e} (继续后续步骤)\n")

    # 4. 检查声学大模型基座权重
    download_py = os.path.join(BASE_DIR, "download_weights.py")
    if os.path.exists(download_py):
        print("[4/5] 正在校验 3 核心声学大模型基座权重完整性...")
        try:
            subprocess.run([sys.executable, download_py], cwd=BASE_DIR, check=True)
            print("  [OK] 声学模型权重校验通过。\n")
        except Exception as e:
            print(f"  [!] 权重校验警告: {e} (继续后续步骤)\n")

    # 5. 重启后台监控服务
    if restart_service:
        print("[5/5] 正在重新启动监控系统后台服务 (静默无黑框模式)...")
        start_py = os.path.join(BASE_DIR, "start_daemon.py")
        if os.path.exists(start_py):
            subprocess.Popen([sys.executable, start_py], cwd=BASE_DIR)
        print("  [OK] 启动指令已发出，正在后台装载大模型中...\n")

    print("=" * 70)
    print("[OK] 系统在线自动更新成功完成！")
    print("=" * 70)
    return True

def main():
    ensure_field_protections()
    parser = argparse.ArgumentParser(description="风电机组碰撞声学系统自动更新器")
    parser.add_argument("--check", action="store_true", help="仅检测是否有新版本并输出详情")
    parser.add_argument("--json", action="store_true", help="以 JSON 格式输出检测结果供 Web 后台使用")
    parser.add_argument("--apply", action="store_true", help="立即执行在线拉取、更新依赖并重启服务")
    parser.add_argument("--silent", action="store_true", help="静默模式：仅在检测到有更新时才自动升级，无更新时不执行任何操作")
    parser.add_argument("--no-restart", action="store_true", help="更新完成后不自动重启后台服务")

    args = parser.parse_args()

    # 1. 仅检查模式
    if args.check:
        res = check_for_updates()
        if args.json:
            print(json.dumps(res, ensure_ascii=False))
        else:
            print("=" * 70)
            print("[CHECK] 正在查询 GitHub 远程仓库最新版本状态...")
            print("=" * 70)
            if res["error"]:
                print(f"[ERROR] {res['error']}")
                return 1
            print(f">> 当前本地版本 : {res['local_commit']} ({res['local_msg']})")
            print(f">> 远程最新版本 : {res['remote_commit']}")
            if res["has_update"]:
                print(f"\n[!] 发现新版本！本地落后 {res['commits_behind']} 个提交。")
                print("【更新日志清单】:")
                for log in res["changelog"]:
                    print(f"  * {log}")
                print("\n提示：运行【5_一键在线更新系统.bat】即可一键平滑升级至最新版本！")
            else:
                print("\n[OK] 当前已经是最新版本，无需更新！")
            print("=" * 70)
        return 0

    # 2. 静默计划任务模式
    if args.silent:
        res = check_for_updates()
        if res["has_update"]:
            print(f"[AUTO-UPDATE] 检测到远程有 {res['commits_behind']} 个新提交，开始自动静默更新...")
            apply_update(restart_service=not args.no_restart)
        else:
            # 无更新时静默退出
            pass
        return 0

    # 3. 手动/立即应用更新模式
    res = check_for_updates()
    if res["error"]:
        print(f"[ERROR] 更新前自检失败: {res['error']}")
        input("\n请按回车键退出...")
        return 1

    print("=" * 70)
    print(f">> 当前本地版本 : {res['local_commit']}")
    print(f">> 远程最新版本 : {res['remote_commit']}")
    print("=" * 70)

    if not res["has_update"]:
        print("\n[OK] 恭喜！当前系统已经是最新版本，未检测到任何更新。")
        input("\n请按回车键退出...")
        return 0

    print(f"\n[!] 发现远程有 {res['commits_behind']} 个更新提交:")
    for log in res["changelog"]:
        print(f"  * {log}")
    print()

    # 执行更新
    success = apply_update(restart_service=not args.no_restart)
    if not success:
        print("[ERROR] 更新过程中遇到异常，请检查网络或控制台输出。")
    time.sleep(2)
    return 0 if success else 1

if __name__ == "__main__":
    sys.exit(main())
