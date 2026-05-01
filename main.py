from src.core.config import settings

# DEBUG TEMPORÁRIO
print(f"🔑 API KEY carregada: {settings.EVOLUTION_API_KEY[:10]}...")
print(f"🌐 URL: {settings.EVOLUTION_API_URL}")
print(f"📱 INSTANCE: {settings.EVOLUTION_INSTANCE}")

from fastapi import FastAPI, Request
from src.handlers.text_handler import handle_text_message
import httpx
import uvicorn
import time

app = FastAPI(title="Porquim 2.0")

@app.post("/webhook")
@app.post("/webhook/{any:path}")
async def evolution_webhook(request: Request, any: str = None):
    data = await request.json()

    event = any or "webhook"
    print(f"\n📥 [WEBHOOK] Evento: {event}")

    text_body = None
    remote_jid = None

    if isinstance(data, dict):
        if "data" in data and isinstance(data["data"], dict):
            msg_data = data["data"]

            # ✅ Ignora mensagens enviadas pelo próprio bot
            from_me = msg_data.get("key", {}).get("fromMe", False)
            if from_me:
                print("⚠️ Mensagem própria do bot, ignorando.")
                return {"status": "ok"}

            # ✅ Ignora mensagens antigas (mais de 30 segundos)
            timestamp = msg_data.get("messageTimestamp", 0)
            agora = int(time.time())
            if agora - timestamp > 30:
                print(f"⚠️ Mensagem antiga ({agora - timestamp}s atrás), ignorando.")
                return {"status": "ok"}

            if "message" in msg_data and isinstance(msg_data["message"], dict):
                text_body = msg_data["message"].get("conversation")
                remote_jid = msg_data.get("key", {}).get("remoteJid")

    if text_body:
        print(f"✅ Mensagem detectada: '{text_body}'")

        response = await handle_text_message({
            "text": {"body": text_body},
            "key": {"remoteJid": remote_jid}
        })

        send_url = f"{settings.EVOLUTION_API_URL}/message/sendText/{settings.EVOLUTION_INSTANCE}"
        print(f"🔄 Enviando para: {send_url}")
        print(f"📦 Número destino: {remote_jid}")

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    send_url,
                    json={
                        "number": remote_jid,
                        "text": response["content"]
                    },
                    headers={"apikey": settings.EVOLUTION_API_KEY},
                    timeout=10
                )
                print(f"📤 Status Evolution: {resp.status_code} {resp.text[:300]}")
                if resp.status_code in [200, 201]:
                    print("✅ Resposta enviada com sucesso!")
                else:
                    print("❌ Evolution retornou erro")
        except Exception as e:
            print(f"❌ Erro ao chamar Evolution: {e}")
    else:
        print("⚠️ Evento sem mensagem de texto (normal)")

    return {"status": "ok"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=settings.PORT)
    
#fix final: envio de resposta corrigido + log completo
