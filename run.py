import uvicorn

if __name__ == "__main__":
    uvicorn.run("main:foodserverapis", reload=True)   # ← changed from app.main
