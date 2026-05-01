from openai import AsyncOpenAI
from src.core.config import settings

client = AsyncOpenAI(
    api_key=settings.GROK_API_KEY,
    base_url="https://api.x.ai/v1"
)

async def processar_gasto_texto(texto: str, numero_usuario: str):
    prompt = f"""
    Você é o Porquim, IA financeira brasileira.
    Analise a mensagem do usuário e extraia em JSON:
    - valor: número (ex: 56.00)
    - descricao: texto limpo
    - categoria: uma das seguintes (escolha a melhor): Alimentação, Transporte, Vestuário, Moradia, Saúde, Educação, Lazer, Outros
    - forma_pagamento: Pix, Cartão, Dinheiro ou Desconhecido
    - data: hoje se não mencionada (formato YYYY-MM-DD)

    Mensagem: {texto}
    Responda APENAS com JSON válido.
    """

    response = await client.chat.completions.create(
        model="grok-4.3",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3
    )
    
    import json
    dados = json.loads(response.choices[0].message.content)
    
    import hashlib
    hashtag = "#" + hashlib.md5(texto.encode()).hexdigest()[:6]
    
    return {
        "valor": dados["valor"],
        "descricao": dados["descricao"],
        "categoria": dados["categoria"],
        "forma_pagamento": dados["forma_pagamento"],
        "data": dados.get("data"),
        "hashtag": hashtag,
        "mensagem_original": texto
    }
