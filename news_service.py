"""News processing service - dedup, classification, text cleaning"""
import re
import html as html_module
from difflib import SequenceMatcher
from typing import Optional
from config import db, logger


def clean_news_text(text: str) -> str:
    """Clean and sanitize news text — fixes encoding, HTML, whitespace"""
    if not text:
        return ""
    # Fix HTML entities
    text = html_module.unescape(text)
    # Fix double-encoded UTF-8 (latin-1 -> utf-8)
    try:
        if any(c in text for c in ["Ã¡", "Ã©", "Ã³", "Ãº", "â€"]):
            text = text.encode('latin-1').decode('utf-8')
    except (UnicodeDecodeError, UnicodeEncodeError):
        pass
    # Strip HTML tags
    text = re.sub(r'<[^>]+>', '', text)
    # Normalize whitespace
    text = re.sub(r'\s+', ' ', text)
    text = re.sub(r'\.{2,}', '.', text)
    # Remove CDATA artifacts
    text = text.replace('[CDATA[', '').replace(']]', '')
    return text.strip()


def normalize_title(title: str) -> str:
    if not title:
        return ""
    t = title.lower().strip()
    t = re.sub(r'[^\w\s]', '', t)
    return re.sub(r'\s+', ' ', t)


def titles_are_similar(title1: str, title2: str, threshold: float = 0.85) -> bool:
    n1 = normalize_title(title1)
    n2 = normalize_title(title2)
    if n1 == n2:
        return True
    return SequenceMatcher(None, n1, n2).ratio() >= threshold


async def is_duplicate_news(title: str, source_url: str = "") -> bool:
    if source_url:
        if await db.news.find_one({"source_url": source_url}):
            return True
    
    if await db.news.find_one({"title": title}):
        return True
    
    normalized = normalize_title(title)
    if not normalized or len(normalized) < 15:
        return False
    
    words = normalized.split()
    if len(words) >= 3:
        candidates = await db.news.find(
            {"title": {"$regex": re.escape(words[0]), "$options": "i"}},
            {"title": 1, "_id": 0}
        ).sort("published_at", -1).limit(50).to_list(length=50)
        
        for candidate in candidates:
            if titles_are_similar(title, candidate["title"]):
                return True
    
    return False


# ==================== CATEGORY CLASSIFICATION ====================

