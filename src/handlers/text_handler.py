import re
from datetime import date
from src.services.ia_service import processar_gasto_texto
from src.services.report_service import gerar_resumo, definir_limite, verificar_limite_pos_gasto
from src.core.database import salvar_gasto, deletar_gasto, atualizar_gasto, buscar_gasto_por_id

# Memória simples para editar/remover
ULTIMO_RESUMO = {}   # numero -> lista de gastos do último resumo
ULTIMO_GASTO = {}    # numero -> id do último gasto salvo

AJUDA = """👋 *Olá! Sou o Porquim* 🐷

📝 *Como registrar*
• iFood 45 cartão
• Uber 23,50 pix
• Farmácia 89 dinheiro

🎤 Áudio ou 📷 foto também funcionam!

📊 *resumo*
✏️ *editar 2 Uber 55 pix* (depois do resumo)
🗑️ *remover 2*

Digite *ajuda* para ver tudo."""

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
    texto_lower = texto.lower().strip()

    # ── Comandos especiais (prioridade) ─────────────────────
    if texto_lower in ["oi", "olá", "ola", "start", "ajuda", "help", "menu"]:
        return {"type": "text", "content": AJUDA}

    if texto_lower.startswith("resumo") or texto_lower in ["relatorio", "relatório", "gastos"]:
        conteudo, gastos = await gerar_resumo(numero)
        ULTIMO_RESUMO[numero] = gastos
        return {"type": "text", "content": conteudo}

    # ── Remover ─────────────────────────────────────────────
    if texto_lower.startswith("remover "):
        match = re.match(r"^remover\s+(\d+)$", texto_lower)
        if match and numero in ULTIMO_RESUMO:
            idx = int(match.group(1)) - 1
            gastos = ULTIMO_RESUMO[numero]
            if 0 <= idx < len(gastos):
                g = gastos[idx]
                await deletar_gasto(g["id"], numero)
                return {"type": "text", "content": f"🗑️ Gasto #{match.group(1)} removido!"}
        return {"type": "text", "content": "❌ Use *remover N* após o resumo."}

    if texto_lower == "remover":
        gasto_id = ULTIMO_GASTO.get(numero)
        if gasto_id:
            await deletar_gasto(gasto_id, numero)
            ULTIMO_GASTO.pop(numero, None)
            return {"type": "text", "content": "🗑️ Último gasto removido!"}
        return {"type": "text", "content": "❌ Nenhum gasto recente para remover."}

    # ── Editar ──────────────────────────────────────────────
    if texto_lower.startswith("editar "):
        match = re.match(r"^editar\s+(\d+)\s+(.+)$", texto_lower)
        if match and numero in ULTIMO_RESUMO:
            idx = int(match.group(1)) - 1
            novo_texto = match.group(2)
            gastos = ULTIMO_RESUMO[numero]
            if 0 <= idx < len(gastos):
                g = gastos[idx]
                try:
                    novos_dados = await processar_gasto_texto(novo_texto)
                    await atualizar_gasto(g["id"], numero, novos_dados)
                    return {"type": "text", "content": f"✏️ Gasto #{match.group(1)} editado!"}
                except:
                    pass
        return {"type": "text", "content": "❌ Use *editar N novo texto* após o resumo."}

    # ── Limite ──────────────────────────────────────────────
    if texto_lower.startswith("limite "):
        match = re.match(r"^limite\s+([\d.,]+)", texto_lower)
        if match:
            valor = float(match.group(1).replace(",", "."))
            return {"type": "text", "content": await definir_limite(numero, valor)}

    # ── Registrar gasto normal (sempre tenta salvar) ────────
    try:
        dados = await processar_gasto_texto(texto)
        gasto_id = await salvar_gasto(numero, dados, fonte="texto")
        ULTIMO_GASTO[numero] = gasto_id

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
        print(f"❌ Erro ao salvar gasto: {e}")
        return {"type": "text", "content": "😅 Não entendi esse gasto.\nTente: _iFood 45 cartão_ ou digite *ajuda*"}
