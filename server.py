"""Local visualization and scenario controls."""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from simulation import SCENARIOS, Simulation

STATIC = Path(__file__).with_name("static")
run = None
task = None
control_lock = asyncio.Lock()


@asynccontextmanager
async def lifespan(app):
    yield
    if task and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


app = FastAPI(title="TrafficControl", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


class Start(BaseModel):
    scenario: Literal["UC-01", "UC-02", "UC-03", "UC-04", "UC-05"]
    speed: Literal[1, 4, 12, 30] = 4


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/scenarios")
async def scenarios():
    return [{"id": key, "title": title, "description": description, "duration": duration}
            for key, (title, description, duration) in SCENARIOS.items()]


@app.post("/api/run", status_code=202)
async def start(settings: Start):
    global run, task
    async with control_lock:
        if task and not task.done():
            raise HTTPException(status_code=409, detail="Сначала остановите текущий сценарий.")
        run = Simulation(settings.scenario, settings.speed)
        task = asyncio.create_task(run.run())
        return run.published


@app.post("/api/stop")
async def stop():
    async with control_lock:
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if run.status == "starting":
                run.status = "stopped"
                run.published = run.snapshot()
        return run.published if run else {"status": "idle"}


@app.get("/api/state")
async def state(after: int = Query(0, ge=0), run_id: str = ""):
    if not run:
        return {"status": "idle", "events": [], "run_id": None, "event_count": 0}
    count = run.published["event_count"]
    start = min(after, count) if run_id == run.bus.id else max(0, count - 100)
    return {**run.published, "events": run.journal.events[start:count], "run_id": run.bus.id}


@app.get("/api/log")
async def log():
    if not run:
        raise HTTPException(status_code=404, detail="Запустите сценарий.")
    return Response(run.journal.text(), media_type="text/plain", headers={
        "Content-Disposition": f'attachment; filename="TrafficControl-{run.scenario}.log"',
    })
