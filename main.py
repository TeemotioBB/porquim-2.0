from fastapi import FastAPI, Request
from src.core.config import settings
from src.handlers.text_handler import handle_text_message
import httpx
import uvicorn

app = FastAPI(title="Porquim 2.0")

@app.post("/webhook")
async def evolution_webhook(request: Request):
    data = await request.json()
    
    if "messages" in data and data["messages"]:
        msg = data["messages"][0]
        
        if msg.get("messageType") == "text" or msg.get("text"):
            response = await handle_text_message(msg)
            
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"{settings.EVOLUTION_API_URL}/message/sendText/{settings.EVOLUTION_INSTANCE}",
                    json={
                        "number": msg["key"]["remoteJid"],
                        "text": response["content"]
                    },
                    headers={"apikey": settings.EVOLUTION_API_KEY}
                )
    
    return {"status": "ok"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=settings.PORT)
