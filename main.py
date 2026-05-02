from src.core.config import settings
import httpx
import uvicorn
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from pydantic import BaseModel
from typing import Optional

from src.core.database import (
    get_pool,
    buscar_gastos_mes,
    atualizar_gasto,
    deletar_gasto,
    salvar_limite,
    buscar_limite,
    buscar_entradas_mes,
    total_entrada_mes,
    deletar_entrada,
)
from src.handlers.text_handler import handle_text_message
from src.handlers.audio_handler import handle_audio_message
from src.handlers.image_handler import handle_image_message


# ── Emoji por categoria ──────────────────────────────────────────────────────
EMOJI_CAT = {
    "Alimentação": "🍔", "Transporte": "🚗", "Moradia": "🏠",
    "Saúde": "💊", "Lazer": "🎮", "Vestuário": "👕",
    "Educação": "📚", "Outros": "📦",
}

EMOJI_ENTRADA_CAT = {
    "Salário": "💼", "Freelance": "💻", "Investimento": "📈",
    "Presente": "🎁", "Reembolso": "🔄", "Outros": "📦",
}

def emoji_para(cat: str) -> str:
    return EMOJI_CAT.get(cat, "📦")

def emoji_entrada_para(cat: str) -> str:
    return EMOJI_ENTRADA_CAT.get(cat, "📦")


# ── Lifespan ─────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🐘 Conectando ao PostgreSQL...")
    await get_pool()
    print("✅ Banco conectado e tabelas criadas!")
    app.state.processed_ids = set()
    yield
    print("👋 Encerrando Porquim...")


app = FastAPI(title="Porquim 2.0 🐷", lifespan=lifespan)

# ── CORS ──────────────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Modelos ───────────────────────────────────────────────────────────────────
class EditarGastoBody(BaseModel):
    descricao: str
    valor: float
    categoria: str
    forma_pagamento: str

class LimiteBody(BaseModel):
    valor: float


# ── Helper Evolution ─────────────────────────────────────────────────────────
async def _enviar_resposta(remote_jid: str, texto: str):
    url = f"{settings.EVOLUTION_API_URL}/message/sendText/{settings.EVOLUTION_INSTANCE}"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                url,
                json={"number": remote_jid, "text": texto},
                headers={"apikey": settings.EVOLUTION_API_KEY}
            )
            print(f"📤 Evolution: {resp.status_code}")
            return resp.status_code in [200, 201]
    except Exception as e:
        print(f"❌ Erro ao enviar resposta: {e}")
        return False


# ════════════════════════════════════════════════════════════════════
# ROTAS
# ════════════════════════════════════════════════════════════════════

@app.get("/")
async def health():
    return {"status": "Porquim 2.0 🐷 online!"}


