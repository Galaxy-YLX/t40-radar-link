@echo off
chcp 65001 >nul
rem 实时：默认就是板卡 tcp://192.168.9.1:46000；
rem 连不上时会自动让 Windows 去连 t40_link 这个 WiFi（--no-wifi 可关）
cd /d "%~dp0.."
echo 连接板卡 192.168.9.1:46000（连不上会自动连 t40_link）...
python host\t40_cloud_view.py
if errorlevel 1 (
  echo.
  echo 启动失败，看 t40_cloud_view.log。也可以先确认板卡在不在：
  echo     ping 192.168.9.1
  pause
)
