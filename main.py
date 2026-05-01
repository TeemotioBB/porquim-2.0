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
    
    # Debug completo do payload quando for mensagem
    if event == "messages-upsert":
        print(f"Payload completo (messages-upsert): {json.dumps(data, indent=2, ensure_ascii=False)[:1500]}...")

    # 🔥 Nova lógica robusta para Evolution API v2.3.7
    message = None
    if isinstance(data, dict):
        # Caso 1: padrão mais comum da Evolution
        if "data" in data and isinstance(data["data"], dict):
            if "messages" in data["data"] and data["data"]["messages"]:
                message = data["data"]["messages"][0]
        # Caso 2: messages direto na raiz
        elif "messages" in data and data["messages"]:
            message = data["messages"][0]
        # Caso 3: payload direto com key/message
        elif "key" in data and "message" in data:
            message = data

    if message and message.get("messageType") == "text" or message.get("text"):
        print(f"✅ Mensagem de texto detectada! Conteúdo: {message.get('text') or message.get('message', {}).get('conversation', '')}")
        
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
        print("⚠️ Nenhuma mensagem de texto encontrada (normal para presence, chats-update, etc.)")

    return {"status": "ok"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=settings.PORT)

#fix: webhook 100% compatível com Evolution API v2.3.7
