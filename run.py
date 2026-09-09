import uvicorn
from model_scheduler.config import load_config

if __name__ == "__main__":
    cfg = load_config("config.yaml")
    uvicorn.run(
        "app:app",
        host=cfg.server.get("host", "127.0.0.1"),
        port=int(cfg.server.get("port", 8090)),
        reload=False,
    )