@app.get("/api/verificar/{usuario}")
async def verificar_usuario(usuario: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        count_gastos = await conn.fetchval(
            "SELECT COUNT(*) FROM gastos WHERE usuario = $1", usuario
        )
        count_entradas = await conn.fetchval(
            "SELECT COUNT(*) FROM entradas WHERE usuario = $1", usuario
        )
    return {"existe": int(count_gastos) > 0 or int(count_entradas) > 0}


@app.get("/api/resumo/{usuario}")
async def resumo(usuario: str, ano: int, mes: int):
    gastos = await buscar_gastos_mes(usuario, ano, mes)
    total_gastos = sum(float(g["valor"]) for g in gastos)
    total_entradas = await total_entrada_mes(usuario, ano, mes)
    saldo = total_entradas - total_gastos

    cat_map: dict[str, float] = {}
    for g in gastos:
        cat_map[g["categoria"]] = cat_map.get(g["categoria"], 0.0) + float(g["valor"])

    categorias = sorted(
        [
            {
                "nome": cat,
                "valor": round(val, 2),
                "pct": round(val / total_gastos * 100, 1) if total_gastos > 0 else 0,
                "emoji": emoji_para(cat),
            }
            for cat, val in cat_map.items()
        ],
        key=lambda x: x["valor"],
        reverse=True,
    )

    limite = await buscar_limite(usuario)
    pct_limite = round(total_gastos / limite * 100, 1) if limite else 0

    return {
        "total": round(total_gastos, 2),
        "total_entradas": round(total_entradas, 2),
        "saldo": round(saldo, 2),
        "num_gastos": len(gastos),
        "categorias": categorias,
        "limite": limite,
        "pct_limite": pct_limite,
    }


@app.get("/api/gastos/{usuario}")
async def listar_gastos(usuario: str, ano: int, mes: int, categoria: Optional[str] = None):
    gastos = await buscar_gastos_mes(usuario, ano, mes)

    if categoria:
        gastos = [g for g in gastos if g["categoria"] == categoria]

    return {
        "gastos": [
            {
                "id": g["id"],
                "descricao": g["descricao"],
                "valor": float(g["valor"]),
                "categoria": g["categoria"],
                "forma_pagamento": g["forma_pagamento"],
                "data": g["data"].isoformat(),
                "fonte": g.get("fonte", "texto"),
                "emoji": emoji_para(g["categoria"]),
            }
            for g in gastos
        ]
    }


@app.get("/api/entradas/{usuario}")
async def listar_entradas(usuario: str, ano: int, mes: int, categoria: Optional[str] = None):
    entradas = await buscar_entradas_mes(usuario, ano, mes)

    if categoria:
        entradas = [e for e in entradas if e["categoria"] == categoria]

    return {
        "entradas": [
            {
                "id": e["id"],
                "descricao": e["descricao"],
                "valor": float(e["valor"]),
                "categoria": e["categoria"],
                "data": e["data"].isoformat(),
                "fonte": e.get("fonte", "texto"),
                "emoji": emoji_entrada_para(e["categoria"]),
            }
            for e in entradas
        ]
    }


@app.delete("/api/entradas/{usuario}/{entrada_id}")
async def excluir_entrada(usuario: str, entrada_id: int):
    ok = await deletar_entrada(entrada_id, usuario)
    if not ok:
        raise HTTPException(status_code=404, detail="Entrada não encontrada")
    return {"ok": True}


@app.put("/api/gastos/{usuario}/{gasto_id}")
async def editar_gasto(usuario: str, gasto_id: int, body: EditarGastoBody):
    ok = await atualizar_gasto(gasto_id, usuario, body.model_dump())
    if not ok:
        raise HTTPException(status_code=404, detail="Gasto não encontrado")
    return {"ok": True}


@app.delete("/api/gastos/{usuario}/{gasto_id}")
async def excluir_gasto(usuario: str, gasto_id: int):
    ok = await deletar_gasto(gasto_id, usuario)
    if not ok:
        raise HTTPException(status_code=404, detail="Gasto não encontrado")
    return {"ok": True}


@app.get("/api/evolucao/{usuario}")
async def evolucao(usuario: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows_gastos = await conn.fetch(
            """
            SELECT
                EXTRACT(YEAR  FROM data)::int AS ano,
                EXTRACT(MONTH FROM data)::int AS mes,
                COALESCE(SUM(valor), 0)::float AS total
            FROM gastos
            WHERE usuario = $1
              AND data >= (CURRENT_DATE - INTERVAL '5 months')::date
            GROUP BY ano, mes
            ORDER BY ano, mes
            """,
            usuario,
        )
        rows_entradas = await conn.fetch(
            """
            SELECT
                EXTRACT(YEAR  FROM data)::int AS ano,
                EXTRACT(MONTH FROM data)::int AS mes,
                COALESCE(SUM(valor), 0)::float AS total
            FROM entradas
            WHERE usuario = $1
              AND data >= (CURRENT_DATE - INTERVAL '5 months')::date
            GROUP BY ano, mes
            ORDER BY ano, mes
            """,
            usuario,
        )

    MESES_SHORT = ["", "Jan", "Fev", "Mar", "Abr", "Mai", "Jun",
                   "Jul", "Ago", "Set", "Out", "Nov", "Dez"]

    # Merge gastos e entradas por ano/mes
    gastos_map = {(r["ano"], r["mes"]): round(r["total"], 2) for r in rows_gastos}
    entradas_map = {(r["ano"], r["mes"]): round(r["total"], 2) for r in rows_entradas}
    all_keys = sorted(set(list(gastos_map.keys()) + list(entradas_map.keys())))

    return {
        "meses": [
            {
                "nome": MESES_SHORT[mes],
                "ano": ano,
                "mes": mes,
                "total": gastos_map.get((ano, mes), 0.0),
                "entradas": entradas_map.get((ano, mes), 0.0),
            }
            for ano, mes in all_keys
        ]
    }


@app.post("/api/limite/{usuario}")
async def definir_limite(usuario: str, body: LimiteBody):
    if body.valor <= 0:
        raise HTTPException(status_code=400, detail="Valor inválido")
    await salvar_limite(usuario, body.valor)
    return {"ok": True}


# ════════════════════════════════════════════════════════════════════
# WEBHOOK WHATSAPP
# ════════════════════════════════════════════════════════════════════

@app.post("/webhook")
@app.post("/webhook/{any:path}")
async def evolution_webhook(request: Request, any: str = None):
    data = await request.json()
    event = any or "webhook"
    print(f"\n📥 [WEBHOOK] Evento: {event}")

    if not isinstance(data, dict) or "data" not in data:
        return {"status": "ok"}

    msg_data = data["data"]

    if isinstance(msg_data, list):
        return {"status": "ok"}

    if msg_data.get("key", {}).get("fromMe", False):
        return {"status": "ok"}

    msg_id = msg_data.get("key", {}).get("id", "")
    if msg_id and msg_id in app.state.processed_ids:
        print(f"⚠️ Duplicata ignorada: {msg_id}")
        return {"status": "ok"}
    if msg_id:
        app.state.processed_ids.add(msg_id)
        if len(app.state.processed_ids) > 200:
            app.state.processed_ids.pop()

    remote_jid = msg_data.get("key", {}).get("remoteJid")
    msg = msg_data.get("message", {})

    if not remote_jid or not msg:
        return {"status": "ok"}

    response = None

    text_body = msg.get("conversation") or msg.get("extendedTextMessage", {}).get("text")
    if text_body:
        print(f"✅ Texto: '{text_body}'")
        response = await handle_text_message({
            "text": {"body": text_body},
            "key": {"remoteJid": remote_jid}
        })

    elif "audioMessage" in msg:
        print("🎤 Áudio recebido")
        response = await handle_audio_message(
            msg_data=msg_data,
            remote_jid=remote_jid,
            ultimo_gasto=None
        )

    elif "imageMessage" in msg:
        caption = msg["imageMessage"].get("caption", "").strip()
        print(f"📷 Imagem recebida (caption: '{caption}')")
        response = await handle_image_message(
            msg_data=msg_data,
            remote_jid=remote_jid
        )

    else:
        print(f"⚠️ Tipo não suportado: {list(msg.keys())}")
        return {"status": "ok"}

    if response:
        await _enviar_resposta(remote_jid, response["content"])

    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=settings.PORT)
