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

def _barra_progresso(pct: float, tamanho: int = 10) -> str:
    cheios = int(min(pct / 100, 1) * tamanho)
    return "█" * cheios + "░" * (tamanho - cheios)


async def gerar_resumo(usuario: str) -> str:
    hoje = date.today()
    ano, mes = hoje.year, hoje.month
    gastos = await buscar_gastos_mes(usuario, ano, mes)
    total = await total_gasto_mes(usuario, ano, mes)
    limite = await buscar_limite(usuario)

    if not gastos:
        return (
            f"📊 *Resumo de {MESES_PT[mes]}*\n\n"
            "Nenhum gasto registrado ainda neste mês.\n"
            "Manda uma mensagem como: _'iFood 45 cartão'_ para começar! 🚀"
        )

    # Agrupa por categoria
    por_cat: dict[str, float] = defaultdict(float)
    for g in gastos:
        por_cat[g["categoria"]] += float(g["valor"])

    categorias_ordenadas = sorted(por_cat.items(), key=lambda x: x[1], reverse=True)

    linhas_cat = []
    for cat, val in categorias_ordenadas:
        emoji = EMOJI_CATEGORIA.get(cat, "📦")
        pct = (val / total * 100) if total > 0 else 0
        linhas_cat.append(f"  {emoji} {cat}: R$ {val:.2f} ({pct:.0f}%)")

    # Bloco de limite
    bloco_limite = ""
    if limite:
        pct_limite = (total / limite) * 100
        barra = _barra_progresso(pct_limite)
        restante = max(0, limite - total)
        status = "🟢" if pct_limite < 75 else "🟡" if pct_limite < 100 else "🔴"
        bloco_limite = (
            f"\n💳 *Limite Mensal*\n"
            f"  {barra} {pct_limite:.0f}%\n"
            f"  {status} R$ {total:.2f} / R$ {limite:.2f}\n"
            f"  💡 Restam R$ {restante:.2f}"
        )

    resumo = (
        f"📊 *Resumo de {MESES_PT[mes]} {ano}*\n\n"
        f"💰 *Total gasto:* R$ {total:.2f}\n"
        f"🧾 *Transações:* {len(gastos)}\n\n"
        f"*Por categoria:*\n"
        + "\n".join(linhas_cat)
        + bloco_limite
        + f"\n\n_Últimos registros:_"
    )

    # Últimos 3 gastos
    for g in gastos[:3]:
        emoji = EMOJI_CATEGORIA.get(g["categoria"], "📦")
        resumo += f"\n  {emoji} {g['descricao']} · R$ {float(g['valor']):.2f}"

    return resumo


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
    """Retorna aviso se o gasto atual ultrapassar 80% ou 100% do limite."""
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
