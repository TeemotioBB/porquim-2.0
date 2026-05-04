from datetime import date, timedelta
from collections import defaultdict
from src.core.database import (
    buscar_gastos_mes, total_gasto_mes, buscar_limite, salvar_limite,
    buscar_entradas_mes, total_entrada_mes,
    buscar_gastos_intervalo, buscar_entradas_intervalo,
    buscar_limite_categoria, total_gasto_categoria_mes,
    listar_limites_categoria, salvar_limite_categoria, deletar_limite_categoria,
)

MESES_PT = [
    "", "Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
    "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro"
]

EMOJI_CATEGORIA = {
    "Alimentação": "🍔", "Transporte": "🚗", "Vestuário": "👕",
    "Moradia": "🏠", "Saúde": "💊", "Educação": "📚",
    "Lazer": "🎮", "Outros": "📦"
}

EMOJI_ENTRADA_CATEGORIA = {
    "Salário": "💼", "Freelance": "💻", "Investimento": "📈",
    "Presente": "🎁", "Reembolso": "🔄", "Outros": "📦"
}

NUMEROS = ["1️⃣","2️⃣","3️⃣","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]

def _barra_progresso(pct: float, tamanho: int = 10) -> str:
    cheios = int(min(pct / 100, 1) * tamanho)
    return "█" * cheios + "░" * (tamanho - cheios)


async def gerar_resumo(usuario: str, ano: int = None, mes: int = None) -> tuple[str, list]:
    """Retorna (texto_resumo, lista_de_gastos) para permitir edição/remoção."""
    hoje = date.today()
    ano = ano or hoje.year
    mes = mes or hoje.month

    gastos = await buscar_gastos_mes(usuario, ano, mes)
    total_gastos = await total_gasto_mes(usuario, ano, mes)
    entradas = await buscar_entradas_mes(usuario, ano, mes)
    total_entradas = await total_entrada_mes(usuario, ano, mes)
    limite = await buscar_limite(usuario) if (ano == hoje.year and mes == hoje.month) else None

    periodo = f"{MESES_PT[mes]} {ano}"
    saldo = total_entradas - total_gastos

    if not gastos and not entradas:
        return (
            f"📊 *Resumo de {periodo}*\n\n"
            f"Nenhum registro em {periodo}. 🙌",
            []
        )

    # Categorias de gastos
    por_cat: dict[str, float] = defaultdict(float)
    for g in gastos:
        por_cat[g["categoria"]] += float(g["valor"])

    categorias_ordenadas = sorted(por_cat.items(), key=lambda x: x[1], reverse=True)

    linhas_cat = []
    for cat, val in categorias_ordenadas:
        emoji = EMOJI_CATEGORIA.get(cat, "📦")
        pct = (val / total_gastos * 100) if total_gastos > 0 else 0
        linhas_cat.append(f"  {emoji} {cat}: R$ {val:.2f} ({pct:.0f}%)")

    # Bloco de limite
    bloco_limite = ""
    if limite:
        pct_limite = (total_gastos / limite) * 100
        barra = _barra_progresso(pct_limite)
        restante = max(0, limite - total_gastos)
        status = "🟢" if pct_limite < 75 else "🟡" if pct_limite < 100 else "🔴"
        bloco_limite = (
            f"\n\n💳 *Limite Mensal*\n"
            f"  {barra} {pct_limite:.0f}%\n"
            f"  {status} R$ {total_gastos:.2f} / R$ {limite:.2f}\n"
            f"  💡 Restam R$ {restante:.2f}"
        )

    # Lista numerada dos gastos (máx 10)
    gastos_exibidos = gastos[:10]
    linhas_gastos = []
    for i, g in enumerate(gastos_exibidos):
        emoji = EMOJI_CATEGORIA.get(g["categoria"], "📦")
        data_fmt = g["data"].strftime("%d/%m") if hasattr(g["data"], "strftime") else str(g["data"])[-5:].replace("-", "/")
        num = NUMEROS[i] if i < len(NUMEROS) else f"{i+1}."
        linhas_gastos.append(f"{num} {emoji} {g['descricao']} · R$ {float(g['valor']):.2f} · {data_fmt}")

    # Bloco de entradas (máx 5)
    bloco_entradas = ""
    if entradas:
        linhas_ent = []
        for e in entradas[:5]:
            emoji = EMOJI_ENTRADA_CATEGORIA.get(e["categoria"], "📦")
            data_fmt = e["data"].strftime("%d/%m") if hasattr(e["data"], "strftime") else str(e["data"])[-5:].replace("-", "/")
            linhas_ent.append(f"  {emoji} {e['descricao']} · R$ {float(e['valor']):.2f} · {data_fmt}")
        bloco_entradas = (
            f"\n\n💵 *Entradas:* R$ {total_entradas:.2f}\n"
            + "\n".join(linhas_ent)
        )

    saldo_emoji = "🟢" if saldo >= 0 else "🔴"

    resumo = (
        f"📊 *Resumo de {periodo}*\n\n"
        f"💰 *Total gasto:* R$ {total_gastos:.2f}\n"
        f"🧾 *Transações:* {len(gastos)}"
        + bloco_entradas
        + f"\n\n{saldo_emoji} *Saldo:* R$ {saldo:.2f}\n\n"
        + "*Por categoria (gastos):*\n"
        + "\n".join(linhas_cat)
        + bloco_limite
        + f"\n\n*Últimos gastos:*\n"
        + "\n".join(linhas_gastos)
        + "\n\n_Para remover: responda *remover 2* (pelo número)_"
        + "\n_Para editar: responda *editar 2* (pelo número)_"
    )

    return resumo, gastos_exibidos


async def gerar_resumo_intervalo(usuario: str, data_inicio: date, data_fim: date,
                                 titulo_periodo: str = None) -> tuple[str, list]:
    """Resumo para intervalo arbitrário (hoje, ontem, semana, etc)."""
    gastos = await buscar_gastos_intervalo(usuario, data_inicio, data_fim)
    entradas = await buscar_entradas_intervalo(usuario, data_inicio, data_fim)

    total_gastos = sum(float(g["valor"]) for g in gastos)
    total_entradas = sum(float(e["valor"]) for e in entradas)
    saldo = total_entradas - total_gastos

    if titulo_periodo is None:
        if data_inicio == data_fim:
            titulo_periodo = data_inicio.strftime("%d/%m/%Y")
        else:
            titulo_periodo = f"{data_inicio.strftime('%d/%m')} a {data_fim.strftime('%d/%m/%Y')}"

    if not gastos and not entradas:
        return (
            f"📊 *Resumo · {titulo_periodo}*\n\n"
            f"Nenhum registro neste período. 🙌",
            []
        )

    # Categorias
    por_cat: dict[str, float] = defaultdict(float)
    for g in gastos:
        por_cat[g["categoria"]] += float(g["valor"])
    categorias_ordenadas = sorted(por_cat.items(), key=lambda x: x[1], reverse=True)

    linhas_cat = []
    for cat, val in categorias_ordenadas:
        emoji = EMOJI_CATEGORIA.get(cat, "📦")
        pct = (val / total_gastos * 100) if total_gastos > 0 else 0
        linhas_cat.append(f"  {emoji} {cat}: R$ {val:.2f} ({pct:.0f}%)")

    # Lista de gastos (máx 10)
    gastos_exibidos = gastos[:10]
    linhas_gastos = []
    for i, g in enumerate(gastos_exibidos):
        emoji = EMOJI_CATEGORIA.get(g["categoria"], "📦")
        data_fmt = g["data"].strftime("%d/%m") if hasattr(g["data"], "strftime") else str(g["data"])[-5:].replace("-", "/")
        num = NUMEROS[i] if i < len(NUMEROS) else f"{i+1}."
        linhas_gastos.append(f"{num} {emoji} {g['descricao']} · R$ {float(g['valor']):.2f} · {data_fmt}")

    bloco_entradas = ""
    if entradas:
        linhas_ent = []
        for e in entradas[:5]:
            emoji = EMOJI_ENTRADA_CATEGORIA.get(e["categoria"], "📦")
            data_fmt = e["data"].strftime("%d/%m") if hasattr(e["data"], "strftime") else str(e["data"])[-5:].replace("-", "/")
            linhas_ent.append(f"  {emoji} {e['descricao']} · R$ {float(e['valor']):.2f} · {data_fmt}")
        bloco_entradas = (
            f"\n\n💵 *Entradas:* R$ {total_entradas:.2f}\n"
            + "\n".join(linhas_ent)
        )

    saldo_emoji = "🟢" if saldo >= 0 else "🔴"

    blocos = [
        f"📊 *Resumo · {titulo_periodo}*\n",
        f"💰 *Total gasto:* R$ {total_gastos:.2f}",
        f"🧾 *Transações:* {len(gastos)}{bloco_entradas}",
        f"\n{saldo_emoji} *Saldo:* R$ {saldo:.2f}",
    ]
    if linhas_cat:
        blocos.append("\n*Por categoria (gastos):*\n" + "\n".join(linhas_cat))
    if linhas_gastos:
        blocos.append("\n*Gastos do período:*\n" + "\n".join(linhas_gastos))

    return "\n".join(blocos), gastos_exibidos


async def definir_limite(usuario: str, valor: float) -> str:
    hoje = date.today()
    total = await total_gasto_mes(usuario, hoje.year, hoje.month)
    await salvar_limite(usuario, valor)

    pct = (total / valor * 100) if valor > 0 else 0
    barra = _barra_progresso(pct)
    status = "🟢" if pct < 75 else "🟡" if pct < 100 else "🔴"

    return (
        f"✅ *Limite definido!*\n\n"
        f"💳 Limite mensal: R$ {valor:.2f}\n"
        f"📊 Gasto atual: R$ {total:.2f}\n"
        f"{barra} {pct:.0f}%\n"
        f"{status} {'Dentro do limite! 👏' if pct < 100 else 'Já ultrapassou o limite! ⚠️'}"
    )


async def definir_limite_categoria_msg(usuario: str, categoria: str, valor: float) -> str:
    """Define um limite para uma categoria e retorna mensagem de confirmação."""
    hoje = date.today()
    await salvar_limite_categoria(usuario, categoria, valor)
    total = await total_gasto_categoria_mes(usuario, categoria, hoje.year, hoje.month)
    pct = (total / valor * 100) if valor > 0 else 0
    barra = _barra_progresso(pct)
    status = "🟢" if pct < 75 else "🟡" if pct < 100 else "🔴"
    emoji = EMOJI_CATEGORIA.get(categoria, "📦")
    return (
        f"✅ *Limite de categoria definido!*\n\n"
        f"{emoji} {categoria}: R$ {valor:.2f}/mês\n"
        f"📊 Gasto atual: R$ {total:.2f}\n"
        f"{barra} {pct:.0f}%\n"
        f"{status} {'Dentro do limite! 👏' if pct < 100 else 'Já ultrapassou! ⚠️'}"
    )


async def listar_limites_categoria_formatado(usuario: str) -> str:
    limites = await listar_limites_categoria(usuario)
    if not limites:
        return (
            "📭 Você não tem limites por categoria definidos.\n\n"
            "Para criar:\n"
            "_'Limite roupas 200'_\n"
            "_'Limite alimentação 800'_"
        )
    hoje = date.today()
    linhas = ["🎯 *Seus limites por categoria:*\n"]
    for l in limites:
        cat = l["categoria"]
        val = l["valor"]
        gasto = await total_gasto_categoria_mes(usuario, cat, hoje.year, hoje.month)
        pct = (gasto / val * 100) if val > 0 else 0
        emoji = EMOJI_CATEGORIA.get(cat, "📦")
        status = "🟢" if pct < 75 else "🟡" if pct < 100 else "🔴"
        barra = _barra_progresso(pct)
        linhas.append(
            f"{emoji} {cat}\n"
            f"   {barra} {pct:.0f}%\n"
            f"   {status} R$ {gasto:.2f} / R$ {val:.2f}"
        )
    linhas.append("\n_Para remover: *remover limite [categoria]*_")
    return "\n\n".join(linhas)


async def verificar_limite_pos_gasto(usuario: str, categoria: str = None) -> str | None:
    """
    Verifica se o gasto recém adicionado estourou:
    - o limite mensal geral
    - o limite da categoria (se houver e categoria for passada)
    Retorna a mensagem combinada (ou None se tudo ok).
    """
    hoje = date.today()
    avisos = []

    # Limite geral
    limite = await buscar_limite(usuario)
    if limite:
        total = await total_gasto_mes(usuario, hoje.year, hoje.month)
        pct = (total / limite) * 100
        if pct >= 100:
            avisos.append(
                f"⚠️ *ATENÇÃO:* Você ultrapassou seu limite mensal de R$ {limite:.2f}! (atual: R$ {total:.2f})"
            )
        elif pct >= 80:
            avisos.append(
                f"⚠️ *Alerta:* Você já usou {pct:.0f}% do seu limite mensal (R$ {total:.2f} / R$ {limite:.2f})"
            )

    # Limite por categoria
    if categoria:
        lim_cat = await buscar_limite_categoria(usuario, categoria)
        if lim_cat:
            total_cat = await total_gasto_categoria_mes(usuario, categoria, hoje.year, hoje.month)
            pct_cat = (total_cat / lim_cat) * 100
            emoji = EMOJI_CATEGORIA.get(categoria, "📦")
            if pct_cat >= 100:
                avisos.append(
                    f"⚠️ *{emoji} {categoria}:* limite estourado! (R$ {total_cat:.2f} / R$ {lim_cat:.2f})"
                )
            elif pct_cat >= 80:
                avisos.append(
                    f"⚠️ *{emoji} {categoria}:* {pct_cat:.0f}% do limite (R$ {total_cat:.2f} / R$ {lim_cat:.2f})"
                )

    if not avisos:
        return None
    return "\n\n" + "\n\n".join(avisos)


async def remover_limite_categoria_msg(usuario: str, categoria: str) -> str:
    ok = await deletar_limite_categoria(usuario, categoria)
    if ok:
        emoji = EMOJI_CATEGORIA.get(categoria, "📦")
        return f"🗑️ *Limite removido!*\n\n{emoji} {categoria} — sem limite agora."
    return f"❌ Você não tinha limite definido para *{categoria}*."
