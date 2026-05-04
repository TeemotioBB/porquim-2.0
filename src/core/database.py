import asyncpg
from datetime import date as _date
from src.core.config import settings

_pool = None

async def get_pool():
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(settings.DATABASE_URL, min_size=2, max_size=10)
        await _create_tables()
    return _pool

async def _create_tables():
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS gastos (
                id SERIAL PRIMARY KEY,
                usuario VARCHAR(50) NOT NULL,
                descricao TEXT NOT NULL,
                valor DECIMAL(10,2) NOT NULL,
                categoria VARCHAR(50) NOT NULL,
                forma_pagamento VARCHAR(30) NOT NULL,
                data DATE NOT NULL,
                hashtag VARCHAR(20),
                fonte VARCHAR(20) DEFAULT 'texto',
                criado_em TIMESTAMP DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS limites (
                usuario VARCHAR(50) PRIMARY KEY,
                limite_mensal DECIMAL(10,2) NOT NULL,
                atualizado_em TIMESTAMP DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS entradas (
                id SERIAL PRIMARY KEY,
                usuario VARCHAR(50) NOT NULL,
                descricao TEXT NOT NULL,
                valor DECIMAL(10,2) NOT NULL,
                categoria VARCHAR(50) NOT NULL,
                data DATE NOT NULL,
                hashtag VARCHAR(20),
                fonte VARCHAR(20) DEFAULT 'texto',
                criado_em TIMESTAMP DEFAULT NOW()
            );

            CREATE INDEX IF NOT EXISTS idx_gastos_usuario ON gastos(usuario);
            CREATE INDEX IF NOT EXISTS idx_gastos_data ON gastos(data);
            CREATE INDEX IF NOT EXISTS idx_entradas_usuario ON entradas(usuario);
            CREATE INDEX IF NOT EXISTS idx_entradas_data ON entradas(data);

            CREATE TABLE IF NOT EXISTS lembretes (
                id SERIAL PRIMARY KEY,
                usuario VARCHAR(50) NOT NULL,
                mensagem TEXT NOT NULL,
                horario TIMESTAMP WITH TIME ZONE NOT NULL,
                enviado BOOLEAN DEFAULT FALSE,
                criado_em TIMESTAMP DEFAULT NOW()
            );

            CREATE INDEX IF NOT EXISTS idx_lembretes_usuario ON lembretes(usuario);
            CREATE INDEX IF NOT EXISTS idx_lembretes_horario ON lembretes(horario);
            CREATE INDEX IF NOT EXISTS idx_lembretes_enviado ON lembretes(enviado);

            -- Tabela de memória persistente (último gasto/entrada por usuário)
            CREATE TABLE IF NOT EXISTS memoria_usuario (
                usuario        VARCHAR(50) PRIMARY KEY,
                ultimo_gasto_id   INTEGER,
                ultima_entrada_id INTEGER,
                lote_gastos_ids   TEXT,  -- IDs separados por vírgula
                intencao_pendente TEXT,  -- ação aguardando confirmação ex: "limite:500"
                atualizado_em  TIMESTAMP DEFAULT NOW()
            );
        """)

def _parse_date(s: str) -> _date:
    """Aceita yyyy-mm-dd ou dd-mm-yyyy."""
    s = s.strip()
    try:
        return _date.fromisoformat(s)
    except ValueError:
        parts = s.split("-")
        if len(parts) == 3 and len(parts[2]) == 4:
            return _date(int(parts[2]), int(parts[1]), int(parts[0]))
        raise

# ── Gastos ──────────────────────────────────────────────

async def salvar_gasto(usuario: str, dados: dict, fonte: str = "texto") -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO gastos (usuario, descricao, valor, categoria, forma_pagamento, data, hashtag, fonte)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
            RETURNING id
        """,
            usuario,
            dados["descricao"],
            float(dados["valor"]),
            dados["categoria"],
            dados["forma_pagamento"],
            _parse_date(dados["data"]) if isinstance(dados["data"], str) else dados["data"],
            dados.get("hashtag", ""),
            fonte
        )
        return row["id"]

async def buscar_gasto_por_id(gasto_id: int, usuario: str) -> dict | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM gastos WHERE id=$1 AND usuario=$2", gasto_id, usuario
        )
        return dict(row) if row else None

async def deletar_gasto(gasto_id: int, usuario: str) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM gastos WHERE id=$1 AND usuario=$2", gasto_id, usuario
        )
        return result == "DELETE 1"

async def atualizar_gasto(gasto_id: int, usuario: str, dados: dict) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute("""
            UPDATE gastos SET
                descricao=$3, valor=$4, categoria=$5, forma_pagamento=$6
            WHERE id=$1 AND usuario=$2
        """, gasto_id, usuario,
            dados["descricao"], float(dados["valor"]),
            dados["categoria"], dados["forma_pagamento"]
        )
        return result == "UPDATE 1"

