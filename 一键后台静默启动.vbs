' =============================================================================
' 风电机组碰撞声学诊断系统 · 零窗口纯静默后台启动器
' 双击此脚本，完全不弹出任何控制台黑框，直接在后台启动并在浏览器打开监控页面
' =============================================================================
Set ws = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

' 切换到脚本所在目录
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
ws.CurrentDirectory = scriptDir

' 0 代表隐藏窗口 (vbHide)，后台启动 start_daemon.py
ws.Run "python start_daemon.py", 0, False
