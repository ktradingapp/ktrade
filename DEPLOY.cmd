@echo off
setlocal enabledelayedexpansion

cd /d "C:\trading-agent\Master Deployment"

echo.
echo ================================
echo        KTrade Deployment
echo ================================
echo.

echo Available KTrade versions:
echo.

set COUNT=0

for /d %%D in (ktrade_*) do (
    set /a COUNT+=1
    set FOLDER=%%D
    set VERSION=!FOLDER:ktrade_=!
    echo   !VERSION!
)

echo.

if "%COUNT%"=="0" (
    echo ERROR: No ktrade_* version folders found in Master Deployment.
    echo.
    echo Example expected folders:
    echo   ktrade_13.9
    echo   ktrade_13.10
    echo.
    pause
    exit /b 1
)

set /p VERSION_NUM=Enter version to deploy, example 13.9 or 13.10: 

if "%VERSION_NUM%"=="" (
    echo ERROR: version cannot be empty.
    pause
    exit /b 1
)

set DEPLOY_FOLDER=ktrade_%VERSION_NUM%

if not exist "%DEPLOY_FOLDER%\" (
    echo.
    echo ERROR: Version "%VERSION_NUM%" not found.
    echo Expected folder:
    echo   %DEPLOY_FOLDER%
    echo.
    echo Available versions:
    for /d %%D in (ktrade_*) do (
        set FOLDER=%%D
        set VERSION=!FOLDER:ktrade_=!
        echo   !VERSION!
    )
    echo.
    pause
    exit /b 1
)

echo.
echo Selected version:
echo   %VERSION_NUM%
echo.
echo Deploy folder:
echo   %DEPLOY_FOLDER%
echo.

echo Checking required files...
echo.

if not exist "%DEPLOY_FOLDER%\backend\ktrade_alpaca.py" (
    echo ERROR: Missing %DEPLOY_FOLDER%\backend\ktrade_alpaca.py
    pause
    exit /b 1
)

if not exist "%DEPLOY_FOLDER%\agent\ktrade_agent_v9.py" (
    echo ERROR: Missing %DEPLOY_FOLDER%\agent\ktrade_agent_v9.py
    pause
    exit /b 1
)

if not exist "%DEPLOY_FOLDER%\frontend\KTrade_preview.html" (
    echo ERROR: Missing %DEPLOY_FOLDER%\frontend\KTrade_preview.html
    pause
    exit /b 1
)

if not exist "server_scripts\deploy_ktrade_release.sh" (
    echo ERROR: Missing server_scripts\deploy_ktrade_release.sh
    pause
    exit /b 1
)

if not exist ".github\workflows\deploy.yml" (
    echo ERROR: Missing .github\workflows\deploy.yml
    pause
    exit /b 1
)

where gh >nul 2>nul
if errorlevel 1 (
    echo ERROR: GitHub CLI not found.
    echo Install GitHub CLI and run:
    echo   gh auth login
    pause
    exit /b 1
)

echo Required files OK.
echo.

set DATESTAMP=%date:~-4%%date:~4,2%%date:~7,2%
set TIMESTAMP=%time:~0,2%%time:~3,2%
set TIMESTAMP=%TIMESTAMP: =0%

set RELEASE_VERSION=v%VERSION_NUM%-local-%DATESTAMP%-%TIMESTAMP%

echo Release version:
echo   %RELEASE_VERSION%
echo.

set /p CONFIRM=Continue deployment? Type Y to continue: 

if /i not "%CONFIRM%"=="Y" (
    echo Deployment cancelled.
    pause
    exit /b 0
)

echo.
echo Checking git status...
echo.

git status

echo.
set /p COMMIT_MSG=Enter commit message, or press Enter for default: 

if "%COMMIT_MSG%"=="" (
    set COMMIT_MSG=Deploy %RELEASE_VERSION%
)

echo.
echo Adding selected version files...
echo.

git add "%DEPLOY_FOLDER%"
git add "server_scripts\deploy_ktrade_release.sh"
git add ".github\workflows\deploy.yml"

echo.
echo Creating commit...
echo.

git commit -m "%COMMIT_MSG%"

if errorlevel 1 (
    echo.
    echo NOTE: git commit failed. This usually means there are no new changes to commit.
    echo Continuing to workflow deploy anyway...
)

echo.
echo Pushing to GitHub...
echo.

git push

if errorlevel 1 (
    echo.
    echo ERROR: git push failed.
    pause
    exit /b 1
)

echo.
echo Starting GitHub Actions deployment...
echo.

gh workflow run deploy.yml -f deploy_folder=%DEPLOY_FOLDER% -f release_version=%RELEASE_VERSION%

if errorlevel 1 (
    echo.
    echo ERROR: GitHub workflow failed to start.
    echo Make sure GitHub CLI is installed and logged in:
    echo   gh auth login
    pause
    exit /b 1
)

echo.
echo Deployment started.
echo.
echo Watching GitHub Actions run...
echo.

gh run watch

echo.
echo Deployment command completed.
echo.
pause
