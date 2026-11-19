"""
PortfolioIQ API.  Run from the backend folder:   python -m uvicorn main:app --port 8000 --reload
Docs (try every endpoint in the browser):        http://localhost:8000/docs
"""
import sys
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "db"))
import db  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

from routers import bi02  # noqa: E402


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()  # creates tables if missing
    yield


app = FastAPI(title="PortfolioIQ API", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
                   allow_methods=["*"], allow_headers=["*"])
app.include_router(bi02.router)

# BI-03 is optional: if it cannot be imported, BI-02 keeps working and the problem is only logged.
try:
    from routers import bi03  # noqa: E402
    app.include_router(bi03.router)
except Exception as e:  # pragma: no cover
    print(f"[main] BI-03 router not loaded: {e!r}")


@app.get("/health")
def health():
    return {"ok": True}
