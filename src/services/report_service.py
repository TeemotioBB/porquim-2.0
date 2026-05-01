from datetime import date
from collections import defaultdict
from src.core.database import (
    buscar_gastos_mes, total_gasto_mes, buscar_limite, salvar_limite
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
    total = await total_gasto_mes(usuario, ano, mes)
    limite = await buscar_limite(usuario) if (ano == hoje.year and mes == hoje.month) else None

    periodo = f"{MESES_PT[mes]} {ano}"

    if not gastos:
        return (
            f"📊 *Resumo de {periodo}*\n\n"
            f"Nenhum gasto registrado em {periodo}. 🙌",
            []
        )

    por_cat: dict[str, float] = defaultdict(float)
    for g in gastos:
        por_cat[g["categoria"]] += float(g["valor"])

    categorias_ordenadas = sorted(por_cat.items(), key=lambda x: x[1], reverse=True)

    linhas_cat = []
    for cat, val in categorias_ordenadas:
        emoji = EMOJI_CATEGORIA.get(cat, "📦")
        pct = (val / total * 100) if total > 0 else 0
        linhas_cat.append(f"  {emoji} {cat}: R$ {val:.2f} ({pct:.0f}%)")

    bloco_limite = ""
    if limite:
        pct_limite = (total / limite) * 100
        barra = _barra_progresso(pct_limite)
        restante = max(0, limite - total)
        status = "🟢" if pct_limite < 75 else "🟡" if pct_limite < 100 else "🔴"
        bloco_limite = (
            f"\n\n💳 *Limite Mensal*\n"
            f"  {barra} {pct_limite:.0f}%\n"
            f"  {status} R$ {total:.2f} / R$ {limite:.2f}\n"
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

    resumo = (
        f"📊 *Resumo de {periodo}*\n\n"
        f"💰 *Total gasto:* R$ {total:.2f}\n"
        f"🧾 *Transações:* {len(gastos)}\n\n"
        f"*Por categoria:*\n"
        + "\n".join(linhas_cat)
        + bloco_limite
        + f"\n\n*Últimos gastos:*\n"
        + "\n".join(linhas_gastos)
        + "\n\n_Para remover: responda *remover 2* (pelo número)_"
        + "\n_Para editar: responda *editar 2* (pelo número)_"
    )

    return resumo, gastos_exibidos


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


async def verificar_limite_pos_gasto(usuario: str) -> str | None:
    hoje = date.today()
    limite = await buscar_limite(usuario)
    if not limite:
        return None

    total = await total_gasto_mes(usuario, hoje.year, hoje.month)
    pct = (total / limite) * 100

    if pct >= 100:
        return f"\n\n⚠️ *ATENÇÃO:* Você ultrapassou seu limite mensal de R$ {limite:.2f}! (atual: R$ {total:.2f})"
    elif pct >= 80:
        return f"\n\n⚠️ *Alerta:* Você já usou {pct:.0f}% do seu limite mensal (R$ {total:.2f} / R$ {limite:.2f})"
    return None
