import httpx
import base64
from src.services.ia_service import processar_comprovante_foto
from src.services.report_service import verificar_limite_pos_gasto
from src.core.database import salvar_gasto
from src.core.config import settings

CARD_FOTO = """✅ *Comprovante Lido!* 📷

📍 {descricao}
💰 R$ {valor:.2f}
🏷️ {categoria}
💳 {forma_pagamento}
📅 {data}
🔖 {hashtag}{alerta}

_Salvo com sucesso!_ 🎉"""


async def handle_image_message(message: dict) -> dict:
    """
    message deve conter:
      - image.url ou image.base64
      - image.mimetype
      - key.remoteJid
    """
    numero = message["key"]["remoteJid"].split("@")[0]
    image_info = message.get("image", {})

    image_bytes: bytes | None = None
    mime_type = image_info.get("mimetype", "image/jpeg")

    # Baixa via URL (Evolution)
    url = image_info.get("url") or image_info.get("mediaUrl")
    if url:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    url,
                    headers={"apikey": settings.EVOLUTION_API_KEY}
                )
                if resp.status_code == 200:
                    image_bytes = resp.content
        except Exception as e:
            print(f"❌ Erro ao baixar imagem: {e}")

    # Fallback: base64 direto
    if not image_bytes:
        b64 = image_info.get("base64") or image_info.get("data")
        if b64:
            image_bytes = base64.b64decode(b64)

    if not image_bytes:
        return {
            "type": "text",
            "content": "❌ Não consegui acessar a imagem. Tente enviar novamente."
        }

    try:
        dados = await processar_comprovante_foto(image_bytes, mime_type)
        await salvar_gasto(numero, dados, fonte="foto")

        alerta = await verificar_limite_pos_gasto(numero) or ""

        card = CARD_FOTO.format(
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
        print(f"❌ Erro ao processar imagem: {e}")
        return {
            "type": "text",
            "content": (
                "😅 Não consegui ler o comprovante. Verifique se:\n"
                "• A foto está nítida e bem iluminada\n"
                "• O valor total está visível\n\n"
                "Ou envie o gasto em texto: _'iFood 45 cartão'_"
            )
        }
