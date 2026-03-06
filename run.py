# run.py
# This file is only for local development
# Do NOT use reload=True in production (Render, Railway, etc.)

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "main:app",               # ← correct reference: file main.py + variable app
        host="0.0.0.0",           # listen on all interfaces
        port=8000,                # local port
        reload=True,              # auto-reload on code changes (dev only)
        log_level="info",         # or "debug" if you want more output
    )
