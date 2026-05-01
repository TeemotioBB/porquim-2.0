import re
from src.services.ia_service import processar_gasto_texto
from src.services.report_service import gerar_resumo, definir_limite, verificar_limite_pos_gasto
from src.core.database import salvar_gasto

AJUDA = """👋 *Olá! Sou o Porquim* 🐷

Registro seus gastos pelo WhatsApp. Veja o que posso fazer:

*📝 Registrar gasto:*
_"iFood 45 cartão"_
_"Uber 23,50 pix"_
_"Farmácia 89 dinheiro"_

*📊 Ver resumo do mês:*
_"resumo"_

*💳 Definir limite mensal:*
_"limite 2000"_

*🎤 Áudio:* Manda um áudio falando o gasto!
*📷 Foto:* Manda foto do comprovante!

Bora controlar as finanças? 🚀"""

CARD_GASTO = """✅ *Gasto Registrado!*

📍 {descricao}
💰 R$ {valor:.2f}
🏷️ {categoria}
💳 {forma_pagamento}
📅 {data}
🔖 {hashtag}{alerta}

_Salvo com sucesso!_ 🎉"""


async def handle_text_message(message: dict) -> dict:
    texto = message["text"]["body"].strip()
    numero = message["key"]["remoteJid"].split("@")[0]

    texto_lower = texto.lower()

    # ── Saudação / Ajuda ──────────────────────────────────
    if texto_lower in ["oi", "olá", "ola", "start", "ajuda", "help", "menu", "inicio", "início"]:
        return {"type": "text", "content": AJUDA}

    # ── Resumo mensal ──────────────────────────────────────
    if texto_lower in ["resumo", "relatorio", "relatório", "gastos", "ver gastos"]:
        conteudo = await gerar_resumo(numero)
        return {"type": "text", "content": conteudo}

    # ── Definir limite ─────────────────────────────────────
    match = re.match(r"^limite\s+([\d.,]+)", texto_lower)
    if match:
        valor_str = match.group(1).replace(",", ".")
        try:
            valor = float(valor_str)
            conteudo = await definir_limite(numero, valor)
            return {"type": "text", "content": conteudo}
        except ValueError:
            return {"type": "text", "content": "❌ Valor inválido. Ex: _limite 2000_"}

    # ── Registrar gasto (texto livre) ──────────────────────
    try:
        dados = await processar_gasto_texto(texto)
        await salvar_gasto(numero, dados, fonte="texto")

        alerta = await verificar_limite_pos_gasto(numero) or ""

        card = CARD_GASTO.format(
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
        print(f"❌ Erro ao processar texto: {e}")
        return {
            "type": "text",
            "content": (
                "😅 Não entendi esse gasto. Tente algo como:\n"
                "_'iFood 45 cartão'_ ou _'Gasolina 120 pix'_\n\n"
                "Digite *ajuda* para ver todos os comandos."
            )
        }
