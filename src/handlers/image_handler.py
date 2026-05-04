import base64
import httpx
from src.services.ia_service import (
    processar_comprovante_foto,
    classificar_foto_comprovante,
    processar_recebimento_foto,
)
from src.services.report_service import verificar_limite_pos_gasto
from src.core.database import salvar_gasto, salvar_entrada, salvar_memoria
from src.core.config import settings

CARD_FOTO = """✅ *Comprovante Lido!* 📷

📍 {descricao}
💰 R$ {valor:.2f}
🏷️ {categoria}
💳 {forma_pagamento}
📅 {data}
🔖 {hashtag}{alerta}

_Salvo com sucesso!_ 🎉
_Para remover este gasto responda: *remover*_
_Para editar responda: *editar*_"""

CARD_FOTO_ENTRADA = """✅ *Recebimento Registrado!* 📷

📍 {descricao}
💵 R$ {valor:.2f}
🏷️ {categoria}
📅 {data}
🔖 {hashtag}

_Salvo com sucesso!_ 🎉
_Para remover esta entrada responda: *remover entrada*_"""


async def _baixar_imagem_evolution(msg_data: dict) -> tuple[bytes | None, str]:
    mime_type = msg_data.get("message", {}).get("imageMessage", {}).get("mimetype", "image/jpeg")
    url = f"{settings.EVOLUTION_API_URL}/chat/getBase64FromMediaMessage/{settings.EVOLUTION_INSTANCE}"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                url,
                json={"message": {"key": msg_data.get("key", {}), "message": msg_data.get("message", {})}},
                headers={"apikey": settings.EVOLUTION_API_KEY}
            )
            print(f"📥 Evolution getBase64 imagem: {resp.status_code}")
            if resp.status_code in [200, 201]:
                data = resp.json()
                b64 = data.get("base64") or data.get("data")
                if b64:
                    if "," in b64:
                        b64 = b64.split(",", 1)[1]
                    return base64.b64decode(b64), mime_type
    except Exception as e:
        print(f"❌ Erro download imagem Evolution: {e}")
    return None, mime_type


async def handle_image_message(msg_data: dict, remote_jid: str, ultimo_gasto: dict) -> dict:
    numero = remote_jid.split("@")[0]
    image_bytes, mime_type = await _baixar_imagem_evolution(msg_data)

    if not image_bytes:
        return {"type": "text", "content": "❌ Não consegui acessar a imagem. Tente enviar novamente."}

    try:
        # Classifica se é gasto ou entrada antes de processar
        tipo = await classificar_foto_comprovante(image_bytes, mime_type)

        if tipo == "ENTRADA":
            # Comprovante de recebimento
            dados = await processar_recebimento_foto(image_bytes, mime_type)
            entrada_id = await salvar_entrada(numero, dados, fonte="foto")
            await salvar_memoria(numero, ultima_entrada_id=entrada_id)

            card = CARD_FOTO_ENTRADA.format(
                descricao=dados["descricao"],
                valor=float(dados["valor"]),
                categoria=dados.get("categoria", "Outros"),
                data=dados["data"],
                hashtag=dados["hashtag"],
            )
            return {"type": "text", "content": card}

        else:
            # Comprovante de pagamento (gasto)
            dados = await processar_comprovante_foto(image_bytes, mime_type)
            gasto_id = await salvar_gasto(numero, dados, fonte="foto")
            ultimo_gasto[numero] = gasto_id
            await salvar_memoria(numero, ultimo_gasto_id=gasto_id)

            # Passa categoria pra checar limite por categoria também
            alerta = await verificar_limite_pos_gasto(numero, dados.get("categoria")) or ""

            card = CARD_FOTO.format(
                descricao=dados["descricao"],
                valor=float(dados["valor"]),
                categoria=dados["categoria"],
                forma_pagamento=dados["forma_pagamento"],
                data=dados["data"],
                hashtag=dados["hashtag"],
                alerta=alerta,
            )
            return {"type": "text", "content": card}

    except Exception as e:
        print(f"❌ Erro ao processar imagem: {e}")
        return {"type": "text", "content": "😅 Não consegui ler o comprovante. Verifique se:\n• A foto está nítida e bem iluminada\n• O valor total está visível\n\nOu envie o gasto em texto: _'iFood 45 cartão'_"}
