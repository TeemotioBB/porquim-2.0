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

    # Debug do payload completo (só quando for mensagem)
    if event == "messages-upsert":
        print(f"Payload messages-upsert: {json.dumps(data, indent=2, ensure_ascii=False)[:1200]}...")

    # 🔥 LÓGICA ROBUSTA para Evolution API v2.3.7
    message = None
    if isinstance(data, dict):
        # Caso mais comum
        if "data" in data and isinstance(data["data"], dict):
            if "messages" in data["data"] and data["data"]["messages"]:
                message = data["data"]["messages"][0]
            elif "message" in data["data"]:
                message = data["data"]
        # Caso direto
        elif "messages" in data and data["messages"]:
            message = data["messages"][0]
        elif "message" in data:
            message = data

    if message:
        # Extrai o texto de várias formas possíveis
        text_body = None
        if isinstance(message.get("text"), dict):
            text_body = message["text"].get("body")
        elif isinstance(message.get("message"), dict):
            text_body = message["message"].get("conversation")
        elif message.get("text"):
            text_body = message.get("text")

        if text_body:
            print(f"✅ Mensagem de texto detectada: {text_body}")

            # Chama o handler com o formato que ele espera
            response = await handle_text_message({
                "text": {"body": text_body},
                "key": message.get("key") or {"remoteJid": message.get("remoteJid")}
            })

            # Envia resposta de volta
            try:
                async with httpx.AsyncClient() as client:
                    await client.post(
                        f"{settings.EVOLUTION_API_URL}/message/sendText/{settings.EVOLUTION_INSTANCE}",
                        json={
                            "number": message.get("key", {}).get("remoteJid") or message.get("remoteJid"),
                            "text": response["content"]
                        },
                        headers={"apikey": settings.EVOLUTION_API_KEY},
                        timeout=10
                    )
                    print("✅ Resposta enviada com sucesso para o WhatsApp!")
            except Exception as e:
                print(f"❌ Erro ao enviar resposta: {e}")
        else:
            print("⚠️ Mensagem recebida, mas não é de texto (sticker, imagem, etc.)")
    else:
        print("⚠️ Nenhum payload de mensagem encontrado")

    return {"status": "ok"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=settings.PORT)

#fix: webhook 100% compatível com Evolution v2.3.7 + tratamento de None