CATEGORY_KEYWORDS = {
    "tecnologia": [
        "inteligência artificial", "ia ", "ai ", "machine learning",
        "startup", "app ", "aplicativo", "software", "hardware",
        "smartphone", "iphone", "android", "apple", "google", "microsoft",
        "openai", "chatgpt", "metaverso", "5g", "robô", "automação",
        "chip", "processador", "cibersegurança", "hacker", "dados",
    ],
    "esportes": [
        "atletismo", "natação", "tênis", "basquete", "nba",
        "vôlei", "ginástica", "olimpíada", "mundial",
        "mma", "ufc", "boxe", "fórmula 1", "f1 ", "nfl",
    ],
    "futebol": [
        "futebol", "gol ", "gols ", "campeonato", "brasileirão",
        "libertadores", "copa do mundo", "champions league",
        "premier league", "la liga", "serie a", "série a",
        "seleção brasileira", "convocação", "escalação",
        "flamengo", "palmeiras", "corinthians", "são paulo fc",
        "santos", "botafogo", "fluminense", "vasco", "grêmio",
        "internacional", "atlético", "cruzeiro",
    ],
    "politica": [
        "governo", "presidente", "senado", "câmara", "deputado",
        "senador", "ministro", "congresso", "legislação", "lei ",
        "projeto de lei", "votação", "eleição", "urna", "cpi",
        "impeachment", "stf", "supremo", "tribunal",
        "lula", "bolsonaro", "prefeito", "governador",
        "reforma", "emenda", "orçamento", "plenário",
    ],
    "economia": [
        "pib", "inflação", "selic", "juros", "banco central",
        "dólar", "câmbio", "exportação", "importação",
        "desemprego", "emprego", "clt", "salário mínimo",
        "reforma tributária", "imposto",
    ],
    "mundo": [
        "guerra", "conflito", "cessar-fogo", "bombardeio", "ataque aéreo",
        "israel", "palestina", "hamas", "hezbollah", "irã", "iran",
        "ucrânia", "rússia", "otan", "nato", "diplomacia",
        "trump", "biden", "putin", "zelensky", "netanyahu",
        "eua", "estados unidos", "china", "índia",
        "ormuz", "oriente médio", "beirute", "gaza",
        "onu", "embaixada", "sanção", "tarifa",
    ],
    "entretenimento": [
        "filme", "cinema", "netflix", "disney", "streaming",
        "oscar", "grammy", "emmy", "premiação",
        "música", "álbum", "show", "turnê", "festival",
        "teatro", "musical", "concerto",
    ],
    "famosos": [
        "celebridade", "famoso", "famosa", "estrela",
        "ator ", "atriz ", "cantor", "cantora",
        "bbb", "big brother", "reality", "reality show",
        "influenciador", "influencer", "influenciadora",
        "relacionamento", "namoro", "separação", "casamento",
        "polêmica", "affair", "novela", "noveleiro",
        "paredão", "eliminação",
    ],
    "economia": [
        "pib", "inflação", "selic", "juros", "banco central",
        "dólar", "câmbio", "exportação", "importação",
        "desemprego", "emprego", "clt", "salário mínimo",
        "reforma tributária", "imposto",
        "lotofácil", "loteria", "mega-sena", "quina", "lotomania",
        "resultado da loto", "resultado da mega", "números sorteados",
        "concurso", "prêmio acumulado",
    ],
    "saude": [
        "saúde", "medicina", "doença", "tratamento", "vacina",
        "hospital", "médico", "enfermeiro", "sus",
        "vírus", "epidemia", "pandemia", "sintoma", "diagnóstico",
        "mental", "ansiedade", "depressão",
    ],
    "ciencia": [
        "ciência", "científico", "pesquisa", "estudo", "descoberta",
        "nasa", "espaço", "planeta", "estrela", "universo",
        "laboratório", "experimento", "dna", "genética",
        "física", "química", "biologia",
    ],
    "financas": [
        "ação ", "ações", "bolsa", "ibovespa", "b3",
        "dividendo", "jcp", "lucro líquido", "resultado trimestral",
        "fundo imobiliário", "fii", "renda fixa", "tesouro direto",
        "carteira", "investidor", "mercado financeiro",
    ],
    "investimentos": [
        "investimento", "investir", "rentabilidade", "rendimento",
        "carteira de investimento", "fundo de investimento",
        "previdência", "aposentadoria", "renda passiva",
    ],
    "criptomoedas": [
        "bitcoin", "btc", "ethereum", "eth", "cripto",
        "criptomoeda", "blockchain", "token", "nft",
        "altcoin", "defi", "mineração", "exchange",
        "binance", "coinbase",
    ],
    "games": [
        "game", "jogo eletrônico", "playstation", "xbox", "nintendo",
        "steam", "pc gamer", "esports", "e-sports", "rpg",
        "fps", "moba", "battle royale",
        "códigos", "código ", "simulador", "roblox", "simulator",
    ],
    "anime": [
        "anime", "mangá", "manga", "otaku", "light novel",
        "shonen", "seinen", "isekai", "crunchyroll",
        "one piece", "naruto", "dragon ball", "demon slayer",
        "jujutsu kaisen", "my hero academia", "attack on titan",
    ],
}

SOURCE_FORCED_CATEGORY = {
    "G1 Mundo": "mundo", "Folha Mundo": "mundo", "CNN Brasil Internacional": "mundo",
    "BBC Brasil": "mundo", "Agência Brasil": "mundo",
    "GE Futebol": "futebol", "GE Internacional": "futebol", "GE Brasileirão": "futebol",
    "GE Flamengo": "futebol", "GE Palmeiras": "futebol", "GE Corinthians": "futebol",
    "GE São Paulo": "futebol", "ESPN Futebol": "futebol", "CNN Futebol": "futebol",
    "AnimeNew": "anime", "IntoxiAnime": "anime",
    "Portal do Bitcoin": "criptomoedas", "Livecoins": "criptomoedas",
    "CriptoFácil": "criptomoedas", "CoinTelegraph BR": "criptomoedas",
    "Hugo Gloss": "famosos", "Extra Famosos": "famosos", "GShow": "famosos",
    "Critical Hits": "games", "Legião dos Heróis": "entretenimento",
    "E-Investidor": "investimentos", "InfoMoney": "financas",
    "Investing.com": "investimentos", "Seu Dinheiro": "financas",
}

