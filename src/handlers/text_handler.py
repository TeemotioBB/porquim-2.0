from src.services.ia_service import processar_gasto_texto

async def handle_text_message(message: dict):
    texto = message["text"]["body"]
    numero = message["key"]["remoteJid"].split("@")[0]
    
    if texto.lower() in ["oi", "olá", "start"]:
        return {"type": "text", "content": "👋 Olá! Manda seu gasto que eu registro na hora! Ex: 'Mac Donalds 56'"}
    
    dados = await processar_gasto_texto(texto, numero)
    
    card = f"""✅ *Gasto Registrado!*
    
📍 {dados['descricao']}
💰 R$ {dados['valor']:.2f}
🏷️ {dados['categoria']}
💳 {dados['forma_pagamento']}
🔖 {dados['hashtag']}

Salvo com sucesso! 🎉"""
    
    return {"type": "text", "content": card}
