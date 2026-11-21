"""Hub phòng chat cho nhiều AI agent. Chạy: python hub.py  rồi mở http://127.0.0.1:8765"""
import asyncio, json, os, re, sqlite3, time
from pathlib import Path

import httpx
import uvicorn
import yaml
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

BASE = Path(__file__).parent
CFG = yaml.safe_load(Path(os.getenv("HUB_CONFIG", BASE / "config.yaml")).read_text(encoding="utf-8"))
AGENTS = {k.lower(): v for k, v in CFG["agents"].items()}
ME = CFG.get("human_name", "Bạn")
MAX_CHAIN = CFG.get("max_bot_chain", 8)
WINDOW = CFG.get("history_window", 30)
TIMEOUT = CFG.get("timeout", 120)
MENTION = re.compile(r'["“](\w+)')  # chỉ tên có dấu " ngay phía trước mới đánh thức

db = sqlite3.connect(os.getenv("HUB_DB", BASE / "room.db"), check_same_thread=False)
db.execute("create table if not exists msgs(id integer primary key autoincrement, ts real, author text, kind text, text text)")

app = FastAPI()
clients: set[WebSocket] = set()
locks = {k: asyncio.Lock() for k in AGENTS}
chain = 0  # số tin agent liên tiếp kể từ tin người gần nhất


async def broadcast(ev: dict):
    for ws in list(clients):
        try:
            await ws.send_json(ev)
        except Exception:
            clients.discard(ws)


def history() -> list[dict]:
    rows = db.execute("select id,ts,author,kind,text from msgs order by id desc limit 200").fetchall()[::-1]
    return [dict(type="msg", id=i, ts=t, author=a, kind=k, text=x) for i, t, a, k, x in rows]


async def post(author: str, kind: str, text: str):
    """Lưu + phát tin, rồi đánh thức các agent được gọi tên."""
    global chain
    if kind == "human":
        chain = 0
    elif kind == "agent":
        chain += 1
    ts = time.time()
    cur = db.execute("insert into msgs(ts,author,kind,text) values(?,?,?,?)", (ts, author, kind, text))
    db.commit()
    await broadcast(dict(type="msg", id=cur.lastrowid, ts=ts, author=author, kind=kind, text=text))
    if kind == "system":
        return
    if kind == "agent" and not AGENTS[author.lower()].get("can_call", True):
        return
    names = []
    for n in MENTION.findall(text):
        n = n.lower()
        if n in AGENTS and n != author.lower() and n not in names:
            names.append(n)
    if not names:
        return
    if chain >= MAX_CHAIN:
        await post("hub", "system", f"Đã có {MAX_CHAIN} tin agent liên tiếp, hub dừng lại chờ bạn.")
        return
    for n in names:
        await broadcast(dict(type="call", frm=author, to=n, chain=chain))
        asyncio.create_task(run_agent(n))


def system_prompt(name: str, cfg: dict) -> str:
    others = ", ".join(f'"{k}' for k in AGENTS if k != name)
    return (
        f"Bạn là {name}, một agent trong phòng chat chung với {ME} và các agent khác ({others}).\n"
        f"Vai trò của bạn: {cfg.get('persona', '(không đặt)')}\n"
        "Luật phòng: chỉ trả lời tin cuối cùng đang gọi bạn, nói đúng việc, ngắn gọn, xong thì im. "
        'Muốn gọi agent khác thì viết dấu " ngay trước tên (ví dụ "' + (next((k for k in AGENTS if k != name), "ten")) + "). "
        "Nhắc tên không có dấu \" thì không đánh thức ai. Chỉ gọi agent khác khi thật sự cần họ làm gì; "
        "việc đã xong thì đừng gọi ai. Không tự viết tiền tố [tên]: ở đầu câu."
    )


