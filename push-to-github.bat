@echo off
cd /d "%~dp0"
echo ========================================
echo Push CW-Worker-01 as ahmadyeladawi
echo ========================================
echo.
echo When Git asks for login:
echo   Username: ahmadyeladawi
echo   Password: use a Personal Access Token (not your normal password)
echo.
echo Create token here if needed:
echo   https://github.com/settings/tokens
echo   (classic token with "repo" permission)
echo.

git remote remove origin 2>nul
git remote add origin https://github.com/ahmadyeladawi/CW-Worker-01.git

git branch -M main
git add .
git status

git -c user.email="ahmadyeladawi@users.noreply.github.com" -c user.name="ahmadyeladawi" commit -m "Add CW Check worker workflow (id 01)" 2>nul

echo.
echo Pushing...
git push -u origin main
if errorlevel 1 (
  echo.
  echo Push failed.
  echo Tip: open Windows Credential Manager and remove old github.com entries,
  echo then run this file again and sign in as ahmadyeladawi.
  pause
  exit /b 1
)
echo.
echo Done. Open: https://github.com/ahmadyeladawi/CW-Worker-01
pause