async def buscar_gastos_mes(usuario: str, ano: int, mes: int) -> list:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT * FROM gastos
            WHERE usuario = $1
              AND EXTRACT(YEAR FROM data) = $2
              AND EXTRACT(MONTH FROM data) = $3
            ORDER BY data DESC, criado_em DESC
        """, usuario, ano, mes)
        return [dict(r) for r in rows]

async def total_gasto_mes(usuario: str, ano: int, mes: int) -> float:
    pool = await get_pool()
    async with pool.acquire() as conn:
        val = await conn.fetchval("""
            SELECT COALESCE(SUM(valor), 0)
            FROM gastos
            WHERE usuario = $1
              AND EXTRACT(YEAR FROM data) = $2
              AND EXTRACT(MONTH FROM data) = $3
        """, usuario, ano, mes)
        return float(val)

# ── Limites ─────────────────────────────────────────────

async def salvar_limite(usuario: str, valor: float):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO limites (usuario, limite_mensal)
            VALUES ($1, $2)
            ON CONFLICT (usuario) DO UPDATE SET limite_mensal=$2, atualizado_em=NOW()
        """, usuario, valor)

async def buscar_limite(usuario: str) -> float | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        val = await conn.fetchval("SELECT limite_mensal FROM limites WHERE usuario=$1", usuario)
        return float(val) if val is not None else None

# ── Entradas ─────────────────────────────────────────────

async def salvar_entrada(usuario: str, dados: dict, fonte: str = "texto") -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO entradas (usuario, descricao, valor, categoria, data, hashtag, fonte)
            VALUES ($1,$2,$3,$4,$5,$6,$7)
            RETURNING id
        """,
            usuario,
            dados["descricao"],
            float(dados["valor"]),
            dados.get("categoria", "Outros"),
            _parse_date(dados["data"]) if isinstance(dados["data"], str) else dados["data"],
            dados.get("hashtag", ""),
            fonte
        )
        return row["id"]

async def buscar_entradas_mes(usuario: str, ano: int, mes: int) -> list:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT * FROM entradas
            WHERE usuario = $1
              AND EXTRACT(YEAR FROM data) = $2
              AND EXTRACT(MONTH FROM data) = $3
            ORDER BY data DESC, criado_em DESC
        """, usuario, ano, mes)
        return [dict(r) for r in rows]

async def total_entrada_mes(usuario: str, ano: int, mes: int) -> float:
    pool = await get_pool()
    async with pool.acquire() as conn:
        val = await conn.fetchval("""
            SELECT COALESCE(SUM(valor), 0)
            FROM entradas
            WHERE usuario = $1
              AND EXTRACT(YEAR FROM data) = $2
              AND EXTRACT(MONTH FROM data) = $3
        """, usuario, ano, mes)
        return float(val)

async def buscar_entrada_por_id(entrada_id: int, usuario: str) -> dict | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM entradas WHERE id=$1 AND usuario=$2", entrada_id, usuario
        )
        return dict(row) if row else None

async def deletar_entrada(entrada_id: int, usuario: str) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM entradas WHERE id=$1 AND usuario=$2", entrada_id, usuario
        )
        return result == "DELETE 1"

# ── Memória persistente ──────────────────────────────────────────────────────

async def salvar_memoria(usuario: str, ultimo_gasto_id: int = None, ultima_entrada_id: int = None, lote_ids: list = None, intencao_pendente: str = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        lote_str = ",".join(str(i) for i in lote_ids) if lote_ids else None
        await conn.execute("""
            INSERT INTO memoria_usuario (usuario, ultimo_gasto_id, ultima_entrada_id, lote_gastos_ids, intencao_pendente, atualizado_em)
            VALUES ($1, $2, $3, $4, $5, NOW())
            ON CONFLICT (usuario) DO UPDATE SET
                ultimo_gasto_id   = COALESCE($2, memoria_usuario.ultimo_gasto_id),
                ultima_entrada_id = COALESCE($3, memoria_usuario.ultima_entrada_id),
                lote_gastos_ids   = COALESCE($4, memoria_usuario.lote_gastos_ids),
                intencao_pendente = COALESCE($5, memoria_usuario.intencao_pendente),
                atualizado_em     = NOW()
        """, usuario, ultimo_gasto_id, ultima_entrada_id, lote_str, intencao_pendente)

async def buscar_memoria(usuario: str) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM memoria_usuario WHERE usuario=$1", usuario)
        if not row:
            return {"ultimo_gasto_id": None, "ultima_entrada_id": None, "lote_gastos_ids": [], "intencao_pendente": None}
        lote = [int(i) for i in row["lote_gastos_ids"].split(",") if i] if row["lote_gastos_ids"] else []
        return {
            "ultimo_gasto_id": row["ultimo_gasto_id"],
            "ultima_entrada_id": row["ultima_entrada_id"],
            "lote_gastos_ids": lote,
            "intencao_pendente": row["intencao_pendente"]
        }

async def limpar_memoria_gasto(usuario: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE memoria_usuario SET ultimo_gasto_id=NULL, lote_gastos_ids=NULL WHERE usuario=$1
        """, usuario)

async def limpar_memoria_entrada(usuario: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE memoria_usuario SET ultima_entrada_id=NULL WHERE usuario=$1
        """, usuario)

async def salvar_intencao_pendente(usuario: str, intencao: str):
    """Salva uma intenção pendente aguardando confirmação do usuário."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO memoria_usuario (usuario, intencao_pendente, atualizado_em)
            VALUES ($1, $2, NOW())
            ON CONFLICT (usuario) DO UPDATE SET
                intencao_pendente = $2,
                atualizado_em = NOW()
        """, usuario, intencao)

async def limpar_intencao_pendente(usuario: str):
    """Remove a intenção pendente após execução ou cancelamento."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE memoria_usuario SET intencao_pendente=NULL WHERE usuario=$1
        """, usuario)
