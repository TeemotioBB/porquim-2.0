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
    print(f"\n📥 [WEBHOOK] Evento recebido: {event}")

    # Debug completo só para messages-upsert
    if event == "messages-upsert":
        print(f"Payload completo: {json.dumps(data, indent=2, ensure_ascii=False)[:1500]}...")

    # 🔥 EXTRAÇÃO CORRETA para Evolution API v2.3.7
    message = None
    if isinstance(data, dict):
        if "data" in data and isinstance(data["data"], dict):
            if "message" in data["data"] and isinstance(data["data"]["message"], dict):
                conversation = data["data"]["message"].get("conversation")
                if conversation:  # mensagem de texto encontrada
                    message = {
                        "text": {"body": conversation},
                        "key": data["data"].get("key", {})
                    }

    if message:
        text = message["text"]["body"]
        print(f"✅ Mensagem de texto detectada: {text}")

        response = await handle_text_message(message)

        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"{settings.EVOLUTION_API_URL}/message/sendText/{settings.EVOLUTION_INSTANCE}",
                    json={
                        "number": message["key"]["remoteJid"],
                        "text": response["content"]
                    },
                    headers={"apikey": settings.EVOLUTION_API_KEY},
                    timeout=10
                )
                print("✅ Resposta enviada com sucesso para o WhatsApp!")
        except Exception as e:
            print(f"❌ Erro ao enviar resposta: {e}")
    else:
        print("⚠️ Nenhum payload de mensagem de texto encontrado (normal para outros eventos)")

    return {"status": "ok"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=settings.PORT)

#fix: extração correta do texto da Evolution v2.3.7 (data.message.conversation)
