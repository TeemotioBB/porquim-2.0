import base64
import httpx
from src.services.ia_service import processar_gasto_audio, transcrever_audio, processar_entrada_texto
from src.services.report_service import verificar_limite_pos_gasto
from src.core.database import salvar_gasto, salvar_entrada, salvar_memoria
from src.core.config import settings
import re

CARD_AUDIO = """✅ *Gasto Registrado por Áudio!* 🎤

🗣️ _"{transcricao}"_

📍 {descricao}
💰 R$ {valor:.2f}
🏷️ {categoria}
💳 {forma_pagamento}
📅 {data}
🔖 {hashtag}{alerta}

_Salvo com sucesso!_ 🎉
_Para remover este gasto responda: *remover*_
_Para editar responda: *editar*_"""

CARD_AUDIO_ENTRADA = """✅ *Entrada Registrada por Áudio!* 🎤

🗣️ _"{transcricao}"_

📍 {descricao}
💵 R$ {valor:.2f}
🏷️ {categoria}
📅 {data}
🔖 {hashtag}

_Salvo com sucesso!_ 🎉
_Para remover esta entrada responda: *remover entrada*_"""


def _detectar_entrada_texto(texto: str) -> bool:
    """Detecta se o texto transcrito é uma entrada de dinheiro."""
    t = texto.strip().lower()
    return bool(re.search(
        r"\b(recebi|receber|salário|salario|freelance|renda|ganho|ganhei|"
        r"entrou|pagaram|me pagou|me pagaram|reembolso|investimento|dividendo)\b", t
    ))


async def _baixar_audio_evolution(msg_data: dict) -> tuple[bytes | None, str]:
    mime_type = msg_data.get("message", {}).get("audioMessage", {}).get("mimetype", "audio/ogg")
    url = f"{settings.EVOLUTION_API_URL}/chat/getBase64FromMediaMessage/{settings.EVOLUTION_INSTANCE}"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                url,
                json={"message": {"key": msg_data.get("key", {}), "message": msg_data.get("message", {})}},
                headers={"apikey": settings.EVOLUTION_API_KEY}
            )
            print(f"📥 Evolution getBase64: {resp.status_code}")
            if resp.status_code in [200, 201]:
                data = resp.json()
                b64 = data.get("base64") or data.get("data")
                if b64:
                    if "," in b64:
                        b64 = b64.split(",", 1)[1]
                    return base64.b64decode(b64), mime_type
    except Exception as e:
        print(f"❌ Erro download áudio Evolution: {e}")
    return None, mime_type


async def handle_audio_message(msg_data: dict, remote_jid: str, ultimo_gasto: dict) -> dict:
    numero = remote_jid.split("@")[0]
    audio_bytes, mime_type = await _baixar_audio_evolution(msg_data)

    if not audio_bytes:
        return {"type": "text", "content": "❌ Não consegui baixar o áudio. Tente enviar novamente ou descreva o gasto em texto."}

    try:
        # Transcreve o áudio primeiro
        transcricao = await transcrever_audio(audio_bytes, mime_type)
        print(f"🎤 Transcrição: {transcricao}")

        # Verifica se é entrada de dinheiro
        if _detectar_entrada_texto(transcricao):
            dados = await processar_entrada_texto(transcricao)
            entrada_id = await salvar_entrada(numero, dados, fonte="audio")
            await salvar_memoria(numero, ultima_entrada_id=entrada_id)

            card = CARD_AUDIO_ENTRADA.format(
                transcricao=transcricao,
                descricao=dados["descricao"],
                valor=float(dados["valor"]),
                categoria=dados.get("categoria", "Outros"),
                data=dados["data"],
                hashtag=dados["hashtag"],
            )
            return {"type": "text", "content": card}

        # Caso contrário, processa como gasto
        dados = await processar_gasto_audio(audio_bytes, mime_type)
        dados["transcricao"] = transcricao  # reusa a transcrição já feita
        gasto_id = await salvar_gasto(numero, dados, fonte="audio")
        ultimo_gasto[numero] = gasto_id
        await salvar_memoria(numero, ultimo_gasto_id=gasto_id)

        alerta = await verificar_limite_pos_gasto(numero) or ""

        card = CARD_AUDIO.format(
            transcricao=transcricao,
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
        return {"type": "text", "content": "😅 Não entendi o áudio. Tente falar mais claramente ou envie em texto.\nEx: _'gastei 45 reais no iFood com cartão'_"}
