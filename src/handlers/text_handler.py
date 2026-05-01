import re
from datetime import date, timedelta
from src.services.ia_service import processar_gasto_texto
from src.services.report_service import gerar_resumo, definir_limite, verificar_limite_pos_gasto
from src.core.database import salvar_gasto

AJUDA = """👋 *Olá! Sou o Porquim* 🐷
_Seu assistente financeiro no WhatsApp!_

━━━━━━━━━━━━━━━━━━
📝 *REGISTRAR GASTO*
━━━━━━━━━━━━━━━━━━
Manda qualquer gasto no texto:
• _"iFood 45 cartão"_
• _"Uber 23,50 pix"_
• _"Farmácia 89 dinheiro"_
• _"Aluguel 1200"_

🎤 *Áudio:* Fala o gasto!
_"Gastei 50 reais no mercado com cartão"_

📷 *Foto:* Manda foto do comprovante!
_O Porquim lê e registra automático_

━━━━━━━━━━━━━━━━━━
📊 *VER RELATÓRIOS*
━━━━━━━━━━━━━━━━━━
• *resumo* → mês atual
• *resumo mês passado* → mês anterior
• *resumo janeiro* → mês específico
• *resumo janeiro 2025* → mês e ano

━━━━━━━━━━━━━━━━━━
💳 *LIMITE MENSAL*
━━━━━━━━━━━━━━━━━━
• *limite 2000* → define seu limite
_Te aviso quando passar de 80% e 100%!_

━━━━━━━━━━━━━━━━━━
❓ *AJUDA*
━━━━━━━━━━━━━━━━━━
• *ajuda* ou *menu* → mostra este guia

Bora controlar as finanças? 🚀"""

CARD_GASTO = """✅ *Gasto Registrado!*

📍 {descricao}
💰 R$ {valor:.2f}
🏷️ {categoria}
💳 {forma_pagamento}
📅 {data}
🔖 {hashtag}{alerta}

_Salvo com sucesso!_ 🎉"""

MESES_NOMES = {
    "janeiro": 1, "fevereiro": 2, "março": 3, "marco": 3,
    "abril": 4, "maio": 5, "junho": 6, "julho": 7,
    "agosto": 8, "setembro": 9, "outubro": 10,
    "novembro": 11, "dezembro": 12
}


def _parse_mes_resumo(texto: str):
    """
    Interpreta o comando de resumo e retorna (ano, mes).
    Exemplos:
      "resumo"              → mês atual
      "resumo mês passado"  → mês anterior
      "resumo janeiro"      → janeiro deste ano
      "resumo janeiro 2025" → janeiro de 2025
    """
    hoje = date.today()
    texto_lower = texto.lower().strip()

    # Remove a palavra "resumo" do início
    resto = re.sub(r"^resumo\s*", "", texto_lower).strip()

    if not resto:
        return hoje.year, hoje.month

    # "mês passado" ou "mes passado"
    if re.search(r"m[eê]s\s+passado", resto):
        primeiro_do_mes = hoje.replace(day=1)
        mes_passado = primeiro_do_mes - timedelta(days=1)
        return mes_passado.year, mes_passado.month

    # "ano passado"
    if "ano passado" in resto:
        return hoje.year - 1, hoje.month

    # Tenta achar nome do mês
    for nome, num in MESES_NOMES.items():
        if nome in resto:
            # Tenta achar ano junto (ex: "janeiro 2025")
            ano_match = re.search(r"\b(20\d{2})\b", resto)
            ano = int(ano_match.group(1)) if ano_match else hoje.year
            return ano, num

    # Tenta só número do mês (ex: "resumo 3" ou "resumo 03")
    num_match = re.match(r"^(\d{1,2})$", resto)
    if num_match:
        mes = int(num_match.group(1))
        if 1 <= mes <= 12:
            return hoje.year, mes

    # Não reconheceu, retorna mês atual
    return hoje.year, hoje.month


async def handle_text_message(message: dict) -> dict:
    texto = message["text"]["body"].strip()
    numero = message["key"]["remoteJid"].split("@")[0]

    texto_lower = texto.lower()

    # ── Saudação / Ajuda ──────────────────────────────────
    if texto_lower in ["oi", "olá", "ola", "start", "ajuda", "help", "menu", "inicio", "início"]:
        return {"type": "text", "content": AJUDA}

    # ── Resumo (com parsing de mês) ────────────────────────
    if texto_lower.startswith("resumo") or texto_lower in ["relatorio", "relatório", "gastos", "ver gastos"]:
        ano, mes = _parse_mes_resumo(texto_lower)
        conteudo = await gerar_resumo(numero, ano=ano, mes=mes)
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
