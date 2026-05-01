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
            return {"type": "text", "content": f"❌ Número inválido. Escolha entre 1 e {len(gastos)}."}
        g = gastos[idx]
        ok = await deletar_gasto(g["id"], numero)
        if ok:
            _resumo_gastos[numero] = [x for x in gastos if x["id"] != g["id"]]
            return {"type": "text", "content": f"🗑️ *Gasto removido!*\n\n_{g['descricao']} · R$ {float(g['valor']):.2f}_"}
        return {"type": "text", "content": "❌ Não consegui remover. Tente novamente."}

    # ── Remover último gasto: "remover" ───────────────────
    if texto_lower == "remover":
        gasto_id = _ultimo_gasto.get(numero)
        if not gasto_id:
            return {"type": "text", "content": "❌ Nenhum gasto recente para remover.\nUse *resumo* para ver seus gastos e remover pelo número."}
        g = await buscar_gasto_por_id(gasto_id, numero)
        if not g:
            return {"type": "text", "content": "❌ Gasto não encontrado ou já foi removido."}
        ok = await deletar_gasto(gasto_id, numero)
        if ok:
            _ultimo_gasto.pop(numero, None)
            return {"type": "text", "content": f"🗑️ *Gasto removido!*\n\n_{g['descricao']} · R$ {float(g['valor']):.2f}_"}
        return {"type": "text", "content": "❌ Não consegui remover. Tente novamente."}

    # ── Editar pelo número do resumo: "editar 2 Uber 50 cartão" ──
    match_edit_num = re.match(r"^editar\s+(\d+)\s+(.+)$", texto_lower)
    if match_edit_num:
        idx = int(match_edit_num.group(1)) - 1
        novo_texto = match_edit_num.group(2)
        gastos = _resumo_gastos.get(numero, [])
        if not gastos:
            return {"type": "text", "content": "❌ Faça um *resumo* primeiro para ver os gastos numerados."}
        if idx < 0 or idx >= len(gastos):
            return {"type": "text", "content": f"❌ Número inválido. Escolha entre 1 e {len(gastos)}."}
        try:
            novos_dados = await _extrair(novo_texto)
            ok = await atualizar_gasto(gastos[idx]["id"], numero, novos_dados)
            if ok:
                return {"type": "text", "content": f"✏️ *Gasto atualizado!*\n\n📍 {novos_dados['descricao']}\n💰 R$ {float(novos_dados['valor']):.2f}\n🏷️ {novos_dados['categoria']}\n💳 {novos_dados['forma_pagamento']}"}
        except Exception as e:
            print(f"❌ Erro ao editar: {e}")
        return {"type": "text", "content": "❌ Não consegui editar. Tente: *editar 2 Uber 50 cartão*"}

    # ── Editar último gasto: "editar" ou "editar Uber 50 cartão" ──
    match_edit = re.match(r"^editar\s+(.+)$", texto_lower)
    if match_edit or texto_lower == "editar":
        gasto_id = _ultimo_gasto.get(numero)
        if not gasto_id:
            return {"type": "text", "content": "❌ Nenhum gasto recente para editar.\nUse *resumo* e depois *editar 2 Uber 50 cartão*."}
        if texto_lower == "editar":
            g = await buscar_gasto_por_id(gasto_id, numero)
            return {"type": "text", "content": f"✏️ Para editar o último gasto, responda:\n*editar {g['descricao']} [novo valor] [forma pagamento]*\n\nEx: *editar Uber 55 pix*"}
        try:
            novo_texto = match_edit.group(1)
            novos_dados = await _extrair(novo_texto)
            ok = await atualizar_gasto(gasto_id, numero, novos_dados)
            if ok:
                return {"type": "text", "content": f"✏️ *Gasto atualizado!*\n\n📍 {novos_dados['descricao']}\n💰 R$ {float(novos_dados['valor']):.2f}\n🏷️ {novos_dados['categoria']}\n💳 {novos_dados['forma_pagamento']}"}
        except Exception as e:
            print(f"❌ Erro ao editar: {e}")
        return {"type": "text", "content": "❌ Não consegui editar. Tente: *editar Uber 55 pix*"}

    # ── Limite ─────────────────────────────────────────────
    match_lim = re.match(r"^limite\s+([\d.,]+)", texto_lower)
    if match_lim:
        try:
            valor = float(match_lim.group(1).replace(",", "."))
            return {"type": "text", "content": await definir_limite(numero, valor)}
        except ValueError:
            return {"type": "text", "content": "❌ Valor inválido. Ex: _limite 2000_"}

    # ── Registrar gasto ────────────────────────────────────
    try:
        dados = await processar_gasto_texto(texto)
        gasto_id = await salvar_gasto(numero, dados, fonte="texto")
        _ultimo_gasto[numero] = gasto_id

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
