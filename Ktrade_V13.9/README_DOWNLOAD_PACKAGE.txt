KTrade v10.4 Download Package

This zip is intentionally kept small so ChatGPT can upload/download it reliably.

Included:
- All KTrade v10.4 source files
- Updated strategy/risk/backend/frontend patches
- .env placeholder file
- .env.template
- requirements.txt
- scripts to recreate .venv locally

Not included:
- Real .env secrets/API keys
- Prebuilt .venv folder
- __pycache__, .pyc, and log files

Why .venv is not included:
The all-in-one runtime zip with .venv is about 279 MB and failed to upload/download from ChatGPT. This package recreates the same .venv locally using requirements.txt.

Windows setup:
1. Extract the zip.
2. Open the ktrade_v10.4 folder.
3. Double-click CREATE_VENV_WINDOWS.cmd.
4. Edit .env and add your real keys.
5. Run the project using the existing RUN_*.cmd files.

Mac/Linux setup:
1. Extract the zip.
2. Open Terminal inside ktrade_v10.4.
3. Run: chmod +x CREATE_VENV_MAC_LINUX.sh
4. Run: ./CREATE_VENV_MAC_LINUX.sh
5. Edit .env and add your real keys.
