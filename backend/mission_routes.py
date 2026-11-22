from fastapi import APIRouter, Depends, HTTPException, Response

from .schemas import Mission, MissionCreate, MissionUpdate


def create_mission_router(store, require_write_access):
    router = APIRouter(prefix="/api/missions", tags=["missions"])
    write_access = [Depends(require_write_access)]
    missing = "저장된 임무 설정을 찾을 수 없어요."

    @router.get("", response_model=list[Mission])
    def list_missions():
        return store.list()

    @router.get("/{mission_id}", response_model=Mission)
    def get_mission(mission_id: int):
        mission = store.get(mission_id)
        if mission is None:
            raise HTTPException(404, missing)
        return mission

    @router.post("", status_code=201, response_model=Mission, dependencies=write_access)
    def create_mission(target: MissionCreate):
        return store.create(target.model_dump())

    @router.patch("/{mission_id}", response_model=Mission, dependencies=write_access)
    def update_mission(mission_id: int, target: MissionUpdate):
        mission = store.update(mission_id, target.model_dump(exclude_unset=True))
        if mission is None:
            raise HTTPException(404, missing)
        return mission

    @router.delete("/{mission_id}", status_code=204, dependencies=write_access)
    def delete_mission(mission_id: int):
        if not store.delete(mission_id):
            raise HTTPException(404, missing)
        return Response(status_code=204)

    return router
