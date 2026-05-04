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

            -- ── Gastos Recorrentes ──────────────────────────────────────────────
            CREATE TABLE IF NOT EXISTS gastos_recorrentes (
                id SERIAL PRIMARY KEY,
                usuario VARCHAR(50) NOT NULL,
                descricao TEXT NOT NULL,
                valor DECIMAL(10,2) NOT NULL,
                categoria VARCHAR(50) NOT NULL,
                forma_pagamento VARCHAR(30) DEFAULT 'Desconhecido',
                dia_mes INTEGER NOT NULL CHECK (dia_mes >= 1 AND dia_mes <= 31),
                ativo BOOLEAN DEFAULT TRUE,
                ultimo_aviso DATE,
                criado_em TIMESTAMP DEFAULT NOW()
            );

            CREATE INDEX IF NOT EXISTS idx_recorrentes_usuario ON gastos_recorrentes(usuario);
            CREATE INDEX IF NOT EXISTS idx_recorrentes_dia ON gastos_recorrentes(dia_mes, ativo);

            -- ── Compras Parceladas ──────────────────────────────────────────────
            CREATE TABLE IF NOT EXISTS parcelas (
                id SERIAL PRIMARY KEY,
                usuario VARCHAR(50) NOT NULL,
                descricao TEXT NOT NULL,
                valor_total DECIMAL(10,2) NOT NULL,
                valor_parcela DECIMAL(10,2) NOT NULL,
                num_parcelas INTEGER NOT NULL CHECK (num_parcelas > 0),
                parcela_atual INTEGER DEFAULT 0,
                categoria VARCHAR(50) NOT NULL,
                forma_pagamento VARCHAR(30) DEFAULT 'Cartão',
                data_compra DATE NOT NULL,
                ativo BOOLEAN DEFAULT TRUE,
                criado_em TIMESTAMP DEFAULT NOW()
            );

            CREATE INDEX IF NOT EXISTS idx_parcelas_usuario ON parcelas(usuario);
            CREATE INDEX IF NOT EXISTS idx_parcelas_ativo ON parcelas(ativo);

            -- ── Limites por Categoria ───────────────────────────────────────────
            CREATE TABLE IF NOT EXISTS limites_categoria (
                usuario VARCHAR(50) NOT NULL,
                categoria VARCHAR(50) NOT NULL,
                valor DECIMAL(10,2) NOT NULL,
                atualizado_em TIMESTAMP DEFAULT NOW(),
                PRIMARY KEY (usuario, categoria)
            );

            -- ── Adiciona coluna parcela_id à tabela gastos (rastreamento) ──────
            ALTER TABLE gastos ADD COLUMN IF NOT EXISTS parcela_id INTEGER;
            CREATE INDEX IF NOT EXISTS idx_gastos_parcela ON gastos(parcela_id);
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

async def salvar_gasto(usuario: str, dados: dict, fonte: str = "texto", parcela_id: int = None) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO gastos (usuario, descricao, valor, categoria, forma_pagamento, data, hashtag, fonte, parcela_id)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
            RETURNING id
        """,
            usuario,
            dados["descricao"],
            float(dados["valor"]),
            dados["categoria"],
            dados["forma_pagamento"],
            _parse_date(dados["data"]) if isinstance(dados["data"], str) else dados["data"],
            dados.get("hashtag", ""),
            fonte,
            parcela_id
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

async def buscar_gastos_intervalo(usuario: str, data_inicio: _date, data_fim: _date) -> list:
    """Busca gastos em um intervalo de datas (inclusivo)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT * FROM gastos
            WHERE usuario = $1 AND data >= $2 AND data <= $3
            ORDER BY data DESC, criado_em DESC
        """, usuario, data_inicio, data_fim)
        return [dict(r) for r in rows]

async def buscar_entradas_intervalo(usuario: str, data_inicio: _date, data_fim: _date) -> list:
    """Busca entradas em um intervalo de datas (inclusivo)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT * FROM entradas
            WHERE usuario = $1 AND data >= $2 AND data <= $3
            ORDER BY data DESC, criado_em DESC
        """, usuario, data_inicio, data_fim)
        return [dict(r) for r in rows]

async def total_gasto_categoria_mes(usuario: str, categoria: str, ano: int, mes: int) -> float:
    """Total gasto numa categoria específica no mês."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        val = await conn.fetchval("""
            SELECT COALESCE(SUM(valor), 0)
            FROM gastos
            WHERE usuario = $1 AND categoria = $2
              AND EXTRACT(YEAR FROM data) = $3
              AND EXTRACT(MONTH FROM data) = $4
        """, usuario, categoria, ano, mes)
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

# ── Limites por Categoria ───────────────────────────────

async def salvar_limite_categoria(usuario: str, categoria: str, valor: float):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO limites_categoria (usuario, categoria, valor)
            VALUES ($1, $2, $3)
            ON CONFLICT (usuario, categoria) DO UPDATE SET valor=$3, atualizado_em=NOW()
        """, usuario, categoria, valor)

