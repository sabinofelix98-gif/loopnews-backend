"""Deep audit: find ALL misclassified news and duplicates"""
import asyncio
import os
import re
from collections import defaultdict
from dotenv import load_dotenv
load_dotenv()
from motor.motor_asyncio import AsyncIOMotorClient

async def deep_audit():
    client = AsyncIOMotorClient(os.environ.get('MONGO_URL'))
    db = client[os.environ.get('DB_NAME', 'test_database')]
    
    # 1. SAUDE - show all titles to find misclassified
    print("=" * 60)
    print("SAUDE - Ultimas 30 noticias:")
    print("=" * 60)
    saude = await db.news.find({"category": "saude"}, {"title": 1, "source_name": 1}).sort("published_at", -1).limit(30).to_list(30)
    for n in saude:
        print(f'  [{n.get("source_name","?"):15s}] {n["title"][:70]}')
    
    print("\n" + "=" * 60)
    print("ENTRETENIMENTO - Ultimas 30 noticias:")
    print("=" * 60)
    ent = await db.news.find({"category": "entretenimento"}, {"title": 1, "source_name": 1}).sort("published_at", -1).limit(30).to_list(30)
    for n in ent:
        print(f'  [{n.get("source_name","?"):15s}] {n["title"][:70]}')
    
    print("\n" + "=" * 60)
    print("FAMOSOS - Ultimas 30 noticias:")
    print("=" * 60)
    fam = await db.news.find({"category": "famosos"}, {"title": 1, "source_name": 1}).sort("published_at", -1).limit(30).to_list(30)
    for n in fam:
        print(f'  [{n.get("source_name","?"):15s}] {n["title"][:70]}')
    
    # 2. Find near-duplicates (first 8 words match)
    print("\n" + "=" * 60)
    print("DUPLICATAS SIMILARES (primeiras 8 palavras iguais):")
    print("=" * 60)
    all_news = await db.news.find({}, {"title": 1, "news_id": 1, "category": 1, "source_name": 1}).to_list(15000)
    
    prefix_groups = defaultdict(list)
    for n in all_news:
        words = re.sub(r'[^\w\s]', '', n.get("title", "").lower()).split()
        if len(words) >= 6:
            prefix = " ".join(words[:6])
            prefix_groups[prefix].append(n)
    
    dup_count = 0
    for prefix, items in sorted(prefix_groups.items(), key=lambda x: -len(x[1])):
        if len(items) > 1:
            dup_count += 1
            if dup_count <= 15:
                print(f'\n  GRUPO ({len(items)}x):')
                for item in items[:3]:
                    print(f'    [{item.get("category","?"):12s}] [{item.get("source_name","?"):12s}] {item["title"][:65]}')
    print(f'\n  Total grupos similares: {dup_count}')
    
    # 3. Check all categories for obvious mismatches
    print("\n" + "=" * 60)
    print("VERIFICACAO CRUZADA DE CATEGORIAS:")
    print("=" * 60)
    
    category_keywords = {
        "futebol": ["futebol", "gol", "campeonato", "brasileirão", "libertadores", "copa", "seleção", "técnico", "escalação"],
        "games": ["game", "jogo eletrônico", "playstation", "xbox", "nintendo", "steam", "rpg", "fps", "gamer", "esports"],
        "anime": ["anime", "mangá", "otaku", "one piece", "dragon ball", "naruto", "jujutsu", "demon slayer"],
        "famosos": ["celebridade", "famoso", "ator", "atriz", "cantor", "cantora", "apresentador", "influencer", "reality", "bbb"],
        "investimentos": ["investimento", "ação", "fundo", "renda fixa", "tesouro direto", "fii"],
        "criptomoedas": ["bitcoin", "ethereum", "cripto", "blockchain", "altcoin", "token"],
    }
    
    for cat in ["saude", "entretenimento", "famosos"]:
        cat_news = await db.news.find({"category": cat}, {"title": 1, "summary": 1, "source_name": 1}).to_list(500)
        mismatches = []
        for n in cat_news:
            text = f"{n.get('title', '')} {n.get('summary', '')}".lower()
            for other_cat, keywords in category_keywords.items():
                if other_cat == cat:
                    continue
                matches = [kw for kw in keywords if kw in text]
                if len(matches) >= 2:
                    mismatches.append((n, other_cat, matches))
                    break
        
        if mismatches:
            print(f'\n  {cat.upper()} - possiveis erros ({len(mismatches)}):')
            for n, should_be, matches in mismatches[:5]:
                print(f'    -> deveria ser [{should_be}]: "{n["title"][:55]}" (matches: {matches})')
    
    client.close()

asyncio.run(deep_audit())
