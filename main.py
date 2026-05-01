from src.core.config import settings

import asyncio
import time
import httpx
import uvicorn
from fastapi import FastAPI, Request
from contextlib import asynccontextmanager

from src.core.database import get_pool
from src.handlers.text_handler import handle_text_message
from src.handlers.audio_handler import handle_audio_message
from src.handlers.image_handler import handle_image_message


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Inicializa o pool do banco na startup
    print("🐘 Conectando ao PostgreSQL...")
    await get_pool()
    print("✅ Banco conectado e tabelas criadas!")
    yield
    print("👋 Encerrando Porquim...")


app = FastAPI(title="Porquim 2.0 🐷", lifespan=lifespan)


async def _enviar_resposta(remote_jid: str, texto: str):
    """Envia resposta de volta via Evolution API."""
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


@app.post("/webhook")
@app.post("/webhook/{any:path}")
async def evolution_webhook(request: Request, any: str = None):
    data = await request.json()
    event = any or "webhook"
    print(f"\n📥 [WEBHOOK] Evento: {event}")

    if not isinstance(data, dict) or "data" not in data:
        return {"status": "ok"}

    msg_data = data["data"]

# Evolution às vezes manda data como lista (ex: contacts-update)
    if isinstance(msg_data, list):
        return {"status": "ok"}

    # Ignora mensagens do próprio bot
    if msg_data.get("key", {}).get("fromMe", False):
        print("⚠️ Mensagem própria, ignorando.")
        return {"status": "ok"}

    # Ignora mensagens antigas (> 30s)
    timestamp = msg_data.get("messageTimestamp", 0)
    if int(time.time()) - timestamp > 30:
        print(f"⚠️ Mensagem antiga, ignorando.")
        return {"status": "ok"}

    remote_jid = msg_data.get("key", {}).get("remoteJid")
    msg = msg_data.get("message", {})

    if not remote_jid or not msg:
        return {"status": "ok"}

    response = None

    # ── 1. Texto simples ────────────────────────────────────
    text_body = msg.get("conversation") or msg.get("extendedTextMessage", {}).get("text")
    if text_body:
        print(f"✅ Texto: '{text_body}'")
        response = await handle_text_message({
            "text": {"body": text_body},
            "key": {"remoteJid": remote_jid}
        })

    # ── 2. Áudio ────────────────────────────────────────────
    elif "audioMessage" in msg:
        print("🎤 Áudio recebido")
        audio_msg = msg["audioMessage"]
        response = await handle_audio_message({
            "audio": {
                "url": audio_msg.get("url"),
                "mediaUrl": audio_msg.get("directPath"),
                "mimetype": audio_msg.get("mimetype", "audio/ogg"),
                "base64": msg_data.get("message", {}).get("base64")
            },
            "key": {"remoteJid": remote_jid}
        })

    # ── 3. Imagem / Comprovante ─────────────────────────────
    elif "imageMessage" in msg:
        caption = msg["imageMessage"].get("caption", "").strip()
        print(f"📷 Imagem recebida (caption: '{caption}')")
        response = await handle_image_message({
            "image": {
                "url": msg["imageMessage"].get("url"),
                "mediaUrl": msg["imageMessage"].get("directPath"),
                "mimetype": msg["imageMessage"].get("mimetype", "image/jpeg"),
                "base64": msg_data.get("message", {}).get("base64")
            },
            "key": {"remoteJid": remote_jid}
        })

    # ── 4. Tipo não reconhecido ─────────────────────────────
    else:
        print(f"⚠️ Tipo de mensagem não suportado: {list(msg.keys())}")
        return {"status": "ok"}

    if response:
        await _enviar_resposta(remote_jid, response["content"])

    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=settings.PORT)
