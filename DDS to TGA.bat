@echo off
rem Drag .dds files or folders onto this file to convert them to TGA.
python "%~dp0dds_convert.py" -f tga %*
pause
