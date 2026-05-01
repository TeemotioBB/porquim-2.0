from src.core.config import settings
import httpx
import uvicorn
from fastapi import FastAPI, Request, HTTPException
from contextlib import asynccontextmanager
from src.core.database import get_pool, salvar_gasto, buscar_gastos_mes, total_gasto_mes, buscar_limite
from src.handlers.text_handler import handle_text_message
from src.handlers.audio_handler import handle_audio_message
from src.handlers.image_handler import handle_image_message

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🐘 Conectando ao PostgreSQL...")
    await get_pool()
    print("✅ Banco conectado e tabelas criadas!")
    app.state.processed_ids = set()
    yield
    print("👋 Encerrando Porquim...")

app = FastAPI(title="Porquim 2.0 🐷", lifespan=lifespan)

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

@app.get("/")
async def health():
    return {"status": "Porquim 2.0 🐷 online!"}

# ==================== API PARA O DASHBOARD ====================

@app.get("/api/verificar/{numero}")
async def verificar_usuario(numero: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT COUNT(*) as total FROM gastos WHERE usuario = $1", numero
        )
        existe = row["total"] > 0
    return {"existe": existe}

@app.get("/api/resumo/{usuario}")
async def api_resumo(usuario: str, ano: int = None, mes: int = None):
    from datetime import date
    hoje = date.today()
    ano = ano or hoje.year
    mes = mes or hoje.month

    gastos = await buscar_gastos_mes(usuario, ano, mes)
    total = await total_gasto_mes(usuario, ano, mes)
    limite = await buscar_limite(usuario) if (ano == hoje.year and mes == hoje.month) else None

    categorias = {}
    for g in gastos:
        cat = g["categoria"]
        categorias[cat] = categorias.get(cat, 0) + float(g["valor"])

    cat_list = [{"nome": cat, "valor": val, "pct": round(val / total * 100, 1) if total > 0 else 0} 
                for cat, val in sorted(categorias.items(), key=lambda x: x[1], reverse=True)]

    return {
        "total": total,
        "num_gastos": len(gastos),
        "limite": limite,
        "pct_limite": round((total / limite * 100), 1) if limite else 0,
        "categorias": cat_list,
        "gastos": gastos
    }

@app.get("/api/gastos/{usuario}")
async def api_gastos(usuario: str, ano: int = None, mes: int = None, categoria: str = None):
    gastos = await buscar_gastos_mes(usuario, ano or date.today().year, mes or date.today().month)
    if categoria:
        gastos = [g for g in gastos if g["categoria"] == categoria]
    return {"gastos": gastos}

@app.get("/api/evolucao/{usuario}")
async def api_evolucao(usuario: str):
    # Simplificado por enquanto - pode expandir depois
    return {"meses": []}  # placeholder

@app.put("/api/gastos/{usuario}/{gasto_id}")
async def api_editar_gasto(usuario: str, gasto_id: int, dados: dict):
    from src.core.database import atualizar_gasto
    ok = await atualizar_gasto(gasto_id, usuario, dados)
    if not ok:
        raise HTTPException(status_code=404, detail="Gasto não encontrado")
    return {"status": "ok"}

@app.delete("/api/gastos/{usuario}/{gasto_id}")
async def api_deletar_gasto(usuario: str, gasto_id: int):
    from src.core.database import deletar_gasto
    ok = await deletar_gasto(gasto_id, usuario)
    if not ok:
        raise HTTPException(status_code=404, detail="Gasto não encontrado")
    return {"status": "ok"}

# ==================== WEBHOOK ====================

@app.post("/webhook")
@app.post("/webhook/{any:path}")
async def evolution_webhook(request: Request, any: str = None):
    # ... (seu webhook atual continua igual, sem mudança)
    data = await request.json()
    event = any or "webhook"
    print(f"\n📥 [WEBHOOK] Evento: {event}")

    if not isinstance(data, dict) or "data" not in data:
        return {"status": "ok"}

    msg_data = data["data"]

    if isinstance(msg_data, list) or msg_data.get("key", {}).get("fromMe", False):
        return {"status": "ok"}

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
        response = await handle_audio_message(msg_data, remote_jid, None)

    elif "imageMessage" in msg:
        print("📷 Imagem recebida")
        response = await handle_image_message(msg_data, remote_jid, None)

    if response:
        await _enviar_resposta(remote_jid, response["content"])

    return {"status": "ok"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=settings.PORT)
