from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from app.build import FILES


ROOT = Path(__file__).resolve().parents[1]


def create_app(root: str | Path = ROOT) -> FastAPI:
    static = Path(root).resolve() / 'app/static'
    application = FastAPI(title='ERC-8004 Agent Confidence', docs_url=None, redoc_url=None, openapi_url=None)

    def document(name: str):
        path = static / name
        if not path.is_file():
            raise HTTPException(404, 'Document unavailable')
        return FileResponse(path)

    @application.get('/')
    def index():
        return document('index.html')

    @application.get('/technical-report')
    @application.get('/technical-report/')
    def technical_report():
        return document('technical-report.html')

    @application.get('/static/{path:path}')
    def asset(path: str):
        if path not in FILES or path.endswith('.html'):
            raise HTTPException(404, 'Asset unavailable')
        return document(path)

    return application


app = create_app()


def run():
    import uvicorn

    uvicorn.run('app.main:app', host='0.0.0.0', port=8000)


if __name__ == '__main__':
    run()
