"""Comprehensive fix: duplicates + misclassification"""
import asyncio
import os
import re
from collections import defaultdict
from dotenv import load_dotenv
load_dotenv()
from motor.motor_asyncio import AsyncIOMotorClient

# Better category detection keywords
CATEGORY_DETECT = {
    "futebol": ["futebol", "campeonato", "brasileirão", "libertadores", "copa do mundo", "seleção brasileira",
                "gol de", "rodada", "liga dos campeões", "escalação", "técnico demitido", "técnico do",
                "flamengo", "palmeiras", "corinthians", "são paulo", "vasco", "botafogo", "cruzeiro",
                "atlético", "grêmio", "inter de", "santos", "bahia", "fortaleza"],
    "criptomoedas": ["bitcoin", "ethereum", "cripto", "blockchain", "altcoin", "token", "btc", "eth",
                     "binance", "coinbase", "nft", "defi", "stablecoin", "mineração de cripto"],
    "investimentos": ["investimento", "renda fixa", "tesouro direto", "fii", "fundos imobiliários",
                      "ações da", "preço-alvo", "dividend", "b3 ", "ibovespa", "nasdaq", "s&p 500",
                      "wall street", "dow jones"],
    "financas": ["imposto de renda", "restituição", "selic", "inflação", "pib", "câmbio", "dólar",
                 "banco central", "copom", "juros", "crédito", "financiamento", "inss", "fgts",
                 "desenrola", "bolsa família"],
    "politica": ["governo", "senado", "câmara dos deputados", "presidente", "ministro", "stf",
                 "congresso", "deputado", "senador", "pf ", "polícia federal", "cpi", "impeachment",
                 "lula", "bolsonaro"],
    "mundo": ["guerra", "conflito", "bombardeio", "terremoto", "tsunami", "embaixada", "otan",
              "onu ", "eua ", "china ", "rússia", "ucrânia", "irã", "israel", "palestin"],
    "saude": ["saúde", "doença", "hospital", "vacina", "câncer", "diabetes", "dengue", "covid",
              "medicamento", "remédio", "cirurgia", "tratamento", "diagnóstico", "sintomas",
              "anvisa", "sus ", "oms ", "epidemia", "pandemia", "emagrecedor", "semaglutida",
              "ozempic", "mounjaro"],
    "famosos": ["bbb", "big brother", "reality", "celebridade", "influencer", "virginia", "zé felipe",
                "anitta", "gshow", "novela", "mesacast", "eliminação", "paredão"],
    "entretenimento": ["filme", "cinema", "netflix", "disney", "série", "temporada", "estreia",
                       "bilheteria", "oscar", "globo de ouro", "streaming", "show", "turnê",
                       "festival", "musical"],
    "games": ["game", "playstation", "xbox", "nintendo", "steam", "pc gamer", "esports",
              "fps", "rpg", "battle royale", "fortnite", "valorant", "league of legends"],
    "anime": ["anime", "mangá", "otaku", "one piece", "dragon ball", "naruto", "jujutsu",
              "demon slayer", "my hero academia", "crunchyroll"],
    "esportes": ["olimpíada", "atletismo", "natação", "basquete", "nba", "nfl", "tênis",
                 "fórmula 1", "f1", "mma", "ufc", "boxe", "vôlei", "surf"],
    "ciencia": ["nasa", "spacex", "astronomia", "cientistas", "pesquisa científica", "estudo publicado",
                "descoberta", "fóssil", "dna", "genoma", "ia ", "inteligência artificial"],
}

# Lottery keywords — should be "economia" or "geral"
LOTTERY_KEYWORDS = ["mega-sena", "lotofácil", "quina", "lotomania", "loteria", "números sorteados",
                    "resultado da mega", "resultado da loto", "resultado da quina", "concurso"]

