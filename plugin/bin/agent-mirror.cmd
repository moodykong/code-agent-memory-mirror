@echo off
rem Windows launcher for agent-mirror: runs the Python script next to this file.
rem Needs Python 3.8 or newer: the py launcher, or python on PATH.
setlocal
set "AM_SCRIPT=%~dp0agent-mirror"
where py >/dev/null 2>/dev/null && goto use_py
where python >/dev/null 2>/dev/null && goto use_python
echo error: agent-mirror needs Python 3.8 or newer ^(py or python on PATH^) 1>&2
exit /b 2
:use_py
py -3 "%AM_SCRIPT%" %*
exit /b %ERRORLEVEL%
:use_python
python "%AM_SCRIPT%" %*
exit /b %ERRORLEVEL%
