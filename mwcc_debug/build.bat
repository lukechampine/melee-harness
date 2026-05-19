@echo off
setlocal enabledelayedexpansion

REM find msvc x86 cl.exe
set "CL_EXE="

for %%V in (
    "C:\Program Files\Microsoft Visual Studio"
    "C:\Program Files (x86)\Microsoft Visual Studio"
) do (
    if exist %%V (
        for /d %%Y in (%%~V\*) do (
            for /d %%E in ("%%Y\*") do (
                for /d %%T in ("%%E\VC\Tools\MSVC\*") do (
                    if exist "%%T\bin\Hostx86\x86\cl.exe" (
                        set "CL_EXE=%%T\bin\Hostx86\x86\cl.exe"
                    )
                    if exist "%%T\bin\Hostx64\x86\cl.exe" (
                        if "!CL_EXE!"=="" (
                            set "CL_EXE=%%T\bin\Hostx64\x86\cl.exe"
                        )
                    )
                )
            )
        )
    )
)

if "%CL_EXE%"=="" (
    echo error: could not find msvc x86 cl.exe
    exit /b 1
)

echo found: %CL_EXE%

REM find kernel32.lib in the windows sdk for VirtualProtect
set "K32LIB="
for /d %%D in ("C:\Program Files (x86)\Windows Kits\10\Lib\*") do (
    if exist "%%D\um\x86\kernel32.lib" (
        set "K32LIB=%%D\um\x86"
    )
)

if "%K32LIB%"=="" (
    echo error: could not find kernel32.lib in windows sdk
    exit /b 1
)

echo.
echo building lmgr326b.dll...
"%CL_EXE%" /nologo /LD /O2 /W3 /GS- mwcc_debug.c /Fe:lmgr326b.dll /link /DEF:mwcc_debug.def /NODEFAULTLIB /ENTRY:DllMain /LIBPATH:"%K32LIB%" kernel32.lib

if %errorlevel% equ 0 (
    echo.
    for %%F in (lmgr326b.dll) do echo   %%~nxF  %%~zF bytes
    echo.
    echo lmgr326b.dll is built!
) else (
    echo build failed.
    exit /b 1
)
