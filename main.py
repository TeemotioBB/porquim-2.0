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
    
    print(f"\n📥 [WEBHOOK] Evento recebido: {any or 'webhook'}")
    print(f"Payload completo: {json.dumps(data, indent=2, ensure_ascii=False)[:1000]}...")  # debug
    
    # Tenta encontrar a mensagem de várias formas que a Evolution envia
    message = None
    if isinstance(data, dict):
        if "data" in data and isinstance(data["data"], dict) and "messages" in data["data"]:
            message = data["data"]["messages"][0] if data["data"]["messages"] else None
        elif "messages" in data:
            message = data["messages"][0] if data["messages"] else None
    
    if message:
        print(f"✅ Mensagem detectada! Tipo: {message.get('messageType') or 'texto'}")
        response = await handle_text_message(message)
        
        # Envia resposta de volta pro WhatsApp
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
        print("⚠️ Nenhuma mensagem de texto encontrada no payload")

    return {"status": "ok"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=settings.PORT)


#fix: webhook robusto + debug completo para Evolution API
