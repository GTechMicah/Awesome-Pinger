import asyncio
import json
import math
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Literal, Optional
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException, Response, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

DATABASE_URL = os.environ["DATABASE_URL"]
PING_INTERVAL = float(os.getenv("PING_INTERVAL_SECONDS", "5"))
TIMEOUT = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "10"))
STATUS_REFRESH_SECONDS = max(1.0, float(os.getenv("STATUS_REFRESH_SECONDS", "5")))
GRAFANA_PORT = os.getenv("GRAFANA_PORT", "3000")
ENDPOINTS_FILE = os.getenv("ENDPOINTS_FILE", "")


class Base(DeclarativeBase):
    pass


class Endpoint(Base):
    __tablename__ = "endpoints"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    probe_type: Mapped[str] = mapped_column(String(8), nullable=False, default="http")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class PingSample(Base):
    __tablename__ = "ping_samples"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    endpoint_id: Mapped[int] = mapped_column(ForeignKey("endpoints.id", ondelete="CASCADE"), nullable=False, index=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    latency_ms: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    status_code: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
Session = async_sessionmaker(engine, expire_on_commit=False)


class EndpointCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    url: str = Field(min_length=1, max_length=2048)
    probe_type: Literal["http", "icmp"] = "icmp"


class EndpointUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    url: Optional[str] = Field(default=None, min_length=1, max_length=2048)
    probe_type: Optional[Literal["http", "icmp"]] = None
    enabled: Optional[bool] = None


class EndpointOut(BaseModel):
    id: int
    name: str
    url: str
    probe_type: str
    enabled: bool
    sort_order: int
    latest_status: Optional[str] = None
    latest_latency_ms: Optional[float] = None
    latest_recorded_at: Optional[datetime] = None
    created_at: datetime
    model_config = {"from_attributes": True}


def validate_target(value: str, probe_type: str) -> str:
    if probe_type == "icmp":
        raw_value = value.strip()
        parsed = urlparse(raw_value)
        target = parsed.hostname if parsed.scheme else raw_value
        if not target or not re.fullmatch(r"[A-Za-z0-9.:-]+", target):
            raise HTTPException(422, "ICMP target must be a hostname or IP address")
        return raw_value
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(422, "url must be an absolute http(s) URL")
    return value


def sample_status(sample: Optional[PingSample]) -> str:
    if sample is None:
        return "No data"
    if sample.error:
        return "Down"
    if sample.status_code is not None and sample.status_code >= 500:
        return "Server error"
    if sample.status_code is not None and sample.status_code >= 400:
        return "HTTP response"
    return "Healthy"


class MoveRequest(BaseModel):
    direction: Literal["up", "down"]


async def probe_http(endpoint: Endpoint, client: httpx.AsyncClient) -> PingSample:
    started = time.perf_counter_ns()
    now = datetime.now(timezone.utc)
    try:
        response = await client.get(endpoint.url)
        latency_ms = (time.perf_counter_ns() - started) / 1_000_000
        return PingSample(endpoint_id=endpoint.id, recorded_at=now, latency_ms=latency_ms, status_code=response.status_code)
    except httpx.HTTPError as exc:
        return PingSample(endpoint_id=endpoint.id, recorded_at=now, error=f"{type(exc).__name__}: {str(exc)[:500]}")


async def probe_icmp(endpoint: Endpoint) -> PingSample:
    started = time.perf_counter_ns()
    now = datetime.now(timezone.utc)
    parsed = urlparse(endpoint.url)
    target = parsed.hostname if parsed.scheme else endpoint.url
    try:
        process = await asyncio.create_subprocess_exec(
            "ping", "-n", "-c", "1", "-W", str(max(1, math.ceil(TIMEOUT))), target,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        if process.returncode == 0:
            return PingSample(endpoint_id=endpoint.id, recorded_at=now, latency_ms=(time.perf_counter_ns() - started) / 1_000_000)
        error = stderr.decode(errors="replace").strip()[:500] or "ICMP ping failed"
        return PingSample(endpoint_id=endpoint.id, recorded_at=now, error=error)
    except OSError as exc:
        return PingSample(endpoint_id=endpoint.id, recorded_at=now, error=f"ICMP unavailable: {exc}")


async def probe(endpoint: Endpoint, client: httpx.AsyncClient) -> PingSample:
    return await probe_icmp(endpoint) if endpoint.probe_type == "icmp" else await probe_http(endpoint, client)


async def probe_loop() -> None:
    timeout = httpx.Timeout(TIMEOUT)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers={"User-Agent": "pinger/1.0"}) as client:
        while True:
            async with Session() as session:
                endpoints = (await session.scalars(select(Endpoint).where(Endpoint.enabled.is_(True)))).all()
                samples = await asyncio.gather(*(probe(endpoint, client) for endpoint in endpoints))
                if samples:
                    session.add_all(samples)
                    await session.commit()
            await asyncio.sleep(PING_INTERVAL)


async def seed_endpoints() -> None:
    """Seed missing endpoints from an optional JSON config file during startup."""
    if not ENDPOINTS_FILE:
        return
    try:
        with open(ENDPOINTS_FILE, encoding="utf-8") as config_file:
            configured = json.load(config_file)
    except FileNotFoundError:
        return
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid endpoint config JSON: {exc}") from exc
    if not isinstance(configured, list):
        raise RuntimeError("endpoint config must be a JSON array")
    async with Session() as session:
        for item in configured:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not isinstance(item.get("url"), str):
                raise RuntimeError("each configured endpoint needs string name and url fields")
            probe_type = item.get("type", "http")
            if probe_type not in {"http", "icmp"}:
                raise RuntimeError("configured endpoint type must be http or icmp")
            target = validate_target(item["url"], probe_type)
            endpoint = await session.scalar(select(Endpoint).where(Endpoint.name == item["name"]))
            if not endpoint:
                session.add(Endpoint(name=item["name"], url=target, probe_type=probe_type, enabled=bool(item.get("enabled", True))))
        await session.commit()


@asynccontextmanager
async def lifespan(_: FastAPI):
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.execute(text("ALTER TABLE endpoints ADD COLUMN IF NOT EXISTS sort_order INTEGER NOT NULL DEFAULT 0"))
        await connection.execute(text("ALTER TABLE endpoints ADD COLUMN IF NOT EXISTS probe_type VARCHAR(8) NOT NULL DEFAULT 'http'"))
        await connection.execute(text("UPDATE endpoints SET sort_order = id WHERE sort_order = 0"))
        await connection.execute(text("CREATE INDEX IF NOT EXISTS ix_ping_samples_endpoint_recorded ON ping_samples (endpoint_id, recorded_at DESC)"))
    await seed_endpoints()
    task = asyncio.create_task(probe_loop())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await engine.dispose()


app = FastAPI(title="Pinger API", version="1.0.0", lifespan=lifespan)


@app.get("/manage", response_class=HTMLResponse, include_in_schema=False)
async def manage_endpoints():
    """Small same-origin-friendly UI embedded in the local Grafana dashboard."""
    return HTMLResponse("""<!doctype html><html><head><meta charset=\"utf-8\"><style>
body{font:14px system-ui,sans-serif;margin:12px;color:#d8d9da;background:#181b1f}h3{margin:0 0 10px}.dashboard-link{float:right;color:#8ab8ff;text-decoration:none}.ok{color:#73bf69;font-weight:600}.warn{color:#ffb357;font-weight:600}.bad{color:#f2495c;font-weight:600}small{color:#aeb7c2}
form{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}input,select{background:#292d33;color:#d8d9da;border:1px solid #535b66;border-radius:3px;padding:7px;min-width:180px;font:inherit}input.invalid{border-color:#f2495c;outline:1px solid #f2495c}select{min-width:130px}button{background:#5794f2;color:#fff;border:0;border-radius:3px;padding:7px 11px;cursor:pointer}button.remove{background:#d44a3a}.save-all{background:#299c46;margin-top:12px}.error-icon{display:none;position:relative;color:#f2495c;font-size:17px;margin-left:4px;cursor:help}.error-icon.visible{display:inline}.error-icon.visible:hover::after{content:attr(data-message);position:absolute;z-index:10;left:20px;bottom:0;width:240px;padding:7px;border-radius:4px;background:#f2495c;color:#fff;font:12px system-ui,sans-serif;line-height:1.3}table{border-collapse:collapse;width:100%}td,th{padding:7px;text-align:left;border-top:1px solid #343a42}code{color:#8ab8ff}#message{min-height:18px;color:#73bf69}</style></head><body>
<a class=\"dashboard-link\" href=\"http://localhost:__GRAFANA_PORT__/d/endpoint-latency/endpoint-latency\" target=\"_blank\" rel=\"noopener\">Open dashboard ↗</a><h3>Endpoint management</h3><form id=\"add\"><span><input id=\"name\" placeholder=\"Display name\" required><span class=\"error-icon\" data-error-for=\"new-name\">⚠</span></span><select id=\"type\"><option value=\"icmp\" selected>ICMP ping</option><option value=\"http\">HTTP(S) request</option></select><span><input id=\"url\" placeholder=\"IP address or hostname\" required><span class=\"error-icon\" data-error-for=\"new-url\">⚠</span></span><button>Add endpoint</button></form><div id=\"message\"></div><small id=\"refreshed\">Loading status…</small> <small id=\"clock\"></small><table><thead><tr><th>Name</th><th>Probe type</th><th>URL / host</th><th>Latest ping</th><th>Enabled</th><th></th></tr></thead><tbody id=\"items\"></tbody></table><button id=\"save-all\" class=\"save-all\" type=\"button\">Save all changes</button>
<script>
const api='/endpoints', items=document.querySelector('#items'), message=document.querySelector('#message'), refreshed=document.querySelector('#refreshed'), clock=document.querySelector('#clock'), refreshPeriodMs=__STATUS_REFRESH_MS__;
const say=(text, bad=false)=>{message.textContent=text;message.style.color=bad?'#f2495c':'#73bf69'};
async function request(url, options={}) { const r=await fetch(url,{headers:{'Content-Type':'application/json'},...options}); if(!r.ok) throw new Error((await r.json()).detail||r.statusText); return r.status===204?null:r.json(); }
const statusView=e=>{const status=e.latest_status||'No data', statusClass=status==='Healthy'?'ok':status==='HTTP response'?'warn':'bad', latency=e.latest_latency_ms==null?'—':e.latest_latency_ms.toFixed(2)+' ms';return {status,statusClass,latency}};
const targetPlaceholder=type=>type==='icmp'?'IP address or hostname':'https://endpoint.example';
const syncTargetType=(type,input)=>input.placeholder=targetPlaceholder(type);
const setFieldError=(input,message)=>{input.classList.toggle('invalid',Boolean(message));input.setAttribute('aria-invalid',Boolean(message));const icon=input.parentElement.querySelector('.error-icon');if(icon){icon.classList.toggle('visible',Boolean(message));icon.title=message||'';icon.dataset.message=message||''}};
const endpointErrors=(name,url,type)=>{const errors={name:'',url:''};if(!name.trim())errors.name='Endpoint name is required';if(!url.trim())errors.url='URL or host is required';else if(type==='http'&&!/^https?:\\/\\//i.test(url.trim()))errors.url='HTTP(S) probes require an absolute URL starting with http:// or https://';else if(type==='icmp'&&!(/^[A-Za-z0-9.:-]+$/.test(url.trim())||/^https?:\\/\\//i.test(url.trim())))errors.url='ICMP targets must be a hostname, IP address, or HTTP(S) URL';return errors};
function validateRows(showTop=true){const names=new Map(), problems=[];for(const row of items.querySelectorAll('tr')){const name=row.querySelector('.endpoint-name'),url=row.querySelector('.endpoint-url'),type=row.querySelector('.probe-type'),errors=endpointErrors(name.value,url.value,type.value);const key=name.value.trim().toLowerCase();if(key&&names.has(key))errors.name='Endpoint names must be unique';else if(key)names.set(key,row);setFieldError(name,errors.name);setFieldError(url,errors.url);if(errors.name)problems.push(errors.name);if(errors.url)problems.push(errors.url)}if(problems.length&&showTop)say('Fix the highlighted fields: '+problems[0],true);return !problems.length}
function validateNew(showTop=true){const name=document.querySelector('#name'),url=document.querySelector('#url'),type=document.querySelector('#type');if(!showTop&&!name.value.trim()&&!url.value.trim()){setFieldError(name,'');setFieldError(url,'');if(message.textContent.startsWith('Fix the highlighted fields:'))say('');return false}const errors=endpointErrors(name.value,url.value,type.value);const duplicate=[...items.querySelectorAll('.endpoint-name')].some(input=>input.value.trim().toLowerCase()===name.value.trim().toLowerCase());if(duplicate&&name.value.trim())errors.name='Endpoint names must be unique';setFieldError(name,errors.name);setFieldError(url,errors.url);if((errors.name||errors.url)&&showTop)say('Fix the highlighted fields: '+(errors.name||errors.url),true);return !errors.name&&!errors.url}
async function saveAll(refresh=true){if(!validateRows())return false;try{for(const row of items.querySelectorAll('tr')){const id=row.dataset.endpointId;await request(api+'/'+id,{method:'PATCH',body:JSON.stringify({name:row.querySelector('.endpoint-name').value,url:row.querySelector('.endpoint-url').value,probe_type:row.querySelector('.probe-type').value,enabled:row.querySelector('.enabled').checked})})}say('All endpoint changes saved');if(refresh)await load();return true}catch(err){say('Could not save all changes: '+err.message,true);return false}}
async function load(){try{const endpoints=await request(api);items.innerHTML='';for(const e of endpoints){const row=document.createElement('tr'), view=statusView(e), type=e.probe_type||'http';row.dataset.endpointId=e.id;row.innerHTML=`<td><span><input class=\"endpoint-name\" value=\"${escapeHtml(e.name)}\" aria-label=\"Name for ${escapeHtml(e.name)}\"><span class=\"error-icon\">⚠</span></span></td><td><select class=\"probe-type\"><option value=\"http\" ${type==='http'?'selected':''}>HTTP(S)</option><option value=\"icmp\" ${type==='icmp'?'selected':''}>ICMP ping</option></select></td><td><span><input class=\"endpoint-url\" value=\"${escapeHtml(e.url)}\" aria-label=\"Target for ${escapeHtml(e.name)}\" placeholder=\"${targetPlaceholder(type)}\"><span class=\"error-icon\">⚠</span></span></td><td><span data-status=\"${e.id}\" class=\"${view.statusClass}\">${escapeHtml(view.status)}</span><br><small data-latency=\"${e.id}\">${view.latency}</small></td><td><input class=\"enabled\" type=\"checkbox\" ${e.enabled?'checked':''}></td><td><button class=\"up\">↑</button> <button class=\"down\">↓</button> <button class=\"remove\">Remove</button></td>`;const name=row.querySelector('.endpoint-name'),url=row.querySelector('.endpoint-url'),probeType=row.querySelector('.probe-type'),validate=()=>validateRows(false);probeType.onchange=()=>{syncTargetType(probeType.value,url);validate()};name.oninput=validate;url.oninput=validate;const move=async direction=>{if(await saveAll(false)){try{await request(api+'/'+e.id+'/move',{method:'POST',body:JSON.stringify({direction})});load()}catch(err){say(err.message,true)}}};row.querySelector('.up').onclick=()=>move('up');row.querySelector('.down').onclick=()=>move('down');row.querySelector('.remove').onclick=async()=>{if(confirm('Stop pinging '+e.name+'?')&&await saveAll(false)){try{await request(api+'/'+e.id,{method:'DELETE'});say('Removed');load()}catch(err){say(err.message,true)}}};items.append(row)}}catch(err){say(err.message,true)}}
async function refreshStatuses(){try{for(const e of await request(api)){const status=document.querySelector('[data-status="'+e.id+'"]'), latency=document.querySelector('[data-latency="'+e.id+'"]');if(status){const view=statusView(e);status.textContent=view.status;status.className=view.statusClass;latency.textContent=view.latency}}refreshed.textContent='Status refreshed '+new Date().toLocaleTimeString()+' (every '+(refreshPeriodMs/1000)+'s)'}catch(err){refreshed.textContent='Status refresh failed';console.warn('Status refresh failed',err)}}
function escapeHtml(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML}
const updateClock=()=>clock.textContent='Local time: '+new Date().toLocaleTimeString();document.querySelector('#type').onchange=e=>{syncTargetType(e.target.value,document.querySelector('#url'));validateNew(false)};document.querySelector('#name').oninput=()=>validateNew(false);document.querySelector('#url').oninput=()=>validateNew(false);document.querySelector('#save-all').onclick=()=>saveAll();document.querySelector('#add').onsubmit=async e=>{e.preventDefault();if(!validateNew())return;try{await request(api,{method:'POST',body:JSON.stringify({name:document.querySelector('#name').value,url:document.querySelector('#url').value,probe_type:document.querySelector('#type').value})});e.target.reset();document.querySelector('#type').value='icmp';document.querySelector('#url').placeholder=targetPlaceholder('icmp');say('Endpoint added and saved');load()}catch(err){say('Could not add endpoint: '+err.message,true)}};load().then(refreshStatuses);updateClock();setInterval(updateClock,1000);setInterval(refreshStatuses,refreshPeriodMs);
</script></body></html>""".replace("__STATUS_REFRESH_MS__", str(int(STATUS_REFRESH_SECONDS * 1000))).replace("__GRAFANA_PORT__", GRAFANA_PORT), headers={"Cache-Control": "no-store"})


@app.get("/health")
async def health():
    async with Session() as session:
        await session.execute(text("SELECT 1"))
    return {"status": "ok", "interval_seconds": PING_INTERVAL, "timeout_seconds": TIMEOUT}


@app.get("/endpoints", response_model=list[EndpointOut])
async def list_endpoints():
    async with Session() as session:
        endpoints = (await session.scalars(select(Endpoint).order_by(Endpoint.sort_order, Endpoint.name))).all()
        for endpoint in endpoints:
            sample = await session.scalar(select(PingSample).where(PingSample.endpoint_id == endpoint.id).order_by(PingSample.recorded_at.desc()).limit(1))
            endpoint.latest_status = sample_status(sample)
            endpoint.latest_latency_ms = sample.latency_ms if sample else None
            endpoint.latest_recorded_at = sample.recorded_at if sample else None
        return endpoints


@app.post("/endpoints", response_model=EndpointOut, status_code=status.HTTP_201_CREATED)
async def create_endpoint(payload: EndpointCreate):
    target = validate_target(payload.url, payload.probe_type)
    async with Session() as session:
        exists = await session.scalar(select(Endpoint).where(Endpoint.name == payload.name))
        if exists:
            raise HTTPException(409, "an endpoint with that name already exists")
        next_order = (await session.scalar(select(func.max(Endpoint.sort_order)))) or 0
        endpoint = Endpoint(name=payload.name, url=target, probe_type=payload.probe_type, sort_order=next_order + 1)
        session.add(endpoint)
        await session.commit()
        await session.refresh(endpoint)
        return endpoint


@app.patch("/endpoints/{endpoint_id}", response_model=EndpointOut)
async def update_endpoint(endpoint_id: int, payload: EndpointUpdate):
    async with Session() as session:
        endpoint = await session.get(Endpoint, endpoint_id)
        if not endpoint:
            raise HTTPException(404, "endpoint not found")
        changes = payload.model_dump(exclude_none=True)
        requested_type = changes.get("probe_type", endpoint.probe_type)
        if "url" in changes or "probe_type" in changes:
            changes["url"] = validate_target(changes.get("url", endpoint.url), requested_type)
        for key, value in changes.items():
            setattr(endpoint, key, value)
        try:
            await session.commit()
        except Exception as exc:
            await session.rollback()
            raise HTTPException(409, "endpoint name must be unique") from exc
        await session.refresh(endpoint)
        return endpoint


@app.delete("/endpoints/{endpoint_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_endpoint(endpoint_id: int):
    async with Session() as session:
        endpoint = await session.get(Endpoint, endpoint_id)
        if not endpoint:
            raise HTTPException(404, "endpoint not found")
        endpoint.enabled = False
        await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post("/endpoints/{endpoint_id}/move", response_model=EndpointOut)
async def move_endpoint(endpoint_id: int, payload: MoveRequest):
    async with Session() as session:
        endpoints = (await session.scalars(select(Endpoint).order_by(Endpoint.sort_order, Endpoint.name))).all()
        index = next((i for i, endpoint in enumerate(endpoints) if endpoint.id == endpoint_id), None)
        if index is None:
            raise HTTPException(404, "endpoint not found")
        other_index = index - 1 if payload.direction == "up" else index + 1
        if 0 <= other_index < len(endpoints):
            current, other = endpoints[index], endpoints[other_index]
            current.sort_order, other.sort_order = other.sort_order, current.sort_order
            await session.commit()
        await session.refresh(endpoints[index])
        return endpoints[index]
