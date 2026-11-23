from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.requests import Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
import pymysql
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .database import Database
from .mission_routes import create_mission_router
from .mission_storage import MissionStore
from .schemas import Target
from .simulator import ControlError, Simulator
from .storage import RunStore
from .training_routes import create_training_router

ASSETS = Path(__file__).resolve().with_name("web")


class ConsoleFiles(StaticFiles):
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if path.endswith((".js", ".css")):
            response.headers["Cache-Control"] = "no-cache"
        return response


def same_origin_control(x_maritime_control: Annotated[str | None, Header()] = None):
    if x_maritime_control != "1":
        raise HTTPException(403, "시뮬레이션 UI에서 요청해 주세요.")


def create_app(simulator=None):
    database = Database()
    sim = simulator or Simulator(RunStore(database))

    @asynccontextmanager
    async def lifespan(_app):
        yield
        sim.close()

    api = FastAPI(title="Maritime Simulator Console", lifespan=lifespan)
    api.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])
    api.mount("/static", ConsoleFiles(directory=ASSETS), name="static")
    api.include_router(create_mission_router(MissionStore(database), same_origin_control))
    api.include_router(create_training_router(same_origin_control))

    @api.exception_handler(ControlError)
    def control_failed(_request: Request, exc: ControlError):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @api.exception_handler(pymysql.MySQLError)
    def database_failed(_request: Request, _exc: pymysql.MySQLError):
        return JSONResponse({"detail": "로컬 MySQL 연결을 확인해 주세요."}, status_code=503)

    @api.exception_handler(RequestValidationError)
    def invalid_request(request: Request, exc: RequestValidationError):
        detail = "좌표를 숫자로 입력하고 고도는 0.1m보다 크게 입력해 주세요."
        if request.url.path.startswith("/api/training"):
            detail = "학습 요청값을 확인해 주세요. 라벨 박스는 이미지 안에, 학습 반복은 1~100으로 입력해 주세요."
        elif request.url.path.startswith("/api/missions"):
            fields = {error["loc"][-1] for error in exc.errors()}
            if "name" in fields:
                detail = "임무 이름을 1~80자로 입력해 주세요."
            elif not fields.intersection({"north", "east", "altitude"}):
                detail = "임무 설정 요청값을 확인해 주세요."
        return JSONResponse({"detail": detail}, status_code=422)

    @api.get("/", include_in_schema=False)
    def index():
        return FileResponse(ASSETS / "index.html", headers={"Cache-Control": "no-cache"})

    @api.get("/training", include_in_schema=False)
    def training():
        return RedirectResponse("/#training")

    @api.get("/api/status")
    def state():
        return sim.snapshot()

    @api.post("/api/runs", status_code=202, dependencies=[Depends(same_origin_control)])
    def start(target: Target):
        return sim.start(target.model_dump())

    @api.post("/api/runs/{run_id}/return", status_code=202, dependencies=[Depends(same_origin_control)])
    def return_to_launch(run_id: int):
        sim.return_to_launch(run_id)
        return {"accepted": True}

    @api.post("/api/runs/{run_id}/stop", status_code=202, dependencies=[Depends(same_origin_control)])
    def stop(run_id: int):
        sim.stop(run_id)
        return {"accepted": True}

    return api


app = create_app()