async def buscar_limite_categoria(usuario: str, categoria: str) -> float | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        val = await conn.fetchval(
            "SELECT valor FROM limites_categoria WHERE usuario=$1 AND categoria=$2",
            usuario, categoria
        )
        return float(val) if val is not None else None

async def listar_limites_categoria(usuario: str) -> list:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT categoria, valor FROM limites_categoria WHERE usuario=$1 ORDER BY categoria",
            usuario
        )
        return [{"categoria": r["categoria"], "valor": float(r["valor"])} for r in rows]

async def deletar_limite_categoria(usuario: str, categoria: str) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM limites_categoria WHERE usuario=$1 AND categoria=$2",
            usuario, categoria
        )
        return result == "DELETE 1"

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

# ── Gastos Recorrentes ──────────────────────────────────────────────────────

async def salvar_recorrente(usuario: str, descricao: str, valor: float, categoria: str,
                            dia_mes: int, forma_pagamento: str = "Desconhecido") -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO gastos_recorrentes (usuario, descricao, valor, categoria, dia_mes, forma_pagamento)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING id
        """, usuario, descricao, float(valor), categoria, int(dia_mes), forma_pagamento)
        return row["id"]

async def listar_recorrentes(usuario: str, apenas_ativos: bool = True) -> list:
    pool = await get_pool()
    async with pool.acquire() as conn:
        if apenas_ativos:
            rows = await conn.fetch(
                "SELECT * FROM gastos_recorrentes WHERE usuario=$1 AND ativo=TRUE ORDER BY dia_mes ASC",
                usuario
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM gastos_recorrentes WHERE usuario=$1 ORDER BY dia_mes ASC",
                usuario
            )
        return [dict(r) for r in rows]

async def buscar_recorrente(rec_id: int, usuario: str) -> dict | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM gastos_recorrentes WHERE id=$1 AND usuario=$2", rec_id, usuario
        )
        return dict(row) if row else None

async def cancelar_recorrente(rec_id: int, usuario: str) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE gastos_recorrentes SET ativo=FALSE WHERE id=$1 AND usuario=$2",
            rec_id, usuario
        )
        return result == "UPDATE 1"

async def marcar_aviso_recorrente(rec_id: int, data: _date):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE gastos_recorrentes SET ultimo_aviso=$2 WHERE id=$1", rec_id, data
        )

async def buscar_recorrentes_do_dia(dia: int) -> list:
    """Busca todos os recorrentes ativos cujo dia_mes == dia (entre todos usuários)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM gastos_recorrentes WHERE dia_mes=$1 AND ativo=TRUE",
            dia
        )
        return [dict(r) for r in rows]

# ── Compras Parceladas ──────────────────────────────────────────────────────

async def salvar_parcela(usuario: str, descricao: str, valor_total: float, num_parcelas: int,
                         categoria: str, forma_pagamento: str, data_compra: _date) -> dict:
    """Cria uma compra parcelada e retorna o registro completo."""
    valor_parcela = round(valor_total / num_parcelas, 2)
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO parcelas (usuario, descricao, valor_total, valor_parcela, num_parcelas,
                                  categoria, forma_pagamento, data_compra)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING *
        """, usuario, descricao, float(valor_total), valor_parcela, num_parcelas,
            categoria, forma_pagamento, data_compra
        )
        return dict(row)

async def listar_parcelas(usuario: str, apenas_ativas: bool = True) -> list:
    pool = await get_pool()
    async with pool.acquire() as conn:
        if apenas_ativas:
            rows = await conn.fetch(
                "SELECT * FROM parcelas WHERE usuario=$1 AND ativo=TRUE ORDER BY data_compra DESC",
                usuario
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM parcelas WHERE usuario=$1 ORDER BY data_compra DESC",
                usuario
            )
        return [dict(r) for r in rows]

async def buscar_parcela(parcela_id: int, usuario: str) -> dict | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM parcelas WHERE id=$1 AND usuario=$2", parcela_id, usuario
        )
        return dict(row) if row else None

async def cancelar_parcela(parcela_id: int, usuario: str) -> bool:
    """Marca uma compra parcelada como inativa (impede futuros lançamentos)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE parcelas SET ativo=FALSE WHERE id=$1 AND usuario=$2",
            parcela_id, usuario
        )
        return result == "UPDATE 1"

async def incrementar_parcela_atual(parcela_id: int) -> int:
    """Incrementa parcela_atual e retorna o novo valor. Marca como inativo se chegou ao fim."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            UPDATE parcelas
            SET parcela_atual = parcela_atual + 1,
                ativo = (parcela_atual + 1) < num_parcelas
            WHERE id = $1
            RETURNING parcela_atual, num_parcelas, ativo
        """, parcela_id)
        return row["parcela_atual"] if row else 0

async def buscar_parcelas_ativas_todas() -> list:
    """Busca todas parcelas ativas (de todos usuários) — usado pelo background."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM parcelas WHERE ativo=TRUE")
        return [dict(r) for r in rows]
