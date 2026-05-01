from fastapi import FastAPI, Request
from src.core.config import settings
from src.handlers.text_handler import handle_text_message
import httpx
import uvicorn
import json

app = FastAPI(title="Porquim 2.0")

@app.post("/webhook")
@app.post("/webhook/{any:path}")
async def evolution_webhook(request: Request, any: str = None):
    data = await request.json()
    
    event = any or "webhook"
    print(f"\n📥 [WEBHOOK] Evento: {event}")

    # Detecção da mensagem
    message = None
    text_body = None

    if isinstance(data, dict):
        if "data" in data and isinstance(data["data"], dict):
            if "message" in data["data"] and isinstance(data["data"]["message"], dict):
                text_body = data["data"]["message"].get("conversation")
                message = data["data"]
            elif "messages" in data["data"] and data["data"]["messages"]:
                message = data["data"]["messages"][0]

    if text_body:
        print(f"✅ Mensagem detectada: '{text_body}'")

        response = await handle_text_message({
            "text": {"body": text_body},
            "key": message.get("key", {}) if message else {}
        })

        # === ENVIO DA RESPOSTA ===
        send_url = f"{settings.EVOLUTION_API_URL}/message/sendText/{settings.EVOLUTION_INSTANCE}"
        print(f"🔄 Tentando enviar resposta para: {send_url}")

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    send_url,
                    json={
                        "number": message.get("key", {}).get("remoteJid"),
                        "text": response["content"]
                    },
                    headers={"apikey": settings.EVOLUTION_API_KEY},
                    timeout=10
                )
                print(f"📤 Status da resposta Evolution: {resp.status_code} {resp.text[:200]}")
                if resp.status_code == 200:
                    print("✅ Resposta enviada com sucesso!")
                else:
                    print("❌ Evolution retornou erro (provavelmente instance name errado)")
        except Exception as e:
            print(f"❌ Erro ao chamar Evolution: {e}")
    else:
        print("⚠️ Evento sem mensagem de texto (normal)")

    return {"status": "ok"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=settings.PORT)


#fix final: envio de resposta corrigido + log completo