async def openai_stream(cfg, system, user):
    key = os.getenv(cfg.get("api_key_env", ""), "") or cfg.get("api_key", "none")
    body = {"model": cfg["model"], "stream": True,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        async with c.stream("POST", url, json=body, headers={"Authorization": f"Bearer {key}"}) as r:
            if r.status_code >= 400:
                raise RuntimeError(f"HTTP {r.status_code}: {(await r.aread())[:200].decode(errors='replace')}")
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                d = line[5:].strip()
                if d == "[DONE]":
                    break
                try:
                    piece = json.loads(d)["choices"][0]["delta"].get("content")
                except (KeyError, IndexError, ValueError):
                    continue
                if piece:
                    yield piece


async def anthropic_stream(cfg, system, user):
    key = os.getenv(cfg.get("api_key_env", "ANTHROPIC_API_KEY"), "")
    if not key:
        raise RuntimeError("thiếu API key Anthropic (biến môi trường " + cfg.get("api_key_env", "ANTHROPIC_API_KEY") + ")")
    body = {"model": cfg["model"], "max_tokens": cfg.get("max_tokens", 1024), "stream": True,
            "system": system, "messages": [{"role": "user", "content": user}]}
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        async with c.stream("POST", "https://api.anthropic.com/v1/messages", json=body, headers=headers) as r:
            if r.status_code >= 400:
                raise RuntimeError(f"HTTP {r.status_code}: {(await r.aread())[:200].decode(errors='replace')}")
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                try:
                    ev = json.loads(line[5:])
                except ValueError:
                    continue
                if ev.get("type") == "error":
                    raise RuntimeError(str(ev.get("error")))
                d = ev.get("delta", {})
                if ev.get("type") == "content_block_delta" and d.get("type") == "text_delta":
                    yield d["text"]


STREAMS = {"openai_compat": openai_stream, "anthropic": anthropic_stream}


async def run_agent(name: str):
    cfg = AGENTS[name]
    async with locks[name]:  # mỗi agent xử lý lần lượt từng lượt gọi
        rows = db.execute("select author,text from msgs where kind!='system' order by id desc limit ?", (WINDOW,)).fetchall()[::-1]
        transcript = "\n".join(f"[{a}]: {t}" for a, t in rows)
        user = f"Lịch sử phòng (cũ → mới):\n{transcript}\n\nHãy trả lời với tư cách {name}, chỉ trả lời tin cuối cùng."
        sid = f"s{time.time_ns()}"
        await broadcast(dict(type="start", sid=sid, author=name))
        buf = ""
        try:
            async for piece in STREAMS[cfg["adapter"]](cfg, system_prompt(name, cfg), user):
                buf += piece
                await broadcast(dict(type="delta", sid=sid, text=piece))
        except Exception as e:
            await broadcast(dict(type="drop", sid=sid))
            await broadcast(dict(type="fail", agent=name))
            await post("hub", "system", f"{name} lỗi: {e}")
            return
        await broadcast(dict(type="drop", sid=sid))
        text = re.sub(r"^\s*\[[^\]]{1,30}\]:\s*", "", buf).strip()
        if text:
            await post(name, "agent", text)


@app.get("/")
async def index():
    return HTMLResponse((BASE / "index.html").read_text(encoding="utf-8"))


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    clients.add(ws)
    await ws.send_json(dict(type="init", me=ME, max=MAX_CHAIN, agents=list(AGENTS), history=history()))
    try:
        while True:
            text = ((await ws.receive_json()).get("text") or "").strip()
            if text == "/clear":
                db.execute("delete from msgs")
                db.commit()
                await broadcast(dict(type="init", me=ME, max=MAX_CHAIN, agents=list(AGENTS), history=[]))
            elif text:
                await post(ME, "human", text)
    except WebSocketDisconnect:
        pass
    finally:
        clients.discard(ws)


if __name__ == "__main__":
    uvicorn.run(app, host=CFG.get("host", "127.0.0.1"), port=CFG.get("port", 8765))
