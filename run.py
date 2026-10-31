#!/usr/bin/env python3
"""Runner script for Scout Desk.

Can be run directly with:
    python3 run.py
"""

import sys
import uvicorn

if __name__ == "__main__":
    port = 8000
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        port = int(sys.argv[1])
    print(f"Starting Scout Desk on http://localhost:{port} ...")
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, reload=True)

