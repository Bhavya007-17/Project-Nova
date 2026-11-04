@echo off
rem Nova's native-messaging host for Chrome (SPEC 9). Chrome starts this; it relays to Nova's named pipe.
"%~dp0..\..\.venv\Scripts\python.exe" -I -u "%~dp0nova_host.py" %*