EXCLUSION_KEYWORDS = {
    "tecnologia": [
        "futebol", "gol ", "flamengo", "palmeiras", "corinthians",
        "campeonato", "brasileirão", "libertadores",
        "bbb", "big brother", "paredão",
    ],
    "futebol": [
        "oncoclínica", "cartão de crédito", "restituição", "imposto",
        "bolsa de valores", "ibovespa", "selic", "investimento",
        "hospital", "sus ", "ministério da saúde", "oms ",
        "doença", "pandemia", "epidemia", "vacina", "medicamento",
        "bitcoin", "criptomoeda", "blockchain",
        "mma", "ufc", "boxe", "lutador", "nocaute", "octógono",
        "poatan", "cinturão", "peso pesado", "peso leve", "bellator",
        "nba", "nfl", "basquete", "tênis", "vôlei",
        "fórmula 1", "f1", "automobilismo", "gp de",
        "verstappen", "hamilton", "norris", "leclerc",
        "thunder", "spurs", "lakers", "warriors", "celtics",
    ],
    "financas": [
        "futebol", "gol ", "flamengo", "palmeiras", "corinthians",
        "campeonato", "brasileirão", "libertadores",
        "filme", "cinema", "netflix", "disney", "marvel",
        "bbb", "big brother", "paredão",
        "mma", "ufc", "nba", "nfl",
        "vacina", "doença", "hospital",
    ],
    "mundo": [
        "futebol", "brasileirão", "libertadores", "campeonato brasileiro",
        "bbb", "big brother", "paredão",
        "horóscopo", "signo", "lotofácil", "mega-sena",
    ],
    "anime": [
        "códigos", "código ", "simulador", "simulator", "roblox",
        "futebol", "campeonato", "brasileirão",
        "bitcoin", "cripto", "blockchain",
        "governo", "senado", "deputado", "presidente",
        "jcp", "dividendo", "ibovespa", "selic",
    ],
    "games": [
        "futebol", "campeonato", "brasileirão", "libertadores",
        "bbb", "big brother", "paredão",
        "bitcoin", "cripto", "blockchain",
        "governo", "senado", "deputado", "presidente",
        "jcp", "dividendo", "ibovespa", "selic",
        "vacina", "doença", "hospital",
        "julgamento", "tribunal", "condenado",
        "restituição", "imposto de renda", "conta bancária",
        "investimento", "ação ", "ações", "bolsa", "fundo",
        "inflação", "pib", "câmbio", "dólar",
        "vendidos", "bilhão", "bilhões", "milhão", "milhões",
    ],
    "famosos": [
        "bitcoin", "cripto", "blockchain",
        "ibovespa", "selic", "investimento", "restituição",
        "imposto", "bolsa de valores",
        "lotofácil", "loteria", "mega-sena", "quina", "lotomania",
        "resultado da loto", "resultado da mega", "números sorteados",
        "senado", "câmara", "deputado", "congresso", "PEC", "plenário",
        "relator", "projeto de lei", "votação", "impeachment",
        "stf", "supremo", "tribunal", "julgamento",
        "mma", "ufc", "f1", "fórmula 1",
    ],
    "saude": [
        "futebol", "campeonato", "brasileirão",
        "bitcoin", "cripto", "blockchain",
        "filme", "cinema", "netflix",
    ],
}


def smart_reclassify(title: str, summary: str, current_category: str, source_name: str = "") -> str:
    """Reclassify news based on content and source"""
    if source_name and source_name in SOURCE_FORCED_CATEGORY:
        return SOURCE_FORCED_CATEGORY[source_name]
    
    text = f"{title} {summary}".lower()
    
    scores = {}
    for category, keywords in CATEGORY_KEYWORDS.items():
        score = 0
        for keyword in keywords:
            if keyword.lower() in text:
                score += 1
        
        exclusions = EXCLUSION_KEYWORDS.get(category, [])
        for exclusion in exclusions:
            if exclusion.lower() in text:
                score -= 2
        
        if score > 0:
            scores[category] = score
    
    if not scores:
        return current_category
    
    best_category = max(scores, key=scores.get)
    best_score = scores[best_category]
    
    if best_score >= 2:
        return best_category
    elif best_score == 1 and current_category == "geral":
        return best_category
    
    return current_category