async def fix_all():
    client = AsyncIOMotorClient(os.environ.get('MONGO_URL'))
    db = client[os.environ.get('DB_NAME', 'test_database')]
    
    print("=" * 60)
    print("CORRECAO COMPLETA")
    print("=" * 60)
    
    # ==========================================
    # 1. REMOVE NEAR-DUPLICATES (keep newest)
    # ==========================================
    print("\n1. REMOVENDO DUPLICATAS SIMILARES...")
    all_news = await db.news.find(
        {}, {"_id": 1, "title": 1, "news_id": 1, "published_at": 1, "ai_summary": 1, "image_url": 1}
    ).sort("published_at", -1).to_list(15000)
    
    prefix_groups = defaultdict(list)
    for n in all_news:
        words = re.sub(r'[^\w\s]', '', n.get("title", "").lower()).split()
        if len(words) >= 6:
            prefix = " ".join(words[:6])
            prefix_groups[prefix].append(n)
    
    deleted = 0
    for prefix, items in prefix_groups.items():
        if len(items) <= 1:
            continue
        # Keep the best one (has AI summary + has image + newest)
        def quality_score(item):
            score = 0
            if item.get("ai_summary"):
                score += 10
            if item.get("image_url"):
                score += 5
            return score
        
        items.sort(key=lambda x: (quality_score(x), x.get("published_at", "")), reverse=True)
        keep = items[0]
        to_delete = items[1:]
        
        for item in to_delete:
            await db.news.delete_one({"_id": item["_id"]})
            deleted += 1
    
    print(f"  Duplicatas removidas: {deleted}")
    
    # ==========================================
    # 2. FIX LOTTERY RESULTS CLASSIFICATION
    # ==========================================
    print("\n2. CORRIGINDO LOTERIA...")
    lottery_fixed = 0
    lottery_cursor = db.news.find(
        {"category": {"$nin": ["economia", "geral"]}},
        {"_id": 1, "title": 1, "summary": 1, "category": 1}
    )
    async for doc in lottery_cursor:
        text = f"{doc.get('title', '')} {doc.get('summary', '')}".lower()
        if any(kw in text for kw in LOTTERY_KEYWORDS):
            await db.news.update_one({"_id": doc["_id"]}, {"$set": {"category": "economia"}})
            lottery_fixed += 1
            if lottery_fixed <= 5:
                print(f"  [{doc['category']} -> economia] {doc['title'][:60]}")
    print(f"  Loteria corrigida: {lottery_fixed}")
    
    # ==========================================
    # 3. FIX ALL MISCLASSIFIED ARTICLES
    # ==========================================
    print("\n3. RECLASSIFICANDO ARTIGOS...")
    reclass_count = 0
    
    # Categories that often have misclassified content
    problem_cats = ["saude", "famosos", "entretenimento", "geral", "mundo"]
    
    for cat in problem_cats:
        cat_news = await db.news.find(
            {"category": cat},
            {"_id": 1, "title": 1, "summary": 1, "source_name": 1}
        ).to_list(3000)
        
        for n in cat_news:
            text = f"{n.get('title', '')} {n.get('summary', '')}".lower()
            
            # Skip if it strongly matches current category
            if cat in CATEGORY_DETECT:
                current_matches = sum(1 for kw in CATEGORY_DETECT[cat] if kw in text)
                if current_matches >= 2:
                    continue
            
            # Find best matching category
            best_cat = None
            best_score = 0
            
            for target_cat, keywords in CATEGORY_DETECT.items():
                if target_cat == cat:
                    continue
                score = sum(1 for kw in keywords if kw in text)
                if score > best_score and score >= 2:
                    best_score = score
                    best_cat = target_cat
            
            if best_cat:
                await db.news.update_one({"_id": n["_id"]}, {"$set": {"category": best_cat}})
                reclass_count += 1
                if reclass_count <= 15:
                    print(f"  [{cat} -> {best_cat}] {n['title'][:60]}")
    
    print(f"  Reclassificados: {reclass_count}")
    
    # ==========================================
    # 4. FINAL COUNTS
    # ==========================================
    print("\n" + "=" * 60)
    total = await db.news.count_documents({})
    print(f"Total apos correcao: {total}")
    
    cats = await db.news.aggregate([
        {"$group": {"_id": "$category", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}}
    ]).to_list(25)
    print("\nCategorias finais:")
    for c in cats:
        print(f"  {c['_id']:15s}: {c['count']:5d}")
    
    # Check remaining near-duplicates
    all_after = await db.news.find({}, {"title": 1}).to_list(15000)
    prefix2 = defaultdict(int)
    for n in all_after:
        words = re.sub(r'[^\w\s]', '', n.get("title", "").lower()).split()
        if len(words) >= 6:
            prefix2[" ".join(words[:6])] += 1
    remaining_dups = sum(1 for v in prefix2.values() if v > 1)
    print(f"\nDuplicatas similares restantes: {remaining_dups}")
    
    print(f"\n=== TOTAL: {deleted} removidas + {lottery_fixed} loteria + {reclass_count} reclassificadas ===")
    
    client.close()

asyncio.run(fix_all())
