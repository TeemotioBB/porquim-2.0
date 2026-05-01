import httpx
from src.services.ia_service import processar_gasto_audio
from src.services.report_service import verificar_limite_pos_gasto
from src.core.database import salvar_gasto
from src.core.config import settings

CARD_AUDIO = """✅ *Gasto Registrado por Áudio!* 🎤

🗣️ _"{transcricao}"_

📍 {descricao}
💰 R$ {valor:.2f}
🏷️ {categoria}
💳 {forma_pagamento}
📅 {data}
🔖 {hashtag}{alerta}

_Salvo com sucesso!_ 🎉"""


async def handle_audio_message(message: dict) -> dict:
    """
    message deve conter:
      - audio.url ou audio.base64
      - audio.mimetype (ex: audio/ogg; codecs=opus)
      - key.remoteJid
    """
    numero = message["key"]["remoteJid"].split("@")[0]
    audio_info = message.get("audio", {})

    audio_bytes: bytes | None = None
    mime_type = audio_info.get("mimetype", "audio/ogg")

    # Tenta baixar via URL (Evolution passa mediaUrl)
    url = audio_info.get("url") or audio_info.get("mediaUrl")
    if url:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    url,
                    headers={"apikey": settings.EVOLUTION_API_KEY}
                )
                if resp.status_code == 200:
                    audio_bytes = resp.content
        except Exception as e:
            print(f"❌ Erro ao baixar áudio: {e}")

    # Fallback: base64 direto
    if not audio_bytes:
        b64 = audio_info.get("base64") or audio_info.get("data")
        if b64:
            import base64
            audio_bytes = base64.b64decode(b64)

    if not audio_bytes:
        return {
            "type": "text",
            "content": "❌ Não consegui processar o áudio. Tente enviar novamente ou descreva o gasto em texto."
        }

    try:
        dados = await processar_gasto_audio(audio_bytes, mime_type)
        await salvar_gasto(numero, dados, fonte="audio")

        alerta = await verificar_limite_pos_gasto(numero) or ""

        card = CARD_AUDIO.format(
            transcricao=dados.get("transcricao", ""),
            descricao=dados["descricao"],
            valor=float(dados["valor"]),
            categoria=dados["categoria"],
            forma_pagamento=dados["forma_pagamento"],
            data=dados["data"],
            hashtag=dados["hashtag"],
            alerta=alerta
        )
        return {"type": "text", "content": card}

    except Exception as e:
        print(f"❌ Erro ao processar áudio: {e}")
        return {
            "type": "text",
            "content": "😅 Não entendi o áudio. Tente falar mais claramente ou envie em texto.\nEx: _'gastei 45 reais no iFood com cartão'_"
        }
