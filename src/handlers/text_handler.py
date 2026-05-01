import re
from datetime import date, timedelta
from src.services.ia_service import processar_gasto_texto
from src.services.report_service import gerar_resumo, definir_limite, verificar_limite_pos_gasto
from src.core.database import salvar_gasto, deletar_gasto, atualizar_gasto, buscar_gasto_por_id
from src.services.ia_service import processar_gasto_texto as _extrair

AJUDA = """👋 *Olá! Sou o Porquim* 🐷
_Seu assistente financeiro no WhatsApp!_

━━━━━━━━━━━━━━━━━━
📝 *REGISTRAR GASTO*
━━━━━━━━━━━━━━━━━━
Manda qualquer gasto no texto:
- _"iFood 45 cartão"_
- _"Uber 23,50 pix"_
- _"Farmácia 89 dinheiro"_
- _"Aluguel 1200"_

🎤 *Áudio:* Fala o gasto!
_"Gastei 50 reais no mercado com cartão"_

📷 *Foto:* Manda foto do comprovante!
_O Porquim lê e registra automático_

━━━━━━━━━━━━━━━━━━
📊 *VER RELATÓRIOS*
━━━━━━━━━━━━━━━━━━
- *resumo* → mês atual
- *resumo mês passado* → mês anterior
- *resumo janeiro* → mês específico
- *resumo janeiro 2025* → mês e ano

━━━━━━━━━━━━━━━━━━
✏️ *EDITAR / REMOVER*
━━━━━━━━━━━━━━━━━━
Após registrar um gasto:
- *remover* → remove o último gasto
- *editar* → edita o último gasto

No resumo, pelo número:
- *remover 2* → remove o gasto 2️⃣
- *editar 2 Uber 50 cartão* → edita o gasto 2️⃣

━━━━━━━━━━━━━━━━━━
💳 *LIMITE MENSAL*
━━━━━━━━━━━━━━━━━━
- *limite 2000* → define seu limite
_Te aviso quando passar de 80% e 100%!_

━━━━━━━━━━━━━━━━━━
- *ajuda* ou *menu* → mostra este guia
━━━━━━━━━━━━━━━━━━
Bora controlar as finanças? 🚀"""

CARD_GASTO = """✅ *Gasto Registrado!*

📍 {descricao}
💰 R$ {valor:.2f}
🏷️ {categoria}
💳 {forma_pagamento}
📅 {data}
🔖 {hashtag}{alerta}

_Salvo com sucesso!_ 🎉
_Para remover este gasto responda: *remover*_
_Para editar responda: *editar*_"""

MESES_NOMES = {
    "janeiro": 1, "fevereiro": 2, "março": 3, "marco": 3,
    "abril": 4, "maio": 5, "junho": 6, "julho": 7,
    "agosto": 8, "setembro": 9, "outubro": 10,
    "novembro": 11, "dezembro": 12
}

# Memória em RAM: último gasto por usuário e lista do resumo
_ultimo_gasto: dict[str, int] = {}       # usuario -> gasto_id
_resumo_gastos: dict[str, list] = {}     # usuario -> lista de gastos do último resumo


def _parse_mes_resumo(texto: str):
    hoje = date.today()
    resto = re.sub(r"^resumo\s*", "", texto.lower().strip()).strip()

    if not resto:
        return hoje.year, hoje.month
    if re.search(r"m[eê]s\s+passado", resto):
        primeiro = hoje.replace(day=1)
        anterior = primeiro - timedelta(days=1)
        return anterior.year, anterior.month
    if "ano passado" in resto:
        return hoje.year - 1, hoje.month
    for nome, num in MESES_NOMES.items():
        if nome in resto:
            ano_match = re.search(r"\b(20\d{2})\b", resto)
            ano = int(ano_match.group(1)) if ano_match else hoje.year
            return ano, num
    num_match = re.match(r"^(\d{1,2})$", resto)
    if num_match:
        mes = int(num_match.group(1))
        if 1 <= mes <= 12:
            return hoje.year, mes
    return hoje.year, hoje.month


async def handle_text_message(message: dict) -> dict:
    texto = message["text"]["body"].strip()
    numero = message["key"]["remoteJid"].split("@")[0]
    texto_lower = texto.lower()

    # ── Ajuda ─────────────────────────────────────────────
    if texto_lower in ["oi", "olá", "ola", "start", "ajuda", "help", "menu", "inicio", "início"]:
        return {"type": "text", "content": AJUDA}

    # ── Resumo ─────────────────────────────────────────────
    if texto_lower.startswith("resumo") or texto_lower in ["relatorio", "relatório", "gastos", "ver gastos"]:
        ano, mes = _parse_mes_resumo(texto_lower)
        conteudo, gastos = await gerar_resumo(numero, ano=ano, mes=mes)
        _resumo_gastos[numero] = gastos
        return {"type": "text", "content": conteudo}

    # ── Remover pelo número do resumo: "remover 2" ─────────
    match_rem_num = re.match(r"^remover\s+(\d+)$", texto_lower)
    if match_rem_num:
        idx = int(match_rem_num.group(1)) - 1
        gastos = _resumo_gastos.get(numero, [])
        if not gastos:
            return {"type": "text", "content": "❌ Faça um *resumo* primeiro para ver os gastos numerados."}
        if idx < 0 or idx >= len(gastos):
