import re
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, StrictBool

from .capture_store import PHOTO, VIDEO


class CollectionUpdate(BaseModel):
    enabled: StrictBool


def media_range(header, size):
    matched = re.fullmatch(r"bytes=([0-9]{0,20})-([0-9]{0,20})", header)
    if matched:
        start, end = matched.groups()
        if start:
            first = int(start)
            last = min(int(end), size - 1) if end else size - 1
            if first <= last:
                return first, last
        elif end and int(end) > 0:
            return max(size - int(end), 0), size - 1
    raise HTTPException(416, "요청한 파일 범위를 제공할 수 없습니다.",
                        headers={"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"})


def create_capture_router(store, require_write_access):
    router = APIRouter(prefix="/api/captures", tags=["patrol captures"])

    @router.get("")
    def records(offset: Annotated[int, Query(ge=0)] = 0,
                limit: Annotated[int, Query(ge=1, le=60)] = 12):
        return store.records(offset, limit)

    @router.patch("/collection", dependencies=[Depends(require_write_access)])
    def collection(change: CollectionUpdate):
        store.set_enabled(change.enabled)
        return {"collection_enabled": store.is_enabled()}

    @router.get("/{run_id}/{capture_id}/{kind}")
    def media(request: Request, run_id: int, capture_id: UUID, kind: Literal["photo", "video"]):
        media_kind, media_type = (PHOTO, "image/jpeg") if kind == "photo" else (VIDEO, "video/webm")
        size = store.media_size(run_id, capture_id.hex, media_kind)
        if not size:
            raise HTTPException(404, "촬영 파일을 찾을 수 없습니다.")
        first, last, status = 0, size - 1, 200
        headers = {"Accept-Ranges": "bytes", "Cache-Control": "private, no-store"}
        if request_range := request.headers.get("range"):
            first, last = media_range(request_range, size)
            status = 206
            headers["Content-Range"] = f"bytes {first}-{last}/{size}"
        headers["Content-Length"] = str(last - first + 1)
        return StreamingResponse(store.media_bytes(run_id, capture_id.hex, media_kind, first, last),
                                 status_code=status, media_type=media_type, headers=headers)

    return router
