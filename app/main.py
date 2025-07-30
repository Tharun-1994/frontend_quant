from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import uvicorn
from app.routes.backtest import router as backtest_router
from app.constants.static_config import INDICATORS, UNIVERSES, OPERATORS, CONNECTORS

app = FastAPI()
app.include_router(backtest_router)
# Serve static files (e.g., Tailwind CSS)

app.mount("/static", StaticFiles(directory=r"C:\Tharun\Projects\SourceCode\frontend_quant\app/static"), name="static")

# Jinja2 templates
templates = Jinja2Templates(directory=r"C:\Tharun\Projects\SourceCode\frontend_quant\app\templates")

@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse("index.html", {
        "request": request,
        "universes": UNIVERSES,
        "indicators": INDICATORS,
        "operators": OPERATORS,
        "connectors": CONNECTORS
    })


# Main entry point
def main():
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=True)

# Only run if this file is executed directly
if __name__ == "__main__":
    main()
