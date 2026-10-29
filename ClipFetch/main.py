from fastapi import FastAPI
from contextlib import asynccontextmanager
from app.engine import engine
from app.routers import auth, downloads
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup code
    async with engine.connect() as conn:
        await conn.exec_driver_sql("SELECT 1") 
    yield
    # Shutdown code
    await engine.dispose()

app = FastAPI(title= "ClipFetch API", description= "A Private Video Downloader ", version= "0.0.1", lifespan=lifespan)
app.include_router(auth.router)
app.include_router(downloads.router)

@app.get("/")
async def root():
    return {"message": "Welcome to ClipFetch API!", "Status": "Running"}

@app.get ("/health")
async def health_check():
    return {"status": "healthy"}